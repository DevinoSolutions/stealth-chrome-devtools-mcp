"""T-1665 -- a disposable clone is probed, and its FIRST miss is captured.

On 2026-10-24 three disposable clones stayed frozen for 17 h with no probe, no
capture and no heal. ``clone_probe.CloneProbe`` probes them (capture only) and, at the
first miss of an incident, records: a fresh ``Browser.getVersion`` and this backend's
own, whether the connection's listener task is alive, and how many
``Fetch.requestPaused`` handlers are still running.

Hermetic: the probe, both ``Browser.getVersion`` checks and the paused count are
keyword seams; the listener tasks are REAL asyncio tasks, and the one nodriver object
is a real ``Connection`` (so a rename of ``_listener_task`` in a nodriver bump fails
here, not silently in production).
"""

import asyncio
from types import SimpleNamespace

import nodriver as uc
import pytest
from nodriver.core.connection import Connection

from stealth_chrome_devtools_mcp.embedded import clone_probe
from stealth_chrome_devtools_mcp.embedded.dynamic_hook_system import (
    DynamicHookSystem,
)

CLONE = "i-clone"


class _Log:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def log_info(self, _component, _method, message, *_a, **_k):
        self.lines.append(("info", str(message)))

    def log_warning(self, _component, _method, message, *_a, **_k):
        self.lines.append(("warning", str(message)))

    def log_error(self, _component, _method, error, *_a, **_k):
        self.lines.append(("error", f"{type(error).__name__}: {error}"))

    def at(self, level: str) -> list[str]:
        return [message for lvl, message in self.lines if lvl == level]


@pytest.fixture()
def log(monkeypatch):
    recorder = _Log()
    monkeypatch.setattr(clone_probe, "debug_logger", recorder)
    return recorder


async def _idle() -> None:
    await asyncio.Event().wait()


async def _dies() -> None:
    raise KeyError("sameParty")


async def _ends() -> None:
    return None


async def _running_task() -> asyncio.Task[None]:
    task = asyncio.create_task(_idle())
    await asyncio.sleep(0)
    return task


async def _finished(coroutine) -> asyncio.Task[None]:
    task = asyncio.create_task(coroutine)
    await asyncio.wait([task])
    return task


def _browser(listener=None, *, close_code=None, tabs=()):
    return SimpleNamespace(
        websocket_url="ws://127.0.0.1:1/devtools/browser/clone",
        _listener_task=listener,
        socket=SimpleNamespace(close_code=close_code),
        tabs=list(tabs),
    )


class _Rig:
    """One probe round's collaborators; every call is recorded."""

    def __init__(self) -> None:
        self.instances: dict[str, dict[str, object]] = {}
        self.misses: dict[str, list[bool]] = {}  # True = this probe fails
        self.fresh_answers = False
        self.own_answers = True
        self.paused = 0
        self.fresh_asked: list[str | None] = []
        self.own_asked: list[object] = []

    def add(self, browser, instance_id: str = CLONE) -> dict[str, object]:
        self.instances[instance_id] = {"browser": browser}
        return self.instances[instance_id]

    async def get_instance(self, instance_id: str):
        return self.instances.get(instance_id)

    async def failure_of(self, browser, steps) -> str | None:
        steps.append("evaluate")
        iid = next(i for i, d in self.instances.items() if d["browser"] is browser)
        queue = self.misses.get(iid, [])
        return "TimeoutError at evaluate" if queue and queue.pop(0) else None

    async def fresh(self, url: str | None) -> None:
        self.fresh_asked.append(url)
        if not self.fresh_answers:
            raise ConnectionError("no")

    async def own(self, browser) -> None:
        self.own_asked.append(browser)
        if not self.own_answers:
            raise ConnectionError("no")

    def probe(self, **overrides) -> clone_probe.CloneProbe:
        kwargs = {"paused": lambda _iid: self.paused}
        kwargs.update(overrides)
        return clone_probe.CloneProbe(
            self.get_instance, self.failure_of, self.fresh, self.own, **kwargs
        )


# ---------------------------------------------------------------------------
# 1. the listener fact
# ---------------------------------------------------------------------------


