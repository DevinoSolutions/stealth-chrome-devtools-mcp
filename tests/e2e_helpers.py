"""Shared harness for the plan_E2E integration suite (real headless Chrome).

ONE home for the mechanism the three ``test_e2e_*.py`` modules reuse when they
drive a real browser through the MCP's own tools: the importlib load of
``embedded/server.py``, the FastMCP ``.fn`` unwrap, the Chrome-availability skip
guard, sandbox kwargs for root/container/CI, a once-per-session warmup, and a
few JS / action-log / cookie readers. Test LOGIC never lives here — only
reusable mechanism (this mirrors ``tests/fakes.py`` for the hermetic tier).

Conventions copied verbatim from ``tests/test_browser_integration.py`` so the
E2E files stay consistent with the existing integration suite.
"""

from __future__ import annotations

import asyncio
import collections
import ctypes
import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest

# THE one home for a singleton, for the E2E tier too (plan_SERVERSPLIT slice 12).
# ``server_mod`` below is a SECOND execution of embedded/server.py under the bare
# name ``server``; it is the right handle for a TOOL — ``get_fn`` reaches one by
# getattr on it, which the binding loop keeps true. It is the WRONG handle for a
# singleton: those were attributes of ``server`` only because of the migration
# alias block, and slice 12 deleted it. ``tool_runtime`` is a normal module,
# imported once, so this is the same object every body and every ``server.py``
# execution drive — exactly what an E2E assertion about live browsers wants.
# Re-exported (like ``get_fn`` and ``server_mod``) for the three E2E files that
# need it: test_browser_integration, test_e2e_interaction, test_stealth. ruff sees
# no use HERE, and per plan_SERVERSPLIT slices 10-11 that is not the prune
# authority — the last consumer, prod or test, is.
from stealth_chrome_devtools_mcp.embedded import (  # noqa: F401  PERMANENT(re-export consumed by the three E2E files named above)
    tool_runtime as runtime,
)

# ── Load embedded/server.py as a module (it uses bare internal imports). ──
_spec = importlib.util.spec_from_file_location(
    "server",
    Path(__file__).resolve().parent.parent
    / "src"
    / "stealth_chrome_devtools_mcp"
    / "embedded"
    / "server.py",
)
_server_mod = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("server", _server_mod)
try:
    _spec.loader.exec_module(_server_mod)
except Exception:
    _server_mod = None

server_mod = _server_mod


def unwrap(fn):
    """A FunctionTool wraps the original coroutine as ``.fn`` (no-op if raw)."""
    return getattr(fn, "fn", fn)


# ── Chrome-availability guard (identical policy to the integration suite). ──
_can_run = False
_needs_no_sandbox = False
try:
    from stealth_chrome_devtools_mcp.embedded.platform_utils import (
        check_browser_executable,
        is_running_as_root,
        is_running_in_container,
    )

    _can_run = _server_mod is not None and check_browser_executable() is not None
    _needs_no_sandbox = (
        is_running_as_root()
        or is_running_in_container()
        or os.environ.get("CI") == "true"
    )
except Exception:
    pass

CAN_RUN = _can_run


def integration_pytestmark():
    """Module-level ``pytestmark``: integration, plus skip when Chrome is absent."""
    if not _can_run:
        return [
            pytest.mark.integration,
            pytest.mark.skip("Chrome not available or server failed to load"),
        ]
    return pytest.mark.integration


def get_fn(name):
    """Return an unwrapped ``server`` tool coroutine by name (skips if missing)."""
    fn = getattr(_server_mod, name, None)
    if fn is None:
        pytest.skip(f"server.{name} not found")
    return unwrap(fn)


def sandbox_kwargs() -> dict:
    """``{'sandbox': False}`` under root/container/CI, else ``{}``."""
    return {"sandbox": False} if _needs_no_sandbox else {}


# ── Warmup: the first Chrome launch on CI is slow / flaky. Run once per session
# (the guard makes every later call a no-op), driven by a tiny autouse fixture
# each E2E module declares — keeps this file logic-only and dodges an unused
# fixture-import lint. ──
_warmed_up = False


