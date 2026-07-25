"""RELEASE-FIX-F (F-775) — the SWALLOWED half of the F-771 tab family.

F-771 was loud: ``list_tabs`` raised a bare ``TypeError`` and the user knew. The
three sites pinned here share the exact same mechanism — nodriver 0.47's
``Browser.update_targets()`` re-appends a rediscovered target as a raw
``Connection`` (``core/browser.py:561-583``) and ``Browser.tabs`` hands it back
despite the ``List[Tab]`` annotation (``browser.py:137-142``) — but each one is
wrapped in a broad ``except Exception`` that turns the failure into a
plausible-looking fallback. The user sees **no error and wrong behaviour**.

* **F-775a** ``get_navigation_tab`` — finds the tracked tab correctly BY TARGET
  ID, then awaits it as a liveness check. The await raises for a ``Connection``,
  the handler concludes the tab is "missing or invalid" (it is not — it was just
  found) and calls ``_replace_main_tab(close_existing=False)``. Every navigation
  after any ``close_tab`` silently lands in a DIFFERENT tab and leaks the
  abandoned one.
* **F-775b** ``close_tab`` — ``Tab.close()`` does not exist on a ``Connection``;
  ``__getattr__`` delegates to the ``TargetInfo`` and the ``AttributeError`` is
  swallowed into ``return False`` for a tab that is perfectly closeable.
* **F-775c** ``switch_to_tab`` — ``bring_to_front()`` is ``Tab``-only, so
  switching reports failure for a target that activates fine.

The F-775a pins assert **tab identity and tab count**, never "no exception was
raised": a no-raise pin passes against today's silent-fallback behaviour and
therefore proves nothing.

See ``audit/stage2/plan_RELEASE_FIX_F.md``.
"""

from __future__ import annotations

import pytest

from fakes import (
    FakeAttachedTab,
    FakeBrowser,
    FakeDiscoveredTarget,
    fake_instance,
    fake_target,
)
from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager

INSTANCE_ID = "i1"

# The instance's tracked main tab: created in-process, so nodriver holds it as a
# real ``Tab`` and the product stores THAT object in ``_instances[id]["tab"]``.
TRACKED = {
    "target_id": "T-main",
    "url": "https://fake.test/index.html",
    "title": "fixture-index-page",
}
# A second, unrelated page the user also has open.
OTHER = {
    "target_id": "T-other",
    "url": "https://fake.test/interact.html",
    "title": "fixture-interact-page",
}


@pytest.fixture()
def manager_and_browser():
    """A real ``BrowserManager`` one ``close_tab`` after the rediscovery.

    ``browser.tabs`` holds the SAME two target ids the browser had before, but
    both entries are now raw ``Connection`` objects — that is exactly what
    ``update_targets()`` leaves behind once a target left nodriver's inventory
    and came back. The instance still tracks the original, live ``Tab``.
    """
    tracked = FakeAttachedTab(fake_target(**TRACKED))
    browser = FakeBrowser(
        tabs=[
            FakeDiscoveredTarget(fake_target(**TRACKED)),
            FakeDiscoveredTarget(fake_target(**OTHER)),
        ]
    )
    manager = BrowserManager()
    manager._instances[INSTANCE_ID] = {
        "browser": browser,
        "tab": tracked,
        "instance": fake_instance(INSTANCE_ID),
        "navigation_count": 0,
    }
    return manager, browser, tracked


# ---------------------------------------------------------------------------
# F-775a — get_navigation_tab: silent tab abandonment + tab leak
# ---------------------------------------------------------------------------


async def test_navigation_keeps_the_tracked_tab_identity(manager_and_browser):
    """THE pin: navigation after a rediscovery uses the SAME tracked tab id.

    Identity, not absence-of-exception. Today the ``await candidate_tab``
    liveness check raises ``TypeError: object FakeDiscoveredTarget can't be used
    in 'await' expression``, the broad handler logs a "tab health check failed"
    warning, and ``_replace_main_tab`` hands back a brand-new tab — so the
    returned object is neither the tracked tab nor the tracked target id.
    """
    manager, _browser, tracked = manager_and_browser

    navigation_tab = await manager.get_navigation_tab(INSTANCE_ID)

    assert navigation_tab is tracked
    assert manager._get_tab_target_id(navigation_tab) == TRACKED["target_id"]


async def test_navigation_after_rediscovery_leaks_no_tab(manager_and_browser):
    """The other half of F-775a: the tab COUNT must not grow.

    ``_replace_main_tab(..., close_existing=False)`` opens a fresh tab and never
    closes the abandoned one, so the leak compounds with every navigation.
    """
    manager, browser, tracked = manager_and_browser

    await manager.get_navigation_tab(INSTANCE_ID)

    assert browser.get_calls == []
    assert len(browser.tabs) == 2
    assert manager._instances[INSTANCE_ID]["tab"] is tracked


