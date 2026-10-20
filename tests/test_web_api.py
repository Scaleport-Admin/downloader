"""
Tests for the web UI management API (/api) and runtime controls.
"""

import functools
import os
import time

import pytest

from conftest import API_KEY, file_content, wait_for

HEADERS = {"X-API-Key": API_KEY}
SLOW = "delay=0.05"  # ~1.3 MB/s from the test server


@pytest.fixture(autouse=True)
def reset_runtime_state(client):
    """Restore default settings and resume everything after each test."""
    yield
    for item in client.get("/api/settings", headers=HEADERS).json()["settings"]:
        if item["overridden"]:
            client.delete(f"/api/settings/{item['key']}", headers=HEADERS)
    client.post("/api/resume-all", headers=HEADERS)
    for d in client.get("/api/status", headers=HEADERS).json()["active_downloads"]:
        client.delete(f"/api/downloads/{d['download_id']}", headers=HEADERS)
    wait_for(lambda: not client.get("/api/status", headers=HEADERS).json()["active_downloads"])


def _start(client, url, filename, path="/web", **extra):
    response = client.post("/api/downloads", headers=HEADERS,
                           json={"url": url, "filename": filename, "path": path, **extra})
    assert response.status_code == 202, response.text
    return response.json()["download_id"]


def _active(client, download_id):
    for d in client.get("/api/status", headers=HEADERS).json()["active_downloads"]:
        if d["download_id"] == download_id:
            return d
    return None


def _history(client, download_id):
    rows = client.get("/api/history", headers=HEADERS, params={"limit": 1000}).json()["downloads"]
    return next((r for r in rows if r["download_id"] == download_id), None)


def _wait_state(client, download_id, state):
    return wait_for(lambda: (d := _active(client, download_id)) and d["state"] == state and d)


def test_ui_served(client):
    response = client.get("/ui")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]

    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/ui"


def test_api_requires_key(client):
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/status", headers={"X-API-Key": "nope"}).status_code == 401
    assert client.get("/api/status", headers=HEADERS).status_code == 200


def test_status_shape(client):
    data = client.get("/api/status", headers=HEADERS).json()
    for key in ("version", "paused", "active_downloads", "interrupted_downloads",
                "disk", "totals", "history_counts", "max_concurrent_downloads"):
        assert key in data
    assert data["paused"] is False


def test_settings_update_validate_and_reset(client):
    response = client.put("/api/settings", headers=HEADERS, json={"max_bandwidth_mbps": 25})
    assert response.status_code == 200
    item = next(s for s in response.json()["settings"] if s["key"] == "max_bandwidth_mbps")
    assert item["value"] == 25.0 and item["overridden"] is True

    from app.database import db
    stored = client.portal.call(db.get_settings)
    assert stored["max_bandwidth_mbps"] == "25.0"

    # Invalid values are rejected and nothing is applied
    response = client.put("/api/settings", headers=HEADERS,
                          json={"max_bandwidth_mbps": 1, "max_concurrent_downloads": 0})
    assert response.status_code == 400
    assert client.put("/api/settings", headers=HEADERS, json={"bogus": 1}).status_code == 400
    assert client.put("/api/settings", headers=HEADERS, json={"auto_decompress": "yes"}).status_code == 400
    item = next(s for s in client.get("/api/settings", headers=HEADERS).json()["settings"]
                if s["key"] == "max_bandwidth_mbps")
    assert item["value"] == 25.0

    response = client.delete("/api/settings/max_bandwidth_mbps", headers=HEADERS)
    assert response.status_code == 200
    item = next(s for s in response.json()["settings"] if s["key"] == "max_bandwidth_mbps")
    assert item["value"] == item["default"] and item["overridden"] is False
    assert "max_bandwidth_mbps" not in client.portal.call(db.get_settings)
    assert client.delete("/api/settings/bogus", headers=HEADERS).status_code == 404


def test_settings_load_ignores_bad_values():
    from app.settings import RuntimeSettings
    rs = RuntimeSettings()
    rs.load({"max_retry_attempts": "5", "max_concurrent_downloads": "-1", "unknown": "1", "log_level": "nope"})
    assert rs.get("max_retry_attempts") == 5
    assert not rs.is_overridden("max_concurrent_downloads")
    assert not rs.is_overridden("log_level")


