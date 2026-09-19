"""F-889 (c) — the proxy never exits because the backend is unreachable.

``proxy_selfheal.drive`` used to RETURN when a recovery ran out of allowance, and
``singleton._proxy_streams`` read that return as "tear down for reconnect": the
proxy process exited and Claude Code rendered it as **"Connection closed"**. The
premise under that exit has been false since this module's first docstring — *MCP
clients do not reliably respawn a stdio server mid-session* — and on 2026-09-18 a
RAM-starved machine took the door 114 times in one window, for a backend that was
answering ``initialize`` in 227 ms.

The exit is deleted rather than lengthened. What these pins hold down:

* ``drive`` does not return when healing fails, and does not return when the
  flap budget is exhausted — it backs off and asks again;
* a generation that never became READY is an incident to retry too (the last
  door the exit still had);
* the delays are bounded, capped and jittered — a fleet of proxies orphaned by
  one death must not re-enter the startup path in the same second;
* in-flight calls are still failed fast, on EVERY generation including the ones
  that end in a backoff, so a client waits for nothing it could be told about;
* a backend that comes back after a backoff is re-bridged and the retry series
  resets;
* the CLIENT going away is still the one exit.

``RETRY_BASE_SECONDS`` is monkeypatched down throughout: what is under test is
the SHAPE of the schedule, and the real 2 s base would make every node a
wall-clock wait.
"""

from __future__ import annotations

import anyio
import pytest

from stealth_chrome_devtools_mcp.embedded import proxy_selfheal, singleton

PORT_A = 41889
PORT_B = 41890


class _Sink:
    def __init__(self):
        self.sent = []

    async def send(self, msg):
        self.sent.append(msg)


def _request(req_id, method):
    from mcp.shared.message import SessionMessage
    from mcp.types import JSONRPCMessage, JSONRPCRequest

    return SessionMessage(
        message=JSONRPCMessage(
            JSONRPCRequest(jsonrpc="2.0", id=req_id, method=method, params={})
        )
    )


def _drive_kwargs(**overrides):
    async def connect(_url, _replay, armed):
        armed.set()
        await anyio.sleep_forever()

    async def watch(_port):
        await anyio.sleep_forever()

    kwargs = {
        "port": PORT_A,
        "url_for": lambda p: f"http://127.0.0.1:{p}/mcp/",
        "connect": connect,
        "watch": watch,
        "confirm_alive": lambda _port: False,
        "replay": lambda: None,
        "pending": proxy_selfheal.PendingCalls(),
        "client_write": _Sink(),
        "ensure_running": lambda p: p,
        "await_ready": None,
    }
    kwargs.update(overrides)
    return kwargs


@pytest.fixture()
def captured_lifecycle(monkeypatch):
    """Spy the ONE capture seam (``test_proxy_sentry_reporting``'s device):
    ``proxy_selfheal`` reaches it lazily, by module attribute, at call time."""
    from stealth_chrome_devtools_mcp import observability

    events = []

    def _spy(message, *, level="warning", **fields):
        events.append((message, level, fields))
        return True

    monkeypatch.setattr(observability, "capture_lifecycle", _spy)
    return events


@pytest.fixture()
def fast_backoff(monkeypatch):
    monkeypatch.setattr(proxy_selfheal, "RETRY_BASE_SECONDS", 0.001)
    monkeypatch.setattr(proxy_selfheal, "RETRY_MAX_SECONDS", 0.01)


async def _dead_watch(_port):
    return  # confirmed dead, immediately


class _EnoughError(Exception):
    """Raised from inside the loop to end a ``drive`` that correctly never ends.

    The loop bound is a COUNT and not a wall-clock window on purpose. A
    ``move_on_after`` here measures how much of its budget the first cold import
    ate, which is a property of the machine; ``drive`` re-raises whatever
    ``heal_backend`` raises, so counting heals is exact on any machine.
    """


def _heal_counter(monkeypatch, *, answers, stop_after):
    """A ``heal_backend`` double: pops ``answers`` (the last sticks) and raises
    :class:`_EnoughError` once it has been asked ``stop_after`` times."""
    asked = []

    async def fake_heal(dead_port, **_kw):
        asked.append(dead_port)
        if len(asked) >= stop_after:
            raise _EnoughError
        return answers[0] if len(answers) == 1 else answers.pop(0)

    monkeypatch.setattr(proxy_selfheal, "heal_backend", fake_heal)
    return asked


