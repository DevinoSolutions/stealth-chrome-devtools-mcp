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
exceptions, ``browser_cmdline.arguments`` answers the fake argv, the kill ladder and
the profile-holder witness are the module's keyword seams, and no process is ever
signalled.
"""

import threading
from types import SimpleNamespace
from typing import ClassVar

import psutil
import pytest

from stealth_chrome_devtools_mcp.embedded import browser_cmdline, forced_end

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
    argv: ClassVar[list[str]] = []

    def __init__(self, pid: int) -> None:
        if self.gone or pid != PID:
            raise psutil.NoSuchProcess(pid)

    def create_time(self) -> float:
        return self.created


@pytest.fixture()
def process(monkeypatch):
    _Process.created, _Process.gone, _Process.argv = CREATED, False, []
    monkeypatch.setattr(psutil, "Process", _Process)
    monkeypatch.setattr(psutil, "pid_exists", lambda _pid: False)
    monkeypatch.setattr(
        browser_cmdline, "arguments", lambda pid: _Process.argv if pid == PID else []
    )
    monkeypatch.setattr(forced_end, "SETTLE_SECONDS", 0.0)
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
    directory: str = DIRECTORY,
    holder=lambda _directory: None,
):
    _Process.argv = argv
    return await forced_end.run(
        INSTANCE_ID,
        pid,
        entry,
        user_data_dir=directory,
        wait_exit=wait or _Wait(),
        timeout=WAIT,
        kill=kill,
        holder=holder,
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
            await _run(kill, _argv(**{"--user-data-dir": "C:/sessions//fleet/"}))
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


class TestTheProfileIsFreeBeforeTheHealRelaunches:
    async def test_a_profile_still_held_after_the_kill_refuses_the_heal(self, process):
        kill = _Kill()

        reason = await _run(kill, _argv(), holder=lambda _d: "pid 77")

        assert kill.calls == [(PID, INSTANCE_ID)]
        assert reason == "was force-ended, but pid 77 still holds its profile"

    async def test_a_child_that_lets_go_inside_the_settle_window_is_waited_for(
        self, process, monkeypatch
    ):
        monkeypatch.setattr(forced_end, "SETTLE_SECONDS", 5.0)
        monkeypatch.setattr(forced_end, "_POLL_SECONDS", 0.0)
        answers = iter(["pid 77", "pid 77", None])

        reason = await _run(_Kill(), _argv(), holder=lambda _d: next(answers))

        assert reason is None

    async def test_the_holder_is_asked_about_the_instances_profile(self, process):
        asked = []

        await _run(_Kill(), _argv(), holder=lambda d: asked.append(d))

        assert asked == [DIRECTORY]


class TestTheBlockingChecksAreOffTheEventLoop:
    async def test_the_identity_check_the_kill_and_the_holder_run_on_worker_threads(
        self, process, monkeypatch
    ):
        loop_thread = threading.get_ident()
        seen: dict[str, int] = {}
        verified = forced_end.refusal

        def refusal(pid, entry, **facts):
            seen["refusal"] = threading.get_ident()
            return verified(pid, entry, **facts)

        def kill(pid, instance_id):
            seen["kill"] = threading.get_ident()
            return True

        def holder(_directory):
            seen["holder"] = threading.get_ident()

        monkeypatch.setattr(forced_end, "refusal", refusal)

        assert await _run(kill, _argv(), holder=holder) is None
        assert set(seen) == {"refusal", "kill", "holder"}
        assert all(ident != loop_thread for ident in seen.values()), seen


class TestTheStartTimeIsAskedAgainAtTheKill:
    async def test_a_pid_recycled_after_the_check_is_not_killed(
        self, process, monkeypatch
    ):
        """The window between the identity check and the kill is closed inside the
        kill's own thread, not by changing the kill ladder's contract."""
        verified = forced_end.refusal

        def recycled_right_after(pid, entry, **facts):
            answer = verified(pid, entry, **facts)
            process.created = CREATED + 3600
            return answer

        monkeypatch.setattr(forced_end, "refusal", recycled_right_after)
        kill = _Kill()

        reason = await _run(kill, _argv())

        assert reason is not None and "changed before the kill" in reason
        assert kill.calls == []


class TestAPrefixIsNotAMatch:
    """A ``startswith`` where an equality belongs would kill the NEXT session's
    browser (``fleet2`` for ``fleet``) or a port that merely begins the same."""

    @pytest.mark.parametrize(
        ("argv_directory", "recorded_directory"),
        [("C:/sessions/fleet2", DIRECTORY), (DIRECTORY, "C:/sessions/fleet2")],
    )
    async def test_a_directory_that_shares_a_prefix_is_not_killed(
        self, process, argv_directory, recorded_directory
    ):
        kill = _Kill()

        reason = await _run(
            kill,
            _argv(**{"--user-data-dir": argv_directory}),
            directory=recorded_directory,
        )

        assert reason is not None and "NOT force-ended" in reason
        assert kill.calls == []

    @pytest.mark.parametrize(
        ("on_argv", "recorded"), [("933", PORT), ("44440", 4444), (str(PORT), 933)]
    )
    async def test_a_port_that_shares_a_prefix_is_not_killed(
        self, process, on_argv, recorded
    ):
        kill = _Kill()

        reason = await _run(
            kill,
            _argv(**{"--remote-debugging-port": on_argv}),
            entry={**ENTRY, "cdp_port": recorded},
        )

        assert reason is not None and "NOT force-ended" in reason
        assert kill.calls == []


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
            {k: v for k, v in ENTRY.items() if k != "pid"},
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
