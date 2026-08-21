"""F-819: `resolve_chrome.py --freeze-updater` stops the image's Chrome moving.

`tools/resolve_chrome.py` is the ONE home of the *expected* Chrome identity. It
is read once per CI job and then trusted for the rest of the run — by the JSON
artifact and by `tests/test_browser_integration.py`'s CDP comparison. That trust
is only earned if the binary cannot change underneath the reading, and on
GitHub's macOS runners it can: Keystone upgrades Chrome Stable mid-run, so the
resolved identity and the launched browser disagree (PR #64, both red macOS runs
on byte-identical trees).

These pins characterise the freeze the flag performs. They are deliberately
command-level: the freeze cannot be exercised for real anywhere — running it on
a developer workstation would disable that human's Chrome updates — so the exact
argv per OS is the only thing there is to assert, and a silent change to it
would otherwise reach CI unreviewed.

Every seam is faked. Nothing here starts a process or touches a real path.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parent.parent / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import resolve_chrome  # (imported after the sys.path line above; tests/** allows E402)


class RecordingRun:
    """A stand-in for `subprocess.run` that records argv and answers as told.

    The reply is a real `subprocess.CompletedProcess`, built by the stdlib's own
    constructor, so a pin can never pass against a shape the real call would not
    produce.
    """

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.calls: list[list[str]] = []
        self._returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    def __call__(self, command, **kwargs):
        self.calls.append(list(command))
        return subprocess.CompletedProcess(
            args=list(command),
            returncode=self._returncode,
            stdout=self._stdout,
            stderr=self._stderr,
        )


@pytest.fixture
def run_recorder(monkeypatch) -> RecordingRun:
    recorder = RecordingRun()
    monkeypatch.setattr(resolve_chrome.subprocess, "run", recorder)
    return recorder


def _as_lines(calls: list[list[str]]) -> list[str]:
    return [" ".join(call) for call in calls]


def _home(*parts: str) -> str:
    """A home-relative path rendered in the HOST's flavour.

    The freeze only ever executes on macOS, but this pin runs on all three
    runners, and `Path.home() / "Library"` renders with a backslash on Windows.
    Building the expectation through the same join keeps the pin about the path
    the code chose rather than about the separator the host prefers.
    """
    return str(Path.home().joinpath(*parts))


# ---------------------------------------------------------------------------
# What the freeze issues, per OS.
# ---------------------------------------------------------------------------
class TestMacOSFreezesKeystone:
    """macOS is the OS the defect was observed on; Keystone is the updater."""

    @pytest.fixture
    def lines(self, monkeypatch, run_recorder) -> list[str]:
        monkeypatch.setattr(resolve_chrome.platform, "system", lambda: "Darwin")
        resolve_chrome.freeze_updater()
        return _as_lines(run_recorder.calls)

    def test_the_user_launch_agents_are_unloaded_and_removed(self, lines):
        for label in ("agent", "xpcservice"):
            plist = _home(
                "Library", "LaunchAgents", f"com.google.keystone.{label}.plist"
            )
            assert f"launchctl unload -w {plist}" in lines
            assert f"rm -f {plist}" in lines

    def test_the_user_agents_are_not_unloaded_as_root(self, lines):
        """`sudo launchctl` targets root's domain and would unload nothing."""
        plist = _home("Library", "LaunchAgents", "com.google.keystone.agent.plist")
        assert f"sudo launchctl unload -w {plist}" not in lines

    def test_the_system_launch_daemons_are_unloaded_as_root(self, lines):
        for plist in (
            "/Library/LaunchAgents/com.google.keystone.agent.plist",
            "/Library/LaunchDaemons/com.google.keystone.daemon.plist",
            "/Library/LaunchDaemons/com.google.keystone.system.agent.plist",
        ):
            assert f"sudo launchctl unload -w {plist}" in lines
            assert f"sudo rm -f {plist}" in lines

    def test_both_googlesoftwareupdate_roots_are_removed(self, lines):
        for root in (
            _home("Library", "Google", "GoogleSoftwareUpdate"),
            "/Library/Google/GoogleSoftwareUpdate",
        ):
            assert f"sudo rm -rf {root}" in lines

    def test_each_root_is_left_as_a_root_owned_unwritable_stub(self, lines):
        """Removal alone is not a freeze: Chrome re-registers Keystone when it
        launches, and this run launches Chrome. The stub is what makes the
        removal survive the very act the identity is being resolved for."""
        for root in (
            _home("Library", "Google", "GoogleSoftwareUpdate"),
            "/Library/Google/GoogleSoftwareUpdate",
        ):
            assert f"sudo mkdir -p {root}" in lines
            assert f"sudo chown root:wheel {root}" in lines
            assert f"sudo chmod 000 {root}" in lines

    def test_the_home_parent_directory_is_locked_too(self, lines):
        """A root-owned stub inside a user-owned parent can still be unlinked by
        that user — the parent's write bit, not the stub's, governs removal."""
        parent = _home("Library", "Google")
        assert f"sudo chown root:wheel {parent}" in lines
        assert f"sudo chmod 555 {parent}" in lines

    def test_the_stub_is_created_before_its_parent_is_locked(self, lines):
        root = _home("Library", "Google", "GoogleSoftwareUpdate")
        assert lines.index(f"sudo mkdir -p {root}") < lines.index(
            f"sudo chmod 555 {_home('Library', 'Google')}"
        )


