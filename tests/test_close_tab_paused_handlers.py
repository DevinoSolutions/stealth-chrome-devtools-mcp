"""T-8944: closing a tab ends the paused-request handlers waiting on THAT tab.

A hooked response that Chrome holds paused has its handler parked on
``Fetch.getResponseBody`` (``dynamic_hook_system._process_request_hooks``), which
has no timeout of its own: it ends only when Chrome tears the page's session down.
In a branch-wheel proof that was about 38 s after ``close_tab``, and the two
warnings (``No response body ...``, ``Not resuming ...``) came only then.
``close_tab`` ends that tab's handlers through the same ``Target.targetDestroyed``
listener that ends them for every other way a target goes (T-9117,
``tests/test_target_gone_cancel.py``): Chrome reports the close, and
``cancel_paused_for_target`` cancels one target's handlers the way
``remove_instance`` cancels an instance's. ``close_tab`` has no cancel of its own.

Every wait below is bounded by ``asyncio.wait_for``; a regression fails in two
seconds instead of hanging the lane.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import nodriver as uc
import pytest
from nodriver.core.connection import ProtocolException

from fakes import FakeAttachedTab, FakeBrowser, FakeTab, fake_instance, fake_target
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


async def _never_answered(_tab, _request, _event=None):
    """``getResponseBody`` for a response Chrome is still holding."""
    await asyncio.get_running_loop().create_future()


async def _system(monkeypatch):
    system = DynamicHookSystem()
    system.add_instance(INSTANCE_ID)
    system._instance_tabs[INSTANCE_ID] = None  # registered, as setup_interception does
    monkeypatch.setattr(system, "_process_request_hooks", _never_answered)
    return system


async def _settle():
    await asyncio.sleep(0)


class TestCancelPausedForTarget:
    async def test_it_ends_the_handlers_of_that_target_only(self, monkeypatch):
        system = await _system(monkeypatch)
        hooked, other = FakeTab(target_id=HOOKED), FakeTab(target_id=OTHER)
        system._spawn_paused(hooked, _paused_event(), INSTANCE_ID)
        system._spawn_paused(hooked, _paused_event(), INSTANCE_ID)
        system._spawn_paused(other, _paused_event(), INSTANCE_ID)
        await _settle()
        tasks = list(system._paused_tasks[INSTANCE_ID])
        assert system.paused_in_flight(INSTANCE_ID) == 3

        assert system.cancel_paused_for_target(INSTANCE_ID, HOOKED) == 2

        done, pending = await asyncio.wait(tasks, timeout=BOUND)
        assert len(done) == 2 and len(pending) == 1
        assert all(t.cancelled() for t in done)
        assert system.paused_in_flight(INSTANCE_ID) == 1
        system.remove_instance(INSTANCE_ID)
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), BOUND)

    async def test_another_instance_is_left_alone(self, monkeypatch):
        system = await _system(monkeypatch)
        system.add_instance("i2")
        system._instance_tabs["i2"] = None
        system._spawn_paused(FakeTab(target_id=HOOKED), _paused_event(), "i2")
        await _settle()
        (task,) = system._paused_tasks["i2"]

        assert system.cancel_paused_for_target(INSTANCE_ID, HOOKED) == 0

        await _settle()
        assert not task.done()
        system.remove_instance("i2")
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), BOUND)

    async def test_a_target_with_nothing_paused_is_a_no_op(self, monkeypatch):
        system = await _system(monkeypatch)

        assert system.cancel_paused_for_target(INSTANCE_ID, HOOKED) == 0
        assert system.cancel_paused_for_target("unknown", HOOKED) == 0

    async def test_a_finished_handler_is_not_counted_or_kept(self, monkeypatch):
        system = await _system(monkeypatch)

        async def answered(_tab, _request, _event=None):
            return None

        monkeypatch.setattr(system, "_process_request_hooks", answered)
        system._spawn_paused(FakeTab(target_id=HOOKED), _paused_event(), INSTANCE_ID)
        (task,) = system._paused_tasks[INSTANCE_ID]
        await asyncio.wait_for(asyncio.wait([task]), BOUND)
        await _settle()

        assert system.cancel_paused_for_target(INSTANCE_ID, HOOKED) == 0
        assert system._paused_target == {}


def _chrome_reports_the_close(browser):
    """Chrome answering ``Target.closeTarget`` and then, discovery being on
    (``tab_open.arm_popups``), sending ``Target.targetDestroyed`` for it."""

    def answer(_name):
        target_id = browser.cdp_frames[-1]["params"]["targetId"]
        browser._deliver(
            uc.cdp.target.TargetDestroyed(target_id=uc.cdp.target.TargetID(target_id))
        )

    browser._cdp_responses["close_target"] = answer


@pytest.fixture
async def manager_and_system(monkeypatch):
    hooked = FakeAttachedTab(fake_target(target_id=HOOKED))
    other = FakeAttachedTab(fake_target(target_id=OTHER))
    browser = FakeBrowser(tabs=[hooked, other])
    manager = BrowserManager()
    manager._instances[INSTANCE_ID] = {
        "browser": browser,
        "tab": other,
        "instance": fake_instance(INSTANCE_ID),
        "navigation_count": 0,
    }
    system = DynamicHookSystem()
    system.add_instance(INSTANCE_ID)
    await system.create_hook("h", {}, CONTINUE, instance_ids=[INSTANCE_ID])
    armed = FakeTab(target_id=HOOKED)
    armed.browser = browser
    await system.setup_interception(armed, INSTANCE_ID)
    monkeypatch.setattr(system, "_process_request_hooks", _never_answered)
    _chrome_reports_the_close(browser)
    return manager, browser, system


class TestCloseTab:
    async def test_it_ends_the_handlers_waiting_on_the_closed_tab(
        self, manager_and_system
    ):
        manager, _browser, system = manager_and_system
        system._spawn_paused(FakeTab(target_id=HOOKED), _paused_event(), INSTANCE_ID)
        system._spawn_paused(FakeTab(target_id=OTHER), _paused_event(), INSTANCE_ID)
        await _settle()
        tasks = list(system._paused_tasks[INSTANCE_ID])

        assert await manager.close_tab(INSTANCE_ID, HOOKED) is True

        done, pending = await asyncio.wait(tasks, timeout=BOUND)
        assert len(done) == 1 and len(pending) == 1
        assert system.paused_in_flight(INSTANCE_ID) == 1
        system.remove_instance(INSTANCE_ID)
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), BOUND)

    async def test_a_close_chrome_refused_leaves_the_handlers_waiting(
        self, manager_and_system
    ):
        manager, browser, system = manager_and_system
        system._spawn_paused(FakeTab(target_id=HOOKED), _paused_event(), INSTANCE_ID)
        await _settle()
        (task,) = system._paused_tasks[INSTANCE_ID]

        def refused(_name):
            raise ProtocolException({"code": -32602, "message": "Invalid parameters"})

        browser._cdp_responses["close_target"] = refused

        assert await manager.close_tab(INSTANCE_ID, HOOKED) is False

        await _settle()
        assert not task.done()
        system.remove_instance(INSTANCE_ID)
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), BOUND)

    async def test_a_tab_chrome_already_dropped_is_closed_and_already_ended(
        self, manager_and_system
    ):
        """Chrome reported the target gone when it dropped it, so the handler ended
        then; ``close_tab`` on that target answers ``True`` and does not need to."""
        manager, browser, system = manager_and_system
        system._spawn_paused(FakeTab(target_id=HOOKED), _paused_event(), INSTANCE_ID)
        await _settle()
        (task,) = system._paused_tasks[INSTANCE_ID]
        browser._deliver(
            uc.cdp.target.TargetDestroyed(target_id=uc.cdp.target.TargetID(HOOKED))
        )

        def dropped(_name):
            raise ProtocolException(
                {"code": -32602, "message": "No target with given id found"}
            )

        browser._cdp_responses["close_target"] = dropped

        assert await manager.close_tab(INSTANCE_ID, HOOKED) is True

        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), BOUND)
        assert task.cancelled()
