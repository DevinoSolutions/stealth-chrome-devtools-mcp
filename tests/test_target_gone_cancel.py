"""T-9117: a hooked target's paused handlers end whenever Chrome reports it gone.

T-8944 made ``close_tab`` cancel the handlers of the tab it closed, because a
handler parked on ``Fetch.getResponseBody`` has no timeout of its own and
otherwise waits for Chrome to tear the page's session down (about 38 s). That left
three ways a hooked target goes away that never reach ``close_tab``: a close from
another tab or ``window.close()``, ``BrowserManager._replace_main_tab`` closing the
previous tab (every 25th navigation and whenever the tracked tab is stale), and
Chrome itself. The one home is Chrome's own report: ``Target.targetDestroyed`` on
the BROWSER connection (the page's own connection never reports its end, see
``python_binding``), which ``tab_open.arm_popups`` switches on with
``Target.setDiscoverTargets``.

Every wait is bounded by ``asyncio.wait_for``; a regression fails in two seconds
instead of hanging the lane.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import nodriver as uc
import pytest
from nodriver.core.connection import Connection

from fakes import FakeAttachedTab, FakeBrowser, FakeTab, fake_instance, fake_target
from stealth_chrome_devtools_mcp.embedded import dynamic_hook_system as dhs
from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager
from stealth_chrome_devtools_mcp.embedded.dynamic_hook_system import DynamicHookSystem

INSTANCE_ID = "i1"
HOOKED = "T-hooked"
OTHER = "T-other"
BOUND = 2.0
CONTINUE = "def process_request(request):\n    return HookAction(action='continue')\n"


def _paused_event():
    return SimpleNamespace(
        request_id=uc.cdp.fetch.RequestId("r1"),
        request=SimpleNamespace(
            url="https://example.invalid/", method="GET", headers={}
        ),
        resource_type="Document",
        response_status_code=200,
        response_error_reason=None,
    )


def _destroyed(target_id: str):
    return uc.cdp.target.TargetDestroyed(target_id=uc.cdp.target.TargetID(target_id))


async def _never_answered(_tab, _request, _event=None):
    """``getResponseBody`` for a response Chrome is still holding."""
    await asyncio.get_running_loop().create_future()


async def _armed(monkeypatch, browser=None):
    """A system whose hooked tab went through the real ``setup_interception``."""
    system = DynamicHookSystem()
    system.add_instance(INSTANCE_ID)
    await system.create_hook("h", {}, CONTINUE, instance_ids=[INSTANCE_ID])
    tab = FakeTab(target_id=HOOKED)
    tab.browser = browser if browser is not None else FakeBrowser()
    await system.setup_interception(tab, INSTANCE_ID)
    monkeypatch.setattr(system, "_process_request_hooks", _never_answered)
    return system, tab


def _pause(system, tab):
    """Chrome pausing one request on *tab*, through the handler setup registered."""
    handler = next(h for t, h in tab.handlers if t is uc.cdp.fetch.RequestPaused)
    handler(_paused_event())


async def _drain(system):
    system.remove_instance(INSTANCE_ID)
    await asyncio.sleep(0)


class TestChromeReportsTheTargetGone:
    async def test_it_ends_the_handlers_of_that_target_only(self, monkeypatch):
        """Path 1: an out-of-band close (another tab's ``Target.closeTarget``,
        ``window.close()``) used to leave the handler parked for 110 s and more."""
        system, tab = await _armed(monkeypatch)
        other = FakeTab(target_id=OTHER)
        _pause(system, tab)
        _pause(system, tab)
        system._spawn_paused(other, _paused_event(), INSTANCE_ID)
        await asyncio.sleep(0)
        tasks = list(system._paused_tasks[INSTANCE_ID])
        assert system.paused_in_flight(INSTANCE_ID) == 3

        tab.browser._deliver(_destroyed(HOOKED))

        done, pending = await asyncio.wait(tasks, timeout=BOUND)
        assert len(done) == 2 and len(pending) == 1
        assert all(t.cancelled() for t in done)
        assert system.paused_in_flight(INSTANCE_ID) == 1
        await _drain(system)

    async def test_a_report_for_another_target_leaves_them_waiting(self, monkeypatch):
        system, tab = await _armed(monkeypatch)
        _pause(system, tab)
        await asyncio.sleep(0)
        (task,) = system._paused_tasks[INSTANCE_ID]

        tab.browser._deliver(_destroyed(OTHER))
        await asyncio.sleep(0)

        assert not task.done()
        await _drain(system)

    async def test_it_arrives_through_nodrivers_own_dispatch(self, monkeypatch):
        """The raw frame Chrome sends, parsed and dispatched by a real nodriver
        ``Connection``: a handler that took the wrong arguments would be called
        twice (``process_event`` retries a ``TypeError`` with one argument) or
        not at all."""
        browser = Connection()
        system, tab = await _armed(monkeypatch, browser)
        _pause(system, tab)
        await asyncio.sleep(0)
        (task,) = system._paused_tasks[INSTANCE_ID]
        calls = []
        real = system.cancel_paused_for_target
        monkeypatch.setattr(
            system,
            "cancel_paused_for_target",
            lambda *args: calls.append(args) or real(*args),
        )

        await browser.process_event(
            {"method": "Target.targetDestroyed", "params": {"targetId": HOOKED}}
        )

        assert calls == [(INSTANCE_ID, HOOKED)]
        await asyncio.wait_for(asyncio.wait([task]), BOUND)
        assert task.cancelled()
        await _drain(system)

    async def test_a_second_arming_does_not_stack_a_second_listener(self, monkeypatch):
        system, tab = await _armed(monkeypatch)
        await system.create_hook("h2", {}, CONTINUE, instance_ids=[INSTANCE_ID])

        assert len(tab.browser.handlers) == 1
        await _drain(system)

    async def test_the_listener_leaves_with_the_instance(self, monkeypatch):
        system, tab = await _armed(monkeypatch)
        assert len(tab.browser.handlers) == 1

        system.remove_instance(INSTANCE_ID)

        assert tab.browser.handlers == []

    async def test_a_report_for_a_removed_instance_is_forgotten(self, monkeypatch):
        system, tab = await _armed(monkeypatch)
        listener = tab.browser.handlers[0][1]
        system.remove_instance(INSTANCE_ID)

        listener(_destroyed(HOOKED))

        assert INSTANCE_ID not in system._closed_targets

    async def test_a_tab_without_a_browser_connection_arms_without_an_error(
        self, caplog
    ):
        """``setup_interception`` swallows and logs what it raises, so the
        absence of an ERROR is what shows the missing connection was handled."""
        caplog.set_level(logging.DEBUG, logger="stealth.backend")
        system = DynamicHookSystem()
        system.add_instance(INSTANCE_ID)
        await system.create_hook("h", {}, CONTINUE, instance_ids=[INSTANCE_ID])
        tab = FakeTab(target_id=HOOKED)

        await system.setup_interception(tab, INSTANCE_ID)

        assert len(tab.handlers) == 1
        assert INSTANCE_ID not in system._destroyed_listeners
        assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


class TestReplacingTheMainTab:
    async def test_closing_the_previous_hooked_tab_ends_its_handlers(self, monkeypatch):
        """Path 2: ``_replace_main_tab`` closes the previous tab with
        ``Tab.close()``, not ``close_tab``, and in the proof it still waited ~38 s.
        Chrome answers that close with ``Target.targetDestroyed`` (measured on
        Chrome 154 by ``tab_open``: exactly one per target)."""
        previous = FakeAttachedTab(fake_target(target_id=HOOKED))
        browser = FakeBrowser(tabs=[previous])
        system, hooked = await _armed(monkeypatch, browser)
        _pause(system, hooked)
        await asyncio.sleep(0)
        tasks = list(system._paused_tasks[INSTANCE_ID])

        async def close():
            browser._deliver(_destroyed(HOOKED))

        previous.close = close
        manager = BrowserManager()
        manager._instances[INSTANCE_ID] = {
            "browser": browser,
            "tab": previous,
            "instance": fake_instance(INSTANCE_ID),
            "navigation_count": 25,
        }

        new_tab = await manager._replace_main_tab(INSTANCE_ID, reason="recycle")

        assert new_tab is not previous
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), BOUND)
        assert all(t.cancelled() for t in tasks)
        await _drain(system)


class TestAnEventAfterTheTargetWentAway:
    async def test_a_paused_request_for_a_closed_target_is_dropped(self, monkeypatch):
        """Path 3: the cancel ran, then Chrome's next ``requestPaused`` for the
        same target arrived and spawned a fresh handler nothing would end."""
        system, tab = await _armed(monkeypatch)
        tab.browser._deliver(_destroyed(HOOKED))

        _pause(system, tab)
        await asyncio.sleep(0)

        assert system._paused_tasks.get(INSTANCE_ID, set()) == set()
        assert system.paused_in_flight(INSTANCE_ID) == 0
        await _drain(system)

    async def test_the_explicit_cancel_remembers_the_target_too(self, monkeypatch):
        system, tab = await _armed(monkeypatch)
        system.cancel_paused_for_target(INSTANCE_ID, HOOKED)

        _pause(system, tab)
        await asyncio.sleep(0)

        assert system._paused_tasks.get(INSTANCE_ID, set()) == set()
        await _drain(system)

    async def test_another_target_is_still_served(self, monkeypatch):
        system, tab = await _armed(monkeypatch)
        tab.browser._deliver(_destroyed(OTHER))

        _pause(system, tab)
        await asyncio.sleep(0)

        assert len(system._paused_tasks[INSTANCE_ID]) == 1
        await _drain(system)

    async def test_the_remembered_set_is_bounded_and_keeps_the_newest(
        self, monkeypatch
    ):
        system, tab = await _armed(monkeypatch)
        total = dhs.CLOSED_TARGETS_KEPT * 3

        for n in range(total):
            tab.browser._deliver(_destroyed(f"T-{n}"))

        kept = system._closed_targets[INSTANCE_ID]
        assert len(kept) == dhs.CLOSED_TARGETS_KEPT
        assert f"T-{total - 1}" in kept and "T-0" not in kept
        await _drain(system)

    async def test_it_is_cleared_with_the_instance(self, monkeypatch):
        system, tab = await _armed(monkeypatch)
        tab.browser._deliver(_destroyed(HOOKED))
        assert INSTANCE_ID in system._closed_targets

        system.remove_instance(INSTANCE_ID)

        assert INSTANCE_ID not in system._closed_targets


class TestTheTargetIsReadBeforeTheTaskExists:
    async def test_a_tab_with_no_target_leaves_no_task_behind(self, monkeypatch):
        """Path 4: the target id was read after the task was created and stored,
        so a tab whose ``target`` is ``None`` (nodriver clears it on a destroyed
        target) raised with a handler already running that nothing would end."""
        system, _tab = await _armed(monkeypatch)
        broken = FakeTab(target_id=HOOKED)
        broken.target = None
        before = len(asyncio.all_tasks())

        with pytest.raises(AttributeError):
            system._spawn_paused(broken, _paused_event(), INSTANCE_ID)
        await asyncio.sleep(0)

        assert system._paused_tasks.get(INSTANCE_ID, set()) == set()
        assert system._paused_target == {}
        assert len(asyncio.all_tasks()) == before
        await _drain(system)
