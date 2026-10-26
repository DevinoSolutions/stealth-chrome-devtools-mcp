"""Behavioral tests for the dynamic hook system (no browser).

Covers the parts that run entirely in-process: matching a request against a
hook's requirements, compiling + executing an AI-generated hook function
(including every failure fallback), and the hook/instance registry. The CDP
dispatch methods (_execute_hook_action, setup_interception) need a real tab and
are covered by the integration suite.

Safety focus: a malformed or malicious hook function must NEVER crash request
processing — every bad path must degrade to HookAction("continue").
"""

import asyncio
import gc
import logging
import weakref
from types import SimpleNamespace

import nodriver as uc
import pytest
from nodriver.core.connection import ProtocolException
from websockets.datastructures import Headers
from websockets.exceptions import (
    ConnectionClosed,
    InvalidMessage,
    InvalidStatus,
    WebSocketException,
)
from websockets.http11 import Response

from fakes import FakeTab, cdp_command_name
from stealth_chrome_devtools_mcp.embedded import dynamic_hook_system as dhs
from stealth_chrome_devtools_mcp.embedded.dynamic_hook_model import (
    DynamicHook,
    HookAction,
    RequestInfo,
)
from stealth_chrome_devtools_mcp.embedded.dynamic_hook_system import DynamicHookSystem

CONTINUE = "def process_request(request):\n    return HookAction(action='continue')\n"


def _req(
    url="https://example.com/api/data",
    method="GET",
    *,
    stage="request",
    resource_type=None,
    headers=None,
):
    return RequestInfo(
        request_id="req-1",
        instance_id="inst-1",
        url=url,
        method=method,
        headers=headers or {},
        resource_type=resource_type,
        stage=stage,
    )


def _hook(requirements, code=CONTINUE, name="h", priority=100):
    return DynamicHook("hid", name, requirements, code, priority)


class TestDataclasses:
    def test_request_info_to_dict_roundtrips_fields(self):
        info = _req(headers={"A": "b"}, method="POST")
        d = info.to_dict()
        assert d["url"] == "https://example.com/api/data"
        assert d["method"] == "POST"
        assert d["headers"] == {"A": "b"}
        assert d["stage"] == "request"

    def test_hook_action_defaults(self):
        a = HookAction(action="block")
        assert a.url is None and a.headers is None and a.status_code is None


class TestMatches:
    def test_no_requirements_matches_everything(self):
        assert _hook({}).matches(_req()) is True

    def test_url_pattern_hit_and_miss(self):
        assert _hook({"url_pattern": "*example.com*"}).matches(_req()) is True
        assert _hook({"url_pattern": "*other.com*"}).matches(_req()) is False

    def test_method_is_case_insensitive(self):
        assert _hook({"method": "get"}).matches(_req(method="GET")) is True
        assert _hook({"method": "POST"}).matches(_req(method="GET")) is False

    def test_resource_type_filter(self):
        assert (
            _hook({"resource_type": "XHR"}).matches(_req(resource_type="XHR")) is True
        )
        assert (
            _hook({"resource_type": "XHR"}).matches(_req(resource_type="Document"))
            is False
        )

    def test_stage_filter(self):
        assert _hook({"stage": "response"}).matches(_req(stage="response")) is True
        assert _hook({"stage": "response"}).matches(_req(stage="request")) is False

    def test_custom_condition_true_and_false(self):
        assert (
            _hook({"custom_condition": "len(request.url) > 5"}).matches(_req()) is True
        )
        assert (
            _hook({"custom_condition": "request.method == 'POST'"}).matches(_req())
            is False
        )

    def test_custom_condition_error_fails_closed(self):
        # A broken condition must not match (and must not raise).
        assert _hook({"custom_condition": "request.nope.nope"}).matches(_req()) is False

    def test_multiple_requirements_all_must_hold(self):
        h = _hook({"url_pattern": "*example.com*", "method": "GET"})
        assert h.matches(_req(method="GET")) is True
        assert h.matches(_req(method="POST")) is False


