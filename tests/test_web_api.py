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
    for item in client.get("/api/schedules", headers=HEADERS).json()["schedules"]:
        client.delete(f"/api/schedules/{item['id']}", headers=HEADERS)
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
    assert client.post("/api/pause-all", headers=HEADERS).json() == {
        "paused": True, "pause_remaining_seconds": None
    }
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


def test_timed_pause_resumes_automatically(client, file_server):
    import app.downloader as downloader

    response = client.post("/api/pause-all", headers=HEADERS, json={"minutes": 15})
    assert response.json()["pause_remaining_seconds"] == 900
    status = client.get("/api/status", headers=HEADERS).json()
    assert status["paused"] is True and 890 <= status["pause_remaining_seconds"] <= 900

    download_id = _start(client, f"{file_server}/file/1000", "timed.bin")
    _wait_state(client, download_id, "paused")

    # Let the timer expire
    downloader._pause_until = time.time() - 1
    assert wait_for(lambda: _history(client, download_id))["status"] == "completed"
    status = client.get("/api/status", headers=HEADERS).json()
    assert status["paused"] is False and status["pause_remaining_seconds"] is None

    assert client.post("/api/pause-all", headers=HEADERS, json={"minutes": 0}).status_code == 422


def test_schedule_crud_and_validation(client):
    body = {"days": [0, 1, 2, 3, 4], "time": "8:00", "action": "speed_limit", "value": 50}
    response = client.post("/api/schedules", headers=HEADERS, json=body)
    assert response.status_code == 201
    schedule = response.json()["schedules"][0]
    assert schedule["time"] == "08:00" and schedule["enabled"] is True and schedule["next_run"]

    bad = [
        {**body, "days": []}, {**body, "days": [7]}, {**body, "time": "24:00"},
        {**body, "time": "abc"}, {**body, "action": "explode"},
    ]
    for item in bad:
        assert client.post("/api/schedules", headers=HEADERS, json=item).status_code == 422
    assert client.post("/api/schedules", headers=HEADERS,
                       json={**body, "value": None}).status_code == 400

    response = client.put(f"/api/schedules/{schedule['id']}", headers=HEADERS,
                          json={**body, "action": "pause", "enabled": False})
    updated = response.json()["schedules"][0]
    assert updated["action"] == "pause" and updated["value"] is None and updated["next_run"] is None

    assert client.delete(f"/api/schedules/{schedule['id']}", headers=HEADERS).status_code == 200
    assert client.delete(f"/api/schedules/{schedule['id']}", headers=HEADERS).status_code == 404
    assert client.put("/api/schedules/999999", headers=HEADERS, json=body).status_code == 404


def test_scheduler_fires_actions(client):
    from datetime import datetime
    from app.scheduler import scheduler
    import app.downloader as downloader

    def run(coro_fn, *args, **kwargs):
        import functools
        return client.portal.call(functools.partial(coro_fn, *args, **kwargs))

    every_day = list(range(7))
    client.post("/api/schedules", headers=HEADERS,
                json={"days": every_day, "time": "08:00", "action": "speed_limit", "value": 50})
    client.post("/api/schedules", headers=HEADERS,
                json={"days": every_day, "time": "22:00", "action": "speed_limit", "value": 0})
    client.post("/api/schedules", headers=HEADERS,
                json={"days": every_day, "time": "12:00", "action": "pause"})
    client.post("/api/schedules", headers=HEADERS,
                json={"days": every_day, "time": "13:00", "action": "resume"})
    client.post("/api/schedules", headers=HEADERS,
                json={"days": every_day, "time": "07:00", "action": "pause", "enabled": False})

    def total_limit():
        return client.get("/api/status", headers=HEADERS).json()["total_bandwidth_mbps"]

    # Fake dates lie in the future so they never collide with the real clock
    # used when the API reloads the schedules
    # Startup at 10:00 applies the latest actions: 08:00 limit 50, 13:00 (yesterday) resume
    run(scheduler.reload, apply_latest=True, now=datetime(2030, 1, 8, 10, 0))
    assert total_limit() == 50
    assert downloader.is_globally_paused() is False

    # A manual change stays until the next scheduled action
    client.put("/api/settings", headers=HEADERS, json={"total_bandwidth_mbps": 80})
    run(scheduler.check, now=datetime(2030, 1, 8, 11, 59))
    assert total_limit() == 80

    run(scheduler.check, now=datetime(2030, 1, 8, 12, 0, 10))
    assert downloader.is_globally_paused() is True
    run(scheduler.check, now=datetime(2030, 1, 8, 13, 0, 5))
    assert downloader.is_globally_paused() is False

    run(scheduler.check, now=datetime(2030, 1, 8, 22, 0, 1))
    assert total_limit() == 0

    # The disabled 07:00 pause never fires
    run(scheduler.check, now=datetime(2030, 1, 9, 7, 0, 30))
    assert downloader.is_globally_paused() is False
    run(scheduler.check, now=datetime(2030, 1, 9, 8, 0, 30))
    assert total_limit() == 50

    # Adding a schedule whose time already passed today does not fire it immediately
    from app.database import db
    run(db.add_schedule, enabled=True, days=every_day, time="08:30", action="speed_limit", value=10)
    run(scheduler.reload, now=datetime(2030, 1, 9, 9, 0))
    run(scheduler.check, now=datetime(2030, 1, 9, 9, 0, 20))
    assert total_limit() == 50


