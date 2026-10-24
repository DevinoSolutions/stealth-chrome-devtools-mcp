"""F-1009 — the contracts of ``devtools_port`` that the launch doors share, each
stated once. The end-to-end pins live in ``test_devtools_port_identity.py`` (over
nodriver's real ``Browser.start``) and ``test_desktop_launch.py`` (the delegated
door)."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import psutil
import pytest
from nodriver import cdp
from nodriver.core.connection import Connection, ProtocolException

from fakes import forbid_real_process_kills
from stealth_chrome_devtools_mcp.embedded import devtools_port
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

#: Far above any pid a machine hands out, so ``psutil`` finds nothing below them.
LAUNCHED = 3_999_101
OTHER = 3_999_102


class _Connection:
    def __init__(self, processes) -> None:
        self.processes = processes
        self.disconnected = False
        self.sent: list[dict] = []
        #: ``"hang"`` never answers; ``("slow", seconds)`` answers after that long;
        #: an exception instance is raised instead.
        self.behaviour: object = None
        #: The request was cancelled rather than left running (T-5899).
        self.send_cancelled = False

    async def send(self, command):
        self.sent.append(next(command))
        try:
            if self.behaviour == "hang":
                await asyncio.Event().wait()
            if isinstance(self.behaviour, tuple) and self.behaviour[0] == "slow":
                await asyncio.sleep(self.behaviour[1])
        except asyncio.CancelledError:
            self.send_cancelled = True
            raise
        if isinstance(self.behaviour, BaseException):
            raise self.behaviour
        return self.processes

    async def disconnect(self):
        self.disconnected = True


@pytest.fixture(autouse=True)
def no_real_process_is_touched(monkeypatch):
    """F-1009 safety net: nothing here may end a REAL process or see the real process table. The
    one test that needs a real ``Process.children`` reads, it never ends one."""
    attempts = forbid_real_process_kills(monkeypatch)
    yield
    assert attempts == []


@pytest.fixture
def logged(monkeypatch):
    """Every ``log_warning`` the module emits, as ``(function, message)``."""
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(
        devtools_port,
        "debug_logger",
        SimpleNamespace(log_warning=lambda _c, fn, msg: seen.append((fn, msg))),
    )
    return seen


ANSWER_PATIENCE_SECONDS = 60.0


def _browser(*processes: tuple[str, int]) -> SimpleNamespace:
    return SimpleNamespace(
        connection=_Connection(
            [cdp.system_info.ProcessInfo(t, i, 0.0) for t, i in processes]
        )
    )


def _short_waits(monkeypatch, *, patience: float = 1.0) -> None:
    """Scale the identity waits down so the pins run in a fraction of a second.

    A pin that EXPECTS an answer passes ``patience=ANSWER_PATIENCE_SECONDS``: it ends
    the moment the answer lands, so the long patience costs nothing, and a loaded host
    that stalls the loop past a 1 s patience before the first throwaway is asked (T-7518)
    cannot turn the answer into F-1009. A pin that expects the REFUSAL keeps the short
    default, which is what makes it end."""
    monkeypatch.setattr(devtools_port, "IDENTITY_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(devtools_port, "IDENTITY_POLL_SECONDS", 0.02)
    monkeypatch.setattr(devtools_port, "IDENTITY_PATIENCE_SECONDS", patience)


@pytest.fixture
def throwaways(monkeypatch):
    """nodriver's ``Connection`` as ``devtools_port`` builds a throwaway one: each is a
    ``_Connection`` answering OURS unless ``plan`` says otherwise (consumed in order)."""
    made: list[_Connection] = []
    plan: list[object] = []

    def build(url):
        connection = _Connection(
            [cdp.system_info.ProcessInfo("browser", os.getpid(), 0.0)]
        )
        connection.websocket_url = url
        connection.behaviour = plan.pop(0) if plan else None
        made.append(connection)
        return connection

    monkeypatch.setattr("nodriver.core.connection.Connection", build)
    return SimpleNamespace(made=made, plan=plan)


def _launcher_exits_after_two(monkeypatch, throwaways) -> None:
    """End the wait once two throwaways exist, not after a wall-clock patience: on a
    loaded host one late wake of the loop spends a 0.3 s patience after a single ask
    (T-7014 pre-push: 1 throwaway made), so a count pinned to the clock flakes."""
    monkeypatch.setattr(devtools_port, "IDENTITY_PATIENCE_SECONDS", 60.0)
    monkeypatch.setattr(devtools_port, "_running", lambda pid: len(throwaways.made) < 2)


def _lost_browser(*processes: tuple[str, int]) -> SimpleNamespace:
    """A browser whose own connection loses the first query (never answers)."""
    browser = _browser(*processes)
    browser.connection.behaviour = "hang"
    browser.connection.websocket_url = "ws://127.0.0.1:1/devtools/browser/x"
    return browser


async def _until(condition) -> None:
    """Wait for *condition*, bounded: a pin for a hang must fail, not hang."""

    async def poll() -> None:
        while not condition():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), 5.0)


class TestOwnedPids:
    def test_a_pid_with_nothing_below_it_owns_itself(self):
        assert devtools_port.owned_pids(LAUNCHED) == {LAUNCHED}

    def test_a_wrapper_owns_what_it_forked(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            assert child.pid in devtools_port.owned_pids(os.getpid())
        finally:
            child.terminate()
            child.wait(timeout=10)

    def test_a_process_psutil_cannot_read_is_just_itself_and_says_so(
        self, monkeypatch, logged
    ):
        def denied(pid):
            raise psutil.AccessDenied(pid)

        monkeypatch.setattr(psutil, "Process", denied)
        assert devtools_port.owned_pids(LAUNCHED) == {LAUNCHED}
        assert [fn for fn, _ in logged] == ["owned_pids"]
        assert "AccessDenied" in logged[0][1]


@pytest.mark.asyncio
class TestConfirmOurs:
    async def test_the_launched_pid_passes_and_asks_the_read_only_query(self):
        browser = _browser(("gpu", 5), ("browser", LAUNCHED))
        await devtools_port.confirm_ours(browser, LAUNCHED)
        assert [c["method"] for c in browser.connection.sent] == [
            "SystemInfo.getProcessInfo"
        ]
        assert not browser.connection.disconnected

    async def test_another_browser_is_refused_naming_both_and_dropped(self):
        browser = _browser(("browser", OTHER))
        with pytest.raises(ToolError, match="F-1009") as err:
            await devtools_port.confirm_ours(browser, LAUNCHED)
        assert f"process {OTHER}" in str(err.value)
        assert f"pid {LAUNCHED}" in str(err.value)
        assert browser.connection.disconnected

    async def test_a_descendant_of_the_launched_pid_passes(self, monkeypatch):
        monkeypatch.setattr(devtools_port, "owned_pids", lambda pid: {pid, OTHER})
        browser = _browser(("browser", OTHER))
        await devtools_port.confirm_ours(browser, LAUNCHED)
        assert not browser.connection.disconnected

    async def test_no_browser_process_in_the_answer_is_allowed_but_logged(self, logged):
        browser = _browser(("gpu", 5), ("renderer", 6))
        await devtools_port.confirm_ours(browser, LAUNCHED)
        assert not browser.connection.disconnected
        assert [fn for fn, _ in logged] == ["confirm_ours"]
        assert "no `browser` process" in logged[0][1]

    async def test_no_launched_pid_means_nothing_to_compare_and_is_logged(self, logged):
        browser = _browser(("browser", OTHER))
        await devtools_port.confirm_ours(browser, None)
        assert browser.connection.sent == []
        assert [fn for fn, _ in logged] == ["confirm_ours"]
        assert "no launched pid" in logged[0][1]

    async def test_a_browser_without_the_query_is_refused_and_dropped(self):
        """An Electron or CEF app squatting on the port has no SystemInfo domain;
        real Chrome always answers. Not being able to answer is not being ours."""
        browser = _browser()
        browser.connection.behaviour = ProtocolException(
            {"code": -32601, "message": "'SystemInfo.getProcessInfo' wasn't found"}
        )
        with pytest.raises(ToolError, match="cannot identify its process"):
            await devtools_port.confirm_ours(browser, LAUNCHED)
        assert browser.connection.disconnected

    async def test_a_slow_answer_from_our_own_running_launch_is_accepted(
        self, monkeypatch, logged
    ):
        """T-5899: a healthy Chrome on a starved runner answered after the floor
        and the spawn of OUR OWN browser was refused. While the pid we launched is
        running, the wait goes on past the floor (here 0.05 s) and the late answer
        is the proof, as ever; the delay is logged so a slow host is visible."""
        _short_waits(monkeypatch, patience=ANSWER_PATIENCE_SECONDS)
        me = os.getpid()
        browser = _browser(("browser", me))
        browser.connection.behaviour = ("slow", 0.4)
        await devtools_port.confirm_ours(browser, me)
        assert not browser.connection.disconnected
        assert [fn for fn, _ in logged] == ["confirm_ours"]
        assert "to say which process it is" in logged[0][1]

    async def test_a_stranger_answering_late_is_still_refused(self, monkeypatch):
        """Patience is not trust: the pid in the answer decides, however late."""
        _short_waits(monkeypatch, patience=ANSWER_PATIENCE_SECONDS)
        browser = _browser(("browser", OTHER))
        browser.connection.behaviour = ("slow", 0.4)
        with pytest.raises(ToolError, match=f"process {OTHER}"):
            await devtools_port.confirm_ours(browser, os.getpid())
        assert browser.connection.disconnected

    async def test_silence_past_the_patience_is_refused_and_the_request_ended(
        self, monkeypatch
    ):
        _short_waits(monkeypatch)
        browser = _browser()
        browser.connection.behaviour = "hang"
        with pytest.raises(ToolError, match="did not say which process it is"):
            await asyncio.wait_for(
                devtools_port.confirm_ours(browser, os.getpid()), 10.0
            )
        assert browser.connection.disconnected
        assert browser.connection.send_cancelled

    async def test_a_launcher_that_is_gone_gets_no_wait_past_the_floor(
        self, monkeypatch
    ):
        """Only a RUNNING launch earns the patience. A pid that is gone (here one no
        machine hands out) is held to the floor; the outer bound fails this pin
        if the patience is given anyway."""
        _short_waits(monkeypatch)
        monkeypatch.setattr(devtools_port, "IDENTITY_PATIENCE_SECONDS", 60.0)
        browser = _browser()
        browser.connection.behaviour = "hang"
        with pytest.raises(ToolError, match="did not say which process it is"):
            await asyncio.wait_for(devtools_port.confirm_ours(browser, LAUNCHED), 10.0)
        assert browser.connection.disconnected

    async def test_the_refusal_names_a_separate_pid_namespace(self):
        browser = _browser(("browser", OTHER))
        with pytest.raises(ToolError, match="flatpak"):
            await devtools_port.confirm_ours(browser, LAUNCHED)

    async def test_a_cancelled_wait_still_drops_the_foreign_socket(self):
        """Cancellation is how a spawn budget or a caller ends the wait; it must
        not leave a websocket open to somebody else's browser."""
        browser = _browser()
        browser.connection.behaviour = "hang"
        task = asyncio.ensure_future(devtools_port.confirm_ours(browser, LAUNCHED))
        while not browser.connection.sent:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert browser.connection.disconnected
        assert [c["method"] for c in browser.connection.sent] == [
            "SystemInfo.getProcessInfo"
        ]

    async def test_a_second_cancel_during_a_slow_drop_still_closes_the_socket(self):
        """The drop runs while the spawn is already being cancelled, and a caller
        that cancels again must not abandon a half-open foreign socket: the
        disconnect is shielded, so it finishes on its own. Without the shield the
        second cancel kills it mid-flight and the socket stays open."""
        browser = _browser()
        browser.connection.behaviour = "hang"
        started = asyncio.Event()

        async def slow_disconnect():
            started.set()
            await asyncio.sleep(0.1)
            browser.connection.disconnected = True

        browser.connection.disconnect = slow_disconnect
        task = asyncio.ensure_future(devtools_port.confirm_ours(browser, LAUNCHED))
        while not browser.connection.sent:
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.3)
        assert browser.connection.disconnected

    async def test_a_disconnect_that_never_returns_cannot_hold_the_refusal_up(
        self, monkeypatch
    ):
        """The drop is bounded by ``DROP_TIMEOUT_SECONDS``: a stuck close of a
        foreign socket must not turn a refusal into a hang. The outer
        ``wait_for`` keeps the pin itself from hanging when the bound is gone."""
        monkeypatch.setattr(devtools_port, "DROP_TIMEOUT_SECONDS", 0.05)
        release = asyncio.Event()
        browser = _browser(("browser", OTHER))

        async def stuck_disconnect():
            await release.wait()

        browser.connection.disconnect = stuck_disconnect
        try:
            with pytest.raises(ToolError, match="not the Chrome this spawn launched"):
                await asyncio.wait_for(
                    devtools_port.confirm_ours(browser, LAUNCHED), 2.0
                )
        finally:
            release.set()