class TestProcess:
    def test_hookaction_return_is_passed_through(self):
        h = _hook(
            {},
            code="def process_request(request):\n    return HookAction(action='block')\n",
        )
        result = h.process(_req())
        assert result.action == "block"
        assert h.trigger_count == 1
        assert h.last_triggered is not None

    def test_dict_return_is_coerced(self):
        h = _hook(
            {},
            code="def process_request(request):\n    return {'action': 'redirect', 'url': 'https://x'}\n",
        )
        result = h.process(_req())
        assert result.action == "redirect" and result.url == "https://x"

    def test_invalid_return_type_becomes_continue(self):
        h = _hook({}, code="def process_request(request):\n    return 42\n")
        assert h.process(_req()).action == "continue"

    def test_function_exception_becomes_continue(self):
        h = _hook(
            {}, code="def process_request(request):\n    raise RuntimeError('boom')\n"
        )
        assert h.process(_req()).action == "continue"

    def test_uncompilable_code_falls_back_to_continue(self):
        h = _hook({}, code="this is not valid python !!!")
        assert h.process(_req()).action == "continue"

    def test_missing_entrypoint_falls_back_to_continue(self):
        h = _hook({}, code="x = 1\n")  # valid python, no process_request
        assert h.process(_req()).action == "continue"

    def test_function_receives_dict_not_object(self):
        # process() passes request.to_dict(); a hook using dict access must work.
        h = _hook(
            {},
            code=(
                "def process_request(request):\n"
                "    return HookAction(action='block' if request['method'] == 'GET' else 'continue')\n"
            ),
        )
        assert h.process(_req(method="GET")).action == "block"


class TestRegistry:
    async def test_create_hook_scoped_to_instances(self):
        sys = DynamicHookSystem()
        hid = await sys.create_hook(
            "h", {"url_pattern": "*"}, CONTINUE, instance_ids=["inst-1"]
        )
        assert hid in sys.hooks
        assert sys.instance_hooks["inst-1"] == [hid]

    async def test_create_hook_without_instances_applies_to_existing(self):
        sys = DynamicHookSystem()
        sys.add_instance("inst-1")
        hid = await sys.create_hook("h", {"url_pattern": "*"}, CONTINUE)
        assert hid in sys.instance_hooks["inst-1"]

    async def test_remove_hook_drops_hook_and_associations(self):
        sys = DynamicHookSystem()
        hid = await sys.create_hook(
            "h", {"url_pattern": "*"}, CONTINUE, instance_ids=["inst-1"]
        )
        assert await sys.remove_hook(hid) is True
        assert hid not in sys.hooks
        assert hid not in sys.instance_hooks["inst-1"]
        assert await sys.remove_hook(hid) is False  # already gone

    async def test_list_and_details(self):
        sys = DynamicHookSystem()
        hid = await sys.create_hook(
            "named", {"url_pattern": "*"}, CONTINUE, instance_ids=["inst-1"]
        )
        listing = sys.list_hooks()
        assert (
            listing and listing[0]["hook_id"] == hid and listing[0]["name"] == "named"
        )
        details = sys.get_hook_details(hid)
        assert details["function_code"] == CONTINUE
        assert sys.get_hook_details("nope") is None

    def test_add_and_remove_instance(self):
        sys = DynamicHookSystem()
        sys.add_instance("inst-1")
        assert sys.instance_hooks["inst-1"] == []
        sys.remove_instance("inst-1")
        assert "inst-1" not in sys.instance_hooks


