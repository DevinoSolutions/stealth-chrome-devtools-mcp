"""F-833: the four tab-moving tools that never got ``navigate``'s guard.

F-802 made ``navigate`` truthful — a navigation Chrome could not perform commits
a ``chrome-error://`` page, every Python-side step around it still succeeds, and
the tool now raises instead of reporting that success. ``go_back``,
``go_forward``, ``reload_page`` and ``new_tab`` move a tab exactly the same way
and were left behind: a dead history entry, an offline reload and a new tab
whose initial URL will not load each land on the same error page and each
answered ``True`` over it. Same defect class, four more surfaces.

Three things are pinned here, and the third is the one that keeps the fix from
becoming its own defect:

*The lie is gone on all four.* :func:`test_landing_on_a_chrome_error_page_raises`
drives every one of them through its real tool body.

*The truth survives.* A 404, a redirect, ``data:`` and ``about:blank`` all LOAD;
a guard that raised on any of them would break more than it fixed, so the
loaded-page half is asserted for every tool in the same file as the fix.

*There is still exactly ONE error-page detector.*
:func:`test_all_five_speak_with_one_voice` compares the four new messages
against ``navigate``'s own, character for character past the name of the move —
a second hand-rolled ``startswith("chrome-error://")`` anywhere would show up
here as a message that drifted.

The read is hermetic because the *decision* is: what only a real browser can
prove — that Chrome really commits an error page — is F-802's evidence in
``tests/test_truthful_success_flags.py``, and this file inherits it by routing
every tool through the same guard.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from fakes import FakeBrowser, FakeBrowserManager, FakeTab
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

#: What Chrome commits a navigation failure to. Verbatim from F-802's pins.
ERROR_PAGE = "chrome-error://chromewebdata/"
REAL_PAGE = "https://fake.test/page"

#: The expression the product reads a landing through, in BOTH the pre-existing
#: ``BrowserManager.navigate`` path and the F-833 guard. A tab's cached
#: ``target.url`` is refreshed by ``update_targets()``, not by a history move.
HREF = "window.location.href"

#: The three bool-returning tools: same shape, same guard, different verb.
HISTORY_TOOLS = ("go_back", "go_forward", "reload_page")
MOVE_OF = {"go_back": "back", "go_forward": "forward", "reload_page": "reload"}
ALL_FOUR = (*HISTORY_TOOLS, "new_tab")


def landing_on(url: str) -> FakeTab:
    """A tab that ANSWERS *url* when asked where it is.

    Deliberately not ``FakeTab(url=...)``: the defect is that a move completes
    while the page under it is an error page, so the seam that has to be seeded
    is the one the product actually reads.
    """
    return FakeTab(url=REAL_PAGE, evaluate_map={HREF: url})


def server_landing_on(patched_server: Any, url: str) -> tuple[Any, FakeTab, Any]:
    """A patched ``server`` whose one instance's tab lands on *url*.

    The SAME tab is both the instance's main tab (what ``go_back`` moves) and the
    tab ``browser.get(..., new_tab=True)`` hands back (what ``new_tab`` opens), so
    one seeding drives all four tools.
    """
    tab = landing_on(url)
    browser = FakeBrowser(opened_tab=tab)
    srv = patched_server(
        browser_manager=FakeBrowserManager(tabs={"i1": tab}, browsers={"i1": browser})
    )
    return srv, tab, browser


# ═══════════════════════════════════════════════════════════════════════════
# The defect: a move onto an error page reported success
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("tool", ALL_FOUR)
async def test_landing_on_a_chrome_error_page_raises(tool, call_tool, patched_server):
    """The F-833 node. Each of the four used to answer success over this page."""
    srv, _tab, _browser = server_landing_on(patched_server, ERROR_PAGE)

    with pytest.raises(ToolError) as raised:
        await call_tool(srv, tool, instance_id="i1")

    message = str(raised.value)
    assert ERROR_PAGE in message, message
    assert "failed" in message, message


@pytest.mark.parametrize(
    ("tool", "kwargs", "names"),
    [
        ("go_back", {}, "the previous page"),
        ("go_forward", {}, "the next page"),
        ("reload_page", {}, "the reloaded page"),
        ("new_tab", {"url": "https://fake.test/dead"}, "https://fake.test/dead"),
    ],
)
async def test_the_error_names_which_move_failed(
    tool, kwargs, names, call_tool, patched_server
):
    """ "Which one failed" is half of what a caller needs to act (F-802's words).

    ``new_tab`` can name the URL it was asked for. A history move cannot — where
    it was going is only knowable after it got there — so it names the direction
    instead, which is the actionable half that exists.
    """
    srv, _tab, _browser = server_landing_on(patched_server, ERROR_PAGE)

    with pytest.raises(ToolError) as raised:
        await call_tool(srv, tool, instance_id="i1", **kwargs)

    assert names in str(raised.value), str(raised.value)


async def test_all_five_speak_with_one_voice(call_tool, patched_server):
    """One guard, five call sites — asserted, not asserted-in-a-comment.

    ``navigate``'s message is the reference. If any tool grew its own error-page
    check, its sentence would stop matching this one.
    """
    shared = (
        f"failed: Chrome loaded an error page ({ERROR_PAGE}). The host may not "
        "resolve, the connection may have been refused, or the TLS handshake "
        "may have failed."
    )

    navigate_srv = patched_server(
        browser_manager=FakeBrowserManager(
            navigate_result={"url": ERROR_PAGE, "title": "", "success": True}
        )
    )
    with pytest.raises(ToolError) as navigated:
        await call_tool(
            navigate_srv, "navigate", instance_id="i1", url="https://nope.invalid/"
        )
    assert shared in str(navigated.value), str(navigated.value)

    for tool in ALL_FOUR:
        srv, _tab, _browser = server_landing_on(patched_server, ERROR_PAGE)
        with pytest.raises(ToolError) as raised:
            await call_tool(srv, tool, instance_id="i1")
        assert shared in str(raised.value), f"{tool}: {raised.value}"


# ═══════════════════════════════════════════════════════════════════════════
# The truthful half — a guard that over-reached would be the worse defect
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("tool", HISTORY_TOOLS)
async def test_a_real_landing_still_reports_success(tool, call_tool, patched_server):
    srv, tab, _browser = server_landing_on(patched_server, REAL_PAGE)

    assert await call_tool(srv, tool, instance_id="i1") is True
    assert tab.move_calls == [MOVE_OF[tool]], tab.move_calls


@pytest.mark.parametrize(
    "landed",
    [
        "https://fake.test/missing",  # a 404 LOADED
        "https://fake.test/redirect/final",  # a redirect's final URL
        "data:text/html,<h1>x</h1>",
        "about:blank",  # NOT an error page
    ],
)
@pytest.mark.parametrize("tool", HISTORY_TOOLS)
async def test_a_loaded_page_is_not_a_failure(tool, landed, call_tool, patched_server):
    srv, _tab, _browser = server_landing_on(patched_server, landed)
    assert await call_tool(srv, tool, instance_id="i1") is True


async def test_new_tab_still_returns_its_tab_on_a_real_landing(
    call_tool, patched_server
):
    srv, tab, browser = server_landing_on(patched_server, REAL_PAGE)

    result = await call_tool(
        srv, "new_tab", instance_id="i1", url="https://fake.test/new"
    )

    assert result["tab_id"] == str(tab.target.target_id), result
    assert tab.closed is False
    assert browser.tabs == [tab], "a tab that landed must stay open"


# ═══════════════════════════════════════════════════════════════════════════
# No leak — the truthful error must not cost the caller a stranded tab
# ═══════════════════════════════════════════════════════════════════════════
async def test_new_tab_closes_the_tab_it_could_not_land(call_tool, patched_server):
    """A raise that leaves the half-open tab behind would only move the defect.

    ``new_tab`` is the one of the four that CREATES the tab, so it is the one
    that owns cleaning it up; the browser listing is asserted empty rather than
    just ``closed is True``, because an orphan is what the caller would pay for.
    """
    srv, tab, browser = server_landing_on(patched_server, ERROR_PAGE)

    with pytest.raises(ToolError):
        await call_tool(srv, "new_tab", instance_id="i1", url="https://fake.test/dead")

    assert tab.closed is True, "the tab new_tab opened was left open"
    assert browser.tabs == [], f"orphaned tab left behind: {browser.tabs}"


@pytest.mark.parametrize("tool", HISTORY_TOOLS)
async def test_a_failed_move_does_not_close_a_tab_it_did_not_open(
    tool, call_tool, patched_server
):
    """The counterpart. A history move onto an error page is still the caller's
    tab on the caller's page — closing it would destroy state they can recover
    from (going forward again, navigating somewhere else)."""
    srv, tab, browser = server_landing_on(patched_server, ERROR_PAGE)

    with pytest.raises(ToolError):
        await call_tool(srv, tool, instance_id="i1")

    assert tab.closed is False
    assert browser.tabs == []  # this browser never opened one


# ═══════════════════════════════════════════════════════════════════════════
# Bounds and the edge this fix deliberately does NOT change
# ═══════════════════════════════════════════════════════════════════════════
class _WedgedTab(FakeTab):
    """A tab whose page never answers — the dead-connection shape the whole
    ``_with_cdp_timeout`` discipline exists for. The landing read has to be
    bounded too, or the guard that made the tool truthful would make it hang."""

    async def evaluate(self, expression: str, *args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(3600)


async def test_a_landing_that_never_answers_fails_instead_of_hanging(
    call_tool, patched_server
):
    srv = patched_server(
        browser_manager=FakeBrowserManager(tabs={"i1": _WedgedTab(url=REAL_PAGE)}),
        CDP_OPERATION_TIMEOUT=0.05,
    )

    with pytest.raises(ToolError, match=r"Could not tell where the previous page"):
        await asyncio.wait_for(call_tool(srv, "go_back", instance_id="i1"), 10)


async def test_a_history_move_with_nowhere_to_go_still_reports_success(
    call_tool, patched_server
):
    """CHARACTERIZATION — the contract F-833 deliberately leaves alone.

    ``Tab.back()`` is a bare ``window.history.back()``: with no entry behind it,
    nothing happens, the URL does not change, and ``go_back`` answers ``True``
    for a move that never occurred. That is untruthful in a DIFFERENT way — a
    no-op reported as a move, not a failure reported as a success — and fixing
    it means reading ``Page.getNavigationHistory`` before the move, which is a
    separate change with its own contract. Pinned here so it is a known
    behaviour rather than an unexamined one, and so the fix that closes it has
    to update this node on purpose.
    """
    srv, tab, _browser = server_landing_on(patched_server, REAL_PAGE)

    assert await call_tool(srv, "go_back", instance_id="i1") is True
    assert tab.move_calls == ["back"]
