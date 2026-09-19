"""F-889 review M3 — "never exits on its own" must not become "never exits".

F-889 (c) deleted the proxy's exit, because that exit IS the outage: 114 Claude
Code sessions saw "Connection closed" for a backend answering ``initialize`` in
227 ms. The SAME outage's cleanup found **116 stale proxy processes** on the
machine, so a retry loop with nobody attached is the previous defect with the
sign flipped.

The ordinary exit is unchanged — stdin EOF, ``pump_client`` returns,
``_proxy_streams`` cancels everything — and these pins are about the case where
that does not happen. Two claims, and the second is what keeps the first from
becoming a new way to disconnect a live session:

1. the proxy ends when the process that STARTED it is gone;
2. it identifies that process by ``(pid, create_time)``, and every uncertainty
   about it resolves to PRESENT.

Hermetic: no process is started, killed or signalled. The one real process asked
about is this test's own.
"""

from __future__ import annotations

import inspect
import itertools
import os

import anyio
import psutil
import pytest

from stealth_chrome_devtools_mcp.embedded import client_presence, singleton


class _FakeProc:
    """One node of a fake process tree. Only the four things ``capture`` asks."""

    def __init__(self, pid, name, cmdline, parent=None, create_time=None, refuse=False):
        self.pid = pid
        self._name = name
        self._cmdline = list(cmdline)
        self._parent = parent
        self._create_time = float(pid) if create_time is None else create_time
        self._refuse = refuse

    def name(self):
        if self._refuse:
            raise psutil.AccessDenied(self.pid)
        return self._name

    def cmdline(self):
        if self._refuse:
            raise psutil.AccessDenied(self.pid)
        return list(self._cmdline)

    def parent(self):
        return self._parent

    def create_time(self):
        return self._create_time


def _tree(monkeypatch, *chain: _FakeProc) -> _FakeProc:
    """Wire ``chain`` leaf-first into a tree ``capture`` will walk, and make the
    leaf this process. Returns the leaf."""
    for child, parent in itertools.pairwise(chain):
        child._parent = parent
    table = {proc.pid: proc for proc in chain}
    monkeypatch.setattr(client_presence.os, "getpid", lambda: chain[0].pid)
    monkeypatch.setattr(client_presence.psutil, "Process", lambda pid: table[pid])
    return chain[0]


#: The proxy's own command line in the fake trees below — the shape a venv
#: python trampoline repeats verbatim.
_OUR_CMDLINE = [r"C:\w\.venv\Scripts\python.exe", "-m", "stealth_chrome_devtools_mcp"]


