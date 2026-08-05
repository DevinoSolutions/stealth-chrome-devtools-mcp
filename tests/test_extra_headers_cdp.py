"""Sentry STEALTH-…-1P and STEALTH-…-K — the two spawn-path defects.

**1P** — ``uc.cdp.network.set_extra_http_headers`` requires a
``uc.cdp.network.Headers``: nodriver calls ``headers.to_json()`` while building the
command frame, so a raw ``dict`` raises ``AttributeError``. Two sites passed raw
dicts (spawn-time ``extra_headers`` and ``navigate``'s referrer), and both crashed
in production. ``network_interceptor.modify_headers`` already had the right shape;
these tests pin all three onto it.

**K** — ``browser_manager.spawn_browser`` prefixed its terminal error with
"Failed to spawn browser:" and ``server.py``'s tool wrapper added the same prefix
again, so users saw it twice. The prefix now belongs to the tool wrapper alone.

A CDP command is a *generator*, so ``set_extra_http_headers(headers=<raw dict>)``
returns fine and only raises when advanced — inside ``tab.send``. ``FakeTab.send``
used to advance it inside a bare ``except Exception: pass``, which is exactly what
let this defect ship green; that swallow is now gone, so the harness itself is the
regression net and these tests need no private tab double.
"""

from __future__ import annotations

from typing import Any

import nodriver as uc
import pytest

from fakes import FakeTab
from stealth_chrome_devtools_mcp.embedded import spawn_exhaustion, window_sizing
from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager
from stealth_chrome_devtools_mcp.embedded.models import BrowserOptions
from stealth_chrome_devtools_mcp.embedded.process_cleanup import process_cleanup

SET_EXTRA = "Network.setExtraHTTPHeaders"

# ``navigate`` reads both back off the tab after a successful get().
NAV_EVALUATES = {"location.href": "https://fake.test/target", "document.title": "t"}


def one_frame(tab: FakeTab, method: str) -> dict[str, Any]:
    """The one recorded frame for ``method``; fails if it was never sent."""
    matches = [f for f in tab.cdp_frames if f.get("method") == method]
    assert len(matches) == 1, f"expected exactly one {method}, got {tab.cdp_frames}"
    return matches[0]


# ---------------------------------------------------------------------------
# The library contract — measured, not assumed
# ---------------------------------------------------------------------------


def test_headers_object_round_trips_through_the_command():
    payload = {"X-Stealth-A": "b", "X-Stealth-C": "d"}
    frame = next(
        uc.cdp.network.set_extra_http_headers(headers=uc.cdp.network.Headers(payload))
    )
    assert frame["method"] == SET_EXTRA
    assert frame["params"]["headers"] == payload


def test_a_raw_dict_is_accepted_at_the_call_and_raises_only_when_advanced():
    """The deferral is the whole reason this shipped: nothing fails where a
    reader is looking, so any double that swallows the advance hides the crash."""
    gen = uc.cdp.network.set_extra_http_headers(headers={"X-Stealth-A": "b"})
    with pytest.raises(AttributeError, match="to_json"):
        next(gen)


def test_headers_is_a_dict_subclass_so_wrapping_changes_nothing_else():
    """Wrapping is free at every other call site: it is still a dict."""
    assert issubclass(uc.cdp.network.Headers, dict)
    assert uc.cdp.network.Headers({"a": "b"}) == {"a": "b"}


# ---------------------------------------------------------------------------
# Site 1 — spawn-time extra_headers (_apply_post_launch)
# ---------------------------------------------------------------------------


@pytest.fixture
def post_launch_manager(monkeypatch):
    """A ``BrowserManager`` whose ``_apply_post_launch`` neighbours are stubbed,
    leaving the extra-headers ``tab.send`` as the only live behaviour."""
    monkeypatch.setattr(process_cleanup, "track_browser_process", lambda *a, **kw: None)

    async def no_measure(tab, options):
        return window_sizing.WindowSizeMetrics()

    monkeypatch.setattr(window_sizing, "apply_and_measure", no_measure)

    async def no_timezone(self, tab, timezone_id):
        return None

    monkeypatch.setattr(BrowserManager, "_apply_timezone_override", no_timezone)
    return BrowserManager()


class _StubBrowser:
    # Truthy so the desktop_launch pid shim (a delegated-launch path) is skipped.
    _process = object()


async def _run_post_launch(manager, tab, options):
    return await manager._apply_post_launch(
        browser=_StubBrowser(),
        tab=tab,
        options=options,
        instance_id="iid-1",
        actual_user_data_dir=None,
        uses_custom_data_dir=False,
    )


async def test_spawn_extra_headers_serialize_into_the_cdp_frame(post_launch_manager):
    tab = FakeTab()
    headers = {"X-Stealth-Extra": "spawn", "Accept-Language": "en-CA"}

    await _run_post_launch(
        post_launch_manager, tab, BrowserOptions(extra_headers=headers)
    )

    assert one_frame(tab, SET_EXTRA)["params"]["headers"] == headers