def test_the_process_kill_net_is_armed(monkeypatch):
    """``forbid_real_process_kills`` is the only thing between these tests and a
    real ``terminate()``. The pid is our own, so the assertion has to fire
    BEFORE anything real is ended; a net that is neutered fails here."""
    attempts = forbid_real_process_kills(monkeypatch)

    # Look at the patch before calling through it: a neutered net would make the
    # call below really end this process (rc 15, no summary) instead of failing.
    assert "forbid_real_process_kills" in psutil.Process.terminate.__qualname__
    assert "forbid_real_process_kills" in psutil.Process.kill.__qualname__

    with pytest.raises(AssertionError, match=r"real psutil.Process.terminate"):
        psutil.Process(os.getpid()).terminate()

    assert attempts == ["Process.terminate"]
    assert list(psutil.process_iter()) == []


@pytest.mark.asyncio
class TestAskedAgain:
    """T-7014: a query nodriver lost without raising is asked again, over a throwaway
    connection, and the launch's own connection is never the one dropped."""

    async def test_the_first_answer_counts_and_the_losers_end_cleanly(
        self, monkeypatch, throwaways
    ):
        _short_waits(monkeypatch, patience=ANSWER_PATIENCE_SECONDS)
        browser = _lost_browser(("browser", os.getpid()))
        await asyncio.wait_for(devtools_port.confirm_ours(browser, os.getpid()), 30.0)
        assert not browser.connection.disconnected
        assert browser.connection.send_cancelled
        assert throwaways.made
        assert all(c.disconnected for c in throwaways.made)
        assert {c.websocket_url for c in throwaways.made} == {
            browser.connection.websocket_url
        }

    async def test_a_stranger_among_the_answers_is_refused(self, monkeypatch):
        """Two asks answered by the time the wait ends, ours first: one that names a
        pid that is not ours still refuses, whichever ask it answered."""

        async def both(browser, launched_pid):
            return [
                [cdp.system_info.ProcessInfo("browser", LAUNCHED, 0.0)],
                [cdp.system_info.ProcessInfo("browser", OTHER, 0.0)],
            ]

        monkeypatch.setattr(devtools_port, "_answer", both)
        browser = _browser()
        with pytest.raises(ToolError, match=f"process {OTHER}"):
            await devtools_port.confirm_ours(browser, LAUNCHED)
        assert browser.connection.disconnected

    async def test_a_failed_request_does_not_beat_one_that_answers(
        self, monkeypatch, throwaways, logged
    ):
        _short_waits(monkeypatch, patience=ANSWER_PATIENCE_SECONDS)
        browser = _lost_browser(("browser", os.getpid()))
        browser.connection.behaviour = OSError("died under the request")
        throwaways.plan.append(None)
        await devtools_port.confirm_ours(browser, os.getpid())
        assert not browser.connection.disconnected
        assert any("OSError" in msg for _, msg in logged)

    async def test_a_throwaway_that_cannot_connect_ends_as_the_named_refusal(
        self, monkeypatch, throwaways, logged
    ):
        """A raw OSError from the websocket must not escape: it is skipped and the
        wait ends as the F-1009 ``ToolError``."""
        _short_waits(monkeypatch)
        _launcher_exits_after_two(monkeypatch, throwaways)
        browser = _lost_browser(("browser", os.getpid()))
        throwaways.plan.extend([ConnectionRefusedError("no endpoint")] * 20)
        with pytest.raises(ToolError, match="did not say which process it is"):
            await asyncio.wait_for(
                devtools_port.confirm_ours(browser, os.getpid()), 10.0
            )
        assert throwaways.made
        assert all(c.disconnected for c in throwaways.made)
        assert any("ConnectionRefusedError" in msg for _, msg in logged)

    async def test_silence_ends_every_request_and_every_throwaway(
        self, monkeypatch, throwaways
    ):
        _short_waits(monkeypatch)
        _launcher_exits_after_two(monkeypatch, throwaways)
        browser = _lost_browser()
        throwaways.plan.extend(["hang"] * 20)
        with pytest.raises(ToolError, match="did not say which process it is"):
            await asyncio.wait_for(
                devtools_port.confirm_ours(browser, os.getpid()), 10.0
            )
        assert browser.connection.send_cancelled
        assert len(throwaways.made) >= 2
        assert all(c.disconnected and c.send_cancelled for c in throwaways.made)

    async def test_a_cancelled_wait_leaves_no_throwaway_open(
        self, monkeypatch, throwaways
    ):
        _short_waits(monkeypatch)
        browser = _lost_browser()
        throwaways.plan.extend(["hang"] * 20)
        task = asyncio.ensure_future(devtools_port.confirm_ours(browser, os.getpid()))
        await _until(lambda: throwaways.made and throwaways.made[0].sent)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert browser.connection.disconnected
        assert all(c.disconnected for c in throwaways.made)

    async def test_a_connection_without_an_endpoint_is_asked_once(
        self, monkeypatch, throwaways
    ):
        """No ``websocket_url`` (nothing to open a second connection to): the single
        request is all there is, as before T-7014."""
        _short_waits(monkeypatch)
        monkeypatch.setattr(devtools_port, "IDENTITY_PATIENCE_SECONDS", 0.3)
        browser = _browser()
        browser.connection.behaviour = "hang"
        with pytest.raises(ToolError, match="did not say which process it is"):
            await asyncio.wait_for(
                devtools_port.confirm_ours(browser, os.getpid()), 10.0
            )
        assert throwaways.made == []


