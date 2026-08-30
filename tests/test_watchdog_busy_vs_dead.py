"""F-820 — the watchdog must not condemn a backend that is merely BUSY.

``_watch_backend_liveness`` used to tear the stdio proxy down after
``failures_before_teardown`` (3) consecutive misses of the 2s app-level probe.
Production, 2026-08-30: under multi-session fleet load the shared backend
answered ``initialize`` slower than 2s for 20-40s stretches while staying
perfectly healthy — four waves (05:12 x7, 05:19 x30, 05:28 x3, 05:34 x10
proxies) where every proxy tore down inside the same second while backend pid
52396 kept serving navigate/screenshot before, during and after. Every Claude
Code session lost the server at once: the user-visible "stealth randomly
disconnects".

The fix does NOT add a second busy-vs-dead policy. The strikes now merely open
a CONFIRMATION phase, and the verdict comes from the gate the cold-start lock
already trusts — ``_same_identity_backend_ready`` (F-807) — driven off-thread.
So the three behaviours the confirmation depends on are pinned once, in
``test_singleton_cold_start_patience.py``: a busy backend that answers inside
``REUSE_PATIENCE_SECONDS`` passes, a socket-dead one fails on the first probe
without buying the window (which is what keeps the human-pinned ~12s hard-down
detection intact), and one that answers nothing for the whole window fails.

What THIS file pins is the watchdog's half: that it consults that gate at all,
that a passing verdict resets the strike run instead of tearing down, that a
failing one still tears down promptly, and that the default seam really is
that gate. Everything is injected — no sockets, no HTTP, no state file.
``sleep`` doubles as the loop bound: a watchdog that correctly decides "busy,
not dead" never returns on its own, so the injected nap raises
``_StopWatchingError`` once the loop has run long enough for the assertion.
"""

from __future__ import annotations

import logging

import anyio
import anyio.lowlevel
import pytest

from stealth_chrome_devtools_mcp.embedded import singleton

PORT = 47820


class _StopWatchingError(Exception):
    """Raised by the injected nap to end an endless (== healthy) watch loop."""


def _bounded_nap(limit: int):
    """A ``sleep`` seam that never really sleeps and stops the loop after
    ``limit`` naps, so a non-returning watchdog is observable as an exception
    instead of a hang."""

    async def nap(_seconds):
        nap.calls += 1
        if nap.calls > limit:
            raise _StopWatchingError
        await anyio.lowlevel.checkpoint()

    nap.calls = 0
    return nap


class _Probe:
    """A recorded seam popping scripted results (the last one sticks)."""

    def __init__(self, *results):
        self.calls = 0
        self._results = list(results)

    def __call__(self):
        self.calls += 1
        if len(self._results) > 1:
            return self._results.pop(0)
        return self._results[0]


@pytest.fixture()
def captured_proxy_records():
    """Direct handler attachment, not caplog — configure_logging sets
    propagate=False by design (same convention as test_watchdog_app_level)."""
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


def _strike_counts(records) -> list[int]:
    """The ``n`` out of every ``n/3`` strike WARNING, in order — a reset shows
    up here as a second run starting back at 1."""
    return [
        r.args[0]
        for r in records
        if r.levelno == logging.WARNING and "probe failed" in r.msg
    ]


class TestBusyBackendIsNotCondemned:
    @pytest.mark.asyncio
    async def test_a_passing_confirmation_resets_the_strike_run(
        self, captured_proxy_records
    ):
        """THE F-820 pin: the fast probe never answers, but the confirmation
        gate says the backend is alive — the watchdog must NOT return, and must
        start its strike run over instead of accumulating toward teardown."""
        confirm = _Probe(True)
        nap = _bounded_nap(6)

        with anyio.fail_after(5), pytest.raises(_StopWatchingError):
            await singleton._watch_backend_liveness(
                port=PORT,
                interval=0.0,
                failures_before_teardown=3,
                is_healthy=lambda: False,
                sleep=nap,
                confirm_probe=confirm,
            )

        assert _strike_counts(captured_proxy_records) == [1, 2, 3, 1, 2, 3], (
            "the strike run must restart once the backend is confirmed alive"
        )
        assert confirm.calls == 2, "confirmation must run on every strike run"
        assert any(
            r.levelno == logging.INFO and "busy" in r.msg
            for r in captured_proxy_records
        ), "a survived confirmation must be reported at INFO, not silently"

    @pytest.mark.asyncio
    async def test_confirmation_is_not_consulted_before_the_strike_limit(self):
        """One or two misses are still just misses: the (blocking, patient)
        gate must not be paid for on every tick."""
        confirm = _Probe(True)
        # Misses that never reach three in a row: a healthy answer in between
        # resets the run, so the confirmation phase is never entered.
        fast = _Probe(False, False, True, False, True)

        with anyio.fail_after(5), pytest.raises(_StopWatchingError):
            await singleton._watch_backend_liveness(
                port=PORT,
                interval=0.0,
                failures_before_teardown=3,
                is_healthy=fast,
                sleep=_bounded_nap(5),
                confirm_probe=confirm,
            )

        assert confirm.calls == 0


class TestConfirmedDeadStillTearsDownPromptly:
    @pytest.mark.asyncio
    async def test_failed_confirmation_returns_on_the_third_strike(
        self, captured_proxy_records
    ):
        """The human-pinned ~12s hard-down window survives F-820: when the gate
        confirms the backend is not merely busy, teardown lands on the third
        strike, with no extra tick in between."""
        confirm = _Probe(False)
        nap = _bounded_nap(50)

        with anyio.fail_after(5):
            await singleton._watch_backend_liveness(
                port=PORT,
                interval=0.0,
                failures_before_teardown=3,
                is_healthy=lambda: False,
                sleep=nap,
                confirm_probe=confirm,
            )

        assert nap.calls == 3, "teardown must land on the third strike, not later"
        assert confirm.calls == 1
        assert any(
            r.levelno == logging.WARNING and "confirmed unusable" in r.msg
            for r in captured_proxy_records
        ), "the confirmed verdict must be distinguishable from the strikes"


class TestDefaultConfirmationIsTheColdStartGate:
    @pytest.mark.asyncio
    async def test_default_seam_drives_same_identity_backend_ready_off_thread(
        self, monkeypatch
    ):
        """The one-policy pin. The default confirmation must be the SAME gate
        the cold-start lock trusts — that is what buys the patience window and
        the fast dead-check without a second copy of the policy here — and it
        must run off-thread, since it blocks for up to REUSE_PATIENCE_SECONDS.
        """
        run_sync_calls = []

        async def fake_run_sync(fn, *args, **kwargs):
            run_sync_calls.append((fn, args))
            return False

        monkeypatch.setattr(anyio.to_thread, "run_sync", fake_run_sync)

        with anyio.fail_after(5):
            await singleton._watch_backend_liveness(
                port=PORT,
                interval=0.0,
                failures_before_teardown=1,
                sleep=_bounded_nap(50),
            )

        assert run_sync_calls == [
            (singleton._backend_http_ready, (PORT,)),
            (singleton._same_identity_backend_ready, (PORT,)),
        ]