async def warmup_once() -> None:
    global _warmed_up
    if not _can_run:
        return
    _install_capture_spies()
    if _warmed_up:
        return
    _warmed_up = True
    spawn = get_fn("spawn_browser")
    close = get_fn("close_instance")
    # Bounded retry with backoff: the cold launch does not merely run slow, it
    # intermittently FAILS outright ("Failed to connect to browser" on the CI
    # runners) — and a warmup that fails leaves the first real test paying the
    # cold cost it was meant to absorb. The backoff matters as much as the
    # retry: an immediate retry hits the same busy machine. Warmup asserts
    # nothing, so this costs only time, and only when the machine is struggling.
    for attempt in range(3):
        try:
            result = await spawn(
                headless=True, user_data_dir="e2e-warmup", **sandbox_kwargs()
            )
            await close(instance_id=result["instance_id"])
            return
        except Exception:  # warmup failure is non-fatal
            await asyncio.sleep(2.0 * (attempt + 1))


# ── F-936: what a capture miss looked like from inside. On the gate a spawned
# tab sometimes captures nothing while the page itself loads and answers, and
# the bare "was never captured" could not say which layer lost the request.
# Two spies record the path in order: nodriver parsing each Network event off
# the websocket, then the interceptor's own request handler. The report reads
# them beside the tracked tab's CDP state. It only observes: a spy passes the
# original's result and exception through unchanged. ──
_CAPTURE_WINDOW_S = 30.0
_network_events: collections.deque = collections.deque(maxlen=5000)
_on_request_calls: collections.deque = collections.deque(maxlen=5000)
_spies_installed = False


def _install_capture_spies() -> None:
    global _spies_installed
    if _spies_installed:
        return
    _spies_installed = True
    from nodriver.cdp import util as cdp_util

    parse = cdp_util.parse_json_event

    def spied_parse(message):
        method = message.get("method", "?") if isinstance(message, dict) else "?"
        parsed = False
        try:
            event = parse(message)
            parsed = True
            return event
        finally:
            if method.startswith("Network."):
                _network_events.append((time.monotonic(), method, parsed))

    cdp_util.parse_json_event = spied_parse

    interceptor = runtime.network_interceptor
    on_request = interceptor._on_request

    async def spied_on_request(event, instance_id):
        await on_request(event, instance_id)
        request = getattr(event, "request", None)
        stored = getattr(event, "request_id", None) in interceptor._requests
        _on_request_calls.append(
            (time.monotonic(), instance_id, getattr(request, "url", None), stored)
        )

    interceptor._on_request = spied_on_request


async def capture_miss_report(iid: str) -> str:
    """Where a request an E2E test expected was lost, as far as the spies saw.

    For an assertion message only, so it never raises: a report that failed
    says so instead of hiding the miss it was called to explain.
    """
    try:
        return await _capture_miss_report(iid)
    except Exception as error:  # noqa: BLE001  PERMANENT(a diagnostic must not mask the assertion it explains)
        return f"F-936 capture-miss report failed: {error!r}"


async def _capture_miss_report(iid: str) -> str:
    manager = runtime.browser_manager
    interceptor = runtime.network_interceptor
    since = time.monotonic() - _CAPTURE_WINDOW_S
    events = [(method, ok) for t, method, ok in _network_events if t >= since]
    sent = [ok for method, ok in events if method == "Network.requestWillBeSent"]
    calls = [(url, stored) for _, i, url, stored in _on_request_calls if i == iid]
    lines = [
        f"spies installed: {_spies_installed}",
        f"Network events parsed in the last {_CAPTURE_WINDOW_S:.0f}s (all "
        f"instances): {sum(ok for _, ok in events)} ok, "
        f"{sum(not ok for _, ok in events)} failed; requestWillBeSent "
        f"{sum(sent)} ok, {len(sent) - sum(sent)} failed",
        f"_on_request calls for {iid}: {len(calls)}, stored "
        f"{sum(stored for _, stored in calls)}; first urls "
        f"{[url for url, _ in calls[:5]]}",
        f"rows listed: {len(await interceptor.list_requests(iid))}",
        f"armed: {iid in interceptor._armed_with}, targets "
        f"{sorted(interceptor._armed_targets.get(iid, ()))}, filters "
        f"{interceptor._instance_filters.get(iid)}",
    ]
    tab = await manager.get_tab(iid)
    if tab is not None:
        handlers = {
            getattr(kind, "__name__", str(kind)): len(callbacks)
            for kind, callbacks in tab.handlers.items()
        }
        domains = [
            getattr(d, "__name__", str(d)).rsplit(".", 1)[-1]
            for d in tab.enabled_domains
        ]
        listener = getattr(tab, "_listener_task", None)
        if listener is None:
            listening = "none"
        elif not listener.done():
            listening = "running"
        elif listener.cancelled():
            listening = "cancelled"
        else:
            listening = f"ended: {listener.exception()!r}"
        socket = getattr(tab, "websocket", None)
        lines.append(
            f"tracked tab {id(tab):#x} target {manager._get_tab_target_id(tab)} "
            f"handlers {handlers} domains {domains} listener {listening} "
            f"websocket close_code {getattr(socket, 'close_code', '?')}"
        )
    browser = await manager.get_browser(iid)
    if browser is not None:
        lines.append(
            "browser targets: "
            f"{[(manager._get_tab_target_id(t), getattr(getattr(t, 'target', None), 'type_', '?')) for t in browser.targets]}"
        )
    return "\n  ".join(["F-936 capture-miss report:", *lines])


