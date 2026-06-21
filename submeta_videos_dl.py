#!/usr/bin/env python3
"""Submeta.io standalone video downloader.

Downloads standalone videos (rolls, discussions, technique, breakdowns, etc.)
from the Submeta "Videos" section -- separate from the course catalog.

Usage:
    python3 submeta_videos_dl.py --dry-run     # Preview catalog
    python3 submeta_videos_dl.py --list        # Show download status
    python3 submeta_videos_dl.py               # Download everything
    python3 submeta_videos_dl.py --monitor     # Check for new videos only (no download)
"""

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

from submeta_client import SubmetaClient, SubmetaAuthError, SubmetaAPIError, CLOUDFLARE_DOMAIN

log = logging.getLogger("submeta_videos_dl")

DEFAULT_DOWNLOAD_DIR = "./downloads"
DEFAULT_RATE_LIMIT = 2
CONNECTIVITY_CHECK_URL = "https://submeta.io"
MAX_CONNECTIVITY_WAIT = 3600
CONNECTIVITY_CHECK_INTERVAL = 30
MAX_VIDEO_RETRIES = 3


def sanitize_filename(name: str, max_len: int = 200) -> str:
    clean = re.sub(r'[^\w\s\-.]', '_', name)
    clean = re.sub(r'_+', '_', clean).strip('_ ')
    return clean[:max_len]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class VideoState:
    """Manages videos_state.json -- tracks standalone video downloads."""

    def __init__(self, state_path: Path):
        self.path = state_path
        self.data = {"last_updated": now_iso(), "last_catalog_check": None, "videos": {}}
        if self.path.exists():
            self.data = json.loads(self.path.read_text())

    def save(self):
        self.data["last_updated"] = now_iso()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        tmp.rename(self.path)

    def is_downloaded(self, video_id: str) -> bool:
        v = self.data["videos"].get(video_id, {})
        return v.get("status") == "complete"

    def mark_complete(self, video_id: str, info: dict, rel_path: str, size_bytes: int = 0,
                      has_thumbnail: bool = False):
        self.data["videos"][video_id] = {
            "title": info.get("title", ""),
            "instructor": info.get("instructor", ""),
            "handle": info.get("handle", ""),
            "tags": info.get("tags", []),
            "duration": info.get("duration", 0),
            "description": info.get("description", ""),
            "published_at": info.get("published_at"),
            "path": rel_path,
            "size_bytes": size_bytes,
            "status": "complete",
            "has_thumbnail": has_thumbnail,
            "downloaded_at": now_iso(),
        }

    def mark_failed(self, video_id: str, info: dict, error: str):
        self.data["videos"][video_id] = {
            "title": info.get("title", ""),
            "instructor": info.get("instructor", ""),
            "handle": info.get("handle", ""),
            "tags": info.get("tags", []),
            "status": "failed",
            "error": error,
            "attempted_at": now_iso(),
        }

    def update_catalog(self, catalog: list[dict]):
        """Update known video list from catalog without changing download status."""
        self.data["last_catalog_check"] = now_iso()
        for v in catalog:
            vid = v["id"]
            if vid not in self.data["videos"]:
                self.data["videos"][vid] = {
                    "title": v.get("title", ""),
                    "instructor": v.get("instructor", ""),
                    "handle": v.get("handle", ""),
                    "tags": v.get("tags", []),
                    "duration": v.get("duration", 0),
                    "description": v.get("description", ""),
                    "published_at": v.get("published_at"),
                    "status": "pending",
                    "first_seen": now_iso(),
                }

    def summary(self) -> str:
        videos = self.data["videos"]
        total = len(videos)
        complete = sum(1 for v in videos.values() if v.get("status") == "complete")
        failed = sum(1 for v in videos.values() if v.get("status") == "failed")
        pending = sum(1 for v in videos.values() if v.get("status") == "pending")

        # Tag breakdown
        tags = {}
        for v in videos.values():
            for t in v.get("tags", []):
                tags[t] = tags.get(t, 0) + 1

        # Instructor breakdown
        instructors = {}
        for v in videos.values():
            i = v.get("instructor", "Unknown")
            instructors[i] = instructors.get(i, 0) + 1

        lines = [
            f"Standalone videos: {total} total ({complete} downloaded, {failed} failed, {pending} pending)",
            f"Last catalog check: {self.data.get('last_catalog_check', 'never')}",
            f"Last updated: {self.data.get('last_updated', 'never')}",
            "",
            "By tag:",
        ]
        for tag, count in sorted(tags.items(), key=lambda x: -x[1]):
            lines.append(f"  {tag}: {count}")
        lines.append("")
        lines.append("By instructor:")
        for inst, count in sorted(instructors.items(), key=lambda x: -x[1]):
            lines.append(f"  {inst}: {count}")

        return "\n".join(lines)


