"""Pins for the F-811 spawn-exhaustion hint: the helper, and its one call site.

The defect: a spawn that fails because the machine has run out of process
capacity surfaced nodriver's opaque connect failure, so the caller (usually an
agent) retried and made the exhaustion worse. The fix appends a paragraph
naming the two measured numbers and the remedy the CLI already ships.

Two properties are load-bearing and pinned here rather than assumed:

* **The helper never raises.** It runs inside an ``except`` block, so a
  diagnostic that throws would destroy the error it decorates. Every failure
  mode — ``process_iter`` refusing, an iterator dying mid-walk, one process
  denying access — degrades to ``None`` or to a skipped process.
* **The call site formats nothing.** The hint carries its own ``"\\n\\n"`` and
  the site is a bare concatenation with an ``or ""``, so below the threshold
  the error is byte-identical to today's (T13 asserts that positively rather
  than asserting an absence).

No test spawns Chrome, and no test touches the real ~/.stealth-mcp: ``pid_file``
is always under ``tmp_path``. ``psutil.process_iter`` is monkeypatched in every
test but T15, which is the deliberate real-contract check — without it a psutil
API change would leave every mocked pin green over a helper that counts nothing.
"""

import asyncio
import inspect
import json
import time
from collections import namedtuple
from pathlib import Path

import nodriver.core.browser
import psutil
import pytest

from fakes import PsutilProc, assert_no_default_paths, psutil_process_iter
from stealth_chrome_devtools_mcp.embedded import (
    browser_cmdline,
    spawn_budget,
    spawn_exhaustion,
    spawn_leak,
)
from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager
from stealth_chrome_devtools_mcp.embedded.models import BrowserOptions
from stealth_chrome_devtools_mcp.embedded.process_cleanup import process_cleanup
from stealth_chrome_devtools_mcp.embedded.spawn_exhaustion import (
    _EXHAUSTION_PROCESS_THRESHOLD as THRESHOLD,
)
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

# ---------------------------------------------------------------------------
# Process-table doubles
#
# Module-local ON PURPOSE, and only until a second module needs them: two
# occurrences is not yet a home (browser_pid_registry._write records the same
# reasoning). At the third, these move into tests/fakes.py — THE harness home —
# because a second hand-rolled copy of a process double would be the defect.
#
# Built to psutil's ACTUAL shape (``fakes.PsutilProc`` / ``psutil_process_iter``:
# a generator that reads requested attrs while it iterates) rather than to our
# assumption about it: a double that copies the production guess keeps a
# permanent defect green. T15 pins that shape against real psutil.
# ---------------------------------------------------------------------------


class FakeProc(PsutilProc):
    """A process that answers its name and fails the test if asked for its argv."""

    def __init__(self, name):
        super().__init__(
            name,
            cmdline_error=AssertionError("the count must never read cmdline"),
        )


class DeniedProc(PsutilProc):
    """A process whose name read fails the way a protected one does."""

    def __init__(self):
        super().__init__("denied", name_error=psutil.AccessDenied(pid=1))


def install_process_table(monkeypatch, procs):
    """Point psutil.process_iter at a fixed list of doubles."""
    monkeypatch.setattr(psutil, "process_iter", psutil_process_iter(list(procs)))


def chrome_procs(count, name="chrome.exe"):
    return [FakeProc(name) for _ in range(count)]


def seed_record(pid_file: Path, tracked: int) -> None:
    """Write a browser_pids.json with *tracked* entries."""
    entries = {
        f"instance-{i}": {
            "pid": 1000 + i,
            "create_time": None,
            "user_data_dir": None,
            "uses_custom_data_dir": None,
            "auto_clone": False,
            "timestamp": 0,
        }
        for i in range(tracked)
    }
    pid_file.write_text(
        json.dumps({"browser_processes": entries, "timestamp": 0}), encoding="utf-8"
    )