async def navigate_and_settle(iid: str, url: str, timeout: float = 10.0):
    """Navigate, then block until the DOM is queryable — returns the nav result.

    After navigation, nodriver's cached document node is transiently stale, so the
    FIRST DOM-node-path tool call (``tab.select``/``select_all``) can fail on slow
    CI: ``click_element`` raises ``ProtocolException`` (-32000, "Could not find
    node with given id") and ``query_elements`` swallows the same exception into an
    empty list (the finding-#8 class). One successful ``body`` select refreshes the
    cached document, making subsequent node-path calls stable — so we settle it
    ONCE here per navigation (a workaround pending the src fix). ``query_elements``
    is the safe probe PRECISELY because it swallows the exception (returns [] rather
    than raising), so the poll can retry until the document is fresh. The real
    navigate result is returned unchanged so callers can still assert on it.
    """
    navigate = get_fn("navigate")
    query_elements = get_fn("query_elements")
    result = await navigate(instance_id=iid, url=url)
    deadline = time.monotonic() + timeout
    body = await query_elements(instance_id=iid, selector="body")
    while not (isinstance(body, list) and body) and time.monotonic() < deadline:
        await asyncio.sleep(0.25)
        body = await query_elements(instance_id=iid, selector="body")
    return result


# ── The fixture server's own ledger: the oracle that does NOT go through the
# browser. Off-thread so a blocking request never stalls the loop driving Chrome.
# ONE implementation, because "what did the browser actually ask the server for"
# is one question however many E2E modules ask it. ──
FIXTURE_HTTP_TIMEOUT = 10


async def fixture_get(url: str):
    """Plain HTTP straight from this process to the fixture origin."""
    import requests

    return await asyncio.to_thread(requests.get, url, timeout=FIXTURE_HTTP_TIMEOUT)


async def reset_fixture_ledger(origin: str) -> None:
    await fixture_get(f"{origin}/e2e/reset")


async def fixture_ledger(origin: str) -> dict:
    return (await fixture_get(f"{origin}/e2e/ledger")).json()


# ── Small readers shared across E2E modules. ──
async def eval_js(iid: str, expression: str) -> Any:
    """Evaluate a non-blocking JS expression via ``execute_script``; return result.

    Asserts the tool reported success, so a page/JS error surfaces immediately
    rather than as a confusing downstream ``None``.
    """
    execute = get_fn("execute_script")
    r = await execute(instance_id=iid, script=expression)
    assert isinstance(r, dict) and r.get("success") is True, r
    return r.get("result")


async def read_actions(iid: str) -> list[str]:
    """Return the in-page action log ``window.__actions`` as a Python list."""
    raw = await eval_js(iid, "JSON.stringify(window.__actions)")
    return json.loads(raw) if raw else []


def instance_entry(listing: list[dict], instance_id: str) -> dict:
    """THE one ``list_instances`` row lookup for this tier.

    Unpacking a one-element list rather than ``next(...)`` on purpose: two rows
    for one instance is a defect in its own right, and this fails on it instead
    of silently reporting the first. It was written out by hand in three places
    before it lived here.
    """
    [entry] = [row for row in listing if row["instance_id"] == instance_id]
    return entry


async def await_visible_window(root_pid: int, timeout: float = 15.0) -> int | None:
    """First pid in ``root_pid``'s process tree owning a visible, non-zero-area
    top-level window — or ``None`` at the deadline (F-808's integration twin).

    Only meaningful when the CALLER shares a window station with the spawned
    Chrome, i.e. the in-process integration lane: ``EnumWindows`` enumerates the
    desktop of the calling process, so it can never see a detached backend's.

    Bounded poll rather than sleep-then-assert, and BOTH the process tree and the
    window set are re-snapshotted every iteration: Chrome's renderer children and
    its first painted window both appear after the launch call returns.
    """
    import psutil

    deadline = time.monotonic() + timeout
    while True:
        try:
            children = psutil.Process(root_pid).children(recursive=True)
        except psutil.Error:
            children = []
        tree = {root_pid} | {p.pid for p in children}
        owners = tree & visible_window_pids()
        if owners:
            return next(iter(owners))
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.25)