class TestProcessRequestHooks:
    """Dispatch is first-match-by-priority (F-163): when several hooks match one
    request, only the highest-priority match (lowest priority number) runs; the
    lower-priority matches are shadowed and never fire. A WARNING names the winner
    and the shadowed hooks so the silent 'trigger_count stuck at 0' trap is
    visible. Reuses FakeTab (M6 canon) + the _req() helper -- no browser.
    """

    async def test_first_match_by_priority_wins_only_one_runs(self):
        sys = DynamicHookSystem()
        hi = await sys.create_hook(
            name="win-hi",
            requirements={"url_pattern": "*example.com*"},
            function_code=CONTINUE,
            instance_ids=["inst-1"],
            priority=10,
        )
        lo = await sys.create_hook(
            name="shadow-lo",
            requirements={"url_pattern": "*example.com*"},
            function_code=CONTINUE,
            instance_ids=["inst-1"],
            priority=20,
        )
        await sys._process_request_hooks(FakeTab(), _req(stage="request"))
        assert sys.hooks[hi].trigger_count == 1
        assert sys.hooks[lo].trigger_count == 0

    async def test_shadowed_match_emits_warning(self, monkeypatch):
        sys = DynamicHookSystem()
        await sys.create_hook(
            name="win-hi",
            requirements={"url_pattern": "*example.com*"},
            function_code=CONTINUE,
            instance_ids=["inst-1"],
            priority=10,
        )
        await sys.create_hook(
            name="shadow-lo",
            requirements={"url_pattern": "*example.com*"},
            function_code=CONTINUE,
            instance_ids=["inst-1"],
            priority=20,
        )
        messages = []
        monkeypatch.setattr(
            dhs.debug_logger,
            "log_warning",
            lambda component, method, message: messages.append(message),
        )
        await sys._process_request_hooks(FakeTab(), _req(stage="request"))
        shadow = [m for m in messages if "shadowed" in m]
        assert len(shadow) == 1
        assert "shadow-lo" in shadow[0]
        assert "win-hi" in shadow[0]

    async def test_single_match_emits_no_shadow_warning(self, monkeypatch):
        sys = DynamicHookSystem()
        only = await sys.create_hook(
            name="solo",
            requirements={"url_pattern": "*example.com*"},
            function_code=CONTINUE,
            instance_ids=["inst-1"],
            priority=10,
        )
        messages = []
        monkeypatch.setattr(
            dhs.debug_logger,
            "log_warning",
            lambda component, method, message: messages.append(message),
        )
        await sys._process_request_hooks(FakeTab(), _req(stage="request"))
        assert [m for m in messages if "shadowed" in m] == []
        assert sys.hooks[only].trigger_count == 1


class TestInterceptionArming:
    """F-772: `Fetch.enable` pauses EVERY request until something resumes it, so
    it must never be enabled when there is nothing to intercept.

    The catch-all fallback armed on every spawn made a zero-hook instance depend
    on the `Fetch.RequestPaused` handler firing for every navigation; on macOS
    under the detached backend it does not, and every navigation hung. These pins
    hold both halves: no hooks -> never enabled, and a hook created after spawn
    still arms interception (the coverage the catch-all was accidentally giving).
    """

    async def test_no_hooks_means_no_fetch_enable(self):
        sys = DynamicHookSystem()
        sys.add_instance("inst-1")
        tab = FakeTab()

        await sys.setup_interception(tab, "inst-1")

        assert tab.send_calls == []
        assert tab.handlers == []

    async def test_hooks_still_intercept(self):
        sys = DynamicHookSystem()
        sys.add_instance("inst-1")
        await sys.create_hook(
            "h", {"url_pattern": "*/api/*"}, CONTINUE, instance_ids=["inst-1"]
        )
        tab = FakeTab()

        await sys.setup_interception(tab, "inst-1")

        assert tab.send_calls == ["enable"]
        patterns = tab.cdp_frames[0]["params"]["patterns"]
        assert [p["urlPattern"] for p in patterns] == ["*/api/*"]
        assert len(tab.handlers) == 1

    async def test_hook_created_after_spawn_arms_interception(self):
        sys = DynamicHookSystem()
        sys.add_instance("inst-1")
        tab = FakeTab()

        await sys.setup_interception(tab, "inst-1")  # spawn: zero hooks
        assert tab.send_calls == []

        await sys.create_hook("h", {"url_pattern": "*/api/*"}, CONTINUE)

        assert tab.send_calls == ["enable"]
        patterns = tab.cdp_frames[0]["params"]["patterns"]
        assert [p["urlPattern"] for p in patterns] == ["*/api/*"]

        # Re-arming for a second hook re-sends the widened pattern set but must
        # not stack a second RequestPaused handler (which would double-continue
        # every paused request).
        await sys.create_hook("h2", {"url_pattern": "*/other/*"}, CONTINUE)

        assert tab.send_calls == ["enable", "enable"]
        patterns = tab.cdp_frames[1]["params"]["patterns"]
        assert sorted(p["urlPattern"] for p in patterns) == ["*/api/*", "*/other/*"]
        assert len(tab.handlers) == 1


class TestResponseStageHooksRemoved:
    """F-721/F-742: the dead ResponseStageProcessor duplicate is deleted, so the
    HookAction -> CDP dispatch lives in exactly one place (dynamic_hook_system)."""

    def test_response_stage_hooks_module_is_deleted(self):
        import importlib

        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("response_stage_hooks")

        # The canonical home for the dispatch types survives the deletion.
        from stealth_chrome_devtools_mcp.embedded.dynamic_hook_model import (
            HookAction,
            RequestInfo,
        )

        assert HookAction is not None and RequestInfo is not None