class TestIdentifyingTheClient:
    def test_capture_names_a_live_ancestor_that_is_not_a_shim_of_ours(self):
        """Against the REAL tree this test is running in. It cannot assert WHICH
        ancestor — that depends on how the suite was launched — only that the
        answer is one of them and is not one of the launchers the walk exists to
        step over (under ``uv run`` the direct parent IS ``uv.exe``)."""
        token = client_presence.capture()

        assert token is not None
        ancestors = {
            (p.pid, p.create_time()) for p in psutil.Process(os.getpid()).parents()
        }
        assert token in ancestors
        assert (
            psutil.Process(token[0]).name().lower()
            not in client_presence._LAUNCHER_NAMES
        )

    def test_the_captured_client_is_present(self):
        assert client_presence.present(client_presence.capture()) is True

    def test_it_walks_past_the_uv_trampoline_to_the_client(self, monkeypatch):
        """The measured Windows chain (F-889 review N2), ancestor by ancestor::

            python.exe  <- this proxy
            python.exe  <- the venv trampoline: the SAME command line
            uv.exe      <- `uv run python -c ...`, waiting on its child
            pwsh.exe    <- the client
            claude.exe

        Both shims outlive us by construction — the trampoline is our own
        process's launcher and ``uv`` waits for it — so naming either one is
        naming something that can never be seen to go away first. That is why
        the exit "missed its population": 116 stale proxies, every one of them
        with a live ``uv`` above it.
        """
        _tree(
            monkeypatch,
            _FakeProc(148200, "python.exe", _OUR_CMDLINE),
            _FakeProc(52904, "python.exe", _OUR_CMDLINE),
            _FakeProc(163492, "uv.exe", ["uv", "run", "python", "-c", "..."]),
            _FakeProc(144992, "pwsh.exe", ["pwsh", "-NoProfile"]),
            _FakeProc(60444, "claude.exe", ["claude"]),
        )

        assert client_presence.capture() == (144992, 144992.0)

    def test_it_walks_past_the_console_script_redirector(self, monkeypatch):
        """``stealth-chrome-devtools-mcp.exe`` is a redirector that re-execs the
        real interpreter (F-866's paragraph, from the other side): it is one of
        OURS, so it is never the client."""
        _tree(
            monkeypatch,
            _FakeProc(10, "python.exe", _OUR_CMDLINE),
            _FakeProc(11, "stealth-chrome-devtools-mcp.exe", ["stealth-…-mcp.exe"]),
            _FakeProc(12, "node", ["node", "/opt/claude/cli.js"]),
        )

        assert client_presence.capture() == (12, 12.0)

    def test_the_posix_shape_walks_past_uvx(self, monkeypatch):
        """No trampoline on POSIX — ``uvx`` spawns the interpreter directly —
        and ``uvx`` waits, so it is the one shim to step over there."""
        _tree(
            monkeypatch,
            _FakeProc(10, "python3.14", ["/home/u/.venv/bin/python3.14", "-m", "x"]),
            _FakeProc(11, "uvx", ["uvx", "stealth-chrome-devtools-mcp"]),
            _FakeProc(12, "node", ["node", "/opt/claude/cli.js"]),
        )

        assert client_presence.capture() == (12, 12.0)

    def test_a_python_running_our_package_is_never_the_client(self, monkeypatch):
        """The third clause: a shim we did not name, spelled as an interpreter
        with our package on its command line. Its cmdline is not byte-identical
        to ours, so the trampoline test alone would stop here."""
        _tree(
            monkeypatch,
            _FakeProc(10, "python.exe", _OUR_CMDLINE),
            _FakeProc(
                11,
                "python.exe",
                [
                    "python.exe",
                    "-c",
                    "import stealth_chrome_devtools_mcp as m; m.main()",
                ],
            ),
            _FakeProc(12, "claude.exe", ["claude"]),
        )

        assert client_presence.capture() == (12, 12.0)

    def test_the_first_ancestor_that_is_nobodys_shim_is_taken_as_is(self, monkeypatch):
        """The walk stops at the FIRST non-shim and never keeps climbing: a
        terminal above the client outlives it, and naming it would be the same
        miss by a longer route."""
        _tree(
            monkeypatch,
            _FakeProc(10, "python.exe", _OUR_CMDLINE),
            _FakeProc(11, "claude.exe", ["claude"]),
            _FakeProc(12, "pwsh.exe", ["pwsh"]),
        )

        assert client_presence.capture() == (11, 11.0)

    def test_a_walk_that_does_not_settle_is_unknown(self, monkeypatch):
        """Ambiguity resolves to "presence unknown", which :func:`present` reads
        as PRESENT — so this proxy never exits on this ground. A bounded walk
        that answered anyway would be guessing at which ancestor is the client,
        and the cost of guessing wrong is a disconnected live session."""
        chain = [_FakeProc(100, "python.exe", _OUR_CMDLINE)]
        chain += [_FakeProc(101 + i, "uv.exe", ["uv", "run"]) for i in range(20)]
        _tree(monkeypatch, *chain)

        assert client_presence.capture() is None

    def test_an_ancestor_that_will_not_be_read_is_unknown(self, monkeypatch):
        """Same direction, and the same reason: a shim we cannot classify is not
        a client we may name."""
        _tree(
            monkeypatch,
            _FakeProc(10, "python.exe", _OUR_CMDLINE),
            _FakeProc(11, "uv.exe", ["uv", "run"]),
            _FakeProc(12, "?", [], refuse=True),
        )

        assert client_presence.capture() is None

    def test_no_parent_at_all_is_unknown(self, monkeypatch):
        _tree(monkeypatch, _FakeProc(10, "python.exe", _OUR_CMDLINE))

        assert client_presence.capture() is None

    def test_a_pid_that_is_not_running_is_gone(self):
        assert client_presence.present((_unused_pid(), 1.0)) is False

    def test_a_recycled_pid_is_gone_not_present(self):
        """THE reason the token is a pair. This process exists, but not with
        that creation time, so it is a different process wearing the same
        number — and reading it as our client would keep a stranded proxy alive
        forever."""
        me = psutil.Process(os.getpid())

        assert client_presence.present((me.pid, me.create_time() - 1.0)) is False

    def test_an_unaskable_parent_is_present(self):
        """Fail-open, and not as a judgement call: a false 'gone' disconnects a
        live session, while a false 'present' costs one idle process that the
        client's next EOF collects anyway."""
        assert client_presence.present(None) is True

    def test_a_psutil_refusal_is_present(self, monkeypatch):
        def refuse(_pid):
            raise psutil.AccessDenied(_pid)

        monkeypatch.setattr(client_presence.psutil, "Process", refuse)

        assert client_presence.present((os.getpid(), 1.0)) is True

    def test_capture_that_cannot_ask_answers_none_rather_than_raising(
        self, monkeypatch
    ):
        def refuse(_pid):
            raise psutil.AccessDenied(_pid)

        monkeypatch.setattr(client_presence.psutil, "Process", refuse)

        assert client_presence.capture() is None


