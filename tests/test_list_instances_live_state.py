"""F-874 — ``list_instances`` must describe the instance as it IS.

Measured on 2.1.6 over real stdio with ten headed browsers (Chrome 152,
Windows 11): every ``current_url``/``title`` ``list_instances`` reported was the
value the LAST ``navigate`` tool call wrote into ``BrowserInstance``, frozen
there. A page that navigated itself (a click on YouTube's search button), a tab
the caller ``switch_tab``'d away from, and a title the page set late (Amazon)
were all invisible to it, while ``get_active_tab`` on the same instance answered
correctly in the same second.

The pins below say what the one truthful contract is:

* an ``active`` entry carries the LIVE url and title of the instance's active
  tab — byte-identical to what ``get_active_tab`` answers for that instance;
* an entry that could not be read live says so in a ``partial``/``detail_error``
  record and names its fallback values ``last_navigated_*``, never
  ``current_url``;
* a ``stored`` entry — an instance that is no longer in memory at all, so there
  is nothing live to read — names its values ``last_navigated_*`` too;
* one WEDGED browser costs its OWN entry and nothing else: the listing still
  answers, bounded, with every other instance live.

Hermetic: ``tests/fakes.py`` doubles plus ``call_tool``. No Chrome. The
real-browser half of this finding is
``tests/test_browser_integration.py::TestListInstancesLiveState``.
"""

import asyncio

import pytest

from fakes import (
    FakeBrowser,
    FakeBrowserManager,
    FakeStorage,
    FakeTab,
    call_tool,
    fake_instance,
)
from stealth_chrome_devtools_mcp.embedded import tool_runtime


def _seeded(patched_server, *, stored=None, **manager_kwargs):
    """A patched server whose manager and storage are seeded together.

    Both singletons have to be faked for every case here: ``list_instances``
    merges the in-memory listing with ``in_memory_storage``'s, so leaving the
    real storage in place would let another test's residue join the answer.
    """
    return patched_server(
        browser_manager=FakeBrowserManager(**manager_kwargs),
        in_memory_storage=FakeStorage(instances=stored or {}),
    )


# ---------------------------------------------------------------------------
# The active entry is LIVE
# ---------------------------------------------------------------------------


async def test_active_entry_reports_the_live_tab_not_the_last_navigation(
    patched_server,
):
    """The instance navigated to ``/login`` once and the page moved on.

    The cached ``BrowserInstance`` still holds the login page; the tab holds the
    feed. ``current_url``/``title`` must be the tab's.
    """
    live = FakeTab(url="https://live.test/feed", target_id="T-live")
    live.target.title = "The Feed"
    srv = _seeded(
        patched_server,
        instances=[fake_instance("i1", "active", "https://live.test/login", "Sign in")],
        tabs={"i1": live},
        browsers={"i1": FakeBrowser(tabs=[live])},
    )

    [entry] = await call_tool(srv, "list_instances")

    assert entry["current_url"] == "https://live.test/feed"
    assert entry["title"] == "The Feed"
    assert entry["partial"] is False
    assert "last_navigated_url" not in entry


async def test_active_entry_matches_get_active_tab_for_the_same_instance(
    patched_server,
):
    """The two tools answer about the same thing, so they answer the same.

    This is the invariant the finding turns on: F-874 was noticed precisely
    because ``get_active_tab`` was right while ``list_instances`` was wrong in
    the same second. One home for the read means they cannot diverge again.
    """
    live = FakeTab(url="https://live.test/watch?v=abc", target_id="T-live")
    live.target.title = "lofi hip hop radio"
    srv = _seeded(
        patched_server,
        instances=[fake_instance("i1", "active", "https://live.test/", "Home")],
        tabs={"i1": live},
        browsers={"i1": FakeBrowser(tabs=[live])},
    )

    [entry] = await call_tool(srv, "list_instances")
    active = await call_tool(srv, "get_active_tab", instance_id="i1")

    assert (entry["current_url"], entry["title"]) == (active["url"], active["title"])


