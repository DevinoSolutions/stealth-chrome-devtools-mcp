"""Pinning tests for M1-3: `_watch_backend_liveness`'s default check is
promoted from a bare socket connect to the app-level probe, and the loop is
made await-aware so both a sync-injected check (every existing watchdog
test) and an async default keep working.

Before this change the watchdog's default polled `_server_is_healthy` — a
wedged backend (dispatch loop dead, socket still open) always passed that
check, so the "sole auto-recovery watchdog" (F-501) never armed against the
exact failure mode it exists for. After: the default runs
`_backend_http_ready(port)` off-thread via `anyio.to_thread.run_sync`, and
`res = check(); if inspect.isawaitable(res): res = await res` drives either
shape. Signature, `interval`, and `failures_before_teardown` are UNCHANGED
(human-resolved decision, plan_M1 appendix) — this file adds coverage, it
does not touch test_proxy_backend_death.py's existing sync-injection tests
(confirmed passing unchanged in M1-3's verification step).

See test_singleton_cold_start_logging.py for the captured_proxy_records
fixture convention this file reuses (direct handler attachment, not caplog —
configure_logging sets propagate=False by design).
"""

import inspect
import logging

import anyio
import anyio.lowlevel
import pytest

from stealth_chrome_devtools_mcp.embedded import singleton


@pytest.fixture()
def captured_proxy_records():
    records = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("stealth.proxy")
    handler = _ListHandler()
    logger.addHandler(handler)
    prior_level = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prior_level)


class TestWatchdogDefaultUsesAppProbe:
    @pytest.mark.asyncio
    async def test_wedged_backend_arms_after_three_failures_and_warns(
        self, monkeypatch, captured_proxy_records
    ):
        # Force the DEFAULT check (is_healthy=None) to run against a stub that
        # always resolves False, standing in for a wedged backend without
        # needing a real socket/thread stub here — the probe function itself
        # is already pinned against real wedged/responsive stubs in
        # test_backend_liveness_probe.py (M1-1); this test's job is the
        # watchdog's DEFAULT-wiring + await-aware loop + WARNING emission.
        calls = {"n": 0}

        def fake_probe(port, **kwargs):
            calls["n"] += 1
            return False

        monkeypatch.setattr(singleton, "_backend_http_ready", fake_probe)

        async def tiny_sleep(_):
            await anyio.lowlevel.checkpoint()

        with anyio.fail_after(5):
            await singleton._watch_backend_liveness(
                port=12345,
                interval=0.0,
                failures_before_teardown=3,
                sleep=tiny_sleep,
                # is_healthy intentionally omitted: pins the DEFAULT wiring.
            )

        assert calls["n"] == 3  # tore down after exactly 3 consecutive failures

        warnings = [r for r in captured_proxy_records if r.levelno == logging.WARNING]
        assert len(warnings) >= 1, "expected a stealth.proxy WARNING on probe failure"

    @pytest.mark.asyncio
    async def test_default_check_runs_off_thread(self, monkeypatch):
        # The default must not call the sync probe inline on the event loop
        # (plan_M1 SS2.2 rejected alternative #3: a blocking httpx call inline
        # would freeze the stdio pump for up to LIVENESS_PROBE_TIMEOUT every
        # interval). Confirmed by observing anyio.to_thread.run_sync is
        # actually invoked with the probe as its target.
        run_sync_calls = []

        async def fake_run_sync(fn, *args, **kwargs):
            run_sync_calls.append((fn, args))
            return False

        monkeypatch.setattr(anyio.to_thread, "run_sync", fake_run_sync)
        monkeypatch.setattr(singleton, "_backend_http_ready", lambda port, **kw: False)

        async def tiny_sleep(_):
            await anyio.lowlevel.checkpoint()

        with anyio.fail_after(5):
            await singleton._watch_backend_liveness(
                port=12345,
                interval=0.0,
                failures_before_teardown=1,
                sleep=tiny_sleep,
            )

        assert len(run_sync_calls) >= 1


class TestWatchdogAwaitAwareLoop:
    @pytest.mark.asyncio
    async def test_injected_sync_check_still_works(self):
        # Pins backward compatibility: every existing watchdog test
        # (test_proxy_backend_death.py) injects a plain sync callable.
        calls = {"n": 0}

        def sync_check():
            calls["n"] += 1
            return False

        async def tiny_sleep(_):
            await anyio.lowlevel.checkpoint()

        with anyio.fail_after(5):
            await singleton._watch_backend_liveness(
                port=1,
                interval=0.0,
                failures_before_teardown=2,
                is_healthy=sync_check,
                sleep=tiny_sleep,
            )

        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_injected_async_check_drives_the_loop(self):
        # Pins the new await-aware branch: an injected check that returns a
        # coroutine (inspect.isawaitable) must be awaited, not treated as a
        # truthy/falsy object.
        calls = {"n": 0}

        async def async_check():
            calls["n"] += 1
            await anyio.lowlevel.checkpoint()
            return False

        async def tiny_sleep(_):
            await anyio.lowlevel.checkpoint()

        with anyio.fail_after(5):
            await singleton._watch_backend_liveness(
                port=1,
                interval=0.0,
                failures_before_teardown=2,
                is_healthy=async_check,
                sleep=tiny_sleep,
            )

        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_async_check_result_is_awaitable_when_uninvoked(self):
        # Direct characterization of the guard itself: calling an async
        # function without awaiting it yields a coroutine object, which
        # inspect.isawaitable must recognize.
        async def _coro():
            return False

        result = _coro()
        assert inspect.isawaitable(result)
        result.close()  # avoid a "coroutine was never awaited" warning