class _RECT(ctypes.Structure):
    _fields_ = (
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    )


def visible_window_pids() -> set[int]:
    """PIDs owning a visible, non-zero-area top-level window. Win32 only.

    Empty set on every other platform, so a caller can branch on emptiness only
    when it has already established it is on Windows.

    This is TEST-side mechanism. The production question — "can a window launched
    by THIS process be seen" — is owned by ``embedded/display_context.py`` via the
    TS session id; a second Win32 probe in the package would be a second way. What
    this adds is the complementary observation display_context deliberately cannot
    make: whether a window actually MATERIALISED for someone else's process.

    Zero-area windows are rejected because Chrome always owns invisible helper
    windows (``Chrome_MessageWindow``), which pass ``IsWindowVisible`` and would
    make the assertion true even for a headless launch.
    """
    if sys.platform != "win32":
        return set()

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    # Declare every signature: an undeclared HWND parameter defaults to c_int and
    # TRUNCATES on 64-bit Windows, so the probe would silently find nothing.
    user32.IsWindowVisible.argtypes = (ctypes.c_void_p,)
    user32.IsWindowVisible.restype = ctypes.c_bool
    user32.GetWindowRect.argtypes = (ctypes.c_void_p, ctypes.POINTER(_RECT))
    user32.GetWindowRect.restype = ctypes.c_bool
    user32.GetWindowThreadProcessId.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ulong),
    )
    user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
    enum_callback = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    user32.EnumWindows.argtypes = (enum_callback, ctypes.c_void_p)
    user32.EnumWindows.restype = ctypes.c_bool

    found: set[int] = set()

    def _collect(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True  # keep enumerating
        rect = _RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        if rect.right - rect.left <= 0 or rect.bottom - rect.top <= 0:
            return True
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        found.add(int(pid.value))
        return True

    user32.EnumWindows(enum_callback(_collect), None)
    return found


def browsers_on(profile) -> bool:
    """Is any Chromium process still running on *profile*?

    Deliberately NOT ``async``: a ``psutil`` walk has no yield point, and a
    coroutine here would promise the loop one it never gets.
    """
    import contextlib

    import psutil

    target = str(profile).lower()
    for proc in psutil.process_iter(["name", "cmdline"]):
        with contextlib.suppress(Exception):
            if "chrome" not in (proc.info["name"] or "").lower():
                continue
            if any(target in (arg or "").lower() for arg in proc.info["cmdline"] or ()):
                return True
    return False


async def released(profile, *, budget: float = 30.0) -> None:
    """Wait for a torn-down node's Chrome to actually EXIT before the next runs.

    ``close_instance`` offloads its teardown, so it returns while the process
    tree is still dying — and the files that use this deliberately leave a
    browser RUNNING mid-test, so without this barrier three real Chromes launch
    over the top of three dying ones. That is how a file passed node by node and
    failed as a FILE on a loaded machine: nodriver's connect deadline is a fixed
    ≈2.75 s and it loses that race, which surfaces as "Failed to connect to
    browser" and a retry onto a DIFFERENT directory — i.e. as a take-over that
    was never attempted. Bounded, and deliberately silent on expiry: a browser
    that outlives the budget is the next node's capacity problem to report, not
    a failure of the node that just passed.

    It lives here rather than in either file that needs it because "has this
    profile been let go" is one question however many E2E modules ask it, and a
    second copy is what would drift.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget
    while loop.time() < deadline and browsers_on(profile):
        await asyncio.sleep(0.2)


async def wait_for_js(
    iid: str,
    expression: str,
    expected: Any,
    timeout: float = 5.0,
    interval: float = 0.1,
) -> Any:
    """Poll a JS expression until it equals ``expected`` or the deadline passes.

    Bounded deadline + fixed interval (no sleep-then-assert), per plan §2.6. On
    timeout the last observed value is returned so the caller's assert shows the
    real mismatch.
    """
    deadline = time.monotonic() + timeout
    last = await eval_js(iid, expression)
    while last != expected and time.monotonic() < deadline:
        await asyncio.sleep(interval)
        last = await eval_js(iid, expression)
    return last