class TestDriveDoesNotReturn:
    async def test_an_unhealable_death_keeps_retrying_instead_of_returning(
        self, monkeypatch, fast_backoff
    ):
        """THE F-889 (c) pin, and the reversal of
        ``test_an_unhealable_death_returns_for_the_legacy_teardown``. A heal that
        never succeeds must not end the session: reaching the fourth ask is
        proof the loop went round rather than returning after the first."""
        heals = _heal_counter(monkeypatch, answers=[None], stop_after=4)

        with anyio.fail_after(5), pytest.raises(_EnoughError):
            await proxy_selfheal.drive(**_drive_kwargs(watch=_dead_watch))

        assert len(heals) == 4

    async def test_a_flapping_backend_backs_off_instead_of_returning(
        self, monkeypatch, fast_backoff
    ):
        """``MAX_CONSECUTIVE_HEALS`` is the trigger for a WAIT now, not for an
        exit: 'stop hammering' is a delay, not the end of the session. Every
        heal here SUCCEEDS and every replacement dies at once, so the only thing
        that can stop the loop is the flap budget — and it must not."""
        beyond = proxy_selfheal.MAX_CONSECUTIVE_HEALS + 2
        heals = _heal_counter(monkeypatch, answers=[PORT_A], stop_after=beyond)

        with anyio.fail_after(5), pytest.raises(_EnoughError):
            await proxy_selfheal.drive(**_drive_kwargs(watch=_dead_watch))

        assert len(heals) == beyond

    async def test_a_generation_that_never_became_ready_is_retried(
        self, monkeypatch, fast_backoff
    ):
        """The last door the exit had. Before F-889 this ending returned at
        once, so a proxy whose backend simply took longer than
        ``BACKEND_READY_TIMEOUT`` under load lost its client — which is the
        outage, reached by the other route."""

        async def connect(_url, _replay, _armed):
            return  # readiness never came; armed stays unset

        heals = _heal_counter(monkeypatch, answers=[PORT_B], stop_after=2)

        with anyio.fail_after(5), pytest.raises(_EnoughError):
            await proxy_selfheal.drive(**_drive_kwargs(connect=connect))

        assert len(heals) == 2, "a backend that never became ready must be healed for"

    async def test_the_client_going_away_is_still_the_one_exit(self, monkeypatch):
        """Cancellation from OUTSIDE (what ``pump_client``'s EOF does) must
        unwind straight through — never be absorbed by the retry loop."""
        heals = []

        async def fake_heal(dead_port, **_kw):
            heals.append(dead_port)
            return PORT_B

        monkeypatch.setattr(proxy_selfheal, "heal_backend", fake_heal)

        async def run():
            await proxy_selfheal.drive(**_drive_kwargs())

        with anyio.fail_after(5):
            async with anyio.create_task_group() as outer:
                outer.start_soon(run)
                await anyio.sleep(0.05)
                outer.cancel_scope.cancel()

        assert heals == [], "a departed client must not be healed for"


class TestTheBackoffSchedule:
    def test_it_doubles_from_the_base(self, monkeypatch):
        monkeypatch.setattr(proxy_selfheal, "RETRY_JITTER", 0.0)

        assert proxy_selfheal._retry_delay(1) == proxy_selfheal.RETRY_BASE_SECONDS
        assert proxy_selfheal._retry_delay(2) == proxy_selfheal.RETRY_BASE_SECONDS * 2
        assert proxy_selfheal._retry_delay(3) == proxy_selfheal.RETRY_BASE_SECONDS * 4

    def test_it_is_capped(self, monkeypatch):
        monkeypatch.setattr(proxy_selfheal, "RETRY_JITTER", 0.0)

        assert proxy_selfheal._retry_delay(50) == proxy_selfheal.RETRY_MAX_SECONDS

    def test_it_is_jittered_so_a_fleet_does_not_converge(self):
        """The population this exists for is 114 proxies orphaned by ONE death.
        Without jitter they all re-enter ``ensure_running`` in the same second,
        which is the herd the cold-start lock then has to absorb."""
        delays = {proxy_selfheal._retry_delay(4) for _ in range(50)}

        assert len(delays) > 1, "the schedule must not be deterministic"
        nominal = proxy_selfheal.RETRY_BASE_SECONDS * 8
        assert all(
            nominal * (1 - proxy_selfheal.RETRY_JITTER)
            <= d
            <= nominal * (1 + proxy_selfheal.RETRY_JITTER)
            for d in delays
        )

    def test_the_cap_is_a_minute_not_an_hour(self):
        """A backend that comes back must be picked up while the user is still
        at the keyboard."""
        assert proxy_selfheal.RETRY_MAX_SECONDS == 60.0


