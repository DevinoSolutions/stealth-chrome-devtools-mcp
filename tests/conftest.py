"""Shared fixtures for stealth-chrome-devtools-mcp test suite."""

import functools
import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

from stealth_chrome_devtools_mcp.settings import get_settings

# ── Make embedded/ importable the same way the real entrypoint does ──
EMBEDDED_DIR = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "stealth_chrome_devtools_mcp"
    / "embedded"
)
if str(EMBEDDED_DIR) not in sys.path:
    sys.path.insert(0, str(EMBEDDED_DIR))

# ── Make the tests/ dir importable so modules can `from fakes import ...`
# (the canonical M6 harness home) regardless of pytest import mode. ──
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

# Redirect clone / large-response artifacts to a temp dir for the whole test
# session. The module-global ResponseHandler()/FileBasedElementCloner() create
# their output dir at import time, and various tools spill files there — none of
# it should touch the installed package or the real ~/.stealth-mcp. setdefault
# so an explicit env (e.g. CI) still wins.
os.environ.setdefault(
    "STEALTH_MCP_CLONE_OUTPUT_DIR",
    str(Path(tempfile.gettempdir()) / "stealth-mcp-test-clone-output"),
)
os.environ.setdefault("STEALTH_MCP_NO_AUTO_RECOVERY", "1")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    """Every test gets a fresh Settings read. ``get_settings()`` is process-cached
    (``@lru_cache``), so without this an env mutation via ``monkeypatch`` /
    ``patch.dict`` would be invisible to any migrated code that reads Settings."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def tmp_session_root(tmp_path):
    """
    Create an isolated session root with master + snapshot + sessions/ dirs.
    Patches the env vars so all profile helpers resolve inside tmp_path.
    """
    master = tmp_path / "master" / "Default"
    master.mkdir(parents=True)
    # Minimal profile files Chrome needs
    (master / "Preferences").write_text("{}", encoding="utf-8")
    (master / "Cookies").write_bytes(b"sqlite-cookie-stub")
    (master / "Login Data").write_bytes(b"sqlite-login-stub")
    (master / "Web Data").write_bytes(b"sqlite-webdata-stub")

    snapshot = tmp_path / "master-snapshot" / "Default"
    shutil.copytree(str(master.parent), str(snapshot.parent))
    # Write clone marker so snapshot is recognised
    marker = snapshot.parent / ".stealth_chrome_devtools_mcp_clone.json"
    marker.write_text(
        json.dumps(
            {
                "source": str(master.parent),
                "source_kind": "test-fixture",
                "created_at": "2026-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    sessions = tmp_path / "sessions"
    sessions.mkdir()

    env_patches = {
        "STEALTH_MCP_BROWSER_SESSION_ROOT": str(tmp_path),
        "BROWSER_MASTER_USER_DATA_DIR": str(master.parent),
        "BROWSER_MASTER_SNAPSHOT_DIR": str(snapshot.parent),
        "BROWSER_PROFILE_CLONE_ROOT": str(sessions),
    }
    with patch.dict(os.environ, env_patches):
        yield {
            "root": tmp_path,
            "master": master.parent,
            "snapshot": snapshot.parent,
            "sessions": sessions,
        }


@pytest.fixture()
def tmp_empty_root(tmp_path):
    """
    Session root with NO master, NO snapshot — simulates first-ever run.
    """
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    master = tmp_path / "master"

    env_patches = {
        "STEALTH_MCP_BROWSER_SESSION_ROOT": str(tmp_path),
        "BROWSER_MASTER_USER_DATA_DIR": str(master),
        "BROWSER_MASTER_SNAPSHOT_DIR": str(tmp_path / "master-snapshot"),
        "BROWSER_PROFILE_CLONE_ROOT": str(sessions),
    }
    with patch.dict(os.environ, env_patches):
        yield {
            "root": tmp_path,
            "master": master,
            "snapshot": tmp_path / "master-snapshot",
            "sessions": sessions,
        }


# ---------------------------------------------------------------------------
# M6 characterization harness fixtures — thin wrappers over tests/fakes.py
# (the canonical home). Convenience defaults; tests needing custom config
# import the classes from ``fakes`` directly.
# ---------------------------------------------------------------------------


@pytest.fixture()
def call_tool():
    """The one in-process tool invoker (unwrap ``.fn``, await if awaitable)."""
    from fakes import call_tool as _call_tool

    return _call_tool


@pytest.fixture()
def fake_tab():
    from fakes import FakeTab

    return FakeTab()


@pytest.fixture()
def fake_browser():
    from fakes import FakeBrowser

    return FakeBrowser()


@pytest.fixture()
def fake_browser_manager():
    from fakes import FakeBrowserManager

    return FakeBrowserManager()


@pytest.fixture()
def patched_server(monkeypatch):
    """Return a helper that swaps ``server``'s module-global singletons for fakes
    and hands back the ``server`` module.

    Tools resolve ``browser_manager``/``element_cloner``/``in_memory_storage``/…
    as names in ``server``'s namespace at call time, so ``setattr(server, name,
    fake)`` is a clean hermetic seam needing no production change. ``monkeypatch``
    restores every attr at teardown.
    """
    import server

    def _patch(**singletons):
        for name, obj in singletons.items():
            monkeypatch.setattr(server, name, obj)
        return server

    return _patch


# ---------------------------------------------------------------------------
# plan_E2E — self-contained fixture web app served over a local HTTP server.
# Session-scoped so the E2E integration suite (and the hermetic smoke test)
# share one ephemeral-port server. Serves tests/fixture_app plus the four
# deterministic API routes from plan_E2E §2.2. No external network; the port is
# ephemeral and threaded through base_url, so it never appears in fixture files.
# ---------------------------------------------------------------------------

FIXTURE_APP_DIR = Path(__file__).resolve().parent / "fixture_app"


class _FixtureHandler(SimpleHTTPRequestHandler):
    """Static file server for tests/fixture_app + the plan_E2E §2.2 API routes."""

    def log_message(self, *args, **kwargs):
        """Silence per-request stderr logging (keeps test output clean)."""

    def _send_json(self, payload, status=200, extra_headers=None):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802  stdlib override, PERMANENT(interface)
        if self.path == "/api/json":
            self._send_json({"ok": True, "value": 42, "source": "fixture"})
            return
        if self.path == "/api/set-cookie":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Set-Cookie", "fixture_cookie=server-set; Path=/")
            self.end_headers()
            self.wfile.write(b"cookie set")
            return
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/api/json")
            self.end_headers()
            return
        super().do_GET()

    def do_POST(self):  # noqa: N802  stdlib override, PERMANENT(interface)
        if self.path == "/api/echo":
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
            reflected = {key.lower(): value for key, value in self.headers.items()}
            self._send_json({"body": raw, "headers": reflected})
            return
        self.send_response(404)
        self.end_headers()


@pytest.fixture(scope="session")
def fixture_app_server():
    """Yield the base_url of a session-scoped HTTP server for the fixture app.

    Binds an ephemeral 127.0.0.1 port, serves ``tests/fixture_app`` plus the
    §2.2 JSON/cookie/redirect routes on a daemon thread, and shuts the server
    down on teardown. Hermetic: importing it costs nothing until a test requests
    it, and it never touches the network or a fixed port.
    """
    handler = functools.partial(_FixtureHandler, directory=str(FIXTURE_APP_DIR))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
