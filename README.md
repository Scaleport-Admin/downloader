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

### Optional - Rate Limiting

| Variable | Default | Description |
|----------|---------|-------------|
| `RATE_LIMIT_REQUESTS` | `10` | Max requests per window per IP |
| `RATE_LIMIT_WINDOW_SECONDS` | `60` | Rate limit window in seconds |

### Optional - Download Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `MAX_CONCURRENT_DOWNLOADS` | `1` | Maximum concurrent downloads |
| `MAX_BANDWIDTH_MBPS` | `0` | Max bandwidth (0 = unlimited) |
| `AUTO_DECOMPRESS` | `true` | Auto-decompress .gz/.zip files |

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

Edit volume paths in `docker-compose.yml`:
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
│   └── utils.py              # Helper functions
├── Dockerfile                # Multi-stage Docker build
├── docker-compose.yml        # Docker Compose config
├── requirements.txt          # Python dependencies
├── .env.example              # Environment template
├── .dockerignore             # Docker ignore rules
└── README.md                 # This file
```

## License

MIT License
