"""F-995 — ``set_cookie`` without a ``url`` names the page the tab shows NOW.

``set_cookie(instance_id, name, value)`` has no ``url`` and no ``domain`` to go
on, so it asks the tab where it is. It asked ``tab.url`` — nodriver's cached
``TargetInfo``, which is only as fresh as the last ``Target.targetInfoChanged``
the browser websocket delivered. Right after a ``navigate`` on a loaded macOS
runner (Chrome 155) that event had not landed, ``tab.url`` was still
``about:blank``, and Chrome refused the cookie with ``URL must have scheme http
or https [code: -32602]`` — on ``release-gate / integration (macOS/ARM64)``,
runs 38882922648 and 38890560391, in
``tests/test_e2e_cookie_listener.py``. Both failing nodes then left their
browsers open, which is what turned one defect into three red nodes (the fleet's
``list_instances`` timed out on those two browsers, and the re-attach node found
them in the pid record).

The answer is the one F-874 already built: ``tab_identity.refreshed`` asks Chrome
(``Target.getTargets``) instead of betting an event landed first. These pins say
the tool uses it, and only when it needs it.

Hermetic: ``tests/fakes.py`` doubles plus ``call_tool``. No Chrome. The
real-browser half is ``tests/test_e2e_cookie_listener.py``.
"""

import pytest

from fakes import (
    FakeBrowser,
    FakeBrowserManager,
    FakeTab,
    call_tool,
)
from stealth_chrome_devtools_mcp.embedded import tool_runtime
from stealth_chrome_devtools_mcp.embedded.network_interceptor import NetworkInterceptor
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

#: Where Chrome says the tab is, once it has been asked.
LIVE_URL = "http://127.0.0.1:8123/interactions.html"


class _LateTargetInfoBrowser(FakeBrowser):
    """A browser whose ``targetInfoChanged`` for *tab* has not been delivered.

    ``tab.url`` holds the page the tab was on when it last heard from Chrome;
    ``update_targets`` — a real ``Target.getTargets`` round trip — rewrites it in
    place, as nodriver's does.
    """

    def __init__(self, tab: FakeTab, live_url: str) -> None:
        super().__init__(tabs=[tab])
        self._tab = tab
        self._live_url = live_url

    async def update_targets(self) -> None:
        await super().update_targets()
        self._tab.url = self._live_url


def _seeded(patched_server, tab: FakeTab, browser: FakeBrowser | None):
    return patched_server(
        browser_manager=FakeBrowserManager(
            tabs={"i1": tab}, browsers={"i1": browser} if browser else {}
        ),
        network_interceptor=NetworkInterceptor(),
    )


async def test_the_cookie_lands_on_the_live_page_not_the_cached_one(patched_server):
    """THE shape: the cache says ``about:blank``, Chrome says the fixture page."""
    tab = FakeTab(url="about:blank")
    browser = _LateTargetInfoBrowser(tab, LIVE_URL)
    srv = _seeded(patched_server, tab, browser)

    assert await call_tool(srv, "set_cookie", instance_id="i1", name="n", value="v")

    assert tab.sent("Network.setCookie")[0]["params"]["url"] == LIVE_URL
    assert browser.update_targets_calls == 1


async def test_a_caller_given_url_pays_for_no_round_trip(patched_server):
    """An explicit ``url`` answers the question; asking Chrome anyway would add a
    CDP call to every ordinary use of the tool."""
    tab = FakeTab(url="about:blank")
    browser = _LateTargetInfoBrowser(tab, LIVE_URL)
    srv = _seeded(patched_server, tab, browser)

    await call_tool(
        srv,
        "set_cookie",
        instance_id="i1",
        name="n",
        value="v",
        url="https://elsewhere.test/",
    )

    assert (
        tab.sent("Network.setCookie")[0]["params"]["url"] == "https://elsewhere.test/"
    )
    assert browser.update_targets_calls == 0


async def test_a_domain_given_needs_no_round_trip_either(patched_server):
    tab = FakeTab(url="about:blank")
    browser = _LateTargetInfoBrowser(tab, LIVE_URL)
    srv = _seeded(patched_server, tab, browser)

    await call_tool(
        srv, "set_cookie", instance_id="i1", name="n", value="v", domain="x.test"
    )

    assert browser.update_targets_calls == 0


async def test_an_instance_with_no_browser_handle_still_answers_from_its_tab(
    patched_server,
):
    """``tab_identity.refreshed`` takes ``browser=None`` on purpose: the one thing
    worse than an unrefreshed answer is no answer."""
    tab = FakeTab(url="https://cached.test/page")
    srv = _seeded(patched_server, tab, None)

    await call_tool(srv, "set_cookie", instance_id="i1", name="n", value="v")

    assert (
        tab.sent("Network.setCookie")[0]["params"]["url"] == "https://cached.test/page"
    )


async def test_a_wedged_browser_costs_a_bounded_error_not_a_hang(
    patched_server, monkeypatch
):
    """The refresh is a CDP round trip, so it carries the same bound every other
    CDP call does — and a browser that never answers is reported, not papered
    over with the cached url this change exists to stop trusting."""
    monkeypatch.setattr(tool_runtime, "CDP_OPERATION_TIMEOUT", 0.05)
    tab = FakeTab(url="https://cached.test/page")
    srv = _seeded(
        patched_server, tab, FakeBrowser(tabs=[tab], update_targets_stalls=True)
    )

    with pytest.raises(ToolError, match="timed out"):
        await call_tool(srv, "set_cookie", instance_id="i1", name="n", value="v")

    # No cookie was written. (The tab was still held first: that is not the cookie.)
    assert tab.sent("Network.setCookie") == []


async def test_a_tab_with_no_url_anywhere_still_raises_the_argument_error(
    patched_server,
):
    """The argument-validation guard is unchanged: nothing to go on, say so."""
    tab = FakeTab(url="")
    srv = _seeded(patched_server, tab, FakeBrowser(tabs=[tab]))

    with pytest.raises(ToolError, match=r"At least one of"):
        await call_tool(srv, "set_cookie", instance_id="i1", name="n", value="v")
