"""Behavioral tests for platform_utils — stealth arg filtering + arg merging.

filter_stealth_args is a core product guarantee: user-supplied Chrome flags that
would unmask automation must be stripped (by prefix), while benign flags pass
through. merge_browser_args must still add the sandbox flags a root/container
environment needs to launch at all, even though those same flags are otherwise
stealth-blocked. Sandbox detection is monkeypatched so the tests are
deterministic across CI environments.
"""

import os
import platform
import subprocess
from pathlib import Path

import pytest

from stealth_chrome_devtools_mcp.embedded import platform_utils
from stealth_chrome_devtools_mcp.embedded.platform_utils import (
    build_reduced_user_agent,
    filter_stealth_args,
    get_platform_info,
    get_required_sandbox_args,
    merge_browser_args,
    reconcile_launched_browser_version,
    record_launched_browser_major_version,
    reset_browser_version_memo,
    resolve_browser_major_version,
)


@pytest.fixture(autouse=True)
def _no_default_user_agent(monkeypatch):
    """Neutralize the F-770 masked-UA default for the pre-existing arg tests.

    ``merge_browser_args`` now also supplies a default ``--user-agent=`` derived
    from the resolved browser, which would make these assertions depend on
    whether the machine running them happens to have Chrome installed. The
    F-770 behaviour has its own tests below, which opt back in explicitly.
    """
    monkeypatch.setattr(platform_utils, "check_browser_executable", lambda: None)


class TestFilterStealthArgs:
    def test_known_automation_flags_are_stripped(self):
        clean, warnings = filter_stealth_args(
            ["--enable-automation", "--headless", "--window-size=1920,1080"]
        )
        assert clean == ["--window-size=1920,1080"]
        assert len(warnings) == 2
        assert all("stripped:" in w for w in warnings)

    def test_prefix_match_catches_variants(self):
        # startswith matching: these share a blocked prefix.
        clean, _ = filter_stealth_args(
            ["--disable-gpu-sandbox", "--remote-debugging-port=9222"]
        )
        assert clean == []

    def test_benign_flags_pass_through_without_warnings(self):
        clean, warnings = filter_stealth_args(["--lang=en-US", "--window-position=0,0"])
        assert clean == ["--lang=en-US", "--window-position=0,0"]
        assert warnings == []

    def test_matching_is_case_insensitive(self):
        clean, warnings = filter_stealth_args(["--HEADLESS"])
        assert clean == [] and len(warnings) == 1

    def test_empty_input(self):
        assert filter_stealth_args([]) == ([], [])


class TestRequiredSandboxArgs:
    def test_none_needed_on_normal_host(self, monkeypatch):
        monkeypatch.setattr(platform_utils, "is_running_as_root", lambda: False)
        monkeypatch.setattr(platform_utils, "is_running_in_container", lambda: False)
        assert get_required_sandbox_args() == []

    def test_root_requires_no_sandbox_deduped(self, monkeypatch):
        monkeypatch.setattr(platform_utils, "is_running_as_root", lambda: True)
        monkeypatch.setattr(platform_utils, "is_running_in_container", lambda: False)
        args = get_required_sandbox_args()
        assert "--no-sandbox" in args
        assert len(args) == len(set(args)), "required args must be de-duplicated"

    def test_container_adds_shm_and_gpu_flags(self, monkeypatch):
        monkeypatch.setattr(platform_utils, "is_running_as_root", lambda: False)
        monkeypatch.setattr(platform_utils, "is_running_in_container", lambda: True)
        args = get_required_sandbox_args()
        assert "--disable-dev-shm-usage" in args and "--no-sandbox" in args


class TestMergeBrowserArgs:
    def test_strips_stealth_flags_but_keeps_benign(self, monkeypatch):
        monkeypatch.setattr(platform_utils, "is_running_as_root", lambda: False)
        monkeypatch.setattr(platform_utils, "is_running_in_container", lambda: False)
        combined, warnings = merge_browser_args(["--headless", "--lang=en-US"])
        assert combined == ["--lang=en-US"]
        assert len(warnings) == 1

    def test_root_sandbox_flags_added_even_though_stealth_blocked(self, monkeypatch):
        # --no-sandbox is stealth-blocked, but a root env needs it to launch, so
        # merge must add it back after filtering. This is the override that keeps
        # the browser runnable at all in CI/containers.
        monkeypatch.setattr(platform_utils, "is_running_as_root", lambda: True)
        monkeypatch.setattr(platform_utils, "is_running_in_container", lambda: False)
        combined, _ = merge_browser_args(["--foo"])
        assert "--foo" in combined
        assert "--no-sandbox" in combined
        assert combined.count("--no-sandbox") == 1