class TestListenerState:
    async def test_a_running_listener_with_an_open_socket(self):
        task = await _running_task()
        try:
            assert (
                clone_probe.listener_state(_browser(task))
                == "browser=running socket=open tabs=0/0"
            )
        finally:
            task.cancel()

    async def test_a_listener_that_ended_is_not_a_listener_that_died(self):
        # nodriver 0.50 ends the task WITHOUT an error when the socket closes
        task = await _finished(_ends())

        shown = clone_probe.listener_state(_browser(task, close_code=1006))

        assert shown == "browser=ended socket=closed tabs=0/0"

    async def test_a_listener_that_died_names_the_error_type_only(self):
        task = await _finished(_dies())

        shown = clone_probe.listener_state(_browser(task))

        assert "browser=died(KeyError)" in shown
        assert "sameParty" not in shown

    async def test_a_cancelled_listener(self):
        task = await _running_task()
        task.cancel()
        await asyncio.wait([task])

        assert "browser=cancelled" in clone_probe.listener_state(_browser(task))

    async def test_the_page_connections_listeners_are_counted(self):
        running = await _running_task()
        ended = await _finished(_ends())
        try:
            tabs = [
                SimpleNamespace(_listener_task=running),
                SimpleNamespace(_listener_task=ended),
                SimpleNamespace(_listener_task=None),
            ]

            shown = clone_probe.listener_state(_browser(running, tabs=tabs))

            assert shown.endswith("tabs=1/3")
        finally:
            running.cancel()

    def test_a_real_nodriver_connection_has_the_attribute_the_fact_reads(self):
        connection = Connection("ws://127.0.0.1:1/devtools/x")

        assert connection._listener_task is None
        assert (
            clone_probe.listener_state(connection)
            == "browser=none socket=none tabs=0/0"
        )


# ---------------------------------------------------------------------------
# 2. the first miss of an incident
# ---------------------------------------------------------------------------


class TestTheFirstMissIsCaptured:
    async def test_one_error_line_carries_the_three_facts(self, log):
        rig = _Rig()
        task = await _running_task()
        browser = _browser(task)
        rig.add(browser)
        rig.misses[CLONE] = [True]
        rig.paused = 3
        probe = rig.probe()

        await probe.check([CLONE])
        task.cancel()

        (error,) = log.at("error")
        assert error.startswith("FrozenCloneError: i-clone: clone probe failed")
        assert "TimeoutError at evaluate" in error
        assert "fresh_cdp(Browser.getVersion): refused (ConnectionError)" in error
        assert "own_cdp(Browser.getVersion): answered in " in error
        assert "listener: browser=running socket=open tabs=0/0" in error
        assert "paused_fetch: 3" in error
        assert "steps(" in error
        assert rig.fresh_asked == [browser.websocket_url]
        assert rig.own_asked == [browser]
        assert probe.records[CLONE] in error

    async def test_a_dead_backend_connection_reads_as_fresh_answers_own_does_not(
        self, log
    ):
        rig = _Rig()
        rig.add(_browser(await _finished(_dies())))
        rig.misses[CLONE] = [True]
        rig.fresh_answers, rig.own_answers = True, False

        await rig.probe().check([CLONE])

        (error,) = log.at("error")
        assert "fresh_cdp(Browser.getVersion): answered in " in error
        assert "own_cdp(Browser.getVersion): refused (ConnectionError)" in error
        assert "browser=died(KeyError)" in error

    async def test_it_is_one_shot_per_incident(self, log):
        rig = _Rig()
        rig.add(_browser())
        rig.misses[CLONE] = [True, True, True]
        probe = rig.probe()

        for _ in range(3):
            await probe.check([CLONE])

        assert len(log.at("error")) == 1
        assert len(rig.fresh_asked) == 1  # the later misses are not re-captured

    async def test_a_probe_that_passes_ends_the_incident_and_the_next_is_new(self, log):
        rig = _Rig()
        rig.add(_browser())
        rig.misses[CLONE] = [True, False, True]
        probe = rig.probe()

        await probe.check([CLONE])
        await probe.check([CLONE])  # passes
        assert CLONE not in probe.records
        assert log.at("info") == [f"{CLONE} answers probes again"]
        await probe.check([CLONE])  # a second incident

        assert len(log.at("error")) == 2

    async def test_a_clone_that_answers_leaves_no_trace(self, log):
        rig = _Rig()
        rig.add(_browser())

        probe = rig.probe()
        await probe.check([CLONE])

        assert log.lines == [] and probe.records == {}
        assert rig.fresh_asked == [] and rig.own_asked == []

    async def test_a_clone_that_is_gone_is_forgotten(self, log):
        rig = _Rig()
        rig.add(_browser())
        rig.misses[CLONE] = [True]
        probe = rig.probe()
        await probe.check([CLONE])
        assert CLONE in probe.records

        await probe.check([])

        assert probe.records == {}

    async def test_a_clone_closed_while_it_was_probed_gets_no_verdict(self, log):
        rig = _Rig()
        rig.add(_browser())
        rig.misses[CLONE] = [True]
        original = rig.failure_of

        async def closing(browser, steps):
            result = await original(browser, steps)
            del rig.instances[CLONE]
            return result

        probe = rig.probe()
        probe._failure_of = closing

        await probe.check([CLONE])

        assert log.at("error") == [] and probe.records == {}

    async def test_a_fact_that_cannot_be_read_is_named_and_the_rest_stay(self, log):
        rig = _Rig()
        rig.add(_browser())
        rig.misses[CLONE] = [True]

        def broken(_iid):
            raise RuntimeError("hooks")

        await rig.probe(paused=broken).check([CLONE])

        (error,) = log.at("error")
        assert "paused_fetch: unavailable (RuntimeError)" in error
        assert "listener: browser=none" in error

    async def test_one_clone_raising_does_not_cost_the_others_their_round(self, log):
        rig = _Rig()
        rig.add(_browser(), "i-a")
        rig.add(_browser(), "i-b")
        rig.misses["i-b"] = [True]
        calls = []
        original = rig.failure_of

        async def failing(browser, steps):
            calls.append(browser)
            if len(calls) == 1:
                raise RuntimeError("probe seam broke")
            return await original(browser, steps)

        probe = rig.probe()
        probe._failure_of = failing

        await probe.check(["i-a", "i-b"])

        assert len(calls) == 2
        assert len(log.at("error")) == 1 and "i-b" in log.at("error")[0]
        assert log.at("warning") == ["i-a: RuntimeError"]


