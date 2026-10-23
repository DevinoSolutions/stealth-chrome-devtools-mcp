"""F-1018 follow-up (T-6367): a 401 to the watchdog's heartbeat is NOT a strike.

A backend that enforces the bearer token answers a client that cannot read the
token file (deleted, locked by antivirus, another user's) with 401 on every route.
That is proof the backend is alive and that only THIS client is wrong. Read as
"dead" it struck the watchdog, the confirmation probe 401ed the same way, and a
healthy backend was condemned and replaced.

Hermetic: the heartbeat's own HTTP client is an ``httpx.MockTransport``, so the
verdict is the product's and the answer is the backend's, with no socket.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

from stealth_chrome_devtools_mcp.embedded import backend_probe, backend_watchdog

URL = "http://127.0.0.1:1/mcp/"


def _beat_answering(monkeypatch, status: int) -> backend_probe.Heartbeat:
    beat = backend_probe.Heartbeat(URL)
    transport = httpx.MockTransport(lambda request: httpx.Response(status))
    monkeypatch.setattr(beat, "_connection", lambda: httpx.Client(transport=transport))
    return beat


def test_a_401_is_its_own_verdict_and_the_backend_counts_as_alive(monkeypatch):
    beat = _beat_answering(monkeypatch, 401)
    assert beat.verdict(1.0) == backend_probe.VERDICT_UNAUTHORIZED
    assert beat.alive(1.0) is True


@pytest.mark.parametrize(
    ("status", "verdict"),
    [(200, "alive"), (503, "dead"), (500, "dead")],
)
def test_the_other_answers_keep_their_verdicts(monkeypatch, status, verdict):
    assert _beat_answering(monkeypatch, status).verdict(1.0) == verdict


def test_a_refused_connection_is_still_dead(monkeypatch):
    beat = backend_probe.Heartbeat(URL)

    def refuse(request):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(
        beat,
        "_connection",
        lambda: httpx.Client(transport=httpx.MockTransport(refuse)),
    )
    assert beat.verdict(1.0) == backend_probe.VERDICT_DEAD
    assert beat.alive(1.0) is False


def test_the_cause_is_logged_at_warning_once_per_run_of_401s(monkeypatch, caplog):
    beat = _beat_answering(monkeypatch, 401)
    with caplog.at_level(logging.DEBUG, logger="stealth.proxy"):
        for _ in range(5):
            beat.verdict(1.0)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "bearer token" in warnings[0].getMessage()


def _beat_answering_in_turn(monkeypatch, answers) -> backend_probe.Heartbeat:
    """Each beat takes the next answer: a status, or ``None`` for a refused dial."""
    beat = backend_probe.Heartbeat(URL)
    queue = list(answers)

    def answer(request):
        status = queue.pop(0)
        if status is None:
            raise httpx.ConnectError("refused")
        return httpx.Response(status)

    transport = httpx.MockTransport(answer)
    monkeypatch.setattr(beat, "_connection", lambda: httpx.Client(transport=transport))
    return beat


@pytest.mark.parametrize(
    "answers",
    [[401, 401, 200, 401], [401, 401, None, 401]],
    ids=["after-a-200", "after-an-outage"],
)
def test_the_warning_re_arms_for_each_new_run_of_401s(monkeypatch, caplog, answers):
    beat = _beat_answering_in_turn(monkeypatch, answers)
    with caplog.at_level(logging.DEBUG, logger="stealth.proxy"):
        for _ in answers:
            beat.verdict(1.0)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2


async def test_a_backend_answering_401_never_reaches_the_condemn_threshold(
    monkeypatch,
):
    """THE pin, through the real watchdog: with the probe the proxy wires
    (``Heartbeat.alive``), 40 ticks of 401 strike nothing, and the confirmation
    gate that would condemn is never asked. RED when a 401 reads as dead: three
    strikes and the gate is asked on the third tick."""
    beat = _beat_answering(monkeypatch, 401)
    asked: list[str] = []
    ticks = 0

    async def tick(_interval: float) -> None:
        nonlocal ticks
        ticks += 1
        await asyncio.sleep(0)

    def confirm() -> bool:
        asked.append("confirm")
        return False  # the gate would call the backend dead

    watching = asyncio.ensure_future(
        backend_watchdog.watch_liveness(
            51999,
            is_healthy=lambda: beat.alive(1.0),
            confirm_probe=confirm,
            interval=0,
            sleep=tick,
        )
    )
    while ticks < 40 and not watching.done():
        await asyncio.sleep(0)
    condemned = watching.done()
    watching.cancel()
    assert (condemned, asked) == (False, [])
    assert ticks >= 40