class TestPlatformInfo:
    def test_reports_expected_keys(self):
        info = get_platform_info()
        for key in ("system", "is_root", "is_container", "required_sandbox_args"):
            assert key in info


# ---------------------------------------------------------------------------
# F-770 — the masked default User-Agent (plan_RELEASE_FIX_D D1).
#
# Headless Chrome advertises `HeadlessChrome/<major>.0.0.0` in its own UA, which
# is the cheapest server-side bot check there is. The product now supplies a
# masked default `--user-agent=` built from Chrome's frozen reduced-UA form. The
# three platform tokens below are the exact strings measured on the three
# qualified runners (D0), so a table typo cannot pass silently: a wrong token
# would make the masked UA disagree with the real OS, which is a WORSE tell than
# the headless token it replaces.
# ---------------------------------------------------------------------------
_REDUCED_UA_CELLS = [
    ("Windows", "Windows NT 10.0; Win64; x64"),
    ("Darwin", "Macintosh; Intel Mac OS X 10_15_7"),
    ("Linux", "X11; Linux x86_64"),
]


@pytest.fixture(autouse=True)
def _clear_version_memo():
    """The browser-version probe is memoized (it runs on the spawn path); clear
    the memo around every test so a fake never bleeds between them."""
    reset_browser_version_memo()
    yield
    reset_browser_version_memo()


class TestReducedUserAgent:
    @pytest.mark.parametrize(("system", "token"), _REDUCED_UA_CELLS)
    def test_masked_ua_matches_the_measured_platform_token(
        self, monkeypatch, system, token
    ):
        monkeypatch.setattr(platform_utils.platform, "system", lambda: system)
        monkeypatch.setattr(
            platform_utils, "resolve_browser_major_version", lambda _exe: "150"
        )
        agent = build_reduced_user_agent("/opt/google/chrome/chrome")
        assert agent == (
            f"Mozilla/5.0 ({token}) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/150.0.0.0 Safari/537.36"
        )
        assert "Headless" not in agent

    def test_edge_keeps_its_own_token(self, monkeypatch):
        # Dropping Edg/ while sec-ch-ua still says "Microsoft Edge" would be a
        # sharper tell than the headless token being masked.
        monkeypatch.setattr(platform_utils.platform, "system", lambda: "Linux")
        monkeypatch.setattr(
            platform_utils, "resolve_browser_major_version", lambda _exe: "150"
        )
        agent = build_reduced_user_agent("/usr/bin/microsoft-edge-stable")
        assert agent.endswith("Chrome/150.0.0.0 Safari/537.36 Edg/150.0.0.0")

    def test_unknown_platform_declines_to_mask(self, monkeypatch):
        monkeypatch.setattr(platform_utils.platform, "system", lambda: "FreeBSD")
        assert build_reduced_user_agent("/usr/local/bin/chrome") is None

    def test_unresolvable_version_declines_to_mask(self, monkeypatch):
        # Masking with a wrong version is worse than not masking: the UA would
        # contradict sec-ch-ua.
        monkeypatch.setattr(platform_utils.platform, "system", lambda: "Linux")
        monkeypatch.setattr(
            platform_utils, "resolve_browser_major_version", lambda _exe: None
        )
        assert build_reduced_user_agent("/usr/bin/google-chrome") is None


