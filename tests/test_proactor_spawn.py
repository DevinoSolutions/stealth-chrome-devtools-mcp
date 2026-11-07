"""T-15385 — on Windows a child process is created OFF the backend's event loop.

The stock ``ProactorEventLoop`` runs ``CreateProcess`` on the loop thread, and on
the fleet PC each call held the loop for 0.15-2.0 s: every Chrome launch and
every idle-canary round stopped every session on the shared backend. The tests
drive REAL subprocesses on a real Proactor loop. The slow spawn is simulated by
a ``windows_utils.Popen`` that sleeps first (releasing the GIL, as
``CreateProcess`` does), and the same measurement is run against the stock loop
to show it detects the defect.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time

import psutil
import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="the Proactor loop's subprocess path is Windows-only",
)

if sys.platform == "win32":
    from asyncio import windows_events, windows_utils

from stealth_chrome_devtools_mcp.embedded import proactor_spawn

SLOW_SPAWN_SECONDS = 0.5


@pytest.fixture()
def stock(monkeypatch):
    """The stock loop, restored after the test whatever it installed."""
    monkeypatch.setattr(
        windows_events.ProactorEventLoop,
        "_make_subprocess_transport",
        windows_events.ProactorEventLoop._make_subprocess_transport,
    )


@pytest.fixture()
def patched(stock):
    assert proactor_spawn.install() is True


@pytest.fixture()
def slow_spawn(monkeypatch):
    """``Popen`` that takes SLOW_SPAWN_SECONDS before creating the child, off
    the GIL like CreateProcess; records every process it made."""
    real = windows_utils.Popen
    made = []

    def slow(*args, **kwargs):
        time.sleep(SLOW_SPAWN_SECONDS)
        proc = real(*args, **kwargs)
        made.append(proc)
        return proc

    monkeypatch.setattr(windows_utils, "Popen", slow)
    return made


def _run(coro):
    """``asyncio.run`` is a Proactor loop on Windows (3.8+); asserted, since every
    test here is about that loop."""

    async def checked():
        assert isinstance(asyncio.get_running_loop(), windows_events.ProactorEventLoop)
        return await coro

    return asyncio.run(checked())


async def _child(*code_lines, **kwargs):
    return await asyncio.create_subprocess_exec(
        sys.executable, "-c", "\n".join(code_lines), **kwargs
    )


async def _longest_gap_while(coro):
    """Run ``coro`` beside a 10 ms ticker; the longest the ticker waited."""
    gaps = []
    stop = asyncio.Event()

    async def tick():
        last = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(0.01)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    ticker = asyncio.create_task(tick())
    await asyncio.sleep(0.05)  # the ticker is running before the spawn starts
    try:
        result = await coro
    finally:
        stop.set()
        await ticker
    return max(gaps), result


class TestInstall:
    def test_install_is_idempotent(self, stock):
        assert proactor_spawn.install() is True
        assert proactor_spawn.install() is False

    def test_the_backend_installs_it_beside_the_accept_patch(self):
        from pathlib import Path

        source = (
            Path(proactor_spawn.__file__)
            .with_name("server.py")
            .read_text(encoding="utf-8")
        )
        accept = source.index("proactor_accept.install()")
        spawn = source.index("proactor_spawn.install()")
        assert 0 < spawn - accept < 200, "both run in the http branch, together"


class TestTheLoopKeepsTurning:
    def test_the_stock_loop_stalls_for_the_whole_spawn(self, stock, slow_spawn):
        """The defect, measured by the same ticker the fix is held to."""

        async def go():
            proc = await _child("pass")
            return await proc.wait()

        gap, code = _run(_longest_gap_while(go()))
        assert code == 0
        assert gap >= SLOW_SPAWN_SECONDS * 0.9

    def test_the_patched_loop_turns_while_the_child_is_created(
        self, patched, slow_spawn
    ):
        async def go():
            proc = await _child("pass")
            return await proc.wait()

        gap, code = _run(_longest_gap_while(go()))
        assert code == 0
        assert gap < SLOW_SPAWN_SECONDS / 2
        assert len(slow_spawn) == 1


class TestTheTransportIsTheStockOne:
    def test_pipes_and_exit_code(self, patched):
        async def go():
            proc = await _child(
                "import sys",
                "data = sys.stdin.read()",
                "sys.stdout.write(data.upper())",
                "sys.stderr.write('err')",
                "sys.exit(3)",
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            out, err = await proc.communicate(b"hello")
            return proc.returncode, out, err

        assert _run(go()) == (3, b"HELLO", b"err")

    def test_a_missing_executable_raises_as_before(self, patched):
        async def go():
            await asyncio.create_subprocess_exec("Z:\\no\\such\\program.exe")

        with pytest.raises(FileNotFoundError):
            _run(go())

    def test_a_child_killed_from_outside_is_reported_exited(self, patched):
        async def go():
            proc = await _child("import time; time.sleep(60)")
            psutil.Process(proc.pid).kill()
            return await asyncio.wait_for(proc.wait(), 10)

        assert _run(go()) != 0

    def test_two_spawns_at_once_each_get_their_own_pipes(self, patched):
        async def one(tag):
            proc = await _child(
                f"print({tag!r})", stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            out, _ = await proc.communicate()
            return out.strip()

        async def go():
            return await asyncio.gather(*(one(f"child-{n}") for n in range(8)))

        assert _run(go()) == [f"child-{n}".encode() for n in range(8)]


class TestCancellation:
    def test_a_launch_cancelled_mid_spawn_leaves_no_child(self, patched, slow_spawn):
        async def go():
            task = asyncio.create_task(_child("import time; time.sleep(60)"))
            await asyncio.sleep(SLOW_SPAWN_SECONDS / 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.sleep(SLOW_SPAWN_SECONDS * 2)

        _run(go())
        deadline = time.monotonic() + 30  # CreateProcess alone took up to 2 s here
        while not slow_spawn and time.monotonic() < deadline:
            time.sleep(0.05)
        [proc] = slow_spawn
        assert proc.wait(timeout=10) is not None, "the cancelled launch's child lives"