@pytest.fixture
def pid_file(tmp_path):
    return tmp_path / "browser_pids.json"


# ---------------------------------------------------------------------------
# T1-T11: the helper
# ---------------------------------------------------------------------------


def test_below_threshold_returns_none(monkeypatch, pid_file):
    install_process_table(monkeypatch, chrome_procs(THRESHOLD - 1))
    assert spawn_exhaustion.exhaustion_hint(pid_file) is None


def test_at_threshold_returns_a_hint(monkeypatch, pid_file):
    install_process_table(monkeypatch, chrome_procs(THRESHOLD))
    hint = spawn_exhaustion.exhaustion_hint(pid_file)
    assert isinstance(hint, str)


def test_threshold_constant_stays_in_a_sane_band():
    """T1/T2 probe RELATIVE to the constant, so retuning needs no golden update.

    This is what stops a retune to 5 (fires on every healthy machine) or to
    100000 (never fires) from sailing through green.
    """
    assert 50 <= THRESHOLD <= 500


def test_non_browser_processes_are_not_counted(monkeypatch, pid_file):
    procs = [FakeProc("python.exe") for _ in range(250)]
    procs += [FakeProc("node.exe") for _ in range(250)]
    procs += chrome_procs(3)
    install_process_table(monkeypatch, procs)
    assert spawn_exhaustion.exhaustion_hint(pid_file) is None


@pytest.mark.parametrize(
    "name",
    [
        "chrome.exe",
        "chrome_proxy.exe",
        "chromium-browser",
        "chrome_crashpad_handler",
        "Google Chrome Helper (Renderer)",
    ],
)
def test_every_platform_spelling_is_counted(monkeypatch, pid_file, name):
    """Windows/Linux/macOS parity in one test, so a lane that never sees the
    other spellings still pins them."""
    install_process_table(monkeypatch, chrome_procs(THRESHOLD, name=name))
    assert spawn_exhaustion.exhaustion_hint(pid_file) is not None


@pytest.mark.parametrize("failure", [RuntimeError("boom"), psutil.Error()])
def test_process_iter_raising_degrades_to_none(monkeypatch, pid_file, failure):
    def exploding_process_iter(attrs=None):
        raise failure

    monkeypatch.setattr(psutil, "process_iter", exploding_process_iter)
    assert spawn_exhaustion.exhaustion_hint(pid_file) is None


def test_process_iter_dying_mid_walk_degrades_to_none(monkeypatch, pid_file):
    """The /proc walk can fail after yielding — nothing may propagate."""

    def half_dead_process_iter(attrs=None):
        yield from chrome_procs(10)
        raise RuntimeError("process table vanished mid-iteration")

    monkeypatch.setattr(psutil, "process_iter", half_dead_process_iter)
    assert spawn_exhaustion.exhaustion_hint(pid_file) is None


def test_per_process_access_denied_is_skipped_not_fatal(monkeypatch, pid_file):
    procs = [DeniedProc(), *chrome_procs(THRESHOLD)]
    install_process_table(monkeypatch, procs)
    assert spawn_exhaustion.exhaustion_hint(pid_file) is not None


@pytest.mark.parametrize("error", [SystemError("proc_cmdline"), OSError("EIO")])
def test_a_process_whose_name_cannot_be_read_does_not_end_the_count(
    monkeypatch, pid_file, error
):
    """T-11618: macOS psutil 7.2.2 raised ``SystemError`` from ``name()`` (its
    truncated-name fallback reads ``cmdline``). Asked for inside ``process_iter``
    that ended the generator, so ONE unreadable process lost the whole count."""
    procs = [PsutilProc("unreadable", name_error=error), *chrome_procs(THRESHOLD)]
    install_process_table(monkeypatch, procs)
    assert spawn_exhaustion.live_chromium_processes() == THRESHOLD
    assert spawn_exhaustion.exhaustion_hint(pid_file) is not None