# ---------------------------------------------------------------------------
# 3. the count of paused Fetch requests
# ---------------------------------------------------------------------------


def _paused_event():
    return SimpleNamespace(
        request_id=uc.cdp.fetch.RequestId("r1"),
        request=SimpleNamespace(url="https://example.invalid/", method="GET"),
        resource_type="Document",
        response_status_code=None,
        response_error_reason=None,
    )


class TestPausedFetchCount:
    async def test_a_handler_in_flight_counts_until_it_finishes(self, monkeypatch):
        system = DynamicHookSystem()
        release = asyncio.Event()
        entered = asyncio.Event()

        async def hooks(_tab, _request, _event=None):
            entered.set()
            await release.wait()

        monkeypatch.setattr(system, "_process_request_hooks", hooks)
        assert system.paused_in_flight(CLONE) == 0

        tasks = [
            asyncio.create_task(system._on_request_paused(None, _paused_event(), CLONE))
            for _ in range(2)
        ]
        await entered.wait()
        await asyncio.sleep(0)

        assert system.paused_in_flight(CLONE) == 2
        assert system.paused_in_flight("i-other") == 0
        release.set()
        await asyncio.gather(*tasks)
        assert system.paused_in_flight(CLONE) == 0
        assert system._paused_in_flight == {}

    async def test_a_handler_that_fails_is_not_counted_afterwards(self, monkeypatch):
        system = DynamicHookSystem()
        sent = []

        async def hooks(_tab, _request, _event=None):
            raise RuntimeError("hook blew up")

        class Tab:
            async def send(self, command):
                sent.append(command)

        monkeypatch.setattr(system, "_process_request_hooks", hooks)

        await system._on_request_paused(Tab(), _paused_event(), CLONE)

        assert len(sent) == 1  # the request was continued, as before
        assert system.paused_in_flight(CLONE) == 0

    async def test_a_cancelled_handler_is_not_counted_afterwards(self, monkeypatch):
        system = DynamicHookSystem()
        entered = asyncio.Event()

        async def hooks(_tab, _request, _event=None):
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(system, "_process_request_hooks", hooks)
        task = asyncio.create_task(
            system._on_request_paused(None, _paused_event(), CLONE)
        )
        await entered.wait()
        assert system.paused_in_flight(CLONE) == 1

        task.cancel()
        await asyncio.wait([task])

        assert system.paused_in_flight(CLONE) == 0

    async def test_the_default_seam_reads_the_one_hook_system(self, log, monkeypatch):
        from stealth_chrome_devtools_mcp.embedded.dynamic_hook_system import (
            dynamic_hook_system,
        )

        rig = _Rig()
        rig.add(_browser())
        rig.misses[CLONE] = [True]
        monkeypatch.setattr(dynamic_hook_system, "_paused_in_flight", {CLONE: 7})

        probe = clone_probe.CloneProbe(
            rig.get_instance, rig.failure_of, rig.fresh, rig.own
        )
        await probe.check([CLONE])

        assert "paused_fetch: 7" in log.at("error")[0]
