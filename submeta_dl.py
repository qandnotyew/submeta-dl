#!/usr/bin/env python3
"""Submeta.io course downloader.

Downloads BJJ instructional courses from Submeta using their GraphQL API
and yt-dlp for video retrieval from Cloudflare Stream.

Usage:
    python3 submeta_dl.py --debug-api          # Dump GraphQL schema
    python3 submeta_dl.py --url URL --dry-run   # Preview single course
    python3 submeta_dl.py --url URL             # Download single course
    python3 submeta_dl.py --dry-run             # Preview full catalog
    python3 submeta_dl.py                       # Download everything
    python3 submeta_dl.py --list                # Show download status
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

from submeta_client import SubmetaClient, SubmetaAuthError, SubmetaAPIError

log = logging.getLogger("submeta_dl")

DEFAULT_DOWNLOAD_DIR = "./downloads"
DEFAULT_RATE_LIMIT = 2  # seconds between video downloads
CONNECTIVITY_CHECK_URL = "https://submeta.io"
MAX_CONNECTIVITY_WAIT = 3600  # max seconds to wait for WAN to come back (1 hour)
CONNECTIVITY_CHECK_INTERVAL = 30  # seconds between connectivity checks
MAX_VIDEO_RETRIES = 3  # retry a video download this many times on network failure


def load_env(env_path: Path) -> dict:
    """Load key=value pairs from .env file."""
    env = {}
    if not env_path.exists():
        return env
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip("'\"")
    return env


def sanitize_filename(name: str, max_len: int = 200) -> str:
    """Replace special chars with underscore, truncate."""
    clean = re.sub(r'[^\w\s\-.]', '_', name)
    clean = re.sub(r'_+', '_', clean).strip('_ ')
    return clean[:max_len]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class DownloadState:
    """Manages downloads.json state file."""

    def __init__(self, state_path: Path):
        self.path = state_path
        self.data = {"last_updated": now_iso(), "courses": {}}
        if self.path.exists():
            self.data = json.loads(self.path.read_text())

    def save(self):
        self.data["last_updated"] = now_iso()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        tmp.rename(self.path)

    def is_course_complete(self, slug: str) -> bool:
        course = self.data["courses"].get(slug, {})
        return course.get("status") == "complete"

    def is_video_downloaded(self, slug: str, video_id: str) -> bool:
        course = self.data["courses"].get(slug, {})
        video = course.get("videos", {}).get(video_id, {})
        return video.get("downloaded", False)

    def has_failed_videos(self, slug: str) -> bool:
        course = self.data["courses"].get(slug, {})
        return any(
            not v.get("downloaded") for v in course.get("videos", {}).values()
        )

    def init_course(self, slug: str, url: str, title: str, instructor: str, total_videos: int):
        if slug not in self.data["courses"]:
            self.data["courses"][slug] = {
                "url": url,
                "title": title,
                "instructor": instructor,
                "total_videos": total_videos,
                "downloaded_videos": 0,
                "status": "pending",
                "first_seen": now_iso(),
                "last_downloaded": None,
                "videos": {},
            }
        else:
            # Update total in case course structure changed
            self.data["courses"][slug]["total_videos"] = total_videos

    def mark_video(self, slug: str, video_id: str, title: str, chapter: str,
                   rel_path: str, size_bytes: int = 0):
        course = self.data["courses"][slug]
        course["videos"][video_id] = {
            "title": title,
            "chapter": chapter,
            "path": rel_path,
            "downloaded": True,
            "downloaded_at": now_iso(),
            "size_bytes": size_bytes,
        }
        course["downloaded_videos"] = sum(
            1 for v in course["videos"].values() if v.get("downloaded")
        )
        course["last_downloaded"] = now_iso()
        if course["downloaded_videos"] >= course["total_videos"]:
            course["status"] = "complete"
        else:
            course["status"] = "partial"

    def mark_video_failed(self, slug: str, video_id: str, title: str,
                          chapter: str, error: str):
        course = self.data["courses"][slug]
        course["videos"][video_id] = {
            "title": title,
            "chapter": chapter,
            "path": "",
            "downloaded": False,
            "error": error,
            "attempted_at": now_iso(),
        }
        course["status"] = "partial"

    def is_course_unauthorized(self, slug: str) -> bool:
        course = self.data["courses"].get(slug, {})
        return course.get("status") == "unauthorized"

    def mark_course_unauthorized(self, slug: str, url: str, title: str, instructor: str):
        """Mark a course as not included in the subscription."""
        if slug not in self.data["courses"]:
            self.data["courses"][slug] = {
                "url": url,
                "title": title,
                "instructor": instructor,
                "total_videos": 0,
                "downloaded_videos": 0,
                "status": "unauthorized",
                "first_seen": now_iso(),
                "last_downloaded": None,
                "videos": {},
            }
        else:
            self.data["courses"][slug]["status"] = "unauthorized"

    def summary(self) -> str:
        courses = self.data["courses"]
        total = len(courses)
        complete = sum(1 for c in courses.values() if c["status"] == "complete")
        partial = sum(1 for c in courses.values() if c["status"] == "partial")
        pending = sum(1 for c in courses.values() if c["status"] == "pending")
        total_vids = sum(c.get("downloaded_videos", 0) for c in courses.values())

        lines = [
            f"Courses: {total} total ({complete} complete, {partial} partial, {pending} pending)",
            f"Videos downloaded: {total_vids}",
            f"Last updated: {self.data.get('last_updated', 'never')}",
        ]
        if courses:
            lines.append("")
            for slug, c in sorted(courses.items()):
                status_icon = {"complete": "+", "partial": "~", "pending": "-"}.get(c["status"], "?")
                lines.append(
                    f"  [{status_icon}] {c.get('instructor', '?')} / {c.get('title', slug)} "
                    f"({c.get('downloaded_videos', 0)}/{c.get('total_videos', '?')} videos)"
                )
        return "\n".join(lines)


def check_connectivity() -> bool:
    """Check if we can reach Submeta."""
    try:
        req = Request(CONNECTIVITY_CHECK_URL, method="HEAD",
                      headers={"User-Agent": "submeta-dl connectivity check"})
        with urlopen(req, timeout=10):
            return True
    except Exception:
        return False


def wait_for_connectivity() -> bool:
    """Block until connection comes back or timeout. Returns True if restored."""
    log.warning("Connection lost. Waiting for it to come back...")
    waited = 0
    while waited < MAX_CONNECTIVITY_WAIT:
        time.sleep(CONNECTIVITY_CHECK_INTERVAL)
        waited += CONNECTIVITY_CHECK_INTERVAL
        if check_connectivity():
            log.info(f"Connection restored after {waited}s")
            print(f"  Connection restored after {waited}s")
            return True
        log.info(f"Still waiting for connection... ({waited}s / {MAX_CONNECTIVITY_WAIT}s)")
    log.error(f"Connection not restored after {MAX_CONNECTIVITY_WAIT}s, giving up")
    return False


def is_network_error(stderr: str) -> bool:
    """Detect if a yt-dlp failure was network-related (vs auth/format/etc)."""
    network_indicators = [
        "urlopen error", "connection reset", "connection refused",
        "timed out", "timeout", "network is unreachable", "name resolution",
        "temporary failure", "no route to host", "connection aborted",
        "broken pipe", "eof occurred", "ssl", "handshake",
        "aria2c exited with", "download failed",
    ]
    lower = stderr.lower()
    return any(indicator in lower for indicator in network_indicators)


def download_video(url: str, output_path: Path) -> bool:
    """Download a video using yt-dlp with aria2c. Retries on network failure."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # yt-dlp output template (without extension, yt-dlp adds it)
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

    log.info(f"Downloading: {output_path.name}")
    log.debug(f"Command: {' '.join(cmd)}")

    for attempt in range(MAX_VIDEO_RETRIES):
        # Pre-flight connectivity check
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

        # Check if this was a network error (worth retrying) or something else
        if is_network_error(result.stderr):
            log.warning(f"Network error on attempt {attempt + 1}/{MAX_VIDEO_RETRIES}: "
                        f"{result.stderr[-200:]}")
            if attempt < MAX_VIDEO_RETRIES - 1:
                if not wait_for_connectivity():
                    return False
                log.info("Retrying download...")
                continue
        else:
            # Non-network error -- don't retry
            log.error(f"yt-dlp failed (non-network): {result.stderr[-500:]}")
            return False

    log.error(f"Download failed after {MAX_VIDEO_RETRIES} attempts")
    return False


