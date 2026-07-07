"""Pinning tests for M10a-7b: network_interceptor.py + dynamic_hook_system.py
silent excepts now log (F-181 rows 7-10). See test_silent_excepts_log.py
(7a) for the fixture/rationale this file continues.

network_interceptor.py sites are classified DEBUG (plan_M3 SS7 risk 3:
per-event network handlers, "expected" per the existing comments - a single
undecodable/unavailable response body must stay quiet by default).
dynamic_hook_system.py:464 is classified WARNING (a genuine header-shape
mismatch silently degrading to `{}`, not a routine/expected case).
"""

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from models import NetworkRequest


@pytest.fixture()
def captured_backend_records():
    records = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("stealth.backend")
    handler = _ListHandler()
    logger.addHandler(handler)
    prior_level = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prior_level)


# ---------------------------------------------------------------------------
# 7b: network_interceptor.py (DEBUG - expected/routine per-event cases)
# ---------------------------------------------------------------------------


class TestNetworkInterceptorSilentExcepts:
    @pytest.mark.asyncio
    async def test_get_response_body_unexpected_error_logs_at_debug(
        self, captured_backend_records
    ):
        from network_interceptor import NetworkInterceptor

        ni = NetworkInterceptor()
        tab = MagicMock()
        tab.send = AsyncMock(side_effect=ValueError("streaming/redirect body-fail"))

        result = await ni.get_response_body(tab, "req-1")

        assert result is None
        assert len(captured_backend_records) == 1
        record = captured_backend_records[0]
        assert record.levelno == logging.DEBUG
        assert "streaming/redirect body-fail" in record.getMessage()

    @pytest.mark.asyncio
    async def test_on_response_unexpected_body_error_logs_at_debug(
        self, captured_backend_records
    ):
        from network_interceptor import NetworkInterceptor

        ni = NetworkInterceptor()
        tab = MagicMock()
        tab.send = AsyncMock(side_effect=ValueError("preflight body-fail"))

        response = MagicMock()
        response.status = 204
        response.headers = {}
        response.mime_type = "text/plain"
        event = MagicMock()
        event.request_id = "req-2"
        event.response = response

        await ni._on_response(event, "inst-1", tab=tab)

        assert ni._responses["req-2"].body is None
        assert len(captured_backend_records) == 1
        record = captured_backend_records[0]
        assert record.levelno == logging.DEBUG
        assert "preflight body-fail" in record.getMessage()

    @pytest.mark.asyncio
    async def test_search_requests_undecodable_body_logs_at_debug(
        self, captured_backend_records
    ):
        from network_interceptor import NetworkInterceptor

        ni = NetworkInterceptor()
        req = NetworkRequest(
            request_id="r1",
            instance_id="i1",
            url="https://api.example.com/data",
            method="GET",
            resource_type="XHR",
        )
        bad_body = MagicMock()
        bad_body.decode.side_effect = UnicodeDecodeError(
            "utf-8", b"\xff", 0, 1, "undecodable"
        )
        # Duck-typed stand-in, NOT the real NetworkResponse pydantic model:
        # search_requests only ever reads .status/.body off this object, and a
        # real BaseModel would reject a MagicMock for the strictly-typed
        # `body: bytes | None` field before the test even reaches the site
        # under test.
        resp = MagicMock()
        resp.status = 200
        resp.body = bad_body

        ni._instance_requests["i1"] = ["r1"]
        ni._requests["r1"] = req
        ni._responses["r1"] = resp

        result = await ni.search_requests("i1", response_contains="anything")

        assert result["results"] == []
        assert len(captured_backend_records) == 1
        record = captured_backend_records[0]
        assert record.levelno == logging.DEBUG
        assert "undecodable" in record.getMessage()


# ---------------------------------------------------------------------------
# 7b: dynamic_hook_system.py (WARNING - genuine header-shape mismatch)
# ---------------------------------------------------------------------------


def _make_matching_response_hook():
    from dynamic_hook_system import DynamicHookSystem

    system = DynamicHookSystem()
    return system


class TestDynamicHookSystemSilentExcepts:
    @pytest.mark.asyncio
    async def test_response_header_normalization_failure_logs_at_warning(
        self, captured_backend_records
    ):
        from dynamic_hook_system import RequestInfo

        system = _make_matching_response_hook()
        hook_id = await system.create_hook(
            name="passthrough",
            requirements={"stage": "response"},
            function_code=(
                'def process_request(request):\n    return {"action": "continue"}\n'
            ),
            instance_ids=["inst-1"],
        )
        assert hook_id in system.hooks

        request = RequestInfo(
            request_id="req-1",
            instance_id="inst-1",
            url="https://api.example.com/data",
            method="GET",
            headers={},
            stage="response",
        )

        class _BrokenHeaders:
            """Not a dict, but has an `items` attribute so the code's
            `hasattr(event.response_headers, "items")` check routes into the
            for-loop branch (dynamic_hook_system.py:459 iterates the object
            directly, not via .items()) - then raises on iteration itself."""

            items = None  # only needs to exist for hasattr(), never called

            def __iter__(self):
                raise RuntimeError("header-shape-mismatch")

        event = MagicMock()
        event.response_status_code = 200
        event.response_headers = _BrokenHeaders()

        tab = MagicMock()
        tab.send = AsyncMock(return_value=(b"body-bytes", False))

        await system._process_request_hooks(tab, request, event=event)

        # _process_request_hooks logs several pre-existing INFO lines along its
        # normal path (hook created/matched/triggered) - isolate WARNING to
        # confirm exactly the header-normalization failure, not total volume.
        warnings = [r for r in captured_backend_records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "header-shape-mismatch" in warnings[0].getMessage()