class TestResolveBrowserMajorVersion:
    def test_windows_falls_back_to_the_version_named_sibling_directory(
        self, monkeypatch, tmp_path
    ):
        # Windows must NOT shell out: `chrome.exe --version` hands the flag to a
        # running Chrome instead of printing anything. A stub with no version
        # resource (as written here) therefore lands on the directory scan.
        monkeypatch.setattr(platform_utils.platform, "system", lambda: "Windows")

        def _explode(*_args, **_kwargs):
            raise AssertionError("must not run a subprocess on Windows")

        monkeypatch.setattr(platform_utils.subprocess, "run", _explode)
        (tmp_path / "150.0.7871.186").mkdir()
        (tmp_path / "149.0.7000.1").mkdir()
        (tmp_path / "SetupMetrics").mkdir()
        assert resolve_browser_major_version(str(tmp_path / "chrome.exe")) == "150"

    def test_posix_parses_the_version_subprocess(self, monkeypatch):
        monkeypatch.setattr(platform_utils.platform, "system", lambda: "Linux")
        monkeypatch.setattr(
            platform_utils.subprocess,
            "run",
            lambda *_a, **_k: subprocess.CompletedProcess(
                args=[], returncode=0, stdout="Google Chrome 150.0.7871.186 \n"
            ),
        )
        assert resolve_browser_major_version("/usr/bin/google-chrome") == "150"

    def test_posix_probe_failure_is_not_fatal(self, monkeypatch):
        monkeypatch.setattr(platform_utils.platform, "system", lambda: "Linux")

        def _boom(*_a, **_k):
            raise subprocess.TimeoutExpired(cmd="chrome", timeout=10)

        monkeypatch.setattr(platform_utils.subprocess, "run", _boom)
        assert resolve_browser_major_version("/usr/bin/google-chrome") is None


class TestDefaultUserAgentInMergeBrowserArgs:
    def _arm(self, monkeypatch):
        monkeypatch.setattr(platform_utils, "is_running_as_root", lambda: False)
        monkeypatch.setattr(platform_utils, "is_running_in_container", lambda: False)
        monkeypatch.setattr(platform_utils.platform, "system", lambda: "Linux")
        monkeypatch.setattr(
            platform_utils, "check_browser_executable", lambda: "/usr/bin/google-chrome"
        )
        monkeypatch.setattr(
            platform_utils, "resolve_browser_major_version", lambda _exe: "150"
        )

    def test_default_spawn_gets_a_masked_user_agent(self, monkeypatch):
        self._arm(monkeypatch)
        combined, _ = merge_browser_args([])
        assert combined == [
            "--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
        ]

    def test_explicit_user_agent_always_wins(self, monkeypatch):
        self._arm(monkeypatch)
        combined, _ = merge_browser_args(["--user-agent=CallerChose/1.0"])
        assert combined == ["--user-agent=CallerChose/1.0"]

    def test_no_browser_means_no_masking(self, monkeypatch):
        self._arm(monkeypatch)
        monkeypatch.setattr(platform_utils, "check_browser_executable", lambda: None)
        combined, _ = merge_browser_args(["--lang=en-US"])
        assert combined == ["--lang=en-US"]


# ---------------------------------------------------------------------------
# F-806 — the masked User-Agent must describe the browser that ACTUALLY runs.
#
# The mask is built from a version probe that is memoized because it sits on the
# spawn path. Memoized on the executable PATH alone, that probe is a snapshot
# taken once per process: Chrome auto-updates in place, so a long-lived backend
# kept advertising `Chrome/<old>` while the browser it launched — and the
# `sec-ch-ua` client hints Chrome derives from its real version — said `<new>`.
# A UA that contradicts its own client hints is a SHARPER tell than the honest
# `HeadlessChrome` token the mask exists to remove, so this is a stealth defect,
# not a cosmetic one. It turned the 2.0.1 macOS gate red (control 151 vs product
# 150) with byte-identical product code.
#
# Three layers are pinned below:
#   1. the Windows probe reads the BINARY's own version resource, so a staged
#      update sitting in a sibling directory cannot answer for a browser that
#      is not running yet;
#   2. the memo is keyed on the executable's on-disk identity, so an in-place
#      upgrade expires it (and only a real change costs a fresh subprocess);
#   3. `Browser.getVersion().product` — measured NOT to be rewritten by
#      `--user-agent=` — is read back after launch and wins over the probe.
# ---------------------------------------------------------------------------
def _install_chrome(root, version: str):
    """Lay down a Windows-shaped Chrome install: launcher stub + version dir.

    The stub's length tracks the major so an "upgrade" changes the executable's
    SIZE as well as its mtime — timestamp granularity varies by filesystem, and
    a freshness test must not depend on it.
    """
    exe = root / "chrome.exe"
    exe.write_bytes(b"x" * (int(version.split(".", maxsplit=1)[0]) * 8))
    (root / version).mkdir(exist_ok=True)
    return exe