def check_connectivity() -> bool:
    try:
        req = Request(CONNECTIVITY_CHECK_URL, method="HEAD",
                      headers={"User-Agent": "submeta-dl connectivity check"})
        with urlopen(req, timeout=10):
            return True
    except Exception:
        return False


def wait_for_connectivity() -> bool:
    log.warning("Connection lost. Waiting for it to come back...")
    waited = 0
    while waited < MAX_CONNECTIVITY_WAIT:
        time.sleep(CONNECTIVITY_CHECK_INTERVAL)
        waited += CONNECTIVITY_CHECK_INTERVAL
        if check_connectivity():
            log.info(f"Connection restored after {waited}s")
            return True
    log.error(f"Connection not restored after {MAX_CONNECTIVITY_WAIT}s")
    return False


def is_network_error(stderr: str) -> bool:
    indicators = [
        "urlopen error", "connection reset", "connection refused",
        "timed out", "timeout", "network is unreachable", "name resolution",
        "temporary failure", "no route to host", "connection aborted",
        "broken pipe", "eof occurred", "ssl", "handshake",
        "aria2c exited with", "download failed",
    ]
    lower = stderr.lower()
    return any(ind in lower for ind in indicators)


def download_thumbnail(token: str, out_path: Path, cloudflare_domain: str) -> bool:
    """Download Cloudflare Stream thumbnail for a video."""
    url = f"https://{cloudflare_domain}/{token}/thumbnails/thumbnail.jpg?height=360"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://submeta.io/"})
        with urlopen(req, timeout=30) as resp:
            data = resp.read()
            if len(data) > 0:
                out_path.write_bytes(data)
                return True
    except Exception as e:
        log.warning(f"Thumbnail download failed: {e}")
    return False


