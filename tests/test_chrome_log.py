"""F-964 -- Chrome leaves its own record on a persistent-profile launch.

nodriver starts Chrome with ``--disable-breakpad`` and no logging, so a Chrome-side
exit left nothing to read. A persistent-profile launch (a profile the caller named, or
the shared ``default`` one) now carries ``--enable-logging --log-level=1
--log-file=<log dir>/chrome-<session key>-<instance id[:8]>-<UTC timestamp>.log``, one
file per LAUNCH, the newest 20 per session key kept. A disposable clone gets none.
Breakpad stays disabled: a crash dump can hold cookie memory.
"""

import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import nodriver as uc
import pytest

from fakes import FakeBrowser, FakeTab
from stealth_chrome_devtools_mcp.embedded import chrome_log, logging_setup
from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager
from stealth_chrome_devtools_mcp.embedded.models import BrowserOptions
from stealth_chrome_devtools_mcp.settings import get_settings

INSTANCE_ID = "0123abcd-4567-89ef-0123-456789abcdef"
NOW = datetime(2026, 10, 11, 16, 27, 5, tzinfo=timezone.utc)


@pytest.fixture()
def log_dir(tmp_path, monkeypatch):
    target = tmp_path / "logs"
    monkeypatch.setenv("STEALTH_MCP_LOG_DIR", str(target))
    get_settings.cache_clear()
    yield target
    get_settings.cache_clear()


def _options(directory, **fields) -> BrowserOptions:
    return BrowserOptions(user_data_dir=str(directory), **fields)


def _path_of(flags: list[str]) -> Path:
    (flag,) = [f for f in flags if f.startswith("--log-file=")]
    return Path(flag.removeprefix("--log-file="))


def _chrome_files(log_dir: Path, key: str) -> list[str]:
    return sorted(
        p.name
        for p in log_dir.glob(f"chrome-{key}-*.log")
        if chrome_log.is_chrome_log(p)
    )


def _touch(path: Path, age_seconds: float) -> None:
    path.write_text("x", encoding="utf-8")
    stamp = (datetime.now(tz=timezone.utc).timestamp()) - age_seconds
    os.utime(path, (stamp, stamp))


class TestTheFlags:
    def test_a_named_session_launch_asks_chrome_for_a_log(
        self, tmp_session_root, log_dir
    ):
        flags = chrome_log.flags(
            _options(tmp_session_root["sessions"] / "fleet"), INSTANCE_ID, now=NOW
        )

        assert flags[:2] == ["--enable-logging", "--log-level=1"]
        assert _path_of(flags) == log_dir / "chrome-fleet-0123abcd-20261011T162705Z.log"
        assert _path_of(flags).exists(), "created at launch, so retention counts it"

    def test_the_shared_default_profile_is_keyed_default(
        self, tmp_session_root, log_dir
    ):
        flags = chrome_log.flags(_options(tmp_session_root["master"]), INSTANCE_ID)

        name = _path_of(flags).name
        assert re.fullmatch(r"chrome-default-0123abcd-\d{8}T\d{6}Z\.log", name), name

    def test_a_disposable_clone_gets_no_log(self, tmp_session_root, log_dir):
        flags = chrome_log.flags(
            _options(tmp_session_root["sessions"] / "clone-1", auto_clone=True),
            INSTANCE_ID,
        )

        assert flags == []
        assert not log_dir.exists() or list(log_dir.iterdir()) == []

    def test_a_launch_with_no_profile_gets_no_log(self, log_dir):
        assert chrome_log.flags(BrowserOptions(), INSTANCE_ID) == []

    def test_breakpad_stays_disabled(self, tmp_session_root, log_dir):
        flags = chrome_log.flags(
            _options(tmp_session_root["sessions"] / "fleet"), INSTANCE_ID
        )

        switches = [f.split("=")[0] for f in flags]
        assert not any("breakpad" in f or "crash" in f for f in switches)
        assert "--disable-breakpad" in uc.Config()()  # nodriver's own default

    def test_each_launch_has_its_own_file(self, tmp_session_root, log_dir):
        options = _options(tmp_session_root["sessions"] / "fleet")

        first = _path_of(chrome_log.flags(options, INSTANCE_ID, now=NOW))
        second = _path_of(
            chrome_log.flags(options, INSTANCE_ID, now=NOW + timedelta(seconds=1))
        )

        assert first != second
        assert first.exists() and second.exists()

    def test_a_directory_outside_the_session_root_is_a_hash_not_a_path(
        self, tmp_path, log_dir
    ):
        flags = chrome_log.flags(_options(tmp_path / "some" / "dir"), INSTANCE_ID)

        name = _path_of(flags).name
        assert re.fullmatch(
            r"chrome-path-[0-9a-f]{10}-0123abcd-\d{8}T\d{6}Z\.log", name
        )
        assert "some" not in name

    def test_an_unwritable_log_dir_costs_the_launch_nothing(
        self, tmp_session_root, log_dir, monkeypatch
    ):
        def refuses(self, *args, **kwargs):
            raise PermissionError("read-only")

        monkeypatch.setattr(Path, "touch", refuses)

        assert (
            chrome_log.flags(
                _options(tmp_session_root["sessions"] / "fleet"), INSTANCE_ID
            )
            == []
        )