def get_output_size(output_path: Path) -> int:
    """Find the actual downloaded file (yt-dlp may change extension).

    Only matches clean final files -- excludes .part fragments, .aria2 control files,
    and any file with 'part' in the stem (e.g., video.fhls-1234.mp4.part-Frag0).
    """
    for f in output_path.parent.glob(f"{output_path.stem}.*"):
        if not f.is_file():
            continue
        name = f.name.lower()
        # Skip fragment files, partial downloads, and aria2 control files
        if ".part" in name or ".aria2" in name or ".frag" in name or ".urls" in name:
            continue
        if f.stat().st_size > 0:
            return f.stat().st_size
    return 0


def download_course(client: SubmetaClient, course_info: dict, download_dir: Path,
                    state: DownloadState, rate_limit: int, dry_run: bool = False) -> bool:
    """Download a single course.

    course_info must have 'slug' and 'handle' keys (from catalog or --url parsing).
    """
    slug = course_info["slug"]
    handle = course_info["handle"]
    print(f"\nFetching course info: @{handle}/{slug}...")
    course = client.get_course(slug, handle)

    title = course["title"]
    instructor = course["instructor"]
    slug = course.get("slug", sanitize_filename(title))
    total_videos = sum(len(ch["videos"]) for ch in course["chapters"])

    print(f"  Course: {title}")
    print(f"  Instructor: {instructor}")
    print(f"  Chapters: {len(course['chapters'])}")
    print(f"  Videos: {total_videos}")

    if state.is_course_complete(slug) and not dry_run:
        print(f"  Already complete, skipping.")
        return True

    # Directory structure
    instructor_dir = sanitize_filename(instructor)
    course_dir = sanitize_filename(title)
    base_path = download_dir / instructor_dir / course_dir

    if dry_run:
        print(f"  Would download to: {base_path}")
        for ci, chapter in enumerate(course["chapters"], 1):
            ch_name = f"{ci:02d}_{sanitize_filename(chapter['title'])}"
            for vi, video in enumerate(chapter["videos"], 1):
                vid_name = f"{vi:02d}_{sanitize_filename(video['title'])}"
                print(f"    {ch_name}/{vid_name}.mp4")
        return True

    # Initialize state
    course_url = f"https://submeta.io/@{handle}/courses/{slug}"
    state.init_course(slug, course_url, title, instructor, total_videos)

    # Write course metadata
    base_path.mkdir(parents=True, exist_ok=True)
    metadata = {
        "title": title,
        "instructor": instructor,
        "handle": handle,
        "url": course_url,
        "slug": slug,
        "downloaded_at": now_iso(),
        "chapters": [],
    }
    for ci, chapter in enumerate(course["chapters"], 1):
        ch_meta = {"index": ci, "title": chapter["title"], "videos": []}
        for vi, video in enumerate(chapter["videos"], 1):
            ch_meta["videos"].append({
                "index": vi,
                "title": video["title"],
                "id": video["id"],
                "filename": f"{vi:02d}_{sanitize_filename(video['title'])}.mp4",
            })
        metadata["chapters"].append(ch_meta)
    (base_path / "course_metadata.json").write_text(json.dumps(metadata, indent=2))

    # Download each video
    success_count = 0
    unauth_count = 0
    for ci, chapter in enumerate(course["chapters"], 1):
        ch_name = f"{ci:02d}_{sanitize_filename(chapter['title'])}"

        for vi, video in enumerate(chapter["videos"], 1):
            video_id = video["id"]
            vid_name = f"{vi:02d}_{sanitize_filename(video['title'])}"
            rel_path = f"{instructor_dir}/{course_dir}/{ch_name}/{vid_name}"
            output_path = base_path / ch_name / vid_name

            if state.is_video_downloaded(slug, video_id):
                log.info(f"  Skipping (already downloaded): {vid_name}")
                success_count += 1
                continue

            # Check if file exists on disk
            existing_size = get_output_size(output_path)
            if existing_size > 0:
                log.info(f"  Found on disk, marking complete: {vid_name}")
                state.mark_video(slug, video_id, video["title"],
                                 chapter["title"], rel_path, existing_size)
                state.save()
                success_count += 1
                continue

            try:
                # Verify connectivity before requesting video token
                if not check_connectivity():
                    print(f"  Connection down before {vid_name}, waiting...")
                    if not wait_for_connectivity():
                        log.error("Connection did not recover, stopping course download")
                        state.save()
                        return False

                video_url = client.get_video_url(video_id)
                print(f"  [{success_count + 1}/{total_videos}] {ch_name}/{vid_name}")

                if download_video(video_url, output_path):
                    # Clean up leftover fragment files from aria2c
                    for junk in output_path.parent.glob(f"{output_path.stem}*.part*"):
                        junk.unlink(missing_ok=True)
                    for junk in output_path.parent.glob(f"{output_path.stem}*.aria2"):
                        junk.unlink(missing_ok=True)
                    for junk in output_path.parent.glob(f"{output_path.stem}*.urls"):
                        junk.unlink(missing_ok=True)
                    size = get_output_size(output_path)
                    state.mark_video(slug, video_id, video["title"],
                                     chapter["title"], rel_path, size)
                    success_count += 1
                else:
                    state.mark_video_failed(slug, video_id, video["title"],
                                            chapter["title"], "download failed after retries")
            except Exception as e:
                err_str = str(e)
                if "not authorized" in err_str.lower():
                    unauth_count += 1
                    log.info(f"  Not authorized: {vid_name}")
                else:
                    log.error(f"  Failed: {vid_name}: {err_str}")
                    # If it looks like a network error, wait for connectivity
                    if "urlopen" in err_str.lower() or "timed out" in err_str.lower():
                        if wait_for_connectivity():
                            log.info("Connection back, but skipping this video for now")
                    state.mark_video_failed(slug, video_id, video["title"],
                                            chapter["title"], err_str)

            state.save()

            if rate_limit > 0:
                time.sleep(rate_limit)

    # If every unattempted video was "not authorized", mark the whole course
    attempted = total_videos - success_count
    if unauth_count > 0 and unauth_count >= attempted:
        log.info(f"  Course not in subscription ({unauth_count} videos unauthorized)")
        state.mark_course_unauthorized(slug, course_url, title, instructor)
        state.save()
        return True  # Not a failure -- just not accessible

    print(f"  Done: {success_count}/{total_videos} videos downloaded")
    return success_count == total_videos