def download_video_file(url: str, output_path: Path) -> bool:
    """Download a video using yt-dlp with aria2c."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    outtmpl = str(output_path.with_suffix(".%(ext)s"))

    cmd = [
        "yt-dlp",
        "--external-downloader", "aria2c",
        "--external-downloader-args", "aria2c:--auto-file-renaming=false --allow-overwrite=true",
        "--referer", "https://submeta.io",
        "--fragment-retries", "10",
        "--retries", "10",
        "-o", outtmpl,
        url,
    ]

    for attempt in range(MAX_VIDEO_RETRIES):
        if not check_connectivity():
            if not wait_for_connectivity():
                return False

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        except subprocess.TimeoutExpired:
            log.warning(f"Download timed out (attempt {attempt + 1}/{MAX_VIDEO_RETRIES})")
            if not wait_for_connectivity():
                return False
            continue

        if result.returncode == 0:
            return True

        if is_network_error(result.stderr):
            log.warning(f"Network error on attempt {attempt + 1}: {result.stderr[-200:]}")
            if attempt < MAX_VIDEO_RETRIES - 1:
                if not wait_for_connectivity():
                    return False
                continue
        else:
            log.error(f"yt-dlp failed: {result.stderr[-500:]}")
            return False

    log.error(f"Download failed after {MAX_VIDEO_RETRIES} attempts")
    return False


def get_output_size(output_path: Path) -> int:
    for f in output_path.parent.glob(f"{output_path.stem}.*"):
        if not f.is_file():
            continue
        name = f.name.lower()
        if ".part" in name or ".aria2" in name or ".frag" in name or ".urls" in name:
            continue
        if f.stat().st_size > 0:
            return f.stat().st_size
    return 0


def main():
    parser = argparse.ArgumentParser(description="Submeta.io standalone video downloader")
    parser.add_argument("--dry-run", action="store_true", help="Preview without downloading")
    parser.add_argument("--list", action="store_true", help="Show download status")
    parser.add_argument("--monitor", action="store_true", help="Check for new videos only, don't download")
    parser.add_argument("--force", action="store_true", help="Re-download even if complete")
    parser.add_argument("--download-dir", help="Override download directory")
    args = parser.parse_args()

    log_handlers = [logging.StreamHandler()]
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    log_handlers.append(logging.FileHandler(log_dir / "submeta_videos_dl.log"))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=log_handlers,
    )

    # Load config
    env = {}
    for env_path in [Path(".env"), Path(__file__).resolve().parent / ".env"]:
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, _, value = line.partition("=")
                    env[key.strip()] = value.strip().strip("'\"")
            break

    username = env.get("SUBMETA_USERNAME", os.environ.get("SUBMETA_USERNAME", ""))
    password = env.get("SUBMETA_PASSWORD", os.environ.get("SUBMETA_PASSWORD", ""))
    download_dir = Path(args.download_dir or
                        env.get("SUBMETA_DOWNLOAD_DIR",
                                os.environ.get("SUBMETA_DOWNLOAD_DIR", DEFAULT_DOWNLOAD_DIR)))
    rate_limit = int(env.get("SUBMETA_RATE_LIMIT",
                             os.environ.get("SUBMETA_RATE_LIMIT", DEFAULT_RATE_LIMIT)))

    # Videos state file -- separate from courses
    state = VideoState(download_dir / "videos_state.json")

    if args.list:
        print(state.summary())
        return

    if not username or not password:
        print("Error: Set SUBMETA_USERNAME and SUBMETA_PASSWORD in .env or environment")
        sys.exit(1)

    for dep in ["yt-dlp", "aria2c"]:
        if not shutil.which(dep):
            print(f"Error: {dep} not found. Install with: brew install {'aria2' if dep == 'aria2c' else dep}")
            sys.exit(1)

    if not args.dry_run and not args.monitor and not download_dir.exists():
        print(f"Error: Download directory {download_dir} does not exist")
        print("Create it with: mkdir -p " + str(download_dir))
        sys.exit(1)

    cloudflare_domain = env.get("SUBMETA_CLOUDFLARE_DOMAIN",
                                os.environ.get("SUBMETA_CLOUDFLARE_DOMAIN",
                                               "customer-3j2pofw9vdbl9sfy.cloudflarestream.com"))
    client = SubmetaClient(username, password, cloudflare_domain=cloudflare_domain)

    print("Logging in to Submeta...")
    try:
        client.login()
        print("Login successful!")
    except SubmetaAuthError as e:
        print(f"Login failed: {e}")
        sys.exit(1)

    # Discover video catalog
    print("\nDiscovering standalone video catalog...")
    catalog = client.discover_videos()
    if not catalog:
        print("No standalone videos found.")
        sys.exit(1)

    print(f"Found {len(catalog)} standalone videos")

    # Update state with catalog
    prev_count = len(state.data["videos"])
    state.update_catalog(catalog)
    new_count = len(state.data["videos"]) - prev_count
    if new_count > 0:
        print(f"  {new_count} new videos discovered!")
    state.save()

    if args.monitor:
        print("\nMonitor mode -- catalog updated, not downloading.")
        print(state.summary())
        return

    if args.dry_run:
        for i, v in enumerate(catalog, 1):
            tags_str = ", ".join(v.get("tags", []))
            print(f"  {i}. [{tags_str}] {v['instructor']} / {v['title']} ({v.get('duration', 0):.0f}s)")
        return

    # Download videos
    # Directory structure: Videos/{Instructor}/{Tag}/{sanitized_title}.mp4
    videos_dir = download_dir / "Videos"
    thumbs_dir = videos_dir / "thumbnails"
    downloaded = 0
    failed = 0
    skipped = 0

    for i, video_info in enumerate(catalog, 1):
        vid = video_info["id"]

        if not args.force and state.is_downloaded(vid):
            # Backfill thumbnail for already-downloaded videos
            thumb_path = thumbs_dir / f"{vid}.jpg"
            if not thumb_path.exists():
                existing_entry = state.data["videos"].get(vid, {})
                if not existing_entry.get("has_thumbnail"):
                    try:
                        video_url = client.get_standalone_video_url(vid)
                        token = video_url.split(f"{cloudflare_domain}/")[1].split("/")[0]
                        if download_thumbnail(token, thumb_path, cloudflare_domain):
                            existing_entry["has_thumbnail"] = True
                            state.save()
                            log.info(f"  Backfill thumbnail: {vid}.jpg")
                    except Exception:
                        pass
            skipped += 1
            continue

        instructor = sanitize_filename(video_info.get("instructor", "Unknown"))
        tag = video_info.get("tags", ["Uncategorized"])[0] if video_info.get("tags") else "Uncategorized"
        tag_dir = sanitize_filename(tag.title())
        title = sanitize_filename(video_info.get("title", vid))
        rel_path = f"Videos/{instructor}/{tag_dir}/{title}"
        output_path = videos_dir / instructor / tag_dir / title

        # Check if already on disk
        existing_size = get_output_size(output_path)
        if existing_size > 0:
            log.info(f"  Found on disk: {title}")
            thumb_path = thumbs_dir / f"{vid}.jpg"
            has_thumb = thumb_path.exists()
            state.mark_complete(vid, video_info, rel_path, existing_size, has_thumbnail=has_thumb)
            state.save()
            skipped += 1
            continue

        print(f"\n[{i}/{len(catalog)}] [{tag}] {video_info['instructor']} / {video_info['title']}")

        if not check_connectivity():
            print("  Connection down, waiting...")
            if not wait_for_connectivity():
                log.error("Connection did not recover, stopping")
                state.save()
                break

        try:
            video_url = client.get_standalone_video_url(vid)
        except SubmetaAPIError as e:
            err = str(e)
            if "not authorized" in err.lower():
                log.info(f"  Not authorized: {title}")
                state.mark_failed(vid, video_info, "unauthorized")
            else:
                log.error(f"  Token error: {err}")
                state.mark_failed(vid, video_info, err)
            state.save()
            failed += 1
            continue

        # Extract token from video URL for thumbnail download
        # URL format: https://domain/{token}/manifest/video.mpd
        token = video_url.split(f"{cloudflare_domain}/")[1].split("/")[0] if cloudflare_domain in video_url else None

        # Download thumbnail while token is valid
        thumb_ok = False
        if token:
            thumb_path = videos_dir / "thumbnails" / f"{vid}.jpg"
            if not thumb_path.exists():
                thumb_ok = download_thumbnail(token, thumb_path, cloudflare_domain)
                if thumb_ok:
                    log.info(f"  Thumbnail saved: {vid}.jpg")
            else:
                thumb_ok = True

        if download_video_file(video_url, output_path):
            # Clean up artifacts
            for junk in output_path.parent.glob(f"{output_path.stem}*.part*"):
                junk.unlink(missing_ok=True)
            for junk in output_path.parent.glob(f"{output_path.stem}*.aria2"):
                junk.unlink(missing_ok=True)
            for junk in output_path.parent.glob(f"{output_path.stem}*.urls"):
                junk.unlink(missing_ok=True)
            size = get_output_size(output_path)
            state.mark_complete(vid, video_info, rel_path, size, has_thumbnail=thumb_ok)
            downloaded += 1
        else:
            state.mark_failed(vid, video_info, "download failed after retries")
            failed += 1

        state.save()

        if rate_limit > 0:
            time.sleep(rate_limit)

    print(f"\nAll done! Downloaded: {downloaded}, Failed: {failed}, Skipped: {skipped}")


if __name__ == "__main__":
    main()