class TestTheWatch:
    async def test_it_returns_once_the_client_is_gone(self, monkeypatch):
        answers = iter([True, True, False])
        monkeypatch.setattr(
            client_presence, "present", lambda _token: next(answers, False)
        )

        with anyio.fail_after(5):
            await client_presence.await_gone((4242, 1.0), interval=0.001)

    async def test_it_never_returns_while_the_client_is_there(self, monkeypatch):
        monkeypatch.setattr(client_presence, "present", lambda _token: True)

        with pytest.raises(TimeoutError), anyio.fail_after(0.05):
            await client_presence.await_gone((4242, 1.0), interval=0.001)

    async def test_with_nobody_to_name_it_never_returns(self):
        """A proxy that could not identify its client must behave exactly as it
        did before this existed — i.e. end on EOF and on nothing else."""
        with pytest.raises(TimeoutError), anyio.fail_after(0.05):
            await client_presence.await_gone(None, interval=0.001)


class TestTheWiring:
    def test_the_proxy_watches_its_client_and_ends_when_it_is_gone(self):
        """Read off the source, because the alternative is orphaning a real
        process to observe it. The capture is at START — asked later it would
        describe whatever we were reparented to, which on POSIX is init and is
        immortal, so the check would stop working exactly when it is needed."""
        source = inspect.getsource(singleton._proxy_streams)

        assert "client_presence.capture()" in source
        watch = source.split("async def client_watch():", 1)[1]
        watch = watch.split("async def backend_leg():", 1)[0]
        assert "await_gone" in watch
        assert "cancel_scope.cancel()" in watch

    def test_the_backend_leg_still_has_no_exit_of_its_own(self):
        """The M3 addition must not reintroduce F-889 (c)'s defect by the other
        door: ``backend_leg`` still cannot end this process over a BACKEND.
        (``test_proxy_retry_forever`` holds the same line; it is stated twice
        because the two findings pull in opposite directions.)"""
        source = inspect.getsource(singleton._proxy_streams)
        leg = source.split("async def backend_leg():", 1)[1]
        leg = leg.split("async with anyio.create_task_group()", 1)[0]

        assert "cancel_scope.cancel()" not in leg


def _unused_pid() -> int:
    """A pid no process on this machine holds. Never signalled, only asked
    about."""
    live = set(psutil.pids())
    for candidate in range(999_000, 1_000_000):
        if candidate not in live:
            return candidate
    raise AssertionError("no free pid to name")