def test_missing_record_still_produces_a_hint(monkeypatch, tmp_path):
    absent = tmp_path / "nowhere" / "browser_pids.json"
    install_process_table(monkeypatch, chrome_procs(THRESHOLD))
    hint = spawn_exhaustion.exhaustion_hint(absent)
    assert hint is not None
    assert "0 browser(s) are tracked" in hint


def test_message_names_both_measured_signals(monkeypatch, pid_file):
    """Both numbers are asserted against real values, not against 0."""
    live = THRESHOLD + 7
    seed_record(pid_file, tracked=4)
    install_process_table(monkeypatch, chrome_procs(live))
    hint = spawn_exhaustion.exhaustion_hint(pid_file)
    assert f"{live} Chromium-family processes" in hint
    assert "4 browser(s) are tracked" in hint


def test_message_names_both_remedy_verbs_and_force(monkeypatch, pid_file):
    install_process_table(monkeypatch, chrome_procs(THRESHOLD))
    hint = spawn_exhaustion.exhaustion_hint(pid_file)
    # --force is load-bearing: kill-orphans REFUSES while a backend is alive,
    # and a spawn failure is by definition raised by a live backend.
    assert "stealth-chrome-devtools kill-orphans --force" in hint
    assert "--force is required" in hint
    # cleanup is the disk step, and must read as the second one.
    assert "stealth-chrome-devtools cleanup --apply" in hint
    assert hint.index("kill-orphans") < hint.index("cleanup --apply")
    # The honest limit, and the finding tag for greppability (F-808 precedent).
    assert "not ours to reap" in hint
    assert "F-811" in hint


def test_the_hint_carries_its_own_separator(monkeypatch, pid_file):
    """The call site does no formatting, so the contract lives here."""
    install_process_table(monkeypatch, chrome_procs(THRESHOLD))
    assert spawn_exhaustion.exhaustion_hint(pid_file).startswith("\n\n")


# ---------------------------------------------------------------------------
# T12-T14: the call site
# ---------------------------------------------------------------------------

INNER_FAILURE = "--- Failed to connect to browser ---"


@pytest.fixture
def doomed_manager(monkeypatch, pid_file):
    """A BrowserManager whose launch phase always fails, reaching S1's except.

    The seam under test is `spawn_exhaustion.exhaustion_hint`, so the process
    table is never consulted here — these pin the WIRING, not the measurement.
    """

    async def failing_launch(self, options, browser_executable, launch_args, attempt):
        raise RuntimeError(INNER_FAILURE)

    monkeypatch.setattr(
        BrowserManager,
        "_resolve_launch_args",
        lambda self, options, proxy, platform_info: ([], "/fake/chrome", []),
    )
    monkeypatch.setattr(BrowserManager, "_launch_browser", failing_launch)
    monkeypatch.setattr(process_cleanup, "pid_file", pid_file)
    return BrowserManager()


async def test_the_hint_lands_in_the_raised_error_when_the_seam_reads_high(
    monkeypatch, doomed_manager, pid_file
):
    sentinel = "\n\nSENTINEL-EXHAUSTION-PARAGRAPH"
    seen = []

    def fake_hint(path, evidence=None):
        seen.append(path)
        return sentinel

    monkeypatch.setattr(spawn_exhaustion, "exhaustion_hint", fake_hint)

    with pytest.raises(Exception) as err:
        await doomed_manager.spawn_browser(BrowserOptions())

    message = str(err.value)
    # The "Failed to spawn browser:" prefix belongs to server.py's tool wrapper
    # alone; carrying a second copy here doubled it in the user-visible ToolError.
    assert not message.startswith("Failed to spawn browser")
    assert INNER_FAILURE in message
    assert message.endswith(sentinel)
    # Read off the live singleton at call time — that binding is what the
    # tests' pid_file redirection reaches.
    assert seen == [pid_file]