async def test_live_read_refreshes_the_targets_before_reading_them(patched_server):
    """The read is Chrome-authoritative, not a bet on an event having arrived.

    ``tab.target`` is only as fresh as the last ``Target.targetInfoChanged``
    nodriver happened to process; ``Browser.update_targets()`` is a
    ``Target.getTargets`` round trip that rewrites it from Chrome. A listing that
    skipped it would be right most of the time, which is the worst kind of wrong
    for a tool whose whole job is to say what is true right now.
    """
    live = FakeTab(url="https://live.test/feed", target_id="T-live")
    browser = FakeBrowser(tabs=[live])
    srv = _seeded(
        patched_server,
        instances=[fake_instance("i1", "active", "https://live.test/", "Home")],
        tabs={"i1": live},
        browsers={"i1": browser},
    )

    await call_tool(srv, "list_instances")

    assert browser.update_targets_calls == 1


# ---------------------------------------------------------------------------
# Degradation is visible and per-entry
# ---------------------------------------------------------------------------


async def test_wedged_browser_degrades_only_its_own_entry(patched_server, monkeypatch):
    """One browser stops answering; the other instance is still reported live.

    The stale-value alternative is what the product used to do, and it is worse
    than an error: a caller cannot tell a cached url from a current one.
    """
    monkeypatch.setattr(tool_runtime, "CDP_OPERATION_TIMEOUT", 0.05)
    wedged_tab = FakeTab(url="https://wedged.test/last", target_id="T-wedged")
    healthy_tab = FakeTab(url="https://healthy.test/now", target_id="T-healthy")
    healthy_tab.target.title = "Now"
    srv = _seeded(
        patched_server,
        instances=[
            fake_instance("i1", "active", "https://wedged.test/last", "Last Seen"),
            fake_instance("i2", "active", "https://healthy.test/then", "Then"),
        ],
        tabs={"i1": wedged_tab, "i2": healthy_tab},
        browsers={
            "i1": FakeBrowser(tabs=[wedged_tab], update_targets_stalls=True),
            "i2": FakeBrowser(tabs=[healthy_tab]),
        },
    )

    wedged, healthy = await call_tool(srv, "list_instances")

    assert wedged["instance_id"] == "i1"
    assert wedged["partial"] is True
    assert "timed out" in wedged["detail_error"].lower()
    assert wedged["last_navigated_url"] == "https://wedged.test/last"
    assert wedged["last_navigated_title"] == "Last Seen"
    # The degraded entry must not claim a current url it did not read.
    assert "current_url" not in wedged

    assert healthy["instance_id"] == "i2"
    assert healthy["partial"] is False
    assert healthy["current_url"] == "https://healthy.test/now"
    assert healthy["title"] == "Now"


async def test_wedged_entry_does_not_serialize_the_whole_listing(
    patched_server, monkeypatch
):
    """N wedged instances cost ONE timeout, not N.

    A serial listing over ten instances at the default CDP budget would leave the
    MCP client waiting minutes for an answer that is mostly cached values anyway.
    """
    monkeypatch.setattr(tool_runtime, "CDP_OPERATION_TIMEOUT", 0.3)
    instances, tabs, browsers = [], {}, {}
    for n in range(4):
        iid = f"i{n}"
        tab = FakeTab(url=f"https://wedged.test/{n}", target_id=f"T-{n}")
        instances.append(fake_instance(iid, "active", f"https://wedged.test/{n}", "x"))
        tabs[iid] = tab
        browsers[iid] = FakeBrowser(tabs=[tab], update_targets_stalls=True)
    srv = _seeded(patched_server, instances=instances, tabs=tabs, browsers=browsers)

    started = asyncio.get_running_loop().time()
    listed = await call_tool(srv, "list_instances")
    elapsed = asyncio.get_running_loop().time() - started

    assert [entry["partial"] for entry in listed] == [True] * 4
    # Four serial 0.3s timeouts would be >= 1.2s; concurrent ones are ~0.3s.
    assert elapsed < 0.9


