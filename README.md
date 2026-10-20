# File Downloader Service

A production-ready Docker container with FastAPI service for downloading large files (10+ GB) with cookie authentication. Designed for deployment on TrueNAS Scale via docker-compose.

## Features

- **Large File Support**: Efficiently download files 10+ GB using async streaming
- **Cookie Authentication**: Support for authenticated downloads with cookies and custom headers
- **Progress Tracking**: Real-time download progress with speed and ETA calculations
- **Resume Support**: Automatically resume interrupted downloads using HTTP Range headers
- **Retry Logic**: Exponential backoff retry for network failures
- **Bandwidth Limiting**: Optional bandwidth throttling
- **Auto Decompression**: Automatically extract `.gz` and `.zip` files after download
- **Webhooks**: Notification callbacks for download completion and failures
- **Prometheus Metrics**: Built-in metrics endpoint for monitoring
- **SQLite Persistence**: Download history with configurable retention
- **Rate Limiting**: Per-IP rate limiting for API protection
- **API Key Authentication**: Secure API access with key validation
- **Web UI**: Dashboard for monitoring and managing downloads and settings (`/ui`)
- **Pause / Resume**: Pause single downloads or everything (also for 5 min ... 6 h or a custom time, like SABnzbd); paused downloads continue where they stopped
- **Scheduler**: Change the speed limit or pause/resume all downloads at set times on selected days
- **Runtime Settings**: Change speed limits, concurrency, retries etc. without restart
- **Restart Recovery**: Downloads interrupted by a restart can be resumed from the web UI

## Web UI

Open `http://<host>:8000/ui` (the service root `/` redirects there) and log in
with your `API_KEY`. The key is stored only in your browser.

- **Přehled (Overview)** – current speed, running/queued downloads, free disk space,
  progress with speed and ETA. Pause/resume/cancel each download, set a per-download
  speed limit, pause everything with one click, change speed limits and concurrency.
- **Nové stahování (New download)** – start a download including cookie, headers,
  POST data, webhooks and an optional speed limit.
- **Historie (History)** – searchable, filterable, paginated history; delete records.
- **Nastavení (Settings)** – all runtime settings (see below).
- **Plánovač (Scheduler)** – scheduled actions, see below.
- **Log** – recent service log records.

The pause button in the header pauses everything; its menu pauses for 5, 15, 30
minutes, 1, 3, 6 hours or a custom time, and the header shows the remaining time.
Without a pause it shows the current total speed.

### Scheduler

Each rule runs at a time of day (container local time, set `TZ`) on selected
weekdays and does one of:

- **Speed limit** – sets the total speed limit (`TOTAL_BANDWIDTH_MBPS`, 0 = unlimited).
  The per-download limit (`MAX_BANDWIDTH_MBPS`) still applies as a ceiling.
- **Pause all** / **Resume all**.

As in SABnzbd, a manual change stays in effect until the next scheduled action,
and at startup the most recent past action of each kind is applied. Example:
weekdays 08:00 limit 50 Mbps, every day 22:00 limit 0 (unlimited).

Downloads interrupted by a container restart are listed on the overview and can be
resumed (continuing from the partial file) or discarded.

### Runtime settings

These settings start with the value of their environment variable. When changed in
the web UI they apply immediately (also to running downloads), are stored in the
database and take precedence over the environment variable until reset with
"Výchozí" (default):

`MAX_CONCURRENT_DOWNLOADS`, `MAX_BANDWIDTH_MBPS`, `TOTAL_BANDWIDTH_MBPS`,
`AUTO_DECOMPRESS`, `MAX_RETRY_ATTEMPTS`, `RETRY_BACKOFF_BASE_SECONDS`,
`WEBHOOK_RETRY_ATTEMPTS`, `WEBHOOK_RETRY_BACKOFF_SECONDS`, `HISTORY_RETENTION_DAYS`,
`LOG_LEVEL`.

A manual "pause all" (including a timed pause) is not persisted – after a restart new
downloads start normally (unless a scheduled pause applies).

### Management API