# ---------------------------------------------------------------------------
# T-8944: a paused request that can no longer be resumed is not an error event
# ---------------------------------------------------------------------------

# Each is the exception type and message of one Sentry family, built from the
# library's own constructors.
GONE_ERRORS = {
    "stale-interception-id": ProtocolException(
        {"code": -32602, "message": "Invalid InterceptionId."}
    ),
    "not-an-http-response": InvalidMessage("did not receive a valid HTTP response"),
    "connection-closed": ConnectionClosed(None, None),
    # asyncio.open_connection to a closed port, as Windows words it (WinError 1225).
    "connection-refused": ConnectionRefusedError(
        1225, "The remote computer refused the network connection"
    ),
    "session-not-found": ProtocolException(
        {"code": -32001, "message": "Session with given id not found"}
    ),
    "no-target": ProtocolException(
        {"code": -32602, "message": "No target with given id found"}
    ),
}
# The hook namespace has no exception builtins; a missing key still raises.
RAISES = "def process_request(request):\n    return {}['hook blew up']\n"


class _FailingTab(FakeTab):
    """A tab whose CDP commands fail by name, after being recorded."""

    def __init__(self, errors):
        super().__init__()
        self._errors = errors

    async def send(self, cdp_obj, *args, **kwargs):
        name = cdp_command_name(cdp_obj)
        result = await super().send(cdp_obj, *args, **kwargs)
        if name in self._errors:
            raise self._errors[name]
        return result


def _request_obj():
    return SimpleNamespace(url="https://example.invalid/", method="GET", headers={})


def _paused_event(*, response_status_code=None, request=None):
    """A ``Fetch.requestPaused`` event. Without a ``request`` building the
    ``RequestInfo`` fails, which is the path ``_on_request_paused`` itself
    catches."""
    event = SimpleNamespace(
        request_id=uc.cdp.fetch.RequestId("req-1"),
        response_status_code=response_status_code,
        response_error_reason=None,
        resource_type="Document",
    )
    if request is not None:
        event.request = request
    return event


def _records(caplog, level):
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "stealth.backend" and r.levelno == level
    ]


@pytest.fixture
def backend_log(caplog):
    caplog.set_level(logging.DEBUG, logger="stealth.backend")
    return caplog


async def _hooked(code=CONTINUE):
    system = DynamicHookSystem()
    await system.create_hook("h", {}, code, instance_ids=["inst-1"])
    return system


@pytest.mark.parametrize("error", GONE_ERRORS.values(), ids=GONE_ERRORS.keys())
class TestGoneRequestIsNotAnErrorEvent:
    """The page navigated away or the tab/browser closed, so Chrome forgot the
    request (``Invalid InterceptionId``) or nodriver could not reopen the socket.
    Nothing is stuck and re-sending cannot help: a warning, never an ERROR record
    (the level Sentry turns into an exception event)."""

    async def test_a_failed_continue_is_not_sent_twice(self, error, backend_log):
        system = DynamicHookSystem()
        tab = _FailingTab({"continue_request": error})

        await system._process_request_hooks(tab, _req())

        assert tab.send_calls == ["continue_request"]
        assert _records(backend_log, logging.ERROR) == []
        warned = _records(backend_log, logging.WARNING)
        assert len(warned) == 1 and str(error) in warned[0]

    async def test_the_failed_fallback_after_a_hook_error_is_a_warning(
        self, error, backend_log
    ):
        system = await _hooked(RAISES)
        tab = _FailingTab({"continue_request": error})

        await system._process_request_hooks(tab, _req())

        assert tab.send_calls == ["continue_request"]
        # the caller's hook bug is a warning (T-9135); so is the gone fallback
        assert _records(backend_log, logging.ERROR) == []
        warned = _records(backend_log, logging.WARNING)
        assert len(warned) == 2 and any("hook blew up" in m for m in warned)

    async def test_a_failed_action_is_not_retried_or_reported(self, error, backend_log):
        system = DynamicHookSystem()
        tab = _FailingTab({"fail_request": error})

        await system._execute_hook_action(tab, _req(), HookAction(action="block"))

        assert tab.send_calls == ["fail_request"]
        assert _records(backend_log, logging.ERROR) == []
        assert len(_records(backend_log, logging.WARNING)) == 1

    async def test_the_paused_handler_itself_does_not_report_it(
        self, error, backend_log
    ):
        system = DynamicHookSystem()
        tab = _FailingTab({"continue_request": error})

        await system._on_request_paused(tab, _paused_event(), "inst-1")

        stuck = [m for m in _records(backend_log, logging.ERROR) if "stuck" in m]
        assert stuck == []
        assert len(_records(backend_log, logging.WARNING)) == 1


