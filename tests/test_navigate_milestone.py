"""F-881 — ``navigate(wait_until=...)`` answers AFTER the milestone it names.

Two Windows full gates failed the same way on unrelated PRs:
``navigate(url="data:text/html,<title>Alpha</title>…")`` returned ``title: ""``.
Measured (finding §2): the tool's ``load`` wait was ``tab.wait(LoadEventFired)``,
which nodriver 0.47 reads as a *duration* and skips (0.02 ms), and ``tab.get``
before it returned at commit — so ``document.title`` was read while the parser
was still running. A 0.5 s dead sleep inside ``tab.get`` (the ``Page`` domain was
never enabled, so its wait saw no event) is what made it pass everywhere else.

The fake here models what Chrome actually sends (``FakeTab``'s navigation
model, built from the finding's §2d): ``Page.navigate`` answers
``(frameId, loaderId, errorText)``, the new document's ``init`` /
``DOMContentLoaded`` / ``load`` lifecycle events land either side of that
response, and a ``title_at_load`` page reads ``""`` until its ``load`` — the
CI shape, reproduced without Chrome.

Drives the REAL ``BrowserManager.navigate`` with its instance bookkeeping
stubbed, exactly as the F-824 pins do, so the wait under test is the product's.
"""

from __future__ import annotations

import asyncio

import pytest
from nodriver import cdp

from fakes import FakeTab
from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

URL = "https://fake.test/target"


@pytest.fixture
def manager(monkeypatch):
    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(BrowserManager, "touch_instance", noop)
    monkeypatch.setattr(BrowserManager, "update_instance_state", noop)
    return BrowserManager()


def _with_tab(monkeypatch, tab) -> list[str]:
    """Bind *tab* as the navigation tab; the returned list records every
    stale-tab recovery (``_replace_main_tab`` reason) the manager asked for."""
    replacements: list[str] = []

    async def get_navigation_tab(self, instance_id):
        return tab

    async def replace_main_tab(self, instance_id, reason, close_existing=True):
        replacements.append(reason)
        return tab

    monkeypatch.setattr(BrowserManager, "get_navigation_tab", get_navigation_tab)
    monkeypatch.setattr(BrowserManager, "_replace_main_tab", replace_main_tab)
    return replacements


def _navigate_frames(tab: FakeTab) -> list[str]:
    return [
        f["params"]["url"] for f in tab.cdp_frames if f["method"] == "Page.navigate"
    ]


# ---------------------------------------------------------------------------
# The CI failure, without Chrome
# ---------------------------------------------------------------------------


async def test_navigate_reports_the_title_the_page_has_at_load(monkeypatch, manager):
    """THE pin: ``load`` lands after ``Page.navigate`` answered; the tool must
    answer after it, not at commit. RED at 3311be9: ``assert '' == 'Alpha'``."""
    tab = FakeTab(lifecycle="after", title_at_load="Alpha")
    _with_tab(monkeypatch, tab)

    result = await manager.navigate(instance_id="iid-1", url=URL, timeout=500)

    assert result["title"] == "Alpha"
    assert result["url"] == URL
    assert _navigate_frames(tab) == [URL]


async def test_a_load_that_preceded_the_navigate_response_still_counts(
    monkeypatch, manager
):
    """Measured: ``DOMContentLoaded`` arrived 0.3 ms BEFORE the response and
    ``load`` 0.3 ms after; either order is real. A wait armed only after the
    response would miss the first shape and spend the whole budget."""
    tab = FakeTab(lifecycle="before", title_at_load="Alpha")
    _with_tab(monkeypatch, tab)

    result = await manager.navigate(instance_id="iid-1", url=URL, timeout=300)

    assert result["title"] == "Alpha"


async def test_domcontentloaded_returns_on_that_event_without_waiting_for_load(
    monkeypatch, manager
):
    """A page whose subresources never finish still reaches DCL; the parser
    has set ``<title>`` by then. RED at 3311be9: ``assert '' == 'Alpha'``."""
    tab = FakeTab(
        lifecycle="after", last_milestone="DOMContentLoaded", title_at_dcl="Alpha"
    )
    _with_tab(monkeypatch, tab)

    result = await manager.navigate(
        instance_id="iid-1", url=URL, wait_until="domcontentloaded", timeout=300
    )

    assert result["title"] == "Alpha"


async def test_load_is_waited_for_and_a_page_that_never_loads_times_out(
    monkeypatch, manager
):
    """The same page under the default ``load``: it must NOT be reported as
    navigated. RED at 3311be9: ``DID NOT RAISE`` (success with ``title ""``).

    And it is NOT retried: Chrome accepted the navigation (``Page.navigate``
    answered), so the page is there and merely never reaches ``load`` — a
    replaced tab would discard it and spend a second full budget. One
    ``Page.navigate``, no ``_replace_main_tab``."""
    tab = FakeTab(
        lifecycle="after", last_milestone="DOMContentLoaded", title_at_dcl="Alpha"
    )
    replacements = _with_tab(monkeypatch, tab)

    with pytest.raises(ToolError, match="timed out"):
        await manager.navigate(instance_id="iid-1", url=URL, timeout=150)

    assert _navigate_frames(tab) == [URL]
    assert replacements == []