The web UI uses a JSON API under `/api` (authentication via the `X-API-Key` header,
separate rate limit `UI_RATE_LIMIT_REQUESTS`). The original endpoints below are
unchanged.

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/status` | Dashboard data (active, queued, interrupted downloads, disk, totals) |
| POST | `/api/downloads` | Start a download (same fields as `POST /download` without `api_key`, plus `limit_mbps`, `paused`) |
| POST | `/api/downloads/{id}/pause` | Pause a download |
| POST | `/api/downloads/{id}/resume` | Resume a download |
| PATCH | `/api/downloads/{id}` | Set per-download speed limit `{"limit_mbps": 50}` |
| DELETE | `/api/downloads/{id}` | Cancel a download |
| POST | `/api/pause-all` | Pause all downloads; optional body `{"minutes": 15}` resumes automatically |
| POST | `/api/resume-all` | Resume all downloads |
| GET / POST | `/api/schedules` | List / add scheduled actions `{"days": [0,1,2,3,4], "time": "08:00", "action": "speed_limit", "value": 50, "enabled": true}` (days: 0 = Monday; action: `speed_limit`, `pause`, `resume`) |
| PUT / DELETE | `/api/schedules/{id}` | Change / delete a scheduled action |
| POST | `/api/interrupted/{id}/resume` | Resume a download interrupted by a restart |
| DELETE | `/api/interrupted/{id}` | Discard an interrupted download |
| GET | `/api/history` | History (`limit`, `offset`, `status`, `search`) |
| DELETE | `/api/history/{id}`, `/api/history?status=` | Delete history records (files are kept) |
| GET / PUT | `/api/settings` | Read / change runtime settings |
| DELETE | `/api/settings/{key}` | Reset a setting to its environment value |
| GET | `/api/logs` | Recent log records (`after` = last seen id) |

## Quick Start

### 1. Clone and Configure

```bash
# Create environment file
cp .env.example .env

# Generate a secure API key
openssl rand -hex 32

# Edit .env and set your API_KEY
nano .env
```

### 2. Build and Run

```bash
# Build the Docker image
docker-compose build

# Start the service
docker-compose up -d

# Check logs
docker-compose logs -f
```

### 3. Verify Installation

```bash
# Health check
curl http://localhost:8000/health

# Expected response:
# {"status":"healthy","active_downloads":0,"disk_space_gb":450.5,"uptime_seconds":10}
```

## API Endpoints

### POST /download

Start a new download.

```bash
curl -X POST http://localhost:8000/download \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.com/largefile.zip",
    "filename": "largefile.zip",
    "path": "/downloads",
    "api_key": "your_api_key",
    "cookie": "session=abc123",
    "headers": {
      "User-Agent": "Mozilla/5.0"
    },
    "webhook_url_complete": "https://webhook.site/xxx",
    "webhook_url_error": "https://webhook.site/xxx"
  }'
```

**Response (202 Accepted):**
```json
{
  "download_id": "dl_1234567890.123",
  "status": "started",
  "message": "Download started in background"
}
```

### GET /downloads

List active downloads with progress.

```bash
curl "http://localhost:8000/downloads?api_key=your_api_key"
```

**Response:**
```json
{
  "active_downloads": [
    {
      "download_id": "dl_1234567890.123",
      "filename": "largefile.zip",
      "url": "https://example.com/largefile.zip",
      "status": "downloading",
      "progress_percent": 45.2,
      "downloaded_bytes": 1234567890,
      "total_bytes": 2730000000,
      "speed_mbps": 12.5,
      "started_at": "2025-01-15T10:30:00Z",
      "eta_seconds": 180
    }
  ]
}
```

### GET /downloads/history

Get download history.

```bash
# All history
curl "http://localhost:8000/downloads/history?api_key=your_api_key"