async def test_navigation_tab_pays_no_per_tab_lifecycle_wait(manager_and_browser):
    """``update_targets()`` refreshed the metadata the loop compares, and the tab
    was located BY TARGET ID — the await added no information and cost up to 0.5s
    (``Tab.wait()`` races the lifecycle event against ``asyncio.sleep(0.5)``)."""
    manager, browser, tracked = manager_and_browser

    await manager.get_navigation_tab(INSTANCE_ID)

    assert browser.update_targets_calls == 1
    assert tracked.awaited == 0


async def test_navigation_never_adopts_a_browser_tabs_entry_as_main_tab(
    manager_and_browser,
):
    """When the tracked target really IS gone, the replacement must be a tab
    opened in-process — never an element of ``browser.tabs``.

    ``get_navigation_tab`` used to adopt ``browser.tabs[0]`` as the instance's
    main tab. That is unsound for the same reason the await was: the adopted
    object may be a raw ``Connection``, and ``navigate()`` immediately calls the
    ``Tab``-only ``get()``/``evaluate()`` on whatever it is handed — an
    ``AttributeError`` that ``_is_recoverable_navigation_error`` does not match,
    so it would escape instead of self-healing. It also hijacked an unrelated
    user tab. ``_replace_main_tab`` is the ONE home for "give me a real main
    tab" (CLAUDE.md convention 4), so the adoption branch is deleted and the
    method falls through to it.

    Seeded with an *awaitable* survivor on purpose: with a non-awaitable one the
    old code reached ``_replace_main_tab`` via its exception handler anyway, and
    the pin would prove nothing.
    """
    manager, browser, _tracked = manager_and_browser
    survivor = FakeAttachedTab(fake_target(**OTHER))
    browser.tabs = [survivor]

    navigation_tab = await manager.get_navigation_tab(INSTANCE_ID)

    assert navigation_tab is not survivor
    assert manager._instances[INSTANCE_ID]["tab"] is not survivor
    assert browser.get_calls == [("about:blank", True)]
    assert manager._instances[INSTANCE_ID]["tab"] is navigation_tab


# ---------------------------------------------------------------------------
# F-775b — close_tab: a lying failure
# ---------------------------------------------------------------------------


async def test_close_tab_closes_a_rediscovered_target(manager_and_browser):
    """A rediscovered tab is perfectly closeable — address it by target id.

    Today ``await target_tab.close()`` resolves through
    ``Connection.__getattr__`` to the ``TargetInfo``, raising
    ``AttributeError: 'SimpleNamespace' object has no attribute 'close'``
    (``'TargetInfo' object has no attribute 'close'`` against real nodriver),
    which the broad ``except`` turns into ``return False``.
    """
    manager, browser, _tracked = manager_and_browser

    closed = await manager.close_tab(INSTANCE_ID, OTHER["target_id"])

    assert closed is True
    assert browser.connection.send_calls == ["close_target"]
    assert browser.connection.cdp_frames[0] == {
        "method": "Target.closeTarget",
        "params": {"targetId": OTHER["target_id"]},
    }


async def test_close_tab_reports_an_unknown_tab_id_unchanged(manager_and_browser):
    """Unchanged contract: no such tab → ``False``, and no CDP traffic."""
    manager, browser, _tracked = manager_and_browser

    assert await manager.close_tab(INSTANCE_ID, "T-nope") is False
    assert browser.connection.send_calls == []


# ---------------------------------------------------------------------------
# F-775c — switch_to_tab: a lying failure + a bad main-tab store
# ---------------------------------------------------------------------------


async def test_switch_to_tab_activates_a_rediscovered_target(manager_and_browser):
    """``bring_to_front()`` is ``Tab``-only; ``Target.activateTarget`` is not."""
    manager, browser, _tracked = manager_and_browser

    switched = await manager.switch_to_tab(INSTANCE_ID, OTHER["target_id"])

    assert switched is True
    assert browser.connection.send_calls == ["activate_target"]
    assert browser.connection.cdp_frames[0] == {
        "method": "Target.activateTarget",
        "params": {"targetId": OTHER["target_id"]},
    }


async def test_switch_to_tab_reports_an_unknown_tab_id_unchanged(manager_and_browser):
    """Unchanged contract: no such tab → ``False``, and no CDP traffic."""
    manager, browser, _tracked = manager_and_browser

    assert await manager.switch_to_tab(INSTANCE_ID, "T-nope") is False
    assert browser.connection.send_calls == []
