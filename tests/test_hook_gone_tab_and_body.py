"""T-9135: hooks follow the live tab (E9) and ask for a body only when there is one (EB).

**E9.** ``dynamic_hook_system`` kept ONE stored tab per instance and re-armed
``create_dynamic_hook`` on it. After ``close_tab`` closed that tab the re-arm hit a
dead session: an ERROR (``Session with given id not found [-32001]``) while the tool
reported success. Hooks now follow the tab the browser manager tracks, the way
``NetworkInterceptor`` does (``arm_tab`` is a ``BrowserManager`` tab armer), each tab
carries its own ``Fetch.requestPaused`` handler, Chrome's ``Target.targetDestroyed``
(the T-9117 listener) forgets a closed tab, a gone target is a WARNING through
``tab_close.report_gone_aware``, and a hook that armed nowhere is not a success.

**EB.** ``Fetch.getResponseBody`` answers ``-32000 Can only get response body on
requests captured after headers received`` for a paused request with no response yet.

Every wait is bounded by ``asyncio.wait_for``.
"""

from __future__ import annotations

import asyncio
import logging

import nodriver as uc
import pytest
from nodriver.core.connection import ProtocolException

from fakes import FakeBrowser, FakeTab, call_tool
from stealth_chrome_devtools_mcp.embedded import tool_runtime as rt
from stealth_chrome_devtools_mcp.embedded.dynamic_hook_system import DynamicHookSystem
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

INSTANCE_ID = "i1"
FIRST = "T-first"
SECOND = "T-second"
BOUND = 2.0
CONTINUE = "def process_request(request):\n    return HookAction(action='continue')\n"
SESSION_GONE = ProtocolException(
    {"code": -32001, "message": "Session with given id not found"}
)
NO_BODY = ProtocolException(
    {
        "code": -32000,
        "message": "Can only get response body on requests captured after "
        "headers received.",
    }
)


@pytest.fixture
def backend_log(caplog):
    caplog.set_level(logging.DEBUG, logger="stealth.backend")
    return caplog


def _levels(caplog, level):
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "stealth.backend" and r.levelno == level
    ]


class _Tab(FakeTab):
    """A page tab on ``browser``; ``die()`` is Chrome closing it."""

    def __init__(self, browser, target_id, error=SESSION_GONE):
        super().__init__(target_id=target_id)
        self.browser = self.parent = browser
        self._error = error
        self.dead = False

    def die(self):
        """Chrome closed the page: its commands fail, and it is no longer listed."""
        self.dead = True
        if self in self.browser.tabs:
            self.browser.tabs.remove(self)

    async def send(self, cdp_obj, *args, **kwargs):
        await super().send(cdp_obj, *args, **kwargs)
        if self.dead:
            raise self._error


def _destroyed(target_id):
    return uc.cdp.target.TargetDestroyed(target_id=uc.cdp.target.TargetID(target_id))


def _enables(tab):
    return tab.send_calls.count("enable")


async def _system(browser=None, *, hook=True):
    """A system whose first tab was armed at spawn."""
    browser = browser or FakeBrowser()
    system = DynamicHookSystem()
    system.add_instance(INSTANCE_ID)
    first = _Tab(browser, FIRST)
    browser.tabs.append(first)
    await system.setup_interception(first, INSTANCE_ID)  # spawn: no hooks yet
    if hook:
        await system.create_hook("h", {}, CONTINUE, instance_ids=[INSTANCE_ID])
    return system, browser, first


def _close(browser, tab):
    """``close_tab``: Chrome closes the page and reports the target gone."""
    tab.die()
    browser._deliver(_destroyed(str(tab.target.target_id)))