class TestRetention:
    def test_the_newest_twenty_per_session_survive_a_launch(
        self, tmp_session_root, log_dir
    ):
        log_dir.mkdir()
        names = [
            f"chrome-fleet-0123abcd-202610{day:02d}T000000Z.log" for day in range(1, 26)
        ]
        for age, name in enumerate(reversed(names)):  # day 25 is the newest
            _touch(log_dir / name, age_seconds=10_000 + age)

        flags = chrome_log.flags(
            _options(tmp_session_root["sessions"] / "fleet"), INSTANCE_ID, now=NOW
        )

        kept = _chrome_files(log_dir, "fleet")
        assert len(kept) == chrome_log.KEEP_PER_SESSION == 20
        assert _path_of(flags).name in kept, "this launch's own file is never pruned"
        assert kept[0] == names[6] and names[5] not in kept, "the oldest went"

    def test_another_sessions_logs_are_not_counted_or_deleted(
        self, tmp_session_root, log_dir
    ):
        log_dir.mkdir()
        for n in range(22):
            _touch(
                log_dir
                / f"chrome-fleet-2-0123abcd-20261001T0{n // 10}{n % 10}000Z.log",
                age_seconds=5_000 - n,
            )
        for n in range(3):
            _touch(
                log_dir / f"chrome-work-0123abcd-20261001T00{n}000Z.log",
                age_seconds=9_000,
            )

        chrome_log.flags(
            _options(tmp_session_root["sessions"] / "fleet"), INSTANCE_ID, now=NOW
        )

        assert len(_chrome_files(log_dir, "fleet-2")) == 22, "`fleet-2` is not `fleet`"
        assert len(_chrome_files(log_dir, "work")) == 3

    def test_the_backends_own_logs_are_left_alone(self, tmp_session_root, log_dir):
        log_dir.mkdir()
        _touch(log_dir / "backend-1234.log", age_seconds=9_000_000)
        _touch(log_dir / "chrome-notes.log", age_seconds=9_000_000)  # not ours

        chrome_log.flags(_options(tmp_session_root["master"]), INSTANCE_ID, now=NOW)

        assert (log_dir / "backend-1234.log").exists()
        assert (log_dir / "chrome-notes.log").exists()


class TestTheBackendSweepLeavesThemAlone:
    def test_a_chrome_log_is_neither_aged_out_nor_counted(self, log_dir):
        log_dir.mkdir()
        old_chrome = log_dir / "chrome-fleet-0123abcd-20260101T000000Z.log"
        _touch(old_chrome, age_seconds=30 * 86400)
        for n in range(60):
            _touch(log_dir / f"backend-{n}.log", age_seconds=60 - n)

        logging_setup.prune_old_logs(log_dir)

        assert old_chrome.exists(), "the 7-day age rule is the backend logs' own"
        survivors = list(log_dir.glob("backend-*.log"))
        assert len(survivors) == 50, "and they do not count against the 50-file cap"


class TestTheSpawnWiring:
    @pytest.fixture()
    def launched(self, monkeypatch):
        seen: list[list[str]] = []

        monkeypatch.setattr(
            BrowserManager,
            "_resolve_launch_args",
            lambda self, options, proxy, platform_info: (
                ["--foo", "about:blank"],
                "/fake/chrome",
                [],
            ),
        )

        async def launch(self, options, executable, args, attempt):
            seen.append(list(args))
            browser = FakeBrowser(alive=True, main_tab=FakeTab())
            browser.config = None
            return browser

        async def post_launch(self, *args, **kwargs):
            return None, {"actual": {}}

        async def no_hooks(self, tab, instance_id):
            return None

        monkeypatch.setattr(BrowserManager, "_launch_browser", launch)
        monkeypatch.setattr(BrowserManager, "_apply_post_launch", post_launch)
        monkeypatch.setattr(BrowserManager, "_setup_dynamic_hooks", no_hooks)
        return seen

    async def test_a_named_spawn_passes_the_flags_before_the_start_page_and_says_where(
        self, launched, tmp_session_root, log_dir
    ):
        manager = BrowserManager()
        directory = tmp_session_root["sessions"] / "fleet"

        instance = await manager.spawn_browser(
            _options(directory), instance_id=INSTANCE_ID
        )

        (args,) = launched
        assert args[-1] == "about:blank", "the start page stays last"
        assert "--enable-logging" in args and "--foo" in args
        path = _path_of(args)
        diagnostics = await manager.get_spawn_diagnostics(instance.instance_id)
        assert diagnostics["chrome_log"] == str(path)
        assert str(path) in " ".join(diagnostics["effective_browser_args"])

    async def test_a_clone_spawn_has_no_flag_and_no_diagnostic(
        self, launched, tmp_session_root, log_dir
    ):
        manager = BrowserManager()

        instance = await manager.spawn_browser(
            _options(tmp_session_root["sessions"] / "clone-1", auto_clone=True)
        )

        (args,) = launched
        assert not any(a.startswith(("--enable-logging", "--log-")) for a in args)
        diagnostics = await manager.get_spawn_diagnostics(instance.instance_id)
        assert "chrome_log" not in diagnostics


class TestDiagnostics:
    def test_it_reads_the_path_off_the_launch_args(self):
        assert chrome_log.diagnostics(["--a", "--log-file=C:/x/y.log"]) == {
            "chrome_log": "C:/x/y.log"
        }

    def test_no_flag_no_key(self):
        assert chrome_log.diagnostics(["--a"]) == {}
