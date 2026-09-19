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
import os

import anyio
import psutil
import pytest

from stealth_chrome_devtools_mcp.embedded import client_presence, singleton


class TestIdentifyingTheClient:
    def test_capture_names_our_own_parent_as_a_pid_and_a_create_time(self):
        token = client_presence.capture()
        parent = psutil.Process(os.getpid()).parent()

        assert token == (parent.pid, parent.create_time())

    def test_our_own_parent_is_present(self):
        assert client_presence.present(client_presence.capture()) is True

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