# Filter by status
curl "http://localhost:8000/downloads/history?api_key=your_api_key&status=completed&limit=50"
```

**Response:**
```json
{
  "downloads": [
    {
      "download_id": "dl_1234567890.123",
      "filename": "largefile.zip",
      "url": "https://example.com/largefile.zip",
      "status": "completed",
      "size_bytes": 2730000000,
      "started_at": "2025-01-15T10:30:00Z",
      "completed_at": "2025-01-15T10:35:23Z",
      "duration_seconds": 323,
      "error_message": null
    }
  ],
  "total": 150
}
```

### DELETE /downloads/{download_id}

Cancel a running download.

```bash
curl -X DELETE "http://localhost:8000/downloads/dl_1234567890.123?api_key=your_api_key"
```

**Response:**
```json
{
  "download_id": "dl_1234567890.123",
  "status": "cancelled",
  "message": "Download cancelled successfully"
}
```

### GET /health

Health check endpoint (no authentication required).

```bash
curl http://localhost:8000/health
```

**Response:**
```json
{
  "status": "healthy",
  "active_downloads": 2,
  "disk_space_gb": 450.5,
  "uptime_seconds": 86400
}
```

### GET /metrics

Prometheus metrics endpoint (no authentication required).

```bash
curl http://localhost:8000/metrics
```

**Response:**
```
# HELP downloads_total Total number of downloads
# TYPE downloads_total counter
downloads_total{status="completed"} 1250
downloads_total{status="failed"} 45
downloads_total{status="cancelled"} 12

# HELP downloads_active Currently active downloads
# TYPE downloads_active gauge
downloads_active 2

# HELP downloads_bytes_total Total bytes downloaded
# TYPE downloads_bytes_total counter
downloads_bytes_total 5.5e+12

# HELP download_duration_seconds Download duration
# TYPE download_duration_seconds histogram
download_duration_seconds_bucket{le="60"} 150
download_duration_seconds_bucket{le="300"} 450
download_duration_seconds_bucket{le="600"} 800
download_duration_seconds_bucket{le="+Inf"} 1250

# HELP uptime_seconds Service uptime in seconds
# TYPE uptime_seconds gauge
uptime_seconds 86400
```

## Configuration

All configuration is done via environment variables. See `.env.example` for full list.

### Required

| Variable | Description |
|----------|-------------|
| `API_KEY` | API key for authentication |

### Optional - Server

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `8000` | Server port |
| `LOG_LEVEL` | `INFO` | Log level (DEBUG, INFO, WARNING, ERROR) |
| `TZ` | `UTC` | Timezone |
| `PUID` / `PGID` | `1000` | User/group the service runs as (ignored with `RUN_AS_ROOT`) |
| `RUN_AS_ROOT` | `false` | Run as root (for NFS/network shares where ownership cannot be changed) |
| `CHOWN_DOWNLOADS_RECURSIVE` | `false` | Change ownership of everything in `/downloads` at startup (slow on large datasets); by default only the directory itself |

### Optional - Rate Limiting

| Variable | Default | Description |
|----------|---------|-------------|
| `RATE_LIMIT_REQUESTS` | `10` | Max requests per window per IP |
| `RATE_LIMIT_WINDOW_SECONDS` | `60` | Rate limit window in seconds |

### Optional - Download Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `MAX_CONCURRENT_DOWNLOADS` | `1` | Maximum concurrent downloads |
| `MAX_BANDWIDTH_MBPS` | `0` | Max bandwidth per download (0 = unlimited) |
| `TOTAL_BANDWIDTH_MBPS` | `0` | Max bandwidth shared by all downloads (0 = unlimited) |
| `AUTO_DECOMPRESS` | `true` | Auto-decompress .gz/.zip files |

### Optional - Web UI

| Variable | Default | Description |
|----------|---------|-------------|
| `WEB_UI_ENABLED` | `true` | Enable web UI (`/ui`) and management API (`/api`) |
| `UI_RATE_LIMIT_REQUESTS` | `600` | Max management API requests per window per IP |
| `UI_RATE_LIMIT_WINDOW_SECONDS` | `60` | Management API rate limit window |

### Optional - Retry Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `MAX_RETRY_ATTEMPTS` | `3` | Max download retries |
| `RETRY_BACKOFF_BASE_SECONDS` | `5` | Base backoff (exponential: 5s, 15s, 45s) |

### Optional - Webhooks

| Variable | Default | Description |
|----------|---------|-------------|
| `WEBHOOK_RETRY_ATTEMPTS` | `3` | Max webhook retries |
| `WEBHOOK_RETRY_BACKOFF_SECONDS` | `5` | Base webhook backoff |

### Optional - Database

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_PATH` | `/data/downloads.db` | SQLite database path |
| `HISTORY_RETENTION_DAYS` | `30` | Days to retain history |