def test_scheduler_occurrence_helpers():
    from datetime import datetime
    from app.scheduler import latest_occurrence, next_occurrence

    weekdays = {"days": [0, 1, 2, 3, 4], "time": "08:00"}
    saturday = datetime(2026, 10, 24, 9, 0)  # Saturday
    assert latest_occurrence(weekdays, saturday) == datetime(2026, 10, 23, 8, 0)
    assert next_occurrence(weekdays, saturday) == datetime(2026, 10, 26, 8, 0)
    assert next_occurrence(weekdays, datetime(2026, 10, 26, 7, 59)) == datetime(2026, 10, 26, 8, 0)


def _sha256(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


def test_checksum_computed_and_verified(client, file_server, downloads_dir):
    size = 500_000
    expected = _sha256(file_content(size))

    # Without expected checksum: computed and shown in history
    download_id = _start(client, f"{file_server}/file/{size}", "sum-plain.bin")
    entry = wait_for(lambda: _history(client, download_id))
    assert entry["status"] == "completed"
    assert entry["sha256"] == expected and entry["checksum_verified"] is False

    # With expected checksum (prefix and uppercase accepted), via the original API
    response = client.post("/download", json={
        "url": f"{file_server}/file/{size}", "filename": "sum-verified.bin", "path": "/web",
        "api_key": API_KEY, "sha256": "sha256:" + expected.upper(),
    })
    assert response.status_code == 202
    entry = wait_for(lambda: _history(client, response.json()["download_id"]))
    assert entry["status"] == "completed" and entry["checksum_verified"] is True

    # Legacy history response keeps its fields
    legacy = client.get("/downloads/history", params={"api_key": API_KEY}).json()["downloads"][0]
    assert "sha256" not in legacy


def test_checksum_mismatch_fails_and_keeps_file(client, file_server, downloads_dir):
    download_id = _start(client, f"{file_server}/file/100000", "sum-bad.bin", sha256="0" * 64)
    entry = wait_for(lambda: _history(client, download_id))
    assert entry["status"] == "failed"
    assert entry["error_message"].startswith("SHA256 mismatch: expected " + "0" * 64)
    target = os.path.join(downloads_dir, "web", "sum-bad.bin")
    assert not os.path.exists(target)
    assert os.path.getsize(target + ".sha256-mismatch") == 100000


def test_checksum_invalid_value(client, file_server):
    for value in ("abc", "g" * 64, "0" * 63):
        response = client.post("/api/downloads", headers=HEADERS, json={
            "url": f"{file_server}/file/10", "filename": "x.bin", "path": "/web", "sha256": value
        })
        assert response.status_code == 422


def test_checksum_survives_pause_and_restart(client, file_server, downloads_dir):
    from app.database import db

    size = 3_000_000
    expected = _sha256(file_content(size))

    # Pause / resume within one run: streaming hash continues
    download_id = _start(client, f"{file_server}/file/{size}?{SLOW}", "sum-pause.bin", sha256=expected)
    wait_for(lambda: (d := _active(client, download_id)) and d["downloaded_bytes"] > 200_000)
    client.post(f"/api/downloads/{download_id}/pause", headers=HEADERS)
    _wait_state(client, download_id, "paused")
    client.post(f"/api/downloads/{download_id}/resume", headers=HEADERS)
    entry = wait_for(lambda: _history(client, download_id), timeout=20)
    assert entry["status"] == "completed" and entry["checksum_verified"] is True

    # Interrupted by a restart: the partial file is hashed before resuming
    size = 400_000
    target_dir = os.path.join(downloads_dir, "sum-restart")
    os.makedirs(target_dir, exist_ok=True)
    with open(os.path.join(target_dir, "part.bin"), "wb") as f:
        f.write(file_content(size)[:150_000])
    client.portal.call(functools.partial(
        db.add_download, download_id="dl_sum_orphan", url=f"{file_server}/file/{size}",
        filename="part.bin", path=target_dir, expected_sha256=_sha256(file_content(size))
    ))
    client.post("/api/interrupted/dl_sum_orphan/resume", headers=HEADERS)
    entry = wait_for(lambda: _history(client, "dl_sum_orphan"))
    assert entry["status"] == "completed" and entry["checksum_verified"] is True


def test_event_stream(client, file_server):
    import json
    from app.web import event_stream

    async def collect(count, action=None):
        stream = event_stream()
        items = []
        try:
            async for item in stream:
                items.append(item)
                if len(items) == 2 and action:
                    await action()
                if len(items) >= count:
                    break
        finally:
            await stream.aclose()
        return items

    items = client.portal.call(collect, 3)
    assert items[0].startswith("retry:")
    assert items[1].startswith("event: status\ndata: ")
    status = json.loads(items[1].split("data: ", 1)[1])
    assert "active_downloads" in status
    assert items[2].startswith("event: logs\ndata: ")

    # A change is pushed right away (well under the 1 s interval)
    import time as _time
    from app.downloader import pause_all

    async def do_pause():
        pause_all()

    started = _time.monotonic()
    items = client.portal.call(collect, 4, do_pause)
    assert _time.monotonic() - started < 0.9
    pushed = [json.loads(i.split("data: ", 1)[1]) for i in items[2:] if i.startswith("event: status")]
    assert pushed and pushed[-1]["paused"] is True


def test_events_endpoint_requires_key(client):
    assert client.get("/api/events").status_code == 401