class TestWindowsFreezesGoogleUpdate:
    @pytest.fixture
    def lines(self, monkeypatch, run_recorder) -> list[str]:
        monkeypatch.setattr(resolve_chrome.platform, "system", lambda: "Windows")
        resolve_chrome.freeze_updater()
        return _as_lines(run_recorder.calls)

    def test_both_update_services_are_stopped_then_disabled(self, lines):
        for service in ("gupdate", "gupdatem"):
            assert f"sc.exe stop {service}" in lines
            assert f"sc.exe config {service} start= disabled" in lines
            assert lines.index(f"sc.exe stop {service}") < lines.index(
                f"sc.exe config {service} start= disabled"
            )

    def test_the_machine_update_tasks_are_disabled(self, lines):
        for task in ("GoogleUpdateTaskMachineCore", "GoogleUpdateTaskMachineUA"):
            assert f"schtasks.exe /Change /TN {task} /DISABLE" in lines

    def test_the_enterprise_policy_turns_updates_off(self, lines):
        key = r"HKLM\SOFTWARE\Policies\Google\Update"
        assert f"reg.exe add {key} /v UpdateDefault /t REG_DWORD /d 0 /f" in lines
        assert (
            f"reg.exe add {key} /v AutoUpdateCheckPeriodMinutes /t REG_DWORD /d 0 /f"
            in lines
        )

    def test_every_step_is_argv_not_a_shell_string(self, lines, run_recorder):
        assert run_recorder.calls, "the Windows freeze issued nothing at all"
        for call in run_recorder.calls:
            assert all(isinstance(arg, str) for arg in call), (
                "argv form only — a shell string would need quoting nobody checks"
            )


class TestLinuxIsAnExplicitNoOp:
    def test_no_command_runs_at_all(self, monkeypatch, run_recorder):
        monkeypatch.setattr(resolve_chrome.platform, "system", lambda: "Linux")
        notes = resolve_chrome.freeze_updater()
        assert run_recorder.calls == [], (
            "the Linux runner images ship no background Chrome updater; issuing "
            "commands there would be a second mechanism with nothing to freeze"
        )
        assert notes, "a no-op must still say so in the log — silence reads as a bug"
        assert any("no background" in note.lower() for note in notes)


# ---------------------------------------------------------------------------
# The freeze may never turn a green run red.
# ---------------------------------------------------------------------------
class TestAbsenceIsNotFailure:
    @pytest.mark.parametrize("system", ["Darwin", "Windows"])
    def test_a_missing_service_dir_or_task_still_succeeds(self, monkeypatch, system):
        """Every sub-step is best-effort: an absent target exits non-zero."""
        monkeypatch.setattr(resolve_chrome.platform, "system", lambda: system)
        monkeypatch.setattr(
            resolve_chrome.subprocess,
            "run",
            RecordingRun(returncode=1, stderr="The specified service does not exist"),
        )
        notes = resolve_chrome.freeze_updater()
        assert notes, "a freeze that did nothing must still report what it tried"

    @pytest.mark.parametrize("system", ["Darwin", "Windows"])
    @pytest.mark.parametrize(
        "boom",
        [FileNotFoundError("no such tool"), subprocess.TimeoutExpired("sc.exe", 60)],
    )
    def test_an_unrunnable_tool_is_swallowed(self, monkeypatch, system, boom):
        monkeypatch.setattr(resolve_chrome.platform, "system", lambda: system)

        def explode(command, **kwargs):
            raise boom

        monkeypatch.setattr(resolve_chrome.subprocess, "run", explode)
        notes = resolve_chrome.freeze_updater()
        assert notes, "the freeze must report the failure, not propagate it"

    def test_the_cli_still_exits_zero_when_every_step_fails(self, monkeypatch, capsys):
        monkeypatch.setattr(resolve_chrome.platform, "system", lambda: "Darwin")

        def explode(command, **kwargs):
            raise OSError("permission denied")

        monkeypatch.setattr(resolve_chrome.subprocess, "run", explode)
        monkeypatch.setattr(resolve_chrome, "_resolve_path", lambda: Path("/chrome"))
        monkeypatch.setattr(resolve_chrome, "_read_version", lambda path: "151.0.1.2")
        assert resolve_chrome.main(["--freeze-updater"]) == 0
        assert json.loads(capsys.readouterr().out)["version"] == "151.0.1.2"


