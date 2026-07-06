"""Behavioral tests for ResponseStageProcessor (no real browser).

This is the CDP dispatch layer for response/request-stage hook actions. A fake
tab records the commands it is asked to send. The load-bearing guarantee is the
error path: if dispatching an action fails, the processor MUST still issue a
continue so the intercepted request/response is never left hanging.
"""

import pytest

from response_stage_hooks import ResponseStageProcessor
from dynamic_hook_system import HookAction, RequestInfo


class FakeTab:
    def __init__(self, fail_times=0):
        self.sent = []
        self._fail_times = fail_times

    async def send(self, command):
        if len(self.sent) < self._fail_times:
            self.sent.append(command)
            raise RuntimeError("cdp send failed")
        self.sent.append(command)
        return None


def _req(stage="response"):
    return RequestInfo(
        request_id="r1",
        instance_id="i1",
        url="https://example.com/api",
        method="GET",
        headers={},
        stage=stage,
    )


def _proc():
    return ResponseStageProcessor(None)


class TestResponseAction:
    @pytest.mark.parametrize(
        "action",
        [
            HookAction(action="block"),
            HookAction(
                action="fulfill", status_code=200, headers={"X": "y"}, body="hi"
            ),
            HookAction(action="modify", status_code=302, headers={"X": "y"}),
            HookAction(action="continue"),
        ],
    )
    async def test_each_action_dispatches_one_command(self, action):
        tab = FakeTab()
        await _proc().execute_response_action(tab, _req(), action, event=None)
        assert len(tab.sent) == 1 and tab.sent[0] is not None

    async def test_error_still_continues_response(self):
        # First send raises; the processor must fall back to a continue send.
        tab = FakeTab(fail_times=1)
        await _proc().execute_response_action(
            tab, _req(), HookAction(action="block"), event=None
        )
        assert len(tab.sent) == 2, (
            "must attempt a fallback continue after a failed action"
        )


class TestRequestAction:
    @pytest.mark.parametrize(
        "action",
        [
            HookAction(action="block"),
            HookAction(action="redirect", url="https://example.com/other"),
            HookAction(action="fulfill", status_code=200, body="hi"),
            HookAction(action="modify", headers={"X": "y"}, method="POST"),
            HookAction(action="continue"),
        ],
    )
    async def test_each_action_dispatches_one_command(self, action):
        tab = FakeTab()
        await _proc().execute_request_action(tab, _req(stage="request"), action)
        assert len(tab.sent) == 1 and tab.sent[0] is not None

    async def test_error_still_continues_request(self):
        tab = FakeTab(fail_times=1)
        await _proc().execute_request_action(
            tab, _req(stage="request"), HookAction(action="block")
        )
        assert len(tab.sent) == 2, (
            "must attempt a fallback continue after a failed action"
        )