async def test_no_hint_when_the_seam_returns_none(monkeypatch, doomed_manager):
    monkeypatch.setattr(
        spawn_exhaustion, "exhaustion_hint", lambda path, evidence=None: None
    )

    with pytest.raises(Exception) as err:
        await doomed_manager.spawn_browser(BrowserOptions())

    # EXACTLY the inner failure: pins the `or ""` and leaves no trailing artifact,
    # and pins that the prefix is server.py's to add, never this layer's.
    assert str(err.value) == INNER_FAILURE


def test_no_public_function_defaults_its_path_parameter():
    """pid_file can never acquire a default — the structural guarantee that a
    future edit cannot route a test at the developer's live record."""
    assert_no_default_paths(spawn_exhaustion)


# ---------------------------------------------------------------------------
# F-1024 / T-5632: a CPU-starved launch is not "out of process capacity"
#
# Measured on the maintainer's box (tasklist, 2026-10-21): 243 chrome.exe and 29
# stealth-chrome-devtools-mcp.exe. The second is OUR OWN stdio proxy, and the
# "chrom" substring counted it, so a launch that failed because Chrome was late
# under CPU load was reported as a lack of process capacity and sent the operator
# to reap browsers that were not the problem.
# ---------------------------------------------------------------------------


def _evidence(started=True, cpu=97.0, late=True):
    return spawn_exhaustion.LaunchEvidence(
        chrome_started=started, cpu_percent=cpu, missed_deadline=late
    )


STARVED = _evidence()
NOT_STARTED = _evidence(started=False)
NOT_LATE = _evidence(late=False)  # started, hot CPU, but a failure AFTER the launch
IDLE_CPU = _evidence(cpu=12.0)
NO_CPU_READING = _evidence(cpu=None)


@pytest.mark.parametrize(
    "name",
    [
        "stealth-chrome-devtools-mcp.exe",
        "stealth-chrome-devtools-mcp",
        "chromedriver.exe",
        "chromedriver",
        "ChromeSetup.exe",
        "chrome-devtools-mcp",
        "ChromeRemoteDesktopHost.exe",
    ],
)
def test_a_lookalike_name_is_not_a_browser_process(monkeypatch, name):
    install_process_table(monkeypatch, [*chrome_procs(3), *[FakeProc(name)] * 300])
    assert spawn_exhaustion.live_chromium_processes() == 3


def test_a_starved_launch_names_the_cpu_not_the_process_cap(monkeypatch, pid_file):
    install_process_table(monkeypatch, chrome_procs(THRESHOLD - 1))
    hint = spawn_exhaustion.exhaustion_hint(pid_file, STARVED)
    assert hint is not None
    assert hint.startswith("\n\n")
    assert "CPU" in hint
    assert "97%" in hint
    assert "F-1024" in hint
    assert "out of process capacity" not in hint
    assert "kill-orphans" not in hint


def test_a_count_at_the_limit_is_never_relabelled_as_starvation(monkeypatch, pid_file):
    """A machine really at the cap is usually pegged too; the count is the decisive
    fact, so the capacity answer wins and the CPU reading is ADDED, not substituted."""
    install_process_table(monkeypatch, chrome_procs(THRESHOLD + 40))
    hint = spawn_exhaustion.exhaustion_hint(pid_file, STARVED)
    assert "out of process capacity" in hint
    assert "kill-orphans --force" in hint
    assert "CPU-starved" not in hint
    assert "CPU also read 97%" in hint  # both facts reported


@pytest.mark.parametrize("evidence", [None, IDLE_CPU, NOT_LATE, NOT_STARTED])
def test_the_cpu_note_needs_a_starved_launch(monkeypatch, pid_file, evidence):
    install_process_table(monkeypatch, chrome_procs(THRESHOLD))
    hint = spawn_exhaustion.exhaustion_hint(pid_file, evidence)
    assert "out of process capacity" in hint
    assert "CPU also read" not in hint