class _UnansweringTab(FakeTab):
    """A tab whose ``Page.navigate`` never answers — the hang-before-headers
    shape, where Chrome has not accepted anything and the tab may be stale."""

    async def send(self, cdp_obj, *args, **kwargs):
        if getattr(getattr(cdp_obj, "gi_code", None), "co_name", None) == "navigate":
            self.cdp_frames.append(next(cdp_obj))
            cdp_obj.close()
            await asyncio.get_running_loop().create_future()
        return await super().send(cdp_obj, *args, **kwargs)


async def test_a_navigation_chrome_never_accepted_keeps_its_one_recovery_retry(
    monkeypatch, manager
):
    """The other side of the line: no ``Page.navigate`` answer means the tab may
    be stale (F-824), so the one-shot recovery on a fresh tab stays — two
    attempts, then the pinned timeout."""
    tab = _UnansweringTab()
    replacements = _with_tab(monkeypatch, tab)

    with pytest.raises(ToolError, match="timed out"):
        await manager.navigate(instance_id="iid-1", url=URL, timeout=100)

    assert _navigate_frames(tab) == [URL, URL]
    assert len(replacements) == 1


async def test_the_milestone_is_keyed_on_the_responses_loader_id(monkeypatch, manager):
    """An older document's ``load`` (a page still finishing when the new
    navigation was sent) must not end the wait for the new one."""
    tab = FakeTab(lifecycle="never", stale_load_for="L-old")
    _with_tab(monkeypatch, tab)

    with pytest.raises(ToolError, match="timed out"):
        await manager.navigate(instance_id="iid-1", url=URL, timeout=150)


async def test_a_same_document_navigation_returns_at_the_response(monkeypatch, manager):
    """``Page.navigate`` answers ``loaderId: null`` for a fragment move and no
    lifecycle event ever fires (measured); there is nothing to wait for."""
    tab = FakeTab(url=URL, lifecycle="never", title_at_load="Alpha")
    _with_tab(monkeypatch, tab)

    result = await manager.navigate(instance_id="iid-1", url=URL + "#deep", timeout=300)

    assert result["url"] == URL + "#deep"
    assert result["title"] == "Alpha"


async def test_an_unknown_wait_until_raises_naming_the_accepted_values(
    monkeypatch, manager
):
    """It used to mean ``load`` silently — which then meant nothing at all.

    Checked BEFORE the retry loop: a typo costs no CDP send (no referrer header,
    no ``Page.navigate``) and no stale-tab recovery."""
    tab = FakeTab(lifecycle="before", title_at_load="Alpha")
    replacements = _with_tab(monkeypatch, tab)

    with pytest.raises(ToolError, match=r"load.*domcontentloaded.*networkidle"):
        await manager.navigate(
            instance_id="iid-1",
            url=URL,
            wait_until="commit",
            referrer="https://r.test/",
        )

    assert tab.send_calls == []
    assert tab.cdp_frames == []
    assert replacements == []


# ---------------------------------------------------------------------------
# Hygiene: the listener is transient and its removal survives nodriver
# ---------------------------------------------------------------------------


async def test_the_lifecycle_listener_is_removed_after_the_navigation(
    monkeypatch, manager
):
    tab = FakeTab(lifecycle="after", title_at_load="Alpha")
    _with_tab(monkeypatch, tab)

    await manager.navigate(instance_id="iid-1", url=URL, timeout=300)

    assert tab.handlers == []


async def test_a_concurrent_wait_deleting_the_listener_does_not_fail_the_navigation(
    monkeypatch, manager
):
    """nodriver's ``remove_handler`` is ``del self.handlers[evt]``; an
    overlapping ``Tab.wait()`` leaves the key gone and the next removal raises
    ``KeyError(<event class>)`` (F-824's race). It must not surface here."""
    tab = FakeTab(lifecycle="before", title_at_load="Alpha")
    _with_tab(monkeypatch, tab)

    def already_deleted(event_type, handler=None):
        raise KeyError(cdp.page.LifecycleEvent)

    tab.remove_handler = already_deleted

    result = await manager.navigate(instance_id="iid-1", url=URL, timeout=300)

    assert result["title"] == "Alpha"


async def test_networkidle_still_sleeps_f787s_fixed_window_after_commit(
    monkeypatch, manager
):
    """F-787 stays OPEN and unchanged in effect: ``networkidle`` is commit plus
    a fixed sleep, not a quiescence wait, so it still answers before ``load``.
    The sleep is observed, not endured."""
    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def observed_sleep(delay, *args, **kwargs):
        slept.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", observed_sleep)
    tab = FakeTab(lifecycle="after", last_milestone="init", title_at_load="Alpha")
    _with_tab(monkeypatch, tab)

    result = await manager.navigate(
        instance_id="iid-1", url=URL, wait_until="networkidle", timeout=5000
    )

    assert result["success"] is True
    assert result["title"] == ""  # F-787: answered before load
    assert 2.0 in slept