# ---------------------------------------------------------------------------
# The flag, and only the flag, changes behaviour.
# ---------------------------------------------------------------------------
class TestTheFlagGatesTheFreeze:
    @pytest.fixture
    def identity_stub(self, monkeypatch) -> None:
        monkeypatch.setattr(resolve_chrome, "_resolve_path", lambda: Path("/chrome"))
        monkeypatch.setattr(resolve_chrome, "_read_version", lambda path: "150.0.0.1")

    def test_without_the_flag_nothing_is_frozen(
        self, monkeypatch, capsys, identity_stub
    ):
        froze: list[bool] = []
        monkeypatch.setattr(
            resolve_chrome, "freeze_updater", lambda: froze.append(True) or []
        )
        assert resolve_chrome.main([]) == 0
        assert froze == [], "the default invocation must not touch the machine"

    def test_without_the_flag_stdout_is_byte_identical_to_the_identity_json(
        self, monkeypatch, capsys, identity_stub
    ):
        monkeypatch.setattr(resolve_chrome.platform, "system", lambda: "Darwin")
        assert resolve_chrome.main([]) == 0
        printed = capsys.readouterr().out
        assert (
            printed
            == json.dumps(resolve_chrome.resolve_chrome(), indent=2, sort_keys=True)
            + "\n"
        )

    def test_with_the_flag_the_freeze_runs(self, monkeypatch, capsys, identity_stub):
        froze: list[bool] = []
        monkeypatch.setattr(
            resolve_chrome, "freeze_updater", lambda: froze.append(True) or []
        )
        assert resolve_chrome.main(["--freeze-updater"]) == 0
        assert froze == [True]

    def test_the_identity_json_on_stdout_is_unchanged_by_the_flag(
        self, monkeypatch, capsys, identity_stub
    ):
        """The freeze narrates to stderr; a consumer parsing stdout sees the same
        document either way."""
        monkeypatch.setattr(
            resolve_chrome, "freeze_updater", lambda: ["froze: something"]
        )
        assert resolve_chrome.main(["--freeze-updater"]) == 0
        captured = capsys.readouterr()
        assert json.loads(captured.out)["version"] == "150.0.0.1"
        assert "froze: something" in captured.err


class TestTheFreezePrecedesTheReading:
    def test_the_version_is_read_after_the_freeze_ran(self, monkeypatch, capsys):
        """The whole point: a version read before the freeze is a version the
        updater may still move."""
        order: list[str] = []
        monkeypatch.setattr(
            resolve_chrome, "freeze_updater", lambda: order.append("freeze") or []
        )
        monkeypatch.setattr(resolve_chrome, "_resolve_path", lambda: Path("/chrome"))

        def read_version(path):
            order.append("read")
            return "151.0.0.0"

        monkeypatch.setattr(resolve_chrome, "_read_version", read_version)
        assert resolve_chrome.main(["--freeze-updater"]) == 0
        assert order == ["freeze", "read"]

    def test_resolving_the_identity_never_freezes_on_its_own(
        self, monkeypatch, run_recorder
    ):
        """`tests/test_browser_integration.py` imports this module and calls
        `resolve_chrome()` directly. That call must stay side-effect-free: the
        freeze belongs to the CLI, so importing the module cannot disable a
        developer's Chrome updates."""
        monkeypatch.setattr(resolve_chrome.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(resolve_chrome, "_resolve_path", lambda: Path("/chrome"))
        monkeypatch.setattr(resolve_chrome, "_read_version", lambda path: "151.0.0.0")
        resolve_chrome.resolve_chrome()
        assert run_recorder.calls == []