def test_a_failure_after_the_launch_is_not_starvation_whatever_the_cpu_reads(
    monkeypatch, pid_file
):
    """Hot CPU and a live Chrome, but the launch did not miss its deadline."""
    install_process_table(monkeypatch, chrome_procs(THRESHOLD - 1))
    assert not NOT_LATE.starved()
    assert spawn_exhaustion.exhaustion_hint(pid_file, NOT_LATE) is None


def test_a_chrome_that_never_started_is_not_called_starved(monkeypatch, pid_file):
    install_process_table(monkeypatch, chrome_procs(THRESHOLD))
    hint = spawn_exhaustion.exhaustion_hint(pid_file, NOT_STARTED)
    assert "out of process capacity" in hint
    assert "CPU-starved" not in hint


@pytest.mark.parametrize("evidence", [IDLE_CPU, NO_CPU_READING])
def test_started_chrome_without_a_hot_cpu_below_the_cap_says_nothing(
    monkeypatch, pid_file, evidence
):
    install_process_table(monkeypatch, chrome_procs(THRESHOLD - 1))
    assert spawn_exhaustion.exhaustion_hint(pid_file, evidence) is None


@pytest.mark.parametrize("evidence", [IDLE_CPU, NO_CPU_READING, None])
def test_the_cap_is_named_only_when_the_count_is_at_the_limit(
    monkeypatch, pid_file, evidence
):
    install_process_table(monkeypatch, chrome_procs(THRESHOLD))
    assert "out of process capacity" in spawn_exhaustion.exhaustion_hint(
        pid_file, evidence
    )


Times = namedtuple("Times", "user system idle")
LinuxTimes = namedtuple("LinuxTimes", "user system idle iowait guest")


class _Attempt:
    def __init__(self, baseline):
        self.cpu_baseline = baseline


@pytest.fixture
def chrome_running(monkeypatch):
    monkeypatch.setattr(spawn_exhaustion.spawn_leak, "started", lambda attempt: True)


def _cpu_now(monkeypatch, reading):
    monkeypatch.setattr(psutil, "cpu_times", lambda *a, **k: reading)


LATE = Exception("Failed to connect to browser")


def test_observe_reads_the_cpu_since_the_launch_began(monkeypatch, chrome_running):
    _cpu_now(monkeypatch, Times(user=80, system=10, idle=10))
    got = spawn_exhaustion.observe(_Attempt(Times(0, 0, 0)), LATE)
    assert got == spawn_exhaustion.LaunchEvidence(
        chrome_started=True, cpu_percent=90.0, missed_deadline=True
    )
    assert got.starved()


def test_cpu_counts_iowait_as_idle_and_leaves_guest_time_out():
    # 50 s user (10 of it guest), 30 s idle, 20 s iowait: busy = 50 of 100.
    after = LinuxTimes(user=50, system=0, idle=30, iowait=20, guest=10)
    assert spawn_exhaustion._idle_and_total(after) == (50, 100)


def test_cpu_with_no_time_elapsed_or_no_baseline_is_unknown(monkeypatch):
    _cpu_now(monkeypatch, Times(5, 5, 5))
    assert spawn_exhaustion._cpu_since(Times(5, 5, 5)) is None
    assert spawn_exhaustion._cpu_since(None) is None


def test_observe_takes_no_blocking_sample(monkeypatch, chrome_running):
    """The old cpu_percent(interval=0.2) slept ON the event loop inside the failure
    handler, under the very load it measured. Nothing may sleep now."""

    def blocks(*args, **kwargs):
        raise AssertionError("observe must not take a blocking sample")

    monkeypatch.setattr(psutil, "cpu_percent", blocks)
    monkeypatch.setattr(time, "sleep", blocks)
    _cpu_now(monkeypatch, Times(95, 0, 5))
    assert spawn_exhaustion.observe(_Attempt(Times(0, 0, 0)), LATE).starved()