async def test_spawn_sends_no_header_command_when_extra_headers_is_empty(
    post_launch_manager,
):
    tab = FakeTab()
    await _run_post_launch(post_launch_manager, tab, BrowserOptions(extra_headers={}))
    assert [f["method"] for f in tab.cdp_frames] == []


# ---------------------------------------------------------------------------
# Site 2 — navigate's referrer
# ---------------------------------------------------------------------------


@pytest.fixture
def navigating_manager(monkeypatch):
    """A ``BrowserManager`` whose instance bookkeeping and post-nav waits are
    stubbed, leaving the referrer ``tab.send`` as the only live behaviour."""

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(BrowserManager, "touch_instance", noop)
    monkeypatch.setattr(BrowserManager, "update_instance_state", noop)
    monkeypatch.setattr(BrowserManager, "_wait_for_navigation_condition", noop)
    return BrowserManager()


def _with_tab(monkeypatch, manager, tab):
    async def get_navigation_tab(self, instance_id):
        return tab

    monkeypatch.setattr(BrowserManager, "get_navigation_tab", get_navigation_tab)
    return manager


async def test_navigate_referrer_serializes_into_the_cdp_frame(
    monkeypatch, navigating_manager
):
    tab = FakeTab(evaluate_map=NAV_EVALUATES)
    _with_tab(monkeypatch, navigating_manager, tab)

    result = await navigating_manager.navigate(
        instance_id="iid-1",
        url="https://fake.test/target",
        referrer="https://fake.test/origin",
    )

    assert result["success"] is True
    assert one_frame(tab, SET_EXTRA)["params"]["headers"] == {
        "Referer": "https://fake.test/origin"
    }


async def test_navigate_without_a_referrer_sends_no_header_command(
    monkeypatch, navigating_manager
):
    tab = FakeTab(evaluate_map=NAV_EVALUATES)
    _with_tab(monkeypatch, navigating_manager, tab)

    await navigating_manager.navigate(instance_id="iid-1", url="https://fake.test/t")

    assert [f["method"] for f in tab.cdp_frames] == []


# ---------------------------------------------------------------------------
# Site 3 — the in-repo precedent this converges on
# ---------------------------------------------------------------------------


async def test_modify_headers_already_wrapped_and_still_does():
    """``network_interceptor.modify_headers`` is where the correct shape lived;
    pinning it here keeps the three sites converged on one way."""
    from stealth_chrome_devtools_mcp.embedded.network_interceptor import (
        NetworkInterceptor,
    )

    tab = FakeTab()
    await NetworkInterceptor().modify_headers(tab, {"X-Stealth-Mod": "1"})

    assert one_frame(tab, SET_EXTRA)["params"]["headers"] == {"X-Stealth-Mod": "1"}


# ---------------------------------------------------------------------------
# STEALTH-…-K — exactly one "Failed to spawn browser:" in the user-visible text
# ---------------------------------------------------------------------------

INNER_FAILURE = "--- Failed to connect to browser ---"
PREFIX = "Failed to spawn browser:"


@pytest.fixture
def failing_spawn_server(patched_server, monkeypatch):
    """The real ``spawn_browser`` tool over the REAL ``BrowserManager``, failed at
    its launch phase.

    Both prefixing layers must be live for this to mean anything: a stub manager
    that merely raises would bypass the very line that used to add the second
    prefix, and the test would pass over the open defect.
    """

    async def failing_launch(self, options, browser_executable, launch_args):
        raise RuntimeError(INNER_FAILURE)

    monkeypatch.setattr(
        BrowserManager,
        "_resolve_launch_args",
        lambda self, options, proxy, platform_info: ([], "/fake/chrome", []),
    )
    monkeypatch.setattr(BrowserManager, "_launch_browser", failing_launch)
    # Keep the F-811 hint out of the assertion AND off the real process table.
    monkeypatch.setattr(spawn_exhaustion, "exhaustion_hint", lambda path: None)

    class _CloneStorage:
        async def resolve_profile_selection(self, user_data_dir):
            return {"user_data_dir": None, "profile_role": "none"}

        async def _fallback_profile_selection(self, selection, attempt):
            return None  # no retry: the first failure is the terminal one

    return patched_server(
        browser_manager=BrowserManager(), clone_storage=_CloneStorage()
    )


async def test_the_user_visible_spawn_failure_carries_exactly_one_prefix(
    call_tool, failing_spawn_server
):
    with pytest.raises(Exception) as err:
        await call_tool(failing_spawn_server, "spawn_browser", headless=True)

    message = str(err.value)
    assert message.count(PREFIX) == 1, f"prefix repeated: {message!r}"
    assert message.startswith(PREFIX)
    assert INNER_FAILURE in message