class _Websocket:
    """The websocket under a REAL nodriver ``Connection``: records what is sent, and
    delivers whatever the test puts on ``inbox`` to the connection's own listener."""

    close_code = None

    def __init__(self) -> None:
        self.inbox: asyncio.Queue[str] = asyncio.Queue()
        self.sent: list[dict] = []

    async def send(self, text: str) -> None:
        self.sent.append(json.loads(text))

    async def recv(self) -> str:
        return await self.inbox.get()

    async def close(self) -> None:
        self.close_code = 1000


def _process_reply(message_id: int, pid: int) -> str:
    return json.dumps(
        {
            "id": message_id,
            "result": {"processInfo": [{"type": "browser", "id": pid, "cpuTime": 0.0}]},
        }
    )


@pytest.mark.asyncio
class TestTheCancelledFirstAsk:
    async def test_a_late_reply_to_the_cancelled_first_ask_leaves_the_listener_alive(
        self, monkeypatch, throwaways
    ):
        """When a throwaway answers first, the first ask on the launch's own
        connection is CANCELLED while Chrome still has the command. The late reply must
        reach a listener that survives it, or the launch's CDP session dies right
        after the spawn is accepted. That holds only because ``cdp_transport`` shields
        ``Transaction.__await__`` (and guards its delivery): the production import path
        installs it, so this test does not, and the real nodriver ``send`` and
        ``_listener`` run over a fake websocket."""
        from stealth_chrome_devtools_mcp.embedded import (
            cdp_transport,
            tool_runtime,  # noqa: F401  PERMANENT(importing it is what installs the transport patch in production)
        )

        _short_waits(monkeypatch, patience=ANSWER_PATIENCE_SECONDS)
        socket = _Websocket()
        connection = Connection("ws://127.0.0.1:1/devtools/browser/x")
        connection._websocket = socket  # noqa: SLF001  PERMANENT(a fake websocket under the real Connection)
        connection._listener_task = asyncio.ensure_future(connection._listener())  # noqa: SLF001  PERMANENT(the real listener over the fake websocket)
        try:
            await devtools_port.confirm_ours(
                SimpleNamespace(connection=connection), os.getpid()
            )
            assert throwaways.made, "the throwaway never asked"
            (first,) = socket.sent
            assert first["method"] == "SystemInfo.getProcessInfo"
            pending = connection.mapper[first["id"]]
            assert not pending.cancelled(), "the cancelled ask took its Transaction"

            socket.inbox.put_nowait(_process_reply(first["id"], os.getpid()))
            await asyncio.sleep(0.2)
            assert not connection._listener_task.done(), (  # noqa: SLF001  PERMANENT(is the listener alive)
                "the late reply killed the launch's listener"
            )
            assert pending.done()

            following = asyncio.ensure_future(
                connection.send(cdp.system_info.get_process_info())
            )
            await _until(lambda: len(socket.sent) == 2)
            socket.inbox.put_nowait(_process_reply(socket.sent[1]["id"], os.getpid()))
            reply = await asyncio.wait_for(following, 5.0)
            assert reply[0].id_ == os.getpid()
            assert cdp_transport.installed()
        finally:
            connection._listener_task.cancel()  # noqa: SLF001  PERMANENT(end the real listener)
