"""Shared fixtures for stealth-chrome-devtools-mcp test suite."""

import json
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from stealth_chrome_devtools_mcp.settings import get_settings

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
# Test runs must not ship their deliberately-injected failures to the real
# Sentry project: sentry_init() is on by default, LoggingIntegration forwards
# every ERROR-level log, and real backends spawned by integration tests inherit
# this env (singleton's child_env strips only NO_AUTO_RECOVERY). One local
# 15-hour test campaign shipped ~50k noise events before this line existed.
# test_observability.py still exercises the default-on path — it deletes the
# var explicitly via monkeypatch.
os.environ.setdefault("STEALTH_MCP_NO_ERROR_REPORTING", "1")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _stealth_logger_hygiene():
    """No test may leave ``stealth.*`` logger state behind for the next one.

    ``logging_setup.configure_logging`` deliberately sets ``propagate=False``
    and attaches a file handler on its ``stealth.<role>`` logger — correct in
    production, poison in a shared test process: any test that drives the real
    proxy/backend bootstrap in-process (e.g. the Ctrl-C shim test in
    ``test_clean_shutdown_noise``) silently starves every later
    ``caplog.at_level(..., logger="stealth.proxy")`` assertion, because caplog
    captures via propagation to the root logger. Four hermetic log-assertion
    tests failed lane-only (green in isolation) before this fixture existed.

    Snapshot propagate/handlers/level for every ``stealth``/``stealth.*``
    logger before the test; restore after. Handlers a test added are closed so
    Windows can delete the tmp log files they hold open.
    """

    def _stealth_loggers():
        return [
            obj
            for name, obj in logging.Logger.manager.loggerDict.items()
            if isinstance(obj, logging.Logger)
            and (name == "stealth" or name.startswith("stealth."))
        ]

    before = {
        lg.name: (lg.propagate, list(lg.handlers), lg.level)
        for lg in _stealth_loggers()
    }
    yield
    for lg in _stealth_loggers():
        propagate, handlers, level = before.get(lg.name, (True, [], logging.NOTSET))
        for handler in list(lg.handlers):
            if handler not in handlers:
                lg.removeHandler(handler)
                handler.close()
        for handler in handlers:
            if handler not in lg.handlers:
                lg.addHandler(handler)
        lg.propagate = propagate
        lg.setLevel(level)


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
    """Swap the tool singletons for fakes and hand back the ``server`` module.

    ``embedded/tool_runtime.py`` is THE one patchable home: a tool body resolves
    ``rt.<name>`` against that module at CALL time, from whichever file it lives
    in, so one ``setattr`` there reaches all 94 bodies — and ``server.py``'s own
    non-tool readers (``app_lifespan``, the four ``@mcp.resource`` handlers, the
    ``__main__`` block) too, because plan_SERVERSPLIT slice 12 re-pointed those to
    ``rt.<name>`` as well. ``server`` is still what is RETURNED, because tool
    lookup is still a ``server`` attribute read (``fakes.call_tool``,
    ``e2e_helpers.get_fn``); the binding loop is what keeps that true.

    Slices 0-11 also patched a second home — ``server.py``'s migration alias
    block, which bound the objects into ITS namespace at import time — under an
    ``if hasattr(server, name)`` guard, with an alias-identity pin to fail the
    moment the two could diverge. Slice 12 deleted the alias block, so both the
    guard and the pin are gone with it: there is exactly one home again, and a
    ``setattr`` that reached only one of two places is no longer possible.
    """
    from stealth_chrome_devtools_mcp.embedded import server, tool_runtime

    def _patch(**singletons):
        for name, obj in singletons.items():
            monkeypatch.setattr(tool_runtime, name, obj, raising=False)
        return server

    return _patch


# ---------------------------------------------------------------------------
# plan_E2E — self-contained fixture web app served over a local HTTP server.
# Session-scoped so the E2E integration suite (and the hermetic smoke test)
# share one ephemeral-port server. The serving MECHANISM lives once in
# ``release_gate_harness.serve_fixture_app`` (plan_RELEASE W1 "no second
# mechanism"): this session fixture just delegates to it. No external network;
# the port is ephemeral and threaded through base_url, so it never appears in
# fixture files.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def fixture_app_server():
    """Yield the base_url of a session-scoped HTTP server for the fixture app.

    Delegates to the one canonical :func:`release_gate_harness.serve_fixture_app`
    mechanism (imported lazily so a unit-only run never pays for it). Hermetic: it
    binds an ephemeral 127.0.0.1 port and never touches the network or a fixed
    port.
    """
    from release_gate_harness import serve_fixture_app

    with serve_fixture_app() as base_url:
        yield base_url


@pytest.fixture(scope="session")
def fixture_origin_pair():
    """Yield ``(origin_a, origin_b)`` for plan_RELEASE W7's cross-origin shapes.

    Delegates to the same one mechanism as ``fixture_app_server`` above — the
    pair form simply binds two independent ephemeral loopback ports and links
    each to the other before either serves. Session-scoped so the eight W7
    nodes share one pair.
    """
    from release_gate_harness import serve_fixture_origin_pair

    with serve_fixture_origin_pair() as origins:
        yield origins