class TestACreatedHookFollowsTheLiveTab:
    async def test_a_hook_created_after_the_tab_closed_arms_the_survivor(
        self, backend_log
    ):
        """The measured case: ``close_tab`` closed the hooked tab, then
        ``create_dynamic_hook`` was called."""
        system, browser, first = await _system()
        second = _Tab(browser, SECOND)
        browser.tabs.append(second)
        _close(browser, first)
        # ``close_tab`` -> ``_repoint_after_close`` -> ``_arm_tracked_tab``
        await system.arm_tab(second, INSTANCE_ID)
        before = _enables(first)

        await asyncio.wait_for(
            system.create_hook("h2", {}, CONTINUE, instance_ids=[INSTANCE_ID]), BOUND
        )

        assert _enables(first) == before  # never touched the dead tab again
        assert _enables(second) == 2  # the survivor's arming and the new hook's
        assert _levels(backend_log, logging.ERROR) == []
        assert _levels(backend_log, logging.WARNING) == []

    async def test_the_survivor_gets_its_own_paused_handler(self):
        system, browser, first = await _system()
        second = _Tab(browser, SECOND)
        browser.tabs.append(second)

        await system.arm_tab(second, INSTANCE_ID)
        await system.arm_tab(second, INSTANCE_ID)  # a second arming stacks nothing

        paused = uc.cdp.fetch.RequestPaused
        assert [t for t, _ in second.handlers].count(paused) == 1
        assert [t for t, _ in first.handlers].count(paused) == 1
        assert len(browser.handlers) == 1  # ONE targetDestroyed listener

    async def test_every_armed_tab_is_re_armed_by_a_new_hook(self):
        system, browser, first = await _system()
        second = _Tab(browser, SECOND)
        browser.tabs.append(second)
        await system.arm_tab(second, INSTANCE_ID)

        await system.create_hook("h2", {}, CONTINUE, instance_ids=[INSTANCE_ID])

        assert _enables(first) == 2 and _enables(second) == 2

    async def test_the_armer_ignores_an_instance_it_never_registered(self):
        system = DynamicHookSystem()
        tab = _Tab(FakeBrowser(), FIRST)

        await system.arm_tab(tab, "unknown")

        assert tab.send_calls == [] and tab.handlers == []
        assert "unknown" not in system._armed_tabs

    async def test_setup_answers_whether_it_armed(self):
        system, browser, _first = await _system(hook=False)
        second = _Tab(browser, SECOND)

        assert await system.setup_interception(second, INSTANCE_ID) is False  # no hook
        await system.create_hook("h", {}, CONTINUE, instance_ids=[INSTANCE_ID])
        assert await system.setup_interception(second, INSTANCE_ID) is True

    async def test_the_destroyed_report_forgets_the_tab(self):
        system, browser, first = await _system()
        assert FIRST in system._armed_tabs[INSTANCE_ID]

        browser._deliver(_destroyed(FIRST))

        assert system._armed_tabs[INSTANCE_ID] == {}
        assert system._paused_handlers[INSTANCE_ID] == set()

    async def test_removing_the_instance_forgets_its_tabs(self):
        system, _browser, _first = await _system()

        system.remove_instance(INSTANCE_ID)

        assert INSTANCE_ID not in system._armed_tabs
        assert INSTANCE_ID not in system._paused_handlers


class TestTheBrowserManagerArmsTheHooks:
    def test_the_manager_the_tools_use_has_the_hook_armer(self):
        """``tool_runtime`` builds the one ``BrowserManager``; its tab armers are how
        a tracked tab is hooked at all."""
        from stealth_chrome_devtools_mcp.embedded import tool_runtime as rt

        assert rt.dynamic_hook_system.arm_tab in rt.browser_manager._tab_armers
        assert rt.network_interceptor.arm_tab in rt.browser_manager._tab_armers


