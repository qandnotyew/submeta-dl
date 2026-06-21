# submeta-dl

Download BJJ instructional courses and standalone videos from [Submeta.io](https://submeta.io) using their GraphQL API. Requires an active Submeta subscription.

## Features

- Downloads full course catalog with chapter/video structure
- Downloads standalone videos (rolls, discussions, technique breakdowns, etc.)
- Resumable — tracks progress in JSON state files, picks up where it left off
- Connectivity-aware — detects network drops, waits for recovery, retries
- Downloads video thumbnails from Cloudflare Stream
- Dry-run mode to preview what would be downloaded

## Requirements

- Python 3.10+
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- [aria2](https://aria2.github.io/)
- An active [Submeta](https://submeta.io) subscription

### Install dependencies

**macOS:**
```bash
brew install yt-dlp aria2
```

**Linux:**
```bash
# yt-dlp
pip install yt-dlp
# or: sudo curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /usr/local/bin/yt-dlp && sudo chmod a+rx /usr/local/bin/yt-dlp

# aria2
sudo apt install aria2    # Debian/Ubuntu
sudo dnf install aria2    # Fedora
```

## Setup

1. Clone this repo
2. Copy `.env.example` to `.env` and fill in your Submeta credentials:
   ```bash
   cp .env.example .env
   ```
3. Create a download directory:
   ```bash
   mkdir -p downloads
   ```

## Usage

### Courses

```bash
# Preview the full course catalog
python3 submeta_dl.py --dry-run

# Download everything
python3 submeta_dl.py

# Download a single course by URL
python3 submeta_dl.py --url https://submeta.io/@lachlangiles/courses/adaptive-guard-passing

# Check download status
python3 submeta_dl.py --list

# Re-download completed courses
python3 submeta_dl.py --force

# Download to a specific directory
python3 submeta_dl.py --download-dir /path/to/storage
```

### Standalone Videos

```bash
# Preview standalone video catalog
python3 submeta_videos_dl.py --dry-run

# Download all standalone videos
python3 submeta_videos_dl.py

# Check for new videos without downloading
python3 submeta_videos_dl.py --monitor

# Check download status
python3 submeta_videos_dl.py --list
```

## Directory Structure

### Courses
```
downloads/
├── downloads.json              # Course download state
├── Lachlan Giles/
│   ├── Adaptive Guard Passing/
│   │   ├── course_metadata.json
│   │   ├── 01_Introduction/
│   │   │   ├── 01_Welcome.mp4
│   │   │   └── 02_Overview.mp4
│   │   └── 02_Passing Concepts/
│   │       └── ...
│   └── K-Guard 1/
│       └── ...
└── ...
```

### Standalone Videos
```
downloads/
├── videos_state.json           # Video download state
└── Videos/
    ├── thumbnails/
    │   └── {video_id}.jpg
    ├── Lachlan Giles/
    │   ├── Technique/
    │   │   └── Video_Title.mp4
    │   └── Rolls/
    │       └── ...
    └── ...
```

## How It Works

1. Authenticates via REST (`POST /auth/login`) to get an access token + session cookies
2. Discovers the full catalog via GraphQL (`searchCourses` / `searchVideos`)
3. For each video, requests a signed Cloudflare Stream JWT token via GraphQL
4. Downloads the DASH stream using yt-dlp + aria2c for fast, reliable downloads
5. Tracks progress in `downloads.json` / `videos_state.json` so interrupted downloads resume cleanly

## State Files

- `downloads.json` — course download progress (which courses/videos are complete, partial, or pending)
- `videos_state.json` — standalone video download progress

These files are safe to inspect and even edit. If a download got corrupted, you can set a video's `"downloaded": false` to re-download it.

## Configuration

All config is via `.env` file or environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `SUBMETA_USERNAME` | (required) | Your Submeta email |
| `SUBMETA_PASSWORD` | (required) | Your Submeta password |
| `SUBMETA_DOWNLOAD_DIR` | `./downloads` | Where to save files |
| `SUBMETA_RATE_LIMIT` | `2` | Seconds between downloads |
| `SUBMETA_CLOUDFLARE_DOMAIN` | `customer-...cloudflarestream.com` | Cloudflare Stream domain (don't change unless Submeta changes theirs) |

## License

MIT