class TestWhatTheClientSeesMeanwhile:
    async def test_inflight_calls_are_failed_on_every_generation(
        self, monkeypatch, fast_backoff
    ):
        """A client must never wait forever on a call the dead backend was
        holding — including across a generation that ends in a backoff."""
        sink = _Sink()
        pending = proxy_selfheal.PendingCalls()
        pending.track(_request(7, "tools/call").message.root)

        _heal_counter(monkeypatch, answers=[None], stop_after=3)

        with anyio.fail_after(5), pytest.raises(_EnoughError):
            await proxy_selfheal.drive(
                **_drive_kwargs(watch=_dead_watch, pending=pending, client_write=sink)
            )

        assert len(sink.sent) == 1
        err = sink.sent[0].message.root
        assert err.id == 7
        assert "tools/call" in err.error.message
        assert "NOT retried" in err.error.message

    async def test_a_backend_that_comes_back_is_re_bridged_and_resets_the_series(
        self, monkeypatch, fast_backoff, captured_lifecycle
    ):
        """The outage's happy ending: the client never disconnected, so the very
        next generation just serves it."""
        generations = []
        rebridged = anyio.Event()

        async def connect(url, _replay, armed):
            generations.append(url)
            armed.set()
            if len(generations) == 2:
                rebridged.set()
            await anyio.sleep_forever()

        async def watch(_port):
            if len(generations) == 1:
                return  # the first backend dies
            await anyio.sleep_forever()  # the replacement is healthy

        # Unhealable twice, then a backend appears.
        _heal_counter(monkeypatch, answers=[None, None, PORT_B], stop_after=99)

        # Cancelled from OUTSIDE once the re-bridge is observed — the same shape
        # the client's own EOF has, and the only way to end a ``drive`` that is
        # happily serving. Raising from inside would surface as an
        # ExceptionGroup out of ``_one_generation``'s task group.
        async def run():
            await proxy_selfheal.drive(**_drive_kwargs(connect=connect, watch=watch))

        with anyio.fail_after(5):
            async with anyio.create_task_group() as outer:
                outer.start_soon(run)
                await rebridged.wait()
                outer.cancel_scope.cancel()

        assert len(generations) == 2, "the client must be re-bridged after the wait"
        assert str(PORT_B) in generations[1]
        unreachable = [
            e for e in captured_lifecycle if e[0] == proxy_selfheal.UNREACHABLE_EVENT
        ]
        assert len(unreachable) == 1, "one event per EPISODE, whatever it costs"
        recovered = [
            e for e in captured_lifecycle if e[0] == proxy_selfheal.REACHABLE_EVENT
        ]
        assert [e[2]["attempts"] for e in recovered] == [2], (
            "the recovery event carries what the series cost"
        )