async def test_missing_tab_degrades_rather_than_raising(patched_server):
    """An instance whose active tab is gone is still a row in the listing.

    ``list_instances`` is the tool a caller reaches for when something is wrong,
    so it must not be the tool that refuses to answer.
    """
    srv = _seeded(
        patched_server,
        instances=[fake_instance("i1", "active", "https://gone.test/", "Gone")],
        tabs={},
        browsers={},
    )

    [entry] = await call_tool(srv, "list_instances")

    assert entry["partial"] is True
    assert entry["last_navigated_url"] == "https://gone.test/"
    assert "current_url" not in entry


# ---------------------------------------------------------------------------
# The stored tier names its values honestly
# ---------------------------------------------------------------------------


async def test_stored_entry_names_its_values_last_navigated(patched_server):
    """A stored entry has no live browser at all, so it may not say
    ``current_url``. Naming is the whole remedy here: the value is fine, the
    claim was not."""
    srv = _seeded(
        patched_server,
        instances=[],
        stored={
            "i9": {
                "instance_id": "i9",
                "state": "closed",
                "last_navigated_url": "https://stored.test/page",
                "last_navigated_title": "Stored Page",
            }
        },
    )

    [entry] = await call_tool(srv, "list_instances")

    assert entry["source"] == "stored"
    assert entry["last_navigated_url"] == "https://stored.test/page"
    assert entry["last_navigated_title"] == "Stored Page"
    assert "current_url" not in entry
    assert "title" not in entry


# ---------------------------------------------------------------------------
# The cache itself: an empty title is an ANSWER, not a missing argument
# ---------------------------------------------------------------------------


class TestLastNavigatedCache:
    """``BrowserManager.update_instance_state`` is the only writer of the cached
    pair, and it guarded both fields on TRUTHINESS.

    That is how the measured Amazon entry came to hold ``title: null`` while the
    page was titled, and how a Wikipedia title survived a navigation to a
    ``data:`` URL: ``navigate`` reported ``title: ""`` (Amazon sets its title
    after load; a bare ``data:text/html`` document has none), ``if title:`` was
    false, and the previous value stayed. An empty title is what the page HAS —
    recording it is the point.
    """

    @pytest.fixture()
    def manager(self, monkeypatch):
        from unittest.mock import MagicMock

        from stealth_chrome_devtools_mcp.embedded import browser_manager as _bm

        monkeypatch.setattr(_bm, "process_cleanup", MagicMock())
        monkeypatch.setattr(_bm, "in_memory_storage", FakeStorage())
        monkeypatch.setattr(_bm, "dynamic_hook_system", MagicMock())
        return _bm.BrowserManager()

    def _seed(self, manager, url, title):
        from stealth_chrome_devtools_mcp.embedded.models import BrowserInstance

        instance = BrowserInstance(instance_id="i1")
        instance.last_navigated_url = url
        instance.last_navigated_title = title
        manager._instances = {
            "i1": {"browser": FakeBrowser(alive=True), "instance": instance}
        }
        return instance

    async def test_empty_title_replaces_the_previous_page_title(self, manager):
        instance = self._seed(manager, "https://old.test/", "Wikipedia")

        await manager.update_instance_state("i1", "data:text/html,<p>hi", "")

        assert instance.last_navigated_url == "data:text/html,<p>hi"
        assert instance.last_navigated_title == ""

    async def test_none_still_means_not_reported(self, manager):
        """``None`` is the one value that leaves a field alone — it is the
        default of both parameters, i.e. "this caller had nothing to say"."""
        instance = self._seed(manager, "https://old.test/", "Old")

        await manager.update_instance_state("i1", title="New")

        assert instance.last_navigated_url == "https://old.test/"
        assert instance.last_navigated_title == "New"