class TestRealFailuresStillReported:
    async def test_an_unrelated_failed_fallback_is_still_an_error(self, backend_log):
        system = await _hooked(RAISES)
        tab = _FailingTab({"continue_request": RuntimeError("cdp exploded")})

        await system._process_request_hooks(tab, _req())

        stuck = [
            m
            for m in _records(backend_log, logging.ERROR)
            if "request may be stuck: cdp exploded" in m
        ]
        assert len(stuck) == 1
        warned = _records(backend_log, logging.WARNING)
        assert len(warned) == 1 and "hook blew up" in warned[0]  # T-9135

    async def test_an_unrelated_action_failure_still_continues_the_request(
        self, backend_log
    ):
        system = DynamicHookSystem()
        tab = _FailingTab({"fail_request": RuntimeError("cdp exploded")})

        await system._execute_hook_action(tab, _req(), HookAction(action="block"))

        assert tab.send_calls == ["fail_request", "continue_request"]
        assert len(_records(backend_log, logging.ERROR)) == 1

    async def test_a_response_stage_request_is_resumed_as_a_response(self):
        system = DynamicHookSystem()
        tab = FakeTab()

        await system._on_request_paused(
            tab, _paused_event(response_status_code=200), "inst-1"
        )

        assert tab.send_calls == ["continue_response"]


class TestPausedHandlerTasks:
    """D7 (`Task was destroyed but it is pending!`): asyncio keeps only a weak
    reference to a task, so a handler parked on a reply that never comes was
    collected mid-await."""

    @staticmethod
    async def _armed(monkeypatch, process):
        system = DynamicHookSystem()
        system.add_instance("inst-1")
        await system.create_hook("h", {}, CONTINUE, instance_ids=["inst-1"])
        tab = FakeTab()
        await system.setup_interception(tab, "inst-1")
        monkeypatch.setattr(system, "_process_request_hooks", process)
        return system, tab.handlers[0][1]

    @staticmethod
    async def _never_answered(_tab, _request, _event=None):
        # A future nothing else references: the shape of a reply that never comes.
        await asyncio.get_running_loop().create_future()

    async def test_an_in_flight_handler_survives_garbage_collection(self, monkeypatch):
        system, handler = await self._armed(monkeypatch, self._never_answered)
        handler(_paused_event(request=_request_obj()))
        await asyncio.sleep(0)
        (task,) = system._paused_tasks["inst-1"]
        ref = weakref.ref(task)
        del task

        gc.collect()

        survivor = ref()
        assert survivor is not None and not survivor.done()
        del survivor
        system.remove_instance("inst-1")
        await asyncio.sleep(0)

    async def test_removing_the_instance_cancels_its_handlers(self, monkeypatch):
        system, handler = await self._armed(monkeypatch, self._never_answered)
        handler(_paused_event(request=_request_obj()))
        handler(_paused_event(request=_request_obj()))
        await asyncio.sleep(0)
        tasks = list(system._paused_tasks["inst-1"])
        assert len(tasks) == 2 and system.paused_in_flight("inst-1") == 2

        system.remove_instance("inst-1")
        await asyncio.wait(tasks, timeout=1)

        assert all(t.cancelled() for t in tasks)
        assert system.paused_in_flight("inst-1") == 0
        assert "inst-1" not in system._paused_tasks

    async def test_removing_another_instance_leaves_them_running(self, monkeypatch):
        system, handler = await self._armed(monkeypatch, self._never_answered)
        handler(_paused_event(request=_request_obj()))
        await asyncio.sleep(0)
        (task,) = system._paused_tasks["inst-1"]

        system.remove_instance("inst-2")
        await asyncio.sleep(0)

        assert not task.done()
        system.remove_instance("inst-1")
        await asyncio.wait([task], timeout=1)

    async def test_a_finished_handler_is_forgotten(self, monkeypatch):
        async def answered(_tab, _request, _event=None):
            return None

        system, handler = await self._armed(monkeypatch, answered)
        handler(_paused_event(request=_request_obj()))
        (task,) = system._paused_tasks["inst-1"]

        await asyncio.wait([task], timeout=1)

        assert system._paused_tasks["inst-1"] == set()


