#!/usr/bin/env python3
"""Scan downloaded videos for corruption (missing audio, duration mismatch, decode errors).

Usage:
    python3 scan_videos.py                    # Scan ./downloads
    python3 scan_videos.py /path/to/videos    # Scan specific directory
"""

import subprocess
import sys
from pathlib import Path


def verify_video(file_path):
    if not file_path.exists():
        return False, "not found"
    if file_path.stat().st_size < 10000:
        return False, f"too small ({file_path.stat().st_size}B)"

    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,duration",
             "-of", "csv=p=0", str(file_path)],
            capture_output=True, text=True, timeout=30)
        lines = [l.strip() for l in result.stdout.strip().split("\n") if l.strip()]
        has_video = any("video" in l for l in lines)
        has_audio = any("audio" in l for l in lines)
        if not has_video:
            return False, "no video stream"
        if not has_audio:
            return False, "no audio stream"

        vdur = adur = None
        for l in lines:
            parts = l.split(",")
            if len(parts) >= 2:
                try:
                    if "video" in parts[0] and parts[1]:
                        vdur = float(parts[1])
                    elif "audio" in parts[0] and parts[1]:
                        adur = float(parts[1])
                except ValueError:
                    pass

        if vdur and adur and abs(vdur - adur) > 10:
            return False, f"duration mismatch (v={vdur:.0f}s a={adur:.0f}s)"
    except Exception:
        return False, "ffprobe failed"

    try:
        result = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(file_path), "-t", "15", "-f", "null", "-"],
            capture_output=True, text=True, timeout=60)
        errs = result.stderr.count("Invalid") + result.stderr.count("corrupt") + result.stderr.count("missing picture")
        if errs > 5:
            return False, f"{errs} errors in first 15s"
    except Exception:
        pass

    return True, "ok"


def scan_dir(path):
    path = Path(path)
    total = bad = 0
    for f in sorted(path.rglob("*.mp4")):
        if ".part" in f.name or ".fhls" in f.name or ".fdash" in f.name:
            continue
        total += 1
        ok, reason = verify_video(f)
        rel = f.relative_to(path)
        if ok:
            print(f"  OK   {rel}")
        else:
            print(f"  BAD  {rel}  ({reason})")
            bad += 1
    print(f"\n{total} files, {bad} corrupted")
    return bad


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "./downloads"
    print(f"Scanning: {path}\n")
    scan_dir(path)
