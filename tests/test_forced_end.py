"""T-4686 -- the heal's last rung: end a browser the close path left running, but only
one whose identity is established.

``browser_health.heal`` closes a hung browser through the close path and then waits
for its pid to go. On 2026-10-21 (backend 46612) the close left it running and the
heal was refused, so the session stayed frozen until a human killed it. ``forced_end``
is the rung after the wait: it ends THE pid the heal took before the close, and only
when the browser's own argv and start time say it is the browser the registry
recorded for this instance (exact pid, matching create time, this instance's
``--user-data-dir`` and ``--remote-debugging-port``, no ``--type``).

Hermetic: ``psutil.Process`` is replaced by a fake that raises psutil's own
exceptions, the argv and the kill ladder are the module's keyword seams, and no
process is ever signalled.
"""

from types import SimpleNamespace

import psutil
import pytest

from stealth_chrome_devtools_mcp.embedded import forced_end

INSTANCE_ID = "i-fleet"
PID = 4242
CREATED = 1_760_000_000.5
PORT = 9333
WAIT = 15.0
DIRECTORY = "C:/sessions/fleet"
ENTRY = {
    "pid": PID,
    "create_time": CREATED,
    "user_data_dir": DIRECTORY,
    "cdp_port": PORT,
}


def _argv(**overrides: str | None) -> list[str]:
    flags = {
        "--user-data-dir": DIRECTORY,
        "--remote-debugging-port": str(PORT),
        **overrides,
    }
    return ["chrome.exe", *(f"{k}={v}" for k, v in flags.items() if v is not None)]


class _Log:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def log_warning(self, _component, _method, message) -> None:
        self.warnings.append(message)


class _Process:
    """``psutil.Process`` for a pid whose start time is *CREATED* (or another)."""

    created = CREATED
    gone = False

    def __init__(self, pid: int) -> None:
        if self.gone or pid != PID:
            raise psutil.NoSuchProcess(pid)

    def create_time(self) -> float:
        return self.created


@pytest.fixture()
def process(monkeypatch):
    monkeypatch.setattr(psutil, "Process", _Process)
    monkeypatch.setattr(forced_end, "debug_logger", _Log())
    return _Process


class _Kill:
    def __init__(self, result: bool = True) -> None:
        self.result = result
        self.calls: list[tuple[int, str]] = []

    def __call__(self, pid: int, instance_id: str) -> bool:
        self.calls.append((pid, instance_id))
        return self.result


class _Wait:
    """``process_exit.wait_for_exit_async``: the browser left on its own, or not."""

    def __init__(self, exited: bool = False) -> None:
        self.exited = exited
        self.calls: list[tuple[int, float]] = []

    async def __call__(self, pid: int, timeout: float) -> SimpleNamespace:
        self.calls.append((pid, timeout))
        return SimpleNamespace(exited=self.exited)


async def _run(
    kill: _Kill,
    argv: list[str],
    entry=ENTRY,
    pid: int = PID,
    wait: _Wait | None = None,
):
    return await forced_end.run(
        INSTANCE_ID,
        pid,
        entry,
        user_data_dir=DIRECTORY,
        wait_exit=wait or _Wait(),
        timeout=WAIT,
        arguments=lambda _pid: argv,
        kill=kill,
    )


class TestAVerifiedBrowserIsEnded:
    async def test_the_recorded_browser_is_killed_once_and_reported_ended(
        self, process
    ):
        kill = _Kill()

        assert await _run(kill, _argv()) is None
        assert kill.calls == [(PID, INSTANCE_ID)]
        (line,) = forced_end.debug_logger.warnings
        assert INSTANCE_ID in line and str(PID) in line and "force-ended" in line

    async def test_a_start_time_inside_the_registrys_tolerance_still_matches(
        self, process
    ):
        process.created = CREATED + 0.4
        kill = _Kill()

        assert await _run(kill, _argv()) is None
        assert kill.calls == [(PID, INSTANCE_ID)]

    async def test_the_path_is_compared_normalized(self, process):
        kill = _Kill()

        assert (
            await _run(kill, _argv(**{"--user-data-dir": "c:\\sessions\\fleet"}))
            is None
        )
        assert kill.calls == [(PID, INSTANCE_ID)]

    async def test_a_kill_ladder_that_fails_says_so_and_reports_the_browser_left(
        self, process
    ):
        kill = _Kill(result=False)

        reason = await _run(kill, _argv())

        assert reason is not None and "could not be force-ended" in reason
        assert kill.calls == [(PID, INSTANCE_ID)]


class TestABrowserThatLeavesOnItsOwnIsLeftAlone:
    async def test_a_browser_gone_inside_the_wait_is_not_killed_or_even_inspected(
        self, process
    ):
        kill, wait = _Kill(), _Wait(exited=True)

        assert await _run(kill, [], entry={}, wait=wait) is None
        assert wait.calls == [(PID, WAIT)]
        assert kill.calls == []

    async def test_the_wait_is_the_callers_pid_and_bound(self, process):
        wait = _Wait()

        await _run(_Kill(), _argv(), wait=wait)

        assert wait.calls == [(PID, WAIT)]


class TestAnUnverifiedProcessIsNeverKilled:
    """Every one of these is a process that is NOT established as the instance's
    own browser; none may reach the kill ladder."""

    @pytest.mark.parametrize(
        ("label", "argv"),
        [
            ("another profile", _argv(**{"--user-data-dir": "C:/sessions/master"})),
            ("another debugging port", _argv(**{"--remote-debugging-port": "9444"})),
            ("no debugging port", _argv(**{"--remote-debugging-port": None})),
            ("no profile", _argv(**{"--user-data-dir": None})),
            ("a child process", [*_argv(), "--type=renderer"]),
            ("an unreadable argv", []),
        ],
    )
    async def test_a_mismatched_command_line_is_not_killed(self, process, label, argv):
        kill = _Kill()

        reason = await _run(kill, argv)

        assert reason is not None and "NOT force-ended" in reason, label
        assert kill.calls == [], label

    async def test_a_recycled_pid_is_not_killed(self, process):
        process.created = CREATED + 3600
        kill = _Kill()

        reason = await _run(kill, _argv())

        assert reason is not None and "start time differs" in reason
        assert kill.calls == []

    async def test_a_pid_that_is_not_the_recorded_one_is_not_killed(self, process):
        kill = _Kill()

        reason = await _run(kill, _argv(), entry={**ENTRY, "pid": PID + 1})

        assert reason is not None and "NOT force-ended" in reason
        assert kill.calls == []

    @pytest.mark.parametrize(
        "entry",
        [
            {},
            {k: v for k, v in ENTRY.items() if k != "create_time"},
            {**ENTRY, "create_time": None},
            {k: v for k, v in ENTRY.items() if k != "cdp_port"},
            {**ENTRY, "cdp_port": 0},
        ],
    )
    async def test_a_record_that_cannot_establish_identity_is_not_killed(
        self, process, entry
    ):
        """The registry's own identity rule reads a missing start time as "nothing
        to contradict" (right for a capture, wrong for a kill)."""
        kill = _Kill()

        reason = await _run(kill, _argv(), entry=entry)

        assert reason is not None and "NOT force-ended" in reason
        assert kill.calls == []

    async def test_a_process_that_is_already_gone_is_not_killed(self, process):
        process.gone = True
        kill = _Kill()

        reason = await _run(kill, _argv())

        assert reason is not None and "NOT force-ended" in reason
        assert kill.calls == []