class TestTheReport:
    """F-889 review M2 — ONE event per outage episode, not one per retry.

    The backoff climbs to a 60 s cap and then keeps going for as long as the
    client is attached, so a report inside the loop is unbounded: an overnight
    outage on one proxy is ~1400 ERROR events, and 114 proxies orphaned by one
    backend death multiply it. That is not a louder signal, it is a quota spent
    on the machine that is least able to say anything new. The question the
    event answers — "how often does stealth still disconnect, and for how
    long" — needs exactly two points: the episode opened, and the episode
    closed with a duration and a cost.
    """

    async def test_it_ships_once_per_episode_however_long_the_series_runs(
        self, monkeypatch, fast_backoff, captured_lifecycle
    ):
        """THE M2 pin. Five failed retries, ONE event."""
        _heal_counter(monkeypatch, answers=[None], stop_after=6)

        with anyio.fail_after(5), pytest.raises(_EnoughError):
            await proxy_selfheal.drive(**_drive_kwargs(watch=_dead_watch))

        events = [
            e for e in captured_lifecycle if e[0] == proxy_selfheal.UNREACHABLE_EVENT
        ]
        assert len(events) == 1, "a retry is not an incident; an outage is"
        assert events[0][1] == "error", "not being served is the thing the user feels"
        first = events[0][2]
        assert first["reason"] == "unhealable"
        assert first["port"] == PORT_A
        assert isinstance(first["delay"], float)

    async def test_the_recovery_carries_the_outage_duration_and_its_cost(
        self, monkeypatch, fast_backoff, captured_lifecycle
    ):
        """The closing point. Without it the opening one is a leak in the data:
        every episode that ever recovered looks identical to one still running.
        """
        # Two failed retries, then a recovery, then the next generation's first
        # ask ends the drive before a second episode can open.
        _heal_counter(monkeypatch, answers=[None, None, PORT_B], stop_after=4)

        with anyio.fail_after(5), pytest.raises(_EnoughError):
            await proxy_selfheal.drive(**_drive_kwargs(watch=_dead_watch))

        recovered = [
            e for e in captured_lifecycle if e[0] == proxy_selfheal.REACHABLE_EVENT
        ]
        assert len(recovered) == 1
        assert recovered[0][1] == "info", "coming back is not an error"
        fields = recovered[0][2]
        assert fields["port"] == PORT_B
        assert fields["attempts"] == 2
        assert isinstance(fields["outage_seconds"], float)

    async def test_a_recovery_with_no_backoff_ships_no_recovery_event(
        self, monkeypatch, fast_backoff, captured_lifecycle
    ):
        """An ordinary heal is not an outage that ended. ``HEALED_EVENT`` has
        always been the denominator for those, and a second event beside it
        would double-count every successful recovery in the tree."""
        _heal_counter(monkeypatch, answers=[PORT_B], stop_after=3)

        with anyio.fail_after(5), pytest.raises(_EnoughError):
            await proxy_selfheal.drive(**_drive_kwargs(watch=_dead_watch))

        assert not [
            e for e in captured_lifecycle if e[0] == proxy_selfheal.REACHABLE_EVENT
        ]
        assert not [
            e for e in captured_lifecycle if e[0] == proxy_selfheal.UNREACHABLE_EVENT
        ]

    async def test_a_second_episode_ships_its_own_pair(
        self, monkeypatch, fast_backoff, captured_lifecycle
    ):
        """The counter is per EPISODE, so a session that survives two outages
        reports two of each — otherwise 'once' would quietly become 'once ever'.
        """
        # heal 1 fails, 2 recovers (episode one); 3 fails, 4 recovers (episode
        # two); 5 is an ordinary heal with no backoff at all; 6 ends the drive.
        _heal_counter(
            monkeypatch, answers=[None, PORT_B, None, PORT_A, PORT_A], stop_after=6
        )

        with anyio.fail_after(5), pytest.raises(_EnoughError):
            await proxy_selfheal.drive(**_drive_kwargs(watch=_dead_watch))

        names = [
            e[0]
            for e in captured_lifecycle
            if e[0]
            in (proxy_selfheal.UNREACHABLE_EVENT, proxy_selfheal.REACHABLE_EVENT)
        ]
        assert names == [
            proxy_selfheal.UNREACHABLE_EVENT,
            proxy_selfheal.REACHABLE_EVENT,
            proxy_selfheal.UNREACHABLE_EVENT,
            proxy_selfheal.REACHABLE_EVENT,
        ]

    async def test_a_flap_is_reported_as_flapping(
        self, monkeypatch, fast_backoff, captured_lifecycle
    ):
        _heal_counter(
            monkeypatch,
            answers=[PORT_A],
            stop_after=proxy_selfheal.MAX_CONSECUTIVE_HEALS + 2,
        )

        with anyio.fail_after(5), pytest.raises(_EnoughError):
            await proxy_selfheal.drive(**_drive_kwargs(watch=_dead_watch))

        reasons = {
            e[2]["reason"]
            for e in captured_lifecycle
            if e[0] == proxy_selfheal.UNREACHABLE_EVENT
        }
        assert "flapping" in reasons

    def test_the_retired_event_name_is_gone(self):
        """``TEARDOWN_EVENT`` described a thing that no longer happens. A report
        whose name outlives its meaning is worse than no report, so it is
        renamed rather than kept as an alias."""
        assert not hasattr(proxy_selfheal, "TEARDOWN_EVENT")
        assert (
            proxy_selfheal.UNREACHABLE_EVENT == "proxy: backend unreachable, retrying"
        )
        assert proxy_selfheal.REACHABLE_EVENT == "proxy: backend reachable again"


class TestTheProxyKeepsItsStdioLegOpen:
    async def test_singleton_no_longer_cancels_the_task_group_on_drive_returning(
        self,
    ):
        """``backend_leg``'s two teardown lines are DELETED, not merely
        unreachable: ``drive`` never returning is the contract, and a cancel
        left behind it would be a second exit waiting for a regression to find
        it. Read off the source because there is no other way to observe a line
        that does not run."""
        import inspect

        source = inspect.getsource(singleton._proxy_streams)
        leg = source.split("async def backend_leg():", 1)[1]
        leg = leg.split("async with anyio.create_task_group()", 1)[0]

        assert "tearing down for reconnect" not in leg
        assert "cancel_scope.cancel()" not in leg