class TestAGoneTabIsNotAnError:
    async def test_a_dead_tab_nobody_reported_is_a_warning_and_is_forgotten(
        self, backend_log
    ):
        """No ``targetDestroyed`` (discovery was off): the listing confirms it."""
        system, browser, first = await _system()
        first.die()

        with pytest.raises(ToolError):
            await asyncio.wait_for(
                system.create_hook("h2", {}, CONTINUE, instance_ids=[INSTANCE_ID]),
                BOUND,
            )

        assert _levels(backend_log, logging.ERROR) == []
        (warned,) = [m for m in _levels(backend_log, logging.WARNING) if "is gone" in m]
        assert "target T-first" in warned
        assert system._armed_tabs[INSTANCE_ID] == {}

    async def test_a_session_error_on_a_target_chrome_still_lists_stays_an_error(
        self, backend_log
    ):
        system, _browser, first = await _system()
        first.dead = True  # fails, but the browser still lists it

        with pytest.raises(ToolError):
            await system.create_hook("h2", {}, CONTINUE, instance_ids=[INSTANCE_ID])

        assert len(_levels(backend_log, logging.ERROR)) == 1
        assert FIRST in system._armed_tabs[INSTANCE_ID]

    async def test_any_other_failure_is_still_an_error(self, backend_log):
        system, browser, _first = await _system()
        broken = _Tab(browser, SECOND, error=RuntimeError("boom"))
        browser.tabs.append(broken)
        await system.arm_tab(broken, INSTANCE_ID)  # healthy so far
        broken.dead = True

        await system.create_hook("h2", {}, CONTINUE, instance_ids=[INSTANCE_ID])

        errors = _levels(backend_log, logging.ERROR)
        assert len(errors) == 1 and "boom" in errors[0]


class TestNothingArmedIsNotASuccess:
    async def test_the_hook_is_not_kept_when_no_tab_could_be_armed(self):
        system, browser, first = await _system(hook=False)
        _close(browser, first)

        with pytest.raises(ToolError, match="no live tab"):
            await system.create_hook("h", {}, CONTINUE, instance_ids=[INSTANCE_ID])

        assert system.hooks == {}
        assert system.instance_hooks[INSTANCE_ID] == []

    async def test_the_tool_raises_without_an_error_event(
        self, backend_log, patched_server, monkeypatch
    ):
        system, browser, first = await _system(hook=False)
        _close(browser, first)
        monkeypatch.setattr(rt.dynamic_hook_ai, "hook_system", system)
        server = patched_server()

        with pytest.raises(ToolError, match="no live tab"):
            await asyncio.wait_for(
                call_tool(
                    server,
                    "create_dynamic_hook",
                    name="h",
                    requirements={},
                    function_code=CONTINUE,
                    instance_ids=[INSTANCE_ID],
                ),
                BOUND,
            )

        assert _levels(backend_log, logging.ERROR) == []

    async def test_a_hook_for_an_instance_not_spawned_yet_is_kept(self):
        """Hooks may be created before the browser they are for; they arm at spawn."""
        system = DynamicHookSystem()

        hook_id = await system.create_hook("h", {}, CONTINUE, instance_ids=["later"])

        assert hook_id in system.hooks

    async def test_one_armed_tab_among_the_instances_is_a_success(self):
        system, browser, first = await _system(hook=False)
        other_browser = FakeBrowser()
        system.add_instance("i2")
        second = _Tab(other_browser, SECOND)
        other_browser.tabs.append(second)
        await system.setup_interception(second, "i2")
        _close(browser, first)

        hook_id = await system.create_hook("h", {}, CONTINUE)  # every instance

        assert hook_id in system.hooks
        assert _enables(second) == 1


def _paused_json(**response):
    return {
        "requestId": "r1",
        "request": {
            "url": "https://example.invalid/data",
            "method": "GET",
            "headers": {},
            "initialPriority": "High",
            "referrerPolicy": "strict-origin-when-cross-origin",
        },
        "frameId": "F1",
        "resourceType": "Fetch",
        **response,
    }


REQUEST_STAGE = _paused_json()
RESPONSE_STAGE = _paused_json(
    responseStatusCode=200,
    responseHeaders=[{"name": "content-type", "value": "text/plain"}],
)
ERROR_REASON_WITH_HEADERS = _paused_json(
    responseErrorReason="ConnectionFailed",
    responseHeaders=[{"name": "content-type", "value": "text/plain"}],
)
ERROR_REASON_ONLY = _paused_json(responseErrorReason="ConnectionFailed")