## TrueNAS Scale Deployment

### 1. Prepare Directories

Create datasets on your TrueNAS pool:
```bash
# Downloads directory
mkdir -p /mnt/pool/downloads

# Service data directory
mkdir -p /mnt/pool/file-downloader/data
```

### 2. Update docker-compose.yml

For a TrueNAS "Custom App" (Install via YAML) use `docker-compose.truenas.yml`:
it builds the image directly from GitHub on every deploy (`pull_policy: build`),
so no local checkout is needed and an outdated cached `file-downloader:latest`
image is never reused. The first line of the container log shows the running
version (`Starting File Downloader Service v1.1.0...`).

Otherwise edit the volume paths in `docker-compose.yml`:

```yaml
volumes:
  - /mnt/pool/downloads:/downloads
  - /mnt/pool/file-downloader/data:/data
```

### 3. Deploy

```bash
# Set your API key
export API_KEY=$(openssl rand -hex 32)

# Deploy
docker-compose up -d
```

### 4. Monitor

```bash
# View logs
docker-compose logs -f

# Check health
curl http://your-truenas-ip:8000/health
```

## Webhook Payloads

### Completion Webhook

```json
{
  "download_id": "dl_1234567890.123",
  "status": "completed",
  "filename": "largefile.zip",
  "path": "/downloads/largefile.zip",
  "size_bytes": 2730000000,
  "started_at": "2025-01-15T10:30:00Z",
  "completed_at": "2025-01-15T10:35:23Z",
  "duration_seconds": 323,
  "decompressed": true
}
```

### Error Webhook

```json
{
  "download_id": "dl_1234567890.123",
  "status": "failed",
  "filename": "largefile.zip",
  "error": "Connection timeout after 3 retries",
  "started_at": "2025-01-15T10:30:00Z",
  "failed_at": "2025-01-15T10:32:15Z"
}
```

## Security Features

- **API Key Authentication**: All endpoints (except `/health` and `/metrics`) require API key
- **Path Traversal Protection**: Validates paths to prevent directory traversal attacks
- **Filename Sanitization**: Removes dangerous characters from filenames
- **Rate Limiting**: Prevents API abuse with per-IP rate limiting
- **Non-root Container**: Runs as non-privileged user inside container

## Development

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

The test suite contains regression tests for the original API (`tests/test_legacy_api.py`)
and tests for the web UI API (`tests/test_web_api.py`).

## Rollback

Version 1.0.0 (before the web UI) is commit `3cf8f67` (tag `v1.0.0`). To go back:

```bash
git checkout 3cf8f67        # or: git checkout v1.0.0
docker-compose build && docker-compose up -d
```

The database stays compatible in both directions: version 1.1.0 only adds the
`settings` and `schedules` tables, which version 1.0.0 ignores.

## Project Structure

```
file-downloader/
├── app/
│   ├── __init__.py           # Package initialization
│   ├── main.py               # FastAPI app & endpoints
│   ├── models.py             # Pydantic models
│   ├── database.py           # SQLite operations
│   ├── downloader.py         # Core download logic
│   ├── webhooks.py           # Webhook handling
│   ├── auth.py               # API key validation
│   ├── rate_limiter.py       # Rate limiting logic
│   ├── metrics.py            # Prometheus metrics
│   ├── settings.py           # Runtime settings (changeable from web UI)
│   ├── runtime.py            # Loading / saving / applying runtime settings
│   ├── scheduler.py          # Scheduled speed limit / pause / resume
│   ├── throttle.py           # Bandwidth throttling and download queue
│   ├── web.py                # Web UI and management API (/ui, /api)
│   ├── static/index.html     # Web UI (single page, no external dependencies)
│   └── utils.py              # Helper functions
├── tests/                    # pytest test suite
├── Dockerfile                # Multi-stage Docker build
├── docker-compose.yml        # Docker Compose config
├── requirements.txt          # Python dependencies
├── .env.example              # Environment template
├── .dockerignore             # Docker ignore rules
└── README.md                 # This file
```

## License

MIT License