def test_observe_never_raises(monkeypatch, chrome_running):
    def refuse(*args, **kwargs):
        raise OSError("no counters")

    monkeypatch.setattr(psutil, "cpu_times", refuse)
    assert spawn_exhaustion.observe(_Attempt(Times(0, 0, 0)), LATE) == (
        spawn_exhaustion.LaunchEvidence(
            chrome_started=True, cpu_percent=None, missed_deadline=True
        )
    )


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("first page never opened"),
        ToolError("Browser spawn stalled in the first-page wait stage: no answer"),
        ToolError("the DevTools endpoint belongs to another process"),
    ],
)
def test_observe_does_not_read_the_cpu_for_a_post_launch_failure(
    monkeypatch, chrome_running, error
):
    """Finding 1: hot CPU and a live Chrome say nothing about a failure that is not a
    missed DevTools deadline, so it is never called starved."""

    def reads(*args, **kwargs):
        raise AssertionError("no CPU read for a failure that cannot be starved")

    monkeypatch.setattr(psutil, "cpu_times", reads)
    got = spawn_exhaustion.observe(_Attempt(Times(0, 0, 0)), error)
    assert got == spawn_exhaustion.LaunchEvidence(
        chrome_started=True, cpu_percent=None, missed_deadline=False
    )
    assert not got.starved()


def test_observe_spends_nothing_when_no_chrome_started(monkeypatch):
    monkeypatch.setattr(spawn_exhaustion.spawn_leak, "started", lambda attempt: False)
    monkeypatch.setattr(
        psutil, "cpu_times", lambda: pytest.fail("a launch that never began is free")
    )
    got = spawn_exhaustion.observe(_Attempt(Times(0, 0, 0)), LATE)
    assert got.cpu_percent is None
    assert not got.starved()


def test_an_attempt_records_the_cpu_when_it_begins(monkeypatch):
    monkeypatch.setattr(psutil, "cpu_times", lambda: Times(1, 2, 3))
    assert spawn_leak.Attempt().cpu_baseline == Times(1, 2, 3)

    def unreadable():
        raise OSError("no counters")

    monkeypatch.setattr(psutil, "cpu_times", unreadable)
    assert spawn_leak.Attempt().cpu_baseline is None  # never fails a spawn


# ---- which failures are a missed DevTools deadline (finding 1) -----------------------


def test_nodriver_still_raises_the_connect_failure_we_match():
    """The text belongs to nodriver, not to us: if a release rewords it, the starved
    verdict would silently never fire, so read it from the installed source."""
    source = inspect.getsource(nodriver.core.browser.Browser.start)
    assert spawn_exhaustion._CONNECT_FAILURE in source


async def test_the_launch_stage_budget_is_a_missed_deadline_and_others_are_not(
    monkeypatch,
):
    """Through the REAL spawn_budget.phase, not a hand-typed message."""
    monkeypatch.setattr(spawn_budget, "TEST_BUDGET_SECONDS", 0.01, raising=False)

    async def hangs():
        await asyncio.sleep(5)

    errors = {}
    for stage in ("launch", "first-page wait", "post-launch setup"):
        with pytest.raises(ToolError) as caught:
            await spawn_budget.phase(stage, "TEST_BUDGET_SECONDS")(hangs)()
        errors[stage] = caught.value
    assert spawn_exhaustion.missed_devtools_deadline(errors["launch"])
    assert not spawn_exhaustion.missed_devtools_deadline(errors["first-page wait"])
    assert not spawn_exhaustion.missed_devtools_deadline(errors["post-launch setup"])
    assert spawn_exhaustion.missed_devtools_deadline(LATE)
    assert not spawn_exhaustion.missed_devtools_deadline(RuntimeError("boom"))


