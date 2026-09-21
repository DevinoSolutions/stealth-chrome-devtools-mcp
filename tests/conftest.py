"""Shared fixtures for stealth-chrome-devtools-mcp test suite."""

import atexit
import json
import logging
import os
import shutil
import sqlite3
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

# Imported by bare name for the same reason ``fakes`` is, and only once the path
# above is in place. What must come before any product import is the INSTALL at
# the bottom of this block, not this import: ``settings`` is already imported at
# line 15 and that is fine, because nothing calls ``get_settings()`` before the
# install and ``_fence_root`` repairs the one binding an early import costs
# (pydantic copied ``env_file``'s VALUE into ``Settings.model_config`` when the
# class body ran). The claim here used to be "before any product import in this
# file", which was false and sounded load-bearing (F-903 review S5).
import operator_fence  # noqa: E402  PERMANENT(F-903: the fence must install before any product import, and the path above is what makes this importable at all)

# Redirect clone / large-response artifacts to a temp dir for the whole test
# session. The module-global ResponseHandler()/FileBasedElementCloner() create
# their output dir at import time, and various tools spill files there — none of
# it should touch the installed package or the real ~/.stealth-mcp. setdefault
# so an explicit env (e.g. CI) still wins.
os.environ.setdefault(
    "STEALTH_MCP_CLONE_OUTPUT_DIR",
    str(Path(tempfile.gettempdir()) / "stealth-mcp-test-clone-output"),
)
# The same redirect, for the OTHER root a test can write to: the browser-session
# root that holds the master profile and every clone. This closes the structural
# gap named in
# ``audit/stage2/finding_F841_resilience_test_deletes_real_master_profile.md``
# ("the whole e2e tier spawns against the operator's real
# STEALTH_MCP_BROWSER_SESSION_ROOT") and it has to be HERE, at conftest import
# time, rather than in a fixture. Two reasons, both measured:
#
# * ``get_settings()`` is ``@lru_cache``d. ``_reset_settings_cache`` below clears
#   it at each test's SETUP, so whatever ``os.environ`` says at that moment is
#   what the product reads for the rest of the test. A per-test fixture that
#   patches the env cannot win that race against an autouse fixture ordered
#   ahead of it — and the E2E modules' ``_warmup`` is exactly such a fixture: it
#   spawns a browser, and therefore resolves the root, BEFORE ``tmp_empty_root``
#   is set up. Measured: a six-browser fleet node declaring ``tmp_empty_root``
#   wrote six 108 MB named profiles into the developer's real
#   ``C:\stealth-mcp-browser-sessions\sessions``.
# * Only the session root needs setting. ``master_profile_dir`` /
#   ``clone_root_dir`` / ``master_snapshot_dir`` all derive from it when their
#   own vars are unset, which is the same single knob the release gate sets.
#
# It is FORCED, not ``setdefault``-ed, since F-903. ``setdefault`` was
# deliberate — it let the release gate's own ``runner.temp`` value win — but it
# cannot tell the gate redirecting the suite from the OPERATOR'S OWN root
# arriving in an inherited environment, and those are the same string-shaped
# thing: an agent shell that inherited ``STEALTH_MCP_BROWSER_SESSION_ROOT`` from
# the MCP client's config ran the whole E2E tier against the real root, and the
# residue is still on this machine (``e2e-warmup``, ``ci-warmup``,
# ``ci-cycle-0/1/2``, ``tree-kill-test``, ``integration-test-profile``,
# ``ci-basic-test`` sitting in ``C:\stealth-mcp-browser-sessions\sessions``
# beside the ``master`` profile a human is logged into and 87 real sessions).
# Forcing costs the gate nothing — its value is a throwaway temp dir and so is
# ours. The whole decision, and the three DERIVED env names that have to be
# cleared with it, is ``operator_fence._fence_session_root``.
#
# A FIXED path rather than a fresh temp dir per session, so the master profile
# is cloned once on this machine instead of once per run. Nothing here is the
# operator's root, which is the whole point.
#
# What a FIXED path costs, stated so no test assumes otherwise: this root is
# SHARED — across git worktrees, across concurrent pytest processes, and with
# any other agent on this machine running this suite. Three consequences:
#
# * a NAMED profile collision is handled by the product (a held ``fleet-type``
#   walks to ``fleet-type-2``), so a test must read the directory it got from
#   ``spawn_diagnostics["profile_selection"]["user_data_dir"]`` and never
#   assume the name it asked for;
# * ``master`` has NO reservation — ``resolve_profile_selection`` protects a
#   clone directory (``_protect_clone_dir``) but not master — so two processes,
#   or two concurrent unnamed spawns in one process, can both read it as free;
# * therefore **a disk assertion must be scoped to directories the test itself
#   was given.** A bare "what appeared in this root since we started" diff is
#   not a fact about the test that makes it; a sibling process creating one
#   directory mid-run would fail it. No path was found by which one process
#   deletes another's LIVE profile — the cap sweeps skip protected and in-use
#   directories — so the residual is noisy assertions, not lost work.
#
# The ``-test-`` infix in the directory name is LOAD-BEARING: the doc lane
# (``tests/test_doc_examples.py``) asserts that the substring
# ``stealth-mcp-browser-sessions`` never appears in CLI output, and this name
# avoids it only because of that infix. Renaming this without renaming that
# pin turns the doc lane red.
_SESSION_FENCE_ROOT = Path(tempfile.gettempdir()) / "stealth-mcp-test-browser-sessions"
os.environ.setdefault("STEALTH_MCP_NO_AUTO_RECOVERY", "1")
# Test runs must not ship their deliberately-injected failures to the real
# Sentry project: sentry_init() is on by default, LoggingIntegration forwards
# every ERROR-level log, and real backends spawned by integration tests inherit
# this env (singleton's child_env strips only NO_AUTO_RECOVERY and, since F-890,
# the FASTMCP_ family). One local
# 15-hour test campaign shipped ~50k noise events before this line existed.
# test_observability.py still exercises the default-on path — it deletes the
# var explicitly via monkeypatch.
os.environ.setdefault("STEALTH_MCP_NO_ERROR_REPORTING", "1")