def cmd_debug_api(client: SubmetaClient):
    """Dump GraphQL schema for development."""
    print("Running GraphQL introspection query...")
    schema = client.introspect_schema()

    # Extract and display query/mutation types
    types = schema.get("__schema", {}).get("types", [])
    query_type_name = schema.get("__schema", {}).get("queryType", {}).get("name")
    mutation_type_name = schema.get("__schema", {}).get("mutationType", {}).get("name")

    print(f"\nQuery type: {query_type_name}")
    print(f"Mutation type: {mutation_type_name}")

    for t in types:
        if t["name"] in (query_type_name, mutation_type_name):
            print(f"\n{'='*60}")
            print(f"Type: {t['name']}")
            print(f"{'='*60}")
            for field in (t.get("fields") or []):
                args_str = ""
                if field.get("args"):
                    args_list = []
                    for a in field["args"]:
                        atype = a.get("type", {})
                        type_name = atype.get("name") or (atype.get("ofType", {}) or {}).get("name", "?")
                        args_list.append(f"{a['name']}: {type_name}")
                    args_str = f"({', '.join(args_list)})"

                ret_type = field.get("type", {})
                ret_name = ret_type.get("name") or (ret_type.get("ofType", {}) or {}).get("name", "?")
                print(f"  {field['name']}{args_str} -> {ret_name}")

    # Also dump interesting types (Course, Video, etc.)
    interesting = {"Course", "Video", "Chapter", "Creator", "User", "Subscription",
                   "Library", "Content", "Instructor"}
    print(f"\n{'='*60}")
    print("Interesting types:")
    print(f"{'='*60}")
    for t in types:
        if t["name"] in interesting or any(t["name"].startswith(p) for p in interesting):
            print(f"\n  {t['name']} ({t.get('kind', '?')}):")
            for field in (t.get("fields") or []):
                ret_type = field.get("type", {})
                ret_name = ret_type.get("name") or (ret_type.get("ofType", {}) or {}).get("name", "?")
                print(f"    {field['name']}: {ret_name}")

    # Dump full schema to file for later analysis
    schema_path = Path("schema_dump.json")
    schema_path.write_text(json.dumps(schema, indent=2))
    print(f"\nFull schema dumped to {schema_path}")


