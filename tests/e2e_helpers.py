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
import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest

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
    if _warmed_up or not _can_run:
        return
    _warmed_up = True
    spawn = get_fn("spawn_browser")
    close = get_fn("close_instance")
    try:
        result = await spawn(
            headless=True, user_data_dir="e2e-warmup", **sandbox_kwargs()
        )
        await close(instance_id=result["instance_id"])
    except Exception:
        pass  # warmup failure is non-fatal


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