def _http_error(status):
    return InvalidStatus(Response(status, "Refused", Headers(), b""))


# Errors that say nothing about whether the request is still paused in a LIVE
# page. Treating one as "gone" skips the continue and leaves the page hanging.
NOT_GONE_ERRORS = {
    "timeout": TimeoutError("timed out"),
    "file-not-found": FileNotFoundError(2, "No such file"),
    "permission-denied": PermissionError(13, "Access is denied"),
    "other-os-error": OSError(5, "Input/output error"),
    # On nodriver 0.50 a tab shares the BROWSER endpoint, so a 500 there is the
    # browser's own fault and says nothing about the paused request (T-8944).
    "http-500": _http_error(500),
    "http-403": _http_error(403),
    "http-404": _http_error(404),
    "other-websocket-error": WebSocketException("handshake failed"),
    "other-protocol-error": ProtocolException(
        {"code": -32602, "message": "Invalid parameters"}
    ),
}


@pytest.mark.parametrize("error", NOT_GONE_ERRORS.values(), ids=NOT_GONE_ERRORS.keys())
class TestOtherErrorsAreStillReportedAndContinued:
    async def test_a_failed_action_is_an_error_and_the_request_continues(
        self, error, backend_log
    ):
        system = DynamicHookSystem()
        tab = _FailingTab({"fail_request": error})

        await system._execute_hook_action(tab, _req(), HookAction(action="block"))

        assert tab.send_calls == ["fail_request", "continue_request"]
        assert len(_records(backend_log, logging.ERROR)) == 1
        assert _records(backend_log, logging.WARNING) == []

    async def test_a_failed_fallback_is_a_stuck_request_error(self, error, backend_log):
        system = await _hooked(RAISES)
        tab = _FailingTab({"continue_request": error})

        await system._process_request_hooks(tab, _req())

        stuck = [m for m in _records(backend_log, logging.ERROR) if "stuck" in m]
        assert len(stuck) == 1
        warned = _records(backend_log, logging.WARNING)
        assert len(warned) == 1 and "hook blew up" in warned[0]  # T-9135


class TestResponseBodyOfAGoneRequest:
    """The same family at the one other place a paused request is read: the
    response body of a request whose interception id is already stale."""

    @staticmethod
    async def _run(error):
        system = DynamicHookSystem()
        await system.create_hook(
            "resp", {"stage": "response"}, CONTINUE, instance_ids=["inst-1"]
        )
        tab = _FailingTab({"get_response_body": error})
        event = SimpleNamespace(response_status_code=200, response_headers=None)
        await system._process_request_hooks(tab, _req(stage="response"), event)
        return tab

    @pytest.mark.parametrize("error", GONE_ERRORS.values(), ids=GONE_ERRORS.keys())
    async def test_a_gone_request_is_a_warning(self, error, backend_log):
        await self._run(error)

        assert _records(backend_log, logging.ERROR) == []
        assert any(
            "No response body" in m for m in _records(backend_log, logging.WARNING)
        )

    async def test_another_error_is_still_an_error(self, backend_log):
        await self._run(RuntimeError("cdp exploded"))

        errors = _records(backend_log, logging.ERROR)
        assert len(errors) == 1 and "Failed to get response body" in errors[0]
        assert _records(backend_log, logging.WARNING) == []


class TestLateEventForARemovedInstance:
    async def test_it_spawns_no_task_and_leaves_nothing_behind(self, monkeypatch):
        system, handler = await TestPausedHandlerTasks._armed(
            monkeypatch, TestPausedHandlerTasks._never_answered
        )
        system.remove_instance("inst-1")

        handler(_paused_event(request=_request_obj()))
        await asyncio.sleep(0)

        assert "inst-1" not in system._paused_tasks
        assert system.paused_in_flight("inst-1") == 0
        assert not [
            t for t in asyncio.all_tasks() if "_on_request_paused" in repr(t.get_coro())
        ]