async def _run(event_json, *, stage="response", get_body=None):
    """Chrome's own ``requestPaused`` object, through the real handler."""
    system = DynamicHookSystem()
    system.add_instance(INSTANCE_ID)
    seen = []
    code = (
        "def process_request(request):\n"
        "    SEEN.append(request.get('response_body'))\n"
        "    return HookAction(action='continue')\n"
    )
    await system.create_hook(
        "h", {"stage": stage}, code.replace("SEEN", "seen_ref"), [INSTANCE_ID]
    )
    hook = next(iter(system.hooks.values()))
    hook._compiled_function.__globals__["seen_ref"] = seen
    tab = FakeTab(
        target_id=FIRST,
        cdp_responses={"get_response_body": get_body or (lambda _n: ("", False))},
    )
    event = uc.cdp.fetch.RequestPaused.from_json(event_json)
    await asyncio.wait_for(system._on_request_paused(tab, event, INSTANCE_ID), BOUND)
    return tab, seen


class TestAResponseBodyIsAskedForOnlyWhenThereIsOne:
    async def test_a_response_with_a_status_gets_its_body(self, backend_log):
        tab, seen = await _run(RESPONSE_STAGE, get_body=lambda _n: ("hello", False))

        assert "get_response_body" in tab.send_calls
        assert seen == ["hello"]
        assert _levels(backend_log, logging.ERROR) == []

    async def test_a_paused_error_reason_with_no_response_asks_for_no_body(
        self, backend_log
    ):
        """The measured case: Chrome would answer -32000 and we logged an ERROR."""

        def refuse(_name):
            raise NO_BODY

        tab, seen = await _run(ERROR_REASON_ONLY, get_body=refuse)

        assert "get_response_body" not in tab.send_calls
        assert seen == [None]  # the hook still ran, with no body
        assert _levels(backend_log, logging.ERROR) == []
        assert tab.send_calls[-1] == "continue_response"

    async def test_response_headers_make_it_a_response_even_with_an_error_reason(
        self, backend_log
    ):
        tab, seen = await _run(
            ERROR_REASON_WITH_HEADERS, get_body=lambda _n: ("hi", False)
        )

        assert "get_response_body" in tab.send_calls
        assert seen == ["hi"]

    async def test_a_request_stage_event_never_asks(self, backend_log):
        tab, seen = await _run(REQUEST_STAGE, stage="request")

        assert "get_response_body" not in tab.send_calls
        assert tab.send_calls[-1] == "continue_request"
        assert seen == [None]

    async def test_a_real_failure_to_get_a_body_is_still_an_error(self, backend_log):
        def refuse(_name):
            raise RuntimeError("body broke")

        await _run(RESPONSE_STAGE, get_body=refuse)

        assert any("body broke" in m for m in _levels(backend_log, logging.ERROR))


async def test_a_tab_with_no_browser_is_kept_and_the_missing_report_is_debug_logged(
    backend_log,
):
    """``_listen_for_destroyed`` has nothing to listen on for a tab with no browser
    connection: not a defect (DEBUG, not WARNING/ERROR), and the tab is still kept."""
    system = DynamicHookSystem()
    system.add_instance(INSTANCE_ID)
    tab = _Tab(None, FIRST)

    await asyncio.wait_for(system.setup_interception(tab, INSTANCE_ID), BOUND)

    assert system._armed_tabs[INSTANCE_ID] == {FIRST: tab}
    assert INSTANCE_ID not in system._destroyed_listeners
    assert any(
        "no browser connection" in m and FIRST in m
        for m in _levels(backend_log, logging.DEBUG)
    )
    assert _levels(backend_log, logging.WARNING) == []
    assert _levels(backend_log, logging.ERROR) == []