# THE one install of the operator fence, covering BOTH real directories: the
# browser-session root set up above, and the state dir -- the one that owns a
# LIVE PROCESS. F-903: a hermetic node drove the proxy's heal path into
# ``ensure_server_running`` and cold-started a real backend (pid 55240, port
# 21770) into the operator's live ``~/.stealth-mcp``; and this finding's own
# census probe imported ``stealth_chrome_devtools_mcp.__main__`` and did it
# again. Until this line the state-dir fence was per-file: ~30 files each
# carried their own ``isolated_state`` copy and the files with none were safe
# only by which collaborator a node happened to mock.
#
# The mechanism has ONE home, ``tests/operator_fence.py`` -- the ten measured
# state-dir bindings, the session-root forcing, the shared filesystem tripwire
# and the kill guard, with the argument for each beside the code. It is
# installed HERE, at conftest import time, and that is load-bearing for both
# halves: an autouse FUNCTION-scoped fixture is ordered after a module-scoped
# one, and the E2E modules' ``_warmup`` both starts a backend and resolves the
# session root during module setup (``get_settings()`` is ``@lru_cache``d, so
# whatever the env says then is what the product reads). Import time is ahead of
# collection and of every fixture of every scope.
#
# The STATE root is per-PROCESS where the session root is a fixed shared path:
# there is nothing here worth sharing (the session root shares a 108 MB master
# profile; this is two small JSON files), and concurrent pytest processes
# sharing one ``server.json`` would fight over it exactly as two backends would.
_STATE_FENCE_ROOT = (
    Path(tempfile.gettempdir()) / "stealth-mcp-test-state" / f"pid-{os.getpid()}"
)
_FENCED_LIVE_BACKEND_PIDS = operator_fence.install(
    _STATE_FENCE_ROOT, session_root=_SESSION_FENCE_ROOT, env=os.environ
)
atexit.register(shutil.rmtree, _STATE_FENCE_ROOT, True)


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
def _in_memory_storage_hygiene():
    """No test may leave an entry in the process-global ``in_memory_storage``
    for the next one (F-899).

    ``embedded/in_memory_storage.py`` ends in a module-level
    ``in_memory_storage = InMemoryStorage()``: production gets one per backend
    PROCESS, a test run gets one for the whole session. Two production writers
    fill it — ``browser_reattach``'s adoption pass and ``BrowserManager``'s spawn
    — and both are reached by hermetic tests that drive the real code against a
    fake manager. Neither is coverable by ``patched_server``: those modules bind
    the singleton by value at import time, so a ``setattr`` on ``tool_runtime``
    never reaches them. The entry then shows up in the NEXT file that calls
    ``list_instances``, which merges the manager's instances with this store and
    reports the strays as ``source: "stored"`` rows.

    Measured on main ``f18ecc5``: ``test_browser_reattach.py`` left ``i-kept``
    and ``i-held`` behind, and two ``list_instances() == []`` assertions in
    ``test_tool_failure_visibility.py`` failed on them. The full lane was green
    only because ``test_mcp_protocol_surface.py`` sorts between the two and boots
    the real transport unpatched, so ``app_lifespan``'s shutdown ran
    ``clear_all()`` on the real singleton in passing — an accident, not a
    guarantee.

    RESTORE, not assert-and-fail, on ``_stealth_logger_hygiene``'s precedent
    above: the tests that write here are exercising production code that is
    RIGHT to write, and a store that is cleared on ``close_instance`` and again
    at lifespan shutdown has no product defect to report. A test that asserts
    its own write was removed still asserts it inside its own body, so nothing
    is hidden. ``tests/test_in_memory_storage_isolation.py`` is the
    order-independent pin that this fixture is still here and still works.

    The snapshot is two levels deep, which is every mutation the class's own
    METHODS make: ``store_instance``/``remove_instance`` write inside
    ``_data["instances"]``, ``set`` writes a top-level key, ``clear_all``
    replaces the whole dict. It is deliberately not a ``deepcopy``, and what
    that costs is one shape: ``get``/``get_instance`` hand back the LIVE nested
    object, so a caller mutating below level 2 in place is not restored. Today
    that is unreachable — measured over 222 nodes, every test starts with an
    EMPTY store, so there is never a nested object to mutate — and
    ``copy.deepcopy`` is the one-word answer if it stops being. Restoration goes
    through the public API only.

    The import is function-local, unlike every other import in this file,
    because it is the one that reaches ``embedded/`` — and that package's
    ``__init__`` runs a ``sys.path`` shim. A conftest that fired it at import
    time would put it in front of every run, including the ones that never touch
    the backend at all.
    """
    from stealth_chrome_devtools_mcp.embedded.in_memory_storage import (
        in_memory_storage,
    )

    before = {
        key: dict(value) if isinstance(value, dict) else value
        for key, value in in_memory_storage.list_instances().items()
    }
    yield
    in_memory_storage.clear_all()
    for key, value in before.items():
        in_memory_storage.set(key, value)


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    """Every test gets a fresh Settings read. ``get_settings()`` is process-cached
    (``@lru_cache``), so without this an env mutation via ``monkeypatch`` /
    ``patch.dict`` would be invisible to any migrated code that reads Settings."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _empty_sqlite(path: Path) -> None:
    """Write a valid, EMPTY SQLite database at *path* (F-910).

    A profile store a browser cannot open is worse than one that is absent:
    Chrome carries on with an in-memory jar and every persistence assertion
    made afterwards is about nothing. One table, committed, so the file has a
    real header rather than being zero-length.
    """
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE IF NOT EXISTS placeholder (id INTEGER)")
        connection.commit()
    finally:
        connection.close()


@pytest.fixture()
def tmp_session_root(tmp_path):
    """
    Create an isolated session root with master + snapshot + sessions/ dirs.
    Patches the env vars so all profile helpers resolve inside tmp_path.
    """
    master = tmp_path / "master" / "Default"
    master.mkdir(parents=True)
    # Minimal profile files Chrome needs. The three stores are REAL (empty)
    # SQLite databases and never byte placeholders (F-910): six of the modules
    # sharing this fixture spawn a real browser onto it, and Chrome >= 96
    # MIGRATES `Default/Cookies` into the Network subdirectory on startup —
    # so an unreadable store there is not inert. Measured while building F-910
    # with 18-byte stubs in place: Chrome keeps its cookie jar in MEMORY for
    # the life of the browser, `document.cookie` reads back perfectly, the
    # on-disk database is left at one empty page and nothing survives the
    # respawn. That is F-910's own symptom manufactured by the harness, and it
    # cost an hour of chasing the product before the fixture was suspected.
    (master / "Preferences").write_text("{}", encoding="utf-8")
    for store in ("Cookies", "Login Data", "Web Data"):
        _empty_sqlite(master / store)

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
