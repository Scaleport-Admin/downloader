"""
Regression tests for the original public API.

These tests describe behaviour that existing clients rely on and must keep
passing unchanged.
"""

import gzip
import os

from conftest import API_KEY, file_content, wait_for

HEADERS = {"X-API-Key": API_KEY}


def _start(client, url, filename, path="/", **extra):
    body = {"url": url, "filename": filename, "path": path, "api_key": API_KEY}
    body.update(extra)
    response = client.post("/download", json=body)
    assert response.status_code == 202, response.text
    data = response.json()
    assert data["status"] == "started"
    assert data["message"] == "Download started in background"
    assert data["download_id"].startswith("dl_")
    return data["download_id"]


def _history_entry(client, download_id):
    response = client.get("/downloads/history", params={"api_key": API_KEY, "limit": 1000})
    assert response.status_code == 200
    for entry in response.json()["downloads"]:
        if entry["download_id"] == download_id:
            return entry
    return None


def _is_active(client, download_id):
    response = client.get("/downloads", headers=HEADERS)
    assert response.status_code == 200
    return any(d["download_id"] == download_id for d in response.json()["active_downloads"])


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert set(data) == {"status", "active_downloads", "disk_space_gb", "uptime_seconds"}
    assert data["status"] == "healthy"


def test_metrics(client):
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    for name in ("downloads_total", "downloads_active", "downloads_bytes_total",
                 "download_duration_seconds_bucket", "uptime_seconds"):
        assert name in response.text


def test_auth_required(client):
    response = client.get("/downloads")
    assert response.status_code == 401
    assert "API key required" in response.text

    response = client.get("/downloads", params={"api_key": "wrong"})
    assert response.status_code == 401
    assert response.text == "Invalid API key"

    response = client.get("/downloads", params={"api_key": API_KEY})
    assert response.status_code == 200

    response = client.get("/downloads", headers=HEADERS)
    assert response.status_code == 200


def test_post_download_wrong_key(client, file_server):
    response = client.post("/download", json={
        "url": f"{file_server}/file/10", "filename": "x", "path": "/", "api_key": "wrong"
    })
    assert response.status_code == 401


def test_validation(client, file_server):
    base = {"url": f"{file_server}/file/10", "filename": "x", "api_key": API_KEY}
    assert client.post("/download", json={**base, "path": "/a/../b"}).status_code == 422
    assert client.post("/download", json={**base, "path": "relative"}).status_code == 422
    assert client.post("/download", json={**base, "path": "/", "method": "PUT"}).status_code == 422


def test_download_completes(client, file_server, downloads_dir):
    size = 300_000
    download_id = _start(client, f"{file_server}/file/{size}", "complete.bin", "/legacy")

    entry = wait_for(lambda: _history_entry(client, download_id))
    assert entry["status"] == "completed"
    assert entry["size_bytes"] == size
    assert entry["filename"] == "complete.bin"
    assert entry["error_message"] is None
    assert not _is_active(client, download_id)

    with open(os.path.join(downloads_dir, "legacy", "complete.bin"), "rb") as f:
        assert f.read() == file_content(size)


def test_active_download_shape(client, file_server):
    download_id = _start(client, f"{file_server}/file/2000000?delay=0.05", "shape.bin")

    def active_entry():
        response = client.get("/downloads", headers=HEADERS)
        for d in response.json()["active_downloads"]:
            if d["download_id"] == download_id and d["downloaded_bytes"] > 0:
                return d
        return None

    entry = wait_for(active_entry)
    assert set(entry) == {
        "download_id", "filename", "url", "status", "progress_percent", "downloaded_bytes",
        "total_bytes", "speed_mbps", "started_at", "eta_seconds",
    }
    assert entry["status"] == "downloading"
    assert entry["total_bytes"] == 2000000

    assert client.delete(f"/downloads/{download_id}", headers=HEADERS).status_code == 200
    wait_for(lambda: _history_entry(client, download_id))


def test_cancel_download(client, file_server):
    download_id = _start(client, f"{file_server}/file/5000000?delay=0.05", "cancel.bin")
    wait_for(lambda: _is_active(client, download_id))

    response = client.delete(f"/downloads/{download_id}", params={"api_key": API_KEY})
    assert response.status_code == 200
    assert response.json() == {
        "download_id": download_id,
        "status": "cancelled",
        "message": "Download cancelled successfully",
    }

    entry = wait_for(lambda: _history_entry(client, download_id))
    assert entry["status"] == "cancelled"
    assert entry["error_message"] == "Cancelled by user"


def test_cancel_unknown(client):
    response = client.delete("/downloads/dl_unknown", headers=HEADERS)
    assert response.status_code == 404
    assert response.text == "Download not found or already completed: dl_unknown"


def test_client_error_fails_without_retry(client, file_server):
    download_id = _start(client, f"{file_server}/status/404", "missing.bin")
    entry = wait_for(lambda: _history_entry(client, download_id))
    assert entry["status"] == "failed"
    assert entry["error_message"].startswith("HTTP 404")


def test_server_error_retries_then_fails(client, file_server):
    download_id = _start(client, f"{file_server}/status/500", "broken.bin")
    entry = wait_for(lambda: _history_entry(client, download_id))
    assert entry["status"] == "failed"
    assert entry["error_message"] == "Download failed after 2 retries"


def test_history_filter(client):
    response = client.get("/downloads/history", params={"api_key": API_KEY, "status": "bogus"})
    assert response.status_code == 400

    response = client.get("/downloads/history", params={"api_key": API_KEY, "status": "completed"})
    assert response.status_code == 200
    data = response.json()
    assert set(data) == {"downloads", "total"}
    assert all(d["status"] == "completed" for d in data["downloads"])
    if data["downloads"]:
        assert set(data["downloads"][0]) == {
            "download_id", "filename", "url", "status", "size_bytes", "started_at",
            "completed_at", "duration_seconds", "error_message",
        }


def test_gzip_auto_decompress(client, file_server, downloads_dir):
    download_id = _start(client, f"{file_server}/gz/data", "data.txt.gz", "/gz")
    entry = wait_for(lambda: _history_entry(client, download_id))
    assert entry["status"] == "completed"
    target = os.path.join(downloads_dir, "gz", "data.txt")
    assert os.path.exists(target)
    assert not os.path.exists(target + ".gz")
    with open(target, "rb") as f:
        assert f.read() == b"hello gzip world\n" * 100


def test_resume_existing_partial_file(client, file_server, downloads_dir):
    size = 200_000
    os.makedirs(os.path.join(downloads_dir, "resume"), exist_ok=True)
    with open(os.path.join(downloads_dir, "resume", "partial.bin"), "wb") as f:
        f.write(file_content(size)[:50_000])

    download_id = _start(client, f"{file_server}/file/{size}", "partial.bin", "/resume")
    entry = wait_for(lambda: _history_entry(client, download_id))
    assert entry["status"] == "completed"
    with open(os.path.join(downloads_dir, "resume", "partial.bin"), "rb") as f:
        assert f.read() == file_content(size)


def test_rate_limiter_blocks():
    import pytest
    from fastapi import HTTPException
    from starlette.requests import Request
    from app.rate_limiter import RateLimiter

    limiter = RateLimiter(max_requests=2, window_seconds=60)
    request = Request({"type": "http", "headers": [], "client": ("1.2.3.4", 1)})
    limiter.check_rate_limit(request)
    limiter.check_rate_limit(request)
    with pytest.raises(HTTPException) as exc:
        limiter.check_rate_limit(request)
    assert exc.value.status_code == 429
    assert "Retry-After" in exc.value.headers