def test_pause_and_resume_download(client, file_server, downloads_dir):
    size = 3_000_000
    download_id = _start(client, f"{file_server}/file/{size}?{SLOW}", "pause.bin")
    wait_for(lambda: (d := _active(client, download_id)) and d["downloaded_bytes"] > 100_000)

    assert client.post(f"/api/downloads/{download_id}/pause", headers=HEADERS).status_code == 200
    paused = _wait_state(client, download_id, "paused")
    assert paused["speed_mbps"] is None

    # Legacy endpoint keeps reporting the original status value
    legacy = client.get("/downloads", headers=HEADERS).json()["active_downloads"]
    assert next(d for d in legacy if d["download_id"] == download_id)["status"] == "downloading"

    path = os.path.join(downloads_dir, "web", "pause.bin")
    size_at_pause = os.path.getsize(path)
    time.sleep(0.6)
    assert os.path.getsize(path) == size_at_pause
    assert 0 < size_at_pause < size

    assert client.post(f"/api/downloads/{download_id}/resume", headers=HEADERS).status_code == 200
    entry = wait_for(lambda: _history(client, download_id), timeout=20)
    assert entry["status"] == "completed"
    with open(path, "rb") as f:
        assert f.read() == file_content(size)


def test_cancel_paused_download(client, file_server):
    download_id = _start(client, f"{file_server}/file/3000000?{SLOW}", "cancel-paused.bin", paused=True)
    _wait_state(client, download_id, "paused")
    assert client.delete(f"/api/downloads/{download_id}", headers=HEADERS).status_code == 200
    entry = wait_for(lambda: _history(client, download_id))
    assert entry["status"] == "cancelled"


def test_unknown_download_actions(client):
    for method, url in (("post", "/api/downloads/x/pause"), ("post", "/api/downloads/x/resume"),
                        ("delete", "/api/downloads/x"), ("post", "/api/interrupted/x/resume"),
                        ("delete", "/api/interrupted/x")):
        assert getattr(client, method)(url, headers=HEADERS).status_code == 404
    assert client.patch("/api/downloads/x", headers=HEADERS, json={"limit_mbps": 1}).status_code == 404


def test_pause_all_holds_queue(client, file_server):
    assert client.post("/api/pause-all", headers=HEADERS).json() == {"paused": True}
    download_id = _start(client, f"{file_server}/file/100000", "held.bin")
    _wait_state(client, download_id, "paused")
    time.sleep(0.5)
    assert _active(client, download_id)["downloaded_bytes"] == 0
    assert client.get("/api/status", headers=HEADERS).json()["paused"] is True

    client.post("/api/resume-all", headers=HEADERS)
    assert wait_for(lambda: _history(client, download_id))["status"] == "completed"


def test_concurrency_change_at_runtime(client, file_server):
    first = _start(client, f"{file_server}/file/3000000?{SLOW}", "c1.bin")
    second = _start(client, f"{file_server}/file/3000000?{SLOW}", "c2.bin")
    _wait_state(client, first, "downloading")
    queued = _wait_state(client, second, "queued")
    assert queued["queue_position"] == 1

    client.put("/api/settings", headers=HEADERS, json={"max_concurrent_downloads": 2})
    _wait_state(client, second, "downloading")
    assert client.get("/api/status", headers=HEADERS).json()["running"] == 2


def test_global_bandwidth_limit(client, file_server):
    # 8 Mbps = 1 MiB/s; 1.5 MiB should take at least ~1.2 s
    client.put("/api/settings", headers=HEADERS, json={"max_bandwidth_mbps": 8})
    size = 1_572_864
    started = time.monotonic()
    download_id = _start(client, f"{file_server}/file/{size}", "limited.bin")
    entry = wait_for(lambda: _history(client, download_id))
    elapsed = time.monotonic() - started
    assert entry["status"] == "completed"
    assert 1.1 < elapsed < 4, elapsed


