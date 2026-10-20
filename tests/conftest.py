"""
Shared test fixtures.

Environment variables must be set before the application modules are
imported, because configuration is read at import time.
"""

import os
import re
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

_TMP_DIR = tempfile.mkdtemp(prefix="downloader-tests-")
DOWNLOADS_DIR = os.path.join(_TMP_DIR, "downloads")
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

API_KEY = "test-api-key"

os.environ.update({
    "API_KEY": API_KEY,
    "DATABASE_PATH": os.path.join(_TMP_DIR, "data", "downloads.db"),
    "DOWNLOADS_BASE_PATH": DOWNLOADS_DIR,
    "RATE_LIMIT_REQUESTS": "100000",
    "MAX_RETRY_ATTEMPTS": "2",
    "RETRY_BACKOFF_BASE_SECONDS": "0",
    "WEBHOOK_RETRY_ATTEMPTS": "1",
    "WEBHOOK_RETRY_BACKOFF_SECONDS": "0",
    "MAX_CONCURRENT_DOWNLOADS": "1",
    "MAX_BANDWIDTH_MBPS": "0",
})

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def file_content(size: int) -> bytes:
    """Deterministic file content of the given size."""
    pattern = bytes(range(256))
    return (pattern * (size // 256 + 1))[:size]


class _FileHandler(BaseHTTPRequestHandler):
    """
    Test file server.

    GET /file/<size>?delay=<seconds per 64 KiB chunk>  - binary file with Range support
    GET /gz/<name>                                      - gzip-compressed text
    GET /status/<code>                                  - empty response with the status code
    """

    def log_message(self, *args):
        pass

    def do_GET(self):
        path, _, query = self.path.partition("?")
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)

        if path.startswith("/status/"):
            self.send_response(int(path.rsplit("/", 1)[1]))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if path.startswith("/gz/"):
            import gzip
            body = gzip.compress(b"hello gzip world\n" * 100)
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if not path.startswith("/file/"):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        size = int(path.rsplit("/", 1)[1])
        delay = float(params.get("delay", "0"))
        data = file_content(size)
        start = 0

        range_header = self.headers.get("Range")
        match = re.match(r"bytes=(\d+)-", range_header or "")
        if match and params.get("norange") != "1":
            start = int(match.group(1))
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{size - 1}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(size - start))
        self.end_headers()

        try:
            chunk = 65536
            for offset in range(start, size, chunk):
                self.wfile.write(data[offset:offset + chunk])
                if delay:
                    time.sleep(delay)
        except (BrokenPipeError, ConnectionResetError):
            pass


@pytest.fixture(scope="session")
def file_server():
    """Start the local test HTTP server and return its base URL."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FileHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture(scope="session")
def client():
    """Application test client with lifespan (database) running."""
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def downloads_dir():
    return DOWNLOADS_DIR


def wait_for(predicate, timeout: float = 15.0, interval: float = 0.05):
    """Poll until predicate returns a truthy value, or fail."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    raise AssertionError("Timed out waiting for condition")
