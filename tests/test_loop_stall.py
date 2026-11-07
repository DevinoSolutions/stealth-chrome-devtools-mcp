"""A stalled event loop leaves every thread's stack in the fault log (2026-11-07).

The three hangs of that night left nothing: the log went quiet and killing the
backend was the only diagnosis. ``loop_stall`` arms ``faulthandler``'s C watchdog
from the heartbeat; these pin the arming and its bounds, and (in a child process,
so this one's faulthandler is untouched) that a real stall writes a real stack.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import textwrap
from types import SimpleNamespace

import pytest

from stealth_chrome_devtools_mcp.embedded import loop_stall


@pytest.fixture
def watchdog(monkeypatch, tmp_path):
    """``loop_stall`` with a fault log open and ``faulthandler`` recorded, not armed."""
    calls: list[tuple[str, float | None]] = []
    monkeypatch.setattr(
        loop_stall,
        "faulthandler",
        SimpleNamespace(
            dump_traceback_later=lambda t, file: calls.append(("arm", t)),
            cancel_dump_traceback_later=lambda: calls.append(("cancel", None)),
        ),
    )
    fault_log = (tmp_path / "backend-1-fault.log").open("a", encoding="utf-8")
    monkeypatch.setattr(loop_stall, "_fault_log", fault_log)
    monkeypatch.setattr(loop_stall, "_armed_at", None)
    monkeypatch.setattr(loop_stall, "_dumps", 0)
    yield calls
    fault_log.close()


def test_the_fault_log_sits_beside_the_backend_log(tmp_path):
    assert loop_stall.fault_log_path(tmp_path / "backend-42.log") == (
        tmp_path / "backend-42-fault.log"
    )


def test_without_a_fault_log_nothing_is_armed(monkeypatch):
    """Every process but a booted backend: the proxy, the CLI, this suite."""
    monkeypatch.setattr(loop_stall, "_fault_log", None)
    monkeypatch.setattr(
        loop_stall,
        "faulthandler",
        SimpleNamespace(dump_traceback_later=pytest.fail),
    )
    assert loop_stall.arm() is False


def test_every_beat_re_arms_and_a_healthy_loop_logs_nothing(watchdog, caplog):
    with caplog.at_level(logging.WARNING, logger="stealth.backend"):
        assert loop_stall.arm(now=100.0)
        assert loop_stall.arm(now=103.0)
        assert loop_stall.arm(now=106.5)
    assert watchdog == [("arm", loop_stall.STALL_DUMP_SECONDS)] * 3
    assert caplog.records == []


def test_a_missed_deadline_is_reported_with_its_length(watchdog, caplog):
    with caplog.at_level(logging.WARNING, logger="stealth.backend"):
        loop_stall.arm(now=100.0)
        assert loop_stall.arm(now=142.0)
    [record] = caplog.records
    assert "did not turn for 42.0s" in record.getMessage()
    assert "backend-1-fault.log" in record.getMessage()
    assert loop_stall._dumps == 1


def test_dumps_stop_at_the_cap_and_say_so_once(watchdog, caplog):
    now = 0.0
    with caplog.at_level(logging.WARNING, logger="stealth.backend"):
        loop_stall.arm(now=now)
        for _ in range(loop_stall.MAX_DUMPS + 3):
            now += loop_stall.STALL_DUMP_SECONDS + 1
            loop_stall.arm(now=now)
    assert loop_stall._dumps == loop_stall.MAX_DUMPS
    assert watchdog[-1] == ("cancel", None)
    assert [r.getMessage() for r in caplog.records].count(
        f"stall dumps stop here: {loop_stall.MAX_DUMPS} written"
    ) == 1
    assert loop_stall.arm(now=now + 100) is False


def test_the_heartbeat_is_what_arms_it():
    """One arming per beat, ON the loop and before the stamp's thread hand-off."""
    import inspect

    from stealth_chrome_devtools_mcp.embedded import backend_liveness

    source = inspect.getsource(backend_liveness.beat)
    assert source.index("loop_stall.arm()") < source.index("to_thread.run_sync")


def test_a_real_stall_writes_the_stalled_frame(tmp_path):
    """In a child: arm, block the loop past the deadline, beat again."""
    script = textwrap.dedent(
        f"""
        import asyncio, logging, sys, time
        from pathlib import Path
        from stealth_chrome_devtools_mcp.embedded import loop_stall
        logging.basicConfig(stream=sys.stdout, level=logging.WARNING)
        loop_stall.STALL_DUMP_SECONDS = 0.5
        loop_stall.open_fault_log(Path(r"{tmp_path}") / "backend-7.log")

        def a_call_that_holds_the_loop():
            time.sleep(1.5)

        async def main():
            loop_stall.arm()
            a_call_that_holds_the_loop()
            loop_stall.arm()

        asyncio.run(main())
        """
    )
    done = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    dump = (tmp_path / "backend-7-fault.log").read_text(encoding="utf-8")
    assert "Timeout (0:00:00.500000)!" in dump
    assert "a_call_that_holds_the_loop" in dump
    assert "did not turn for" in done.stdout