def test_per_download_limit(client, file_server):
    size = 1_572_864
    download_id = _start(client, f"{file_server}/file/{size}", "limited2.bin", limit_mbps=8, paused=True)
    _wait_state(client, download_id, "paused")
    response = client.patch(f"/api/downloads/{download_id}", headers=HEADERS, json={"limit_mbps": 16})
    assert response.json()["limit_mbps"] == 16
    started = time.monotonic()
    client.post(f"/api/downloads/{download_id}/resume", headers=HEADERS)
    wait_for(lambda: _history(client, download_id))
    elapsed = time.monotonic() - started
    assert 0.5 < elapsed < 3, elapsed


def test_interrupted_download_resume_and_discard(client, file_server, downloads_dir):
    from app.database import db

    size = 400_000
    target_dir = os.path.join(downloads_dir, "interrupted")
    os.makedirs(target_dir, exist_ok=True)
    with open(os.path.join(target_dir, "orphan.bin"), "wb") as f:
        f.write(file_content(size)[:100_000])

    # Simulate rows left behind by a previous run of the service
    for download_id, name in (("dl_orphan_1", "orphan.bin"), ("dl_orphan_2", "orphan2.bin")):
        client.portal.call(functools.partial(
            db.add_download, download_id=download_id, url=f"{file_server}/file/{size}",
            filename=name, path=target_dir, headers='{"X-Test": "1"}'
        ))

    interrupted = client.get("/api/status", headers=HEADERS).json()["interrupted_downloads"]
    assert {d["download_id"] for d in interrupted} >= {"dl_orphan_1", "dl_orphan_2"}

    assert client.post("/api/interrupted/dl_orphan_1/resume", headers=HEADERS).status_code == 200
    assert wait_for(lambda: _history(client, "dl_orphan_1"))["status"] == "completed"
    with open(os.path.join(target_dir, "orphan.bin"), "rb") as f:
        assert f.read() == file_content(size)

    assert client.delete("/api/interrupted/dl_orphan_2", headers=HEADERS).status_code == 200
    assert _history(client, "dl_orphan_2")["status"] == "cancelled"
    assert client.get("/api/status", headers=HEADERS).json()["interrupted_downloads"] == []


def test_history_page_search_and_delete(client, file_server):
    ids = [_start(client, f"{file_server}/file/1000", f"hist_{i}_needle.bin") for i in range(3)]
    for download_id in ids:
        wait_for(lambda: _history(client, download_id))

    response = client.get("/api/history", headers=HEADERS,
                          params={"search": "_needle", "limit": 2, "status": "completed"})
    data = response.json()
    assert data["total"] == 3 and len(data["downloads"]) == 2
    response = client.get("/api/history", headers=HEADERS, params={"search": "_needle", "offset": 2})
    assert len(response.json()["downloads"]) == 1
    # LIKE wildcards are matched literally
    assert client.get("/api/history", headers=HEADERS, params={"search": "%"}).json()["total"] == 0
    assert client.get("/api/history", headers=HEADERS, params={"status": "bogus"}).status_code == 400

    assert client.delete(f"/api/history/{ids[0]}", headers=HEADERS).json() == {"deleted": 1}
    assert client.delete(f"/api/history/{ids[0]}", headers=HEADERS).status_code == 404
    assert client.get("/api/history", headers=HEADERS, params={"search": "_needle"}).json()["total"] == 2


def test_logs(client):
    client.post("/api/pause-all", headers=HEADERS)
    logs = client.get("/api/logs", headers=HEADERS).json()["logs"]
    assert any("All downloads paused" in r["message"] for r in logs)
    last_id = logs[-1]["id"]
    assert client.get("/api/logs", headers=HEADERS, params={"after": last_id}).json()["logs"] == []


def test_legacy_download_respects_runtime_settings(client, file_server):
    """Downloads started through the original API are controlled by the UI as well."""
    client.post("/api/pause-all", headers=HEADERS)
    response = client.post("/download", json={
        "url": f"{file_server}/file/1000", "filename": "legacy-held.bin", "path": "/", "api_key": API_KEY
    })
    download_id = response.json()["download_id"]
    _wait_state(client, download_id, "paused")
    client.post("/api/resume-all", headers=HEADERS)
    assert wait_for(lambda: _history(client, download_id))["status"] == "completed"