class TestWindowsProbeReadsTheBinaryNotItsNeighbours:
    """The launched version, not the newest one staged beside it (F-806).

    Chrome's updater lands the new version-named directory long before it swaps
    the launcher stub, and that window stays open until the browser is next
    restarted — days on a workstation. A probe that takes ``max()`` over those
    directories therefore reports a version the browser will not run, and the
    first spawn of every fresh backend ships a UA whose ``sec-ch-ua`` disagrees
    with it. Neither the identity re-key nor the post-launch reconciliation can
    close that: the executable's bytes do not move while the update is pending,
    and a launch flag is fixed for the life of the process.
    """

    def test_the_version_resource_wins_over_a_newer_sibling_directory(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(platform_utils.platform, "system", lambda: "Windows")
        monkeypatch.setattr(
            platform_utils, "_windows_file_version", lambda _exe: "150.0.7871.186"
        )
        exe = tmp_path / "chrome.exe"
        exe.write_bytes(b"stub")
        (tmp_path / "150.0.7871.186").mkdir()
        # Staged by the updater; the stub still runs 150 until Chrome restarts.
        (tmp_path / "151.0.7922.72").mkdir()

        assert resolve_browser_major_version(str(exe)) == "150", (
            "the probe followed a staged update instead of the binary that will "
            "actually launch (F-806)"
        )

    @pytest.mark.skipif(
        platform.system() != "Windows",
        reason="reads a real Win32 file-version resource",
    )
    def test_a_real_binarys_version_resource_beats_a_planted_directory(self, tmp_path):
        # Nothing is patched and no code under test supplies the expectation: a
        # real PE that really carries a version resource is copied in as
        # `chrome.exe`, and a version-named directory that disagrees with it is
        # planted alongside — the shape a staged Chrome update leaves on disk.
        # `cmd.exe` is the stand-in because it is present on every Windows and
        # its file-version resource IS the OS build, so `platform.version()`
        # yields the expected major independently.
        source = Path(os.environ.get("COMSPEC") or r"C:\Windows\System32\cmd.exe")
        if not source.is_file():  # pragma: no cover - defensive
            pytest.skip(f"{source} not present")
        exe = tmp_path / "chrome.exe"
        exe.write_bytes(source.read_bytes())
        (tmp_path / "9999.0.0.0").mkdir()

        expected_major = platform.version().split(".", maxsplit=1)[0]
        assert resolve_browser_major_version(str(exe)) == expected_major, (
            "the planted 9999.0.0.0 directory beat the binary's own version "
            "resource — a staged Chrome update does exactly this, and the "
            "browser keeps running the old build until it restarts (F-806)"
        )

    def test_an_unreadable_resource_still_falls_back_to_the_directories(
        self, monkeypatch, tmp_path
    ):
        # The old behaviour is the floor: no machine may get a worse answer than
        # it had before, so a binary with no readable resource is still probed.
        monkeypatch.setattr(platform_utils.platform, "system", lambda: "Windows")
        monkeypatch.setattr(platform_utils, "_windows_file_version", lambda _exe: None)
        exe = tmp_path / "chrome.exe"
        exe.write_bytes(b"stub")
        (tmp_path / "150.0.7871.186").mkdir()
        assert resolve_browser_major_version(str(exe)) == "150"

    def test_a_zeroed_resource_is_treated_as_unreadable(self, monkeypatch, tmp_path):
        # A stripped/repacked binary reports 0.0.0.0. Masking as `Chrome/0.0.0.0`
        # would be a louder tell than not masking at all.
        monkeypatch.setattr(platform_utils.platform, "system", lambda: "Windows")
        monkeypatch.setattr(
            platform_utils, "_windows_file_version", lambda _exe: "0.0.0.0"
        )
        exe = tmp_path / "chrome.exe"
        exe.write_bytes(b"stub")
        (tmp_path / "150.0.7871.186").mkdir()
        assert resolve_browser_major_version(str(exe)) == "150"


class TestBrowserVersionMemoFreshness:
    """The memo must expire when the binary on disk changes — and only then."""

    def test_an_in_place_upgrade_expires_the_memo(self, monkeypatch, tmp_path):
        monkeypatch.setattr(platform_utils.platform, "system", lambda: "Windows")
        exe = _install_chrome(tmp_path, "150.0.7871.129")
        assert resolve_browser_major_version(str(exe)) == "150"

        # Chrome's updater lands a new version directory beside the binary and
        # rewrites the launcher stub — while THIS process stays alive. That is
        # the whole F-806 mechanism.
        _install_chrome(tmp_path, "151.0.7900.1")

        assert resolve_browser_major_version(str(exe)) == "151", (
            "the memoized version survived an in-place upgrade (F-806)"
        )

    def test_the_masked_user_agent_follows_the_upgrade(self, monkeypatch, tmp_path):
        monkeypatch.setattr(platform_utils.platform, "system", lambda: "Windows")
        exe = _install_chrome(tmp_path, "150.0.7871.129")
        assert build_reduced_user_agent(str(exe)).endswith(
            "Chrome/150.0.0.0 Safari/537.36"
        )

        _install_chrome(tmp_path, "151.0.7900.1")

        assert build_reduced_user_agent(str(exe)).endswith(
            "Chrome/151.0.0.0 Safari/537.36"
        ), "the mask kept advertising a version the browser no longer has (F-806)"

    def test_a_staged_version_directory_alone_does_not_move_the_answer(
        self, monkeypatch, tmp_path
    ):
        # The updater lands the new directory BEFORE it swaps the launcher stub,
        # and the browser keeps running the old build until it restarts. So a new
        # directory on its own must change nothing: the answer is the binary's,
        # and the binary has not moved. (An earlier revision put the parent
        # directory's mtime in the memo key precisely to re-probe here — which
        # only re-derived the staged guess sooner, and cost a subprocess on every
        # unrelated write to /usr/bin off Windows.)
        monkeypatch.setattr(platform_utils.platform, "system", lambda: "Windows")
        exe = tmp_path / "chrome.exe"
        exe.write_bytes(b"stub")
        (tmp_path / "150.0.7871.129").mkdir()
        assert resolve_browser_major_version(str(exe)) == "150"

        (tmp_path / "151.0.7900.1").mkdir()
        # Bump the directory mtime explicitly: this test is about the KEY, not
        # about the filesystem's timestamp granularity.
        bumped = tmp_path.stat().st_mtime_ns + 5_000_000_000
        os.utime(tmp_path, ns=(bumped, bumped))

        assert resolve_browser_major_version(str(exe)) == "150"

    def test_the_identity_key_is_the_binarys_own_bytes(self, tmp_path):
        # Stated directly, because it is what makes the line above true: nothing
        # outside the executable participates in the freshness key.
        exe = tmp_path / "chrome.exe"
        exe.write_bytes(b"stub")
        before = platform_utils._executable_identity(str(exe))
        assert before == (exe.stat().st_mtime_ns, exe.stat().st_size)

        bumped = tmp_path.stat().st_mtime_ns + 5_000_000_000
        os.utime(tmp_path, ns=(bumped, bumped))
        assert platform_utils._executable_identity(str(exe)) == before

    def test_an_unchanged_executable_is_never_reprobed(self, monkeypatch, tmp_path):
        # The memo exists because the probe runs on the spawn path; deleting it
        # would trade a stealth defect for a per-spawn subprocess. Pin that the
        # freshness key did NOT cost us the memo.
        monkeypatch.setattr(platform_utils.platform, "system", lambda: "Linux")
        exe = tmp_path / "google-chrome"
        exe.write_bytes(b"stub")
        probes = []

        def _run(*_a, **_k):
            probes.append(1)
            return subprocess.CompletedProcess(
                args=[], returncode=0, stdout="Google Chrome 150.0.7871.186\n"
            )

        monkeypatch.setattr(platform_utils.subprocess, "run", _run)
        assert [resolve_browser_major_version(str(exe)) for _ in range(5)] == [
            "150"
        ] * 5
        assert len(probes) == 1, f"one subprocess per spawn is the regression: {probes}"

    def test_a_changed_executable_is_reprobed(self, monkeypatch, tmp_path):
        monkeypatch.setattr(platform_utils.platform, "system", lambda: "Linux")
        exe = tmp_path / "google-chrome"
        exe.write_bytes(b"stub")
        outputs = iter(
            ["Google Chrome 150.0.7871.186\n", "Google Chrome 151.0.7900.1\n"]
        )

        monkeypatch.setattr(
            platform_utils.subprocess,
            "run",
            lambda *_a, **_k: subprocess.CompletedProcess(
                args=[], returncode=0, stdout=next(outputs)
            ),
        )
        assert resolve_browser_major_version(str(exe)) == "150"
        exe.write_bytes(b"stub-after-the-upgrade")
        assert resolve_browser_major_version(str(exe)) == "151"

    def test_an_unresolvable_probe_is_memoized_too(self, monkeypatch, tmp_path):
        # A browser whose version cannot be read must not re-pay for a failed
        # subprocess on every spawn (masking is already disabled for it).
        monkeypatch.setattr(platform_utils.platform, "system", lambda: "Linux")
        exe = tmp_path / "google-chrome"
        exe.write_bytes(b"stub")
        probes = []

        def _run(*_a, **_k):
            probes.append(1)
            return subprocess.CompletedProcess(args=[], returncode=1, stdout="")

        monkeypatch.setattr(platform_utils.subprocess, "run", _run)
        assert [resolve_browser_major_version(str(exe)) for _ in range(3)] == [None] * 3
        assert len(probes) == 1


class TestRecordLaunchedBrowserVersion:
    """``Browser.getVersion().product`` is the post-launch truth, and it wins.

    Measured on Chrome 150.0.7871.186 (Windows, headless): launching with
    ``--user-agent=...Chrome/1.0.0.0...SKEW-PROBE`` returned
    ``product: "Chrome/150.0.7871.186"`` with ``userAgent`` carrying the
    override — so ``product`` is NOT rewritten by the mask and is a usable
    reading of what is really running.
    """

    def _armed(self, monkeypatch, tmp_path):
        monkeypatch.setattr(platform_utils.platform, "system", lambda: "Windows")
        return _install_chrome(tmp_path, "150.0.7871.129")

    def test_the_launched_browser_corrects_a_wrong_memo(self, monkeypatch, tmp_path):
        exe = self._armed(monkeypatch, tmp_path)
        assert resolve_browser_major_version(str(exe)) == "150"

        assert (
            record_launched_browser_major_version(str(exe), "Chrome/151.0.7900.1")
            == "151"
        )

        # The next spawn masks as the browser that actually ran, not as the probe.
        assert resolve_browser_major_version(str(exe)) == "151"
        assert build_reduced_user_agent(str(exe)).endswith(
            "Chrome/151.0.0.0 Safari/537.36"
        )

    def test_a_headless_product_string_is_parsed(self, monkeypatch, tmp_path):
        exe = self._armed(monkeypatch, tmp_path)
        assert (
            record_launched_browser_major_version(
                str(exe), "HeadlessChrome/151.0.7900.1"
            )
            == "151"
        )

    def test_skew_is_logged_not_silently_repaired(self, monkeypatch, tmp_path):
        exe = self._armed(monkeypatch, tmp_path)
        resolve_browser_major_version(str(exe))
        warnings = []
        monkeypatch.setattr(
            platform_utils.debug_logger,
            "log_warning",
            lambda *args: warnings.append(args),
        )

        record_launched_browser_major_version(str(exe), "Chrome/151.0.7900.1")

        assert len(warnings) == 1, warnings
        message = warnings[0][2]
        assert "150" in message and "151" in message and "F-806" in message

    def test_agreement_is_silent(self, monkeypatch, tmp_path):
        exe = self._armed(monkeypatch, tmp_path)
        resolve_browser_major_version(str(exe))
        warnings = []
        monkeypatch.setattr(
            platform_utils.debug_logger,
            "log_warning",
            lambda *args: warnings.append(args),
        )

        record_launched_browser_major_version(str(exe), "Chrome/150.0.7871.129")

        assert warnings == []

    def test_an_unprobed_executable_is_seeded_without_warning(
        self, monkeypatch, tmp_path
    ):
        # A caller-supplied user_agent short-circuits the mask, so the version is
        # never probed pre-launch. Seeding the memo from the launch is a gain,
        # not a skew — it must not warn.
        exe = self._armed(monkeypatch, tmp_path)
        warnings = []
        monkeypatch.setattr(
            platform_utils.debug_logger,
            "log_warning",
            lambda *args: warnings.append(args),
        )

        assert (
            record_launched_browser_major_version(str(exe), "Chrome/151.0.7900.1")
            == "151"
        )
        assert warnings == []
        assert resolve_browser_major_version(str(exe)) == "151"

    def test_an_unparseable_product_changes_nothing(self, monkeypatch, tmp_path):
        exe = self._armed(monkeypatch, tmp_path)
        assert resolve_browser_major_version(str(exe)) == "150"
        assert record_launched_browser_major_version(str(exe), "Chrome/dev") is None
        assert record_launched_browser_major_version(str(exe), None) is None
        assert resolve_browser_major_version(str(exe)) == "150"


class _FakeTab:
    """Minimal ``tab.send`` seam: returns a canned Browser.getVersion tuple."""

    def __init__(self, version=None, error=None):
        self._version = version
        self._error = error

    async def send(self, command):
        command.close()
        if self._error is not None:
            raise self._error
        return self._version


class TestReconcileLaunchedBrowserVersion:
    async def test_it_reads_product_over_cdp_and_reconciles(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(platform_utils.platform, "system", lambda: "Windows")
        exe = _install_chrome(tmp_path, "150.0.7871.129")
        assert resolve_browser_major_version(str(exe)) == "150"

        tab = _FakeTab(
            version=(
                "1.3",
                "HeadlessChrome/151.0.7900.1",
                "@abcdef",
                "Mozilla/5.0 ... Chrome/150.0.0.0 Safari/537.36",
                "15.1",
            )
        )
        assert await reconcile_launched_browser_version(tab, str(exe)) == "151"
        assert resolve_browser_major_version(str(exe)) == "151"

    async def test_a_cdp_failure_degrades_to_none(self, monkeypatch, tmp_path):
        # A spawn must not die because a diagnostic probe did.
        monkeypatch.setattr(platform_utils.platform, "system", lambda: "Windows")
        exe = _install_chrome(tmp_path, "150.0.7871.129")
        assert resolve_browser_major_version(str(exe)) == "150"

        tab = _FakeTab(error=ConnectionError("target closed"))
        assert await reconcile_launched_browser_version(tab, str(exe)) is None
        assert resolve_browser_major_version(str(exe)) == "150"

    async def test_a_short_version_tuple_is_tolerated(self, monkeypatch, tmp_path):
        monkeypatch.setattr(platform_utils.platform, "system", lambda: "Windows")
        exe = _install_chrome(tmp_path, "150.0.7871.129")
        assert (
            await reconcile_launched_browser_version(_FakeTab(version=()), str(exe))
            is None
        )

    async def test_a_failing_write_back_does_not_escape_the_guard(
        self, monkeypatch, tmp_path
    ):
        # "A spawn must not fail because a diagnostic probe did" has to cover the
        # WHOLE reconciliation, not just the CDP call: the write-back stats the
        # executable (swallowing only OSError) and logs the skew warning
        # unguarded, and it runs inside `_apply_post_launch_setup`, so anything
        # escaping here takes the spawn down with it.
        monkeypatch.setattr(platform_utils.platform, "system", lambda: "Windows")
        exe = _install_chrome(tmp_path, "150.0.7871.129")

        def _boom(*_a, **_k):
            raise RuntimeError("the debug logger fell over")

        monkeypatch.setattr(
            platform_utils, "record_launched_browser_major_version", _boom
        )
        tab = _FakeTab(version=("1.3", "Chrome/151.0.7900.1", "@abc", "UA", "15.1"))
        assert await reconcile_launched_browser_version(tab, str(exe)) is None