async def test_the_call_site_reads_the_evidence_before_the_teardown_ends_the_chrome(
    monkeypatch, doomed_manager
):
    """After the teardown the failed Chrome is gone and the CPU has dropped, so a
    sample taken then would read an idle machine for a launch that died of load."""
    order = []

    def fake_observe(attempt, error):
        order.append("observe")
        assert isinstance(attempt, spawn_leak.Attempt)
        assert INNER_FAILURE in str(error)
        return STARVED

    async def fake_teardown(self, *args, **kwargs):
        order.append("teardown")

    seen = []

    def fake_hint(path, evidence=None):
        seen.append(evidence)
        return "\n\nSTARVED-PARAGRAPH"

    monkeypatch.setattr(spawn_exhaustion, "observe", fake_observe)
    monkeypatch.setattr(spawn_exhaustion, "exhaustion_hint", fake_hint)
    monkeypatch.setattr(BrowserManager, "_teardown_failed_spawn", fake_teardown)

    with pytest.raises(Exception) as err:
        await doomed_manager.spawn_browser(BrowserOptions())

    assert order == ["observe", "teardown"]
    assert seen == [STARVED]
    assert str(err.value).endswith("STARVED-PARAGRAPH")


def _fail_launch_with(monkeypatch, error):
    """A launch whose Chrome "started" (the registry says so), on a pegged CPU."""

    async def failing_launch(self, options, browser_executable, launch_args, attempt):
        attempt.cpu_baseline = Times(0, 0, 0)
        raise error

    monkeypatch.setattr(BrowserManager, "_launch_browser", failing_launch)
    monkeypatch.setattr(spawn_leak, "started", lambda attempt: True)
    monkeypatch.setattr(psutil, "cpu_times", lambda: Times(97, 0, 3))


async def test_a_deadline_miss_on_a_pegged_cpu_is_reported_starved_end_to_end(
    monkeypatch, doomed_manager, pid_file
):
    _fail_launch_with(monkeypatch, LATE)
    install_process_table(monkeypatch, chrome_procs(THRESHOLD - 1))
    with pytest.raises(Exception) as err:
        await doomed_manager.spawn_browser(BrowserOptions())
    assert "CPU-starved" in str(err.value)
    assert "97%" in str(err.value)


async def test_a_post_launch_failure_on_a_pegged_cpu_is_not_reported_starved(
    monkeypatch, doomed_manager, pid_file
):
    """Finding 1 end to end: the same live Chrome and the same 97% CPU, but the failure
    is not the DevTools deadline, so the error stays exactly what it was."""
    _fail_launch_with(monkeypatch, RuntimeError("timezone override refused"))
    install_process_table(monkeypatch, chrome_procs(THRESHOLD - 1))
    with pytest.raises(Exception) as err:
        await doomed_manager.spawn_browser(BrowserOptions())
    assert str(err.value) == "timezone override refused"


async def test_a_real_cap_with_a_pegged_cpu_reports_both_end_to_end(
    monkeypatch, doomed_manager, pid_file
):
    _fail_launch_with(monkeypatch, LATE)
    install_process_table(monkeypatch, chrome_procs(THRESHOLD + 5))
    with pytest.raises(Exception) as err:
        await doomed_manager.spawn_browser(BrowserOptions())
    assert "out of process capacity" in str(err.value)
    assert "CPU also read 97%" in str(err.value)
    assert "CPU-starved" not in str(err.value)


# ---------------------------------------------------------------------------
# T15: the one real-contract check
# ---------------------------------------------------------------------------


def test_psutil_process_iter_still_yields_readable_processes():
    """The ONE test that calls real psutil. Read-only, spawns nothing.

    Without it a psutil API change would leave every pin above green over a
    helper that counts nothing — the "mocked fakes can encode the bug" mode.
    """
    first = next(iter(browser_cmdline.named_processes()), None)
    assert first is not None, "no processes visible — psutil is not working here"
    assert isinstance(first[1], str)