def cmd_list(state: DownloadState):
    """Show download status."""
    print(state.summary())


def main():
    parser = argparse.ArgumentParser(description="Submeta.io course downloader")
    parser.add_argument("--url", help="Download a single course by URL")
    parser.add_argument("--dry-run", action="store_true", help="Preview without downloading")
    parser.add_argument("--debug-api", action="store_true", help="Dump GraphQL schema and exit")
    parser.add_argument("--list", action="store_true", help="Show download status")
    parser.add_argument("--force", action="store_true", help="Re-download even if complete")
    parser.add_argument("--download-dir", help="Override download directory")
    args = parser.parse_args()

    # Setup logging
    log_handlers = [logging.StreamHandler()]
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    log_handlers.append(logging.FileHandler(log_dir / "submeta_dl.log"))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=log_handlers,
    )

    # Load config from .env in current directory or parent
    env = load_env(Path(".env"))
    if not env:
        env = load_env(Path(__file__).resolve().parent / ".env")

    username = env.get("SUBMETA_USERNAME", os.environ.get("SUBMETA_USERNAME", ""))
    password = env.get("SUBMETA_PASSWORD", os.environ.get("SUBMETA_PASSWORD", ""))
    download_dir = Path(args.download_dir or
                        env.get("SUBMETA_DOWNLOAD_DIR",
                                os.environ.get("SUBMETA_DOWNLOAD_DIR", DEFAULT_DOWNLOAD_DIR)))
    rate_limit = int(env.get("SUBMETA_RATE_LIMIT",
                             os.environ.get("SUBMETA_RATE_LIMIT", DEFAULT_RATE_LIMIT)))

    # State file lives in download dir
    state = DownloadState(download_dir / "downloads.json")

    if args.list:
        cmd_list(state)
        return

    if not username or not password:
        print("Error: Set SUBMETA_USERNAME and SUBMETA_PASSWORD in .env or environment")
        sys.exit(1)

    # Check dependencies
    for dep in ["yt-dlp", "aria2c"]:
        if not shutil.which(dep):
            print(f"Error: {dep} not found. Install with: brew install {'aria2' if dep == 'aria2c' else dep}")
            sys.exit(1)

    # Check download dir
    if not args.debug_api and not args.dry_run:
        if not download_dir.exists():
            print(f"Error: Download directory {download_dir} does not exist")
            print("Create it with: mkdir -p " + str(download_dir))
            sys.exit(1)

    # Login
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

    if args.debug_api:
        cmd_debug_api(client)
        return

    if args.url:
        # Parse URL: https://submeta.io/@handle/courses/slug/id
        m = re.match(r'https?://submeta\.io/@([^/]+)/courses/([^/]+)', args.url)
        if not m:
            print(f"Error: Could not parse URL. Expected format: https://submeta.io/@handle/courses/slug")
            sys.exit(1)
        course_info = {"handle": m.group(1), "slug": m.group(2)}
        try:
            download_course(client, course_info, download_dir, state, rate_limit, args.dry_run)
        except Exception as e:
            log.error(f"Failed to download course: {e}")
            sys.exit(1)
    else:
        # Full catalog download
        print("\nDiscovering course catalog...")
        catalog = client.discover_catalog()
        if not catalog:
            print("Could not discover catalog.")
            sys.exit(1)

        print(f"Found {len(catalog)} courses")

        if args.dry_run:
            for i, course in enumerate(catalog, 1):
                print(f"  {i}. {course.get('instructor', '?')} / {course['title']}")
            return

        downloaded = 0
        failed = 0
        skipped = 0
        for i, course_info in enumerate(catalog, 1):
            slug = course_info.get("slug", "")
            if not args.force and state.is_course_complete(slug):
                skipped += 1
                continue

            print(f"\n[{i}/{len(catalog)}] {course_info.get('instructor', '?')} / {course_info['title']}")
            try:
                if download_course(client, course_info, download_dir,
                                   state, rate_limit, args.dry_run):
                    downloaded += 1
                else:
                    failed += 1
            except Exception as e:
                log.error(f"Course failed: {e}")
                failed += 1

        print(f"\nAll done! Downloaded: {downloaded}, Failed: {failed}, Skipped: {skipped}")


if __name__ == "__main__":
    main()
