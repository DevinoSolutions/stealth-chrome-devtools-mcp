"""F-892 / F-893 / F-894 / F-895 — the seed tells the truth about itself.

Four findings, one subject: the master snapshot every session is copied from.

* **F-892** the staleness witness stats ``Default/Cookies``, a path Chrome has
  not written since version 96. The fixture here is built from the layout of a
  REAL Chrome profile measured on this machine (``fixtures-from-the-same-
  serializer-cannot-fail``) — never from ``_copy_profile_tree``'s own output,
  which would only prove the copier agrees with itself.
* **F-893** a refresh whose target was held copied nothing and reported success.
* **F-894** ``user_data_dir="master"`` silently meant ``sessions/master``.
* **F-895** the marker now records which seed a profile came from and when.
"""

import json
import os
import time
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from fakes import FakeBrowserManager, held_profile
from stealth_chrome_devtools_mcp.embedded import (
    clone_storage,
    desktop_launch,
    profile_seed,
)
from stealth_chrome_devtools_mcp.embedded import tool_runtime as rt
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError
from stealth_chrome_devtools_mcp.settings import get_settings

MARKER = profile_seed.MARKER_NAME
PACKAGE = Path(clone_storage.__file__).resolve().parent.parent

# MEASURED 2026-09-20 on C:\stealth-mcp-browser-sessions\master (Chrome 153, a
# real human-driven profile). Names and sizes only; no file was opened. The
# point of the list is the SHAPE: the cookie jar lives under Default/Network/,
# Default/Cookies does not exist at all, and Login Data / Web Data sit beside
# Preferences in Default/.
REAL_PROFILE_LAYOUT: dict[str, int] = {
    "Local State": 90406,
    "Last Version": 13,
    "Default/Preferences": 72264,
    "Default/History": 4915200,
    "Default/Web Data": 196608,
    "Default/Login Data": 40960,
    "Default/Network/Cookies": 524288,
    "Default/Network/Cookies-journal": 0,
    "Default/Network/Network Persistent State": 96263,
    "Default/Network/TransportSecurity": 56700,
}


def real_chrome_profile(root: Path) -> Path:
    """A directory laid out like the measured Chrome profile above."""
    for rel, size in REAL_PROFILE_LAYOUT.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\0" * min(size, 64))
    return root


@pytest.fixture()
def real_layout_root(tmp_path, monkeypatch):
    """A session root whose master and snapshot are real-Chrome-shaped."""
    master = real_chrome_profile(tmp_path / "master")
    snapshot = real_chrome_profile(tmp_path / "master-snapshot")
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    profile_seed.write_marker(
        snapshot, source=master, source_kind="test-fixture", seeded_from="master"
    )
    for key, value in {
        "STEALTH_MCP_BROWSER_SESSION_ROOT": str(tmp_path),
        "BROWSER_MASTER_USER_DATA_DIR": str(master),
        "BROWSER_MASTER_SNAPSHOT_DIR": str(snapshot),
        "BROWSER_PROFILE_CLONE_ROOT": str(sessions),
    }.items():
        monkeypatch.setenv(key, value)
    return {
        "root": tmp_path,
        "master": master,
        "snapshot": snapshot,
        "sessions": sessions,
    }


def _settle(dirs: dict) -> None:
    """Put the whole fixture in the past and the snapshot marker AFTER it, so a
    later write to ONE file is the only thing that can make the snapshot stale.

    Without this every pin below passes for the wrong reason: the fixture writes
    ``Login Data`` and ``Web Data`` at fixture time, and those are witnesses the
    SHIPPED list already sees — so "stale" would be true before the fix and the
    pin would prove nothing about the cookie jar.
    """
    settled = time.time() - 3600
    for profile in (dirs["master"], dirs["snapshot"]):
        for path in profile.rglob("*"):
            if path.is_file():
                os.utime(path, (settled, settled))
    marker_at = settled + 60
    os.utime(dirs["snapshot"] / MARKER, (marker_at, marker_at))


def _touch(path: Path, payload: bytes) -> None:
    """Write *payload* and stamp the file well after the snapshot marker."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    later = time.time()
    os.utime(path, (later, later))


# ---------------------------------------------------------------------------
# F-892 — the staleness witness must be able to see a cookie login
# ---------------------------------------------------------------------------


class TestLoginWitnesses:
    def test_fixture_matches_the_measured_chrome_layout(self, real_layout_root):
        """The fixture is a real Chrome profile's shape, not our copier's."""
        master = real_layout_root["master"]
        assert (master / "Default" / "Network" / "Cookies").exists()
        assert not (master / "Default" / "Cookies").exists()

    def test_the_witnessed_cookie_jar_exists_in_a_real_profile(self, real_layout_root):
        """The path the witness list leads with is one a real profile HAS."""
        master = real_layout_root["master"]
        assert profile_seed.LOGIN_WITNESSES[0] == "Default/Network/Cookies"
        assert (master / profile_seed.LOGIN_WITNESSES[0]).exists()

    def test_newer_network_cookies_alone_makes_the_snapshot_stale(
        self, real_layout_root
    ):
        """A pure COOKIE login — no saved password, no autofill — is a reason to
        refresh. This is the whole of F-892: the shipped list could not see it,
        and the settled fixture leaves the cookie jar as the ONLY thing newer
        than the marker."""
        _settle(real_layout_root)
        assert clone_storage._snapshot_needs_refresh() is False, "fixture not settled"
        _touch(
            real_layout_root["master"] / "Default" / "Network" / "Cookies", b"logged-in"
        )
        assert clone_storage._snapshot_needs_refresh() is True

    def test_legacy_cookie_path_still_witnesses(self, real_layout_root):
        """A profile carried over from a pre-96 Chrome keeps its witness."""
        _settle(real_layout_root)
        _touch(real_layout_root["master"] / "Default" / "Cookies", b"pre-96")
        assert clone_storage._snapshot_needs_refresh() is True

    def test_untouched_real_profile_is_not_stale(self, real_layout_root):
        """No login since the marker — nothing to refresh."""
        _settle(real_layout_root)
        assert clone_storage._snapshot_needs_refresh() is False

    @pytest.mark.parametrize(
        "literal",
        [*profile_seed.LOGIN_WITNESSES, profile_seed.MARKER_NAME],
    )
    def test_seed_literals_have_exactly_one_home(self, literal):
        """One home: no module under the package may spell a witnessed path —
        or the marker's own filename — a second time, docstrings included.

        A second copy is how the shipped witness list came to name a file
        Chrome stopped writing four years ago and nobody noticed. The marker
        name joined this pin in the review round: it was spelled twice at
        6e3424e, in `profile_seed` and in `clone_storage._clone_needs_refresh`,
        which broke the one-home claim inside the commit that made it.
        """
        spellings = sorted(
            path.resolve()
            for path in PACKAGE.rglob("*.py")
            if literal in path.read_text(encoding="utf-8")
        )
        assert spellings == [Path(profile_seed.__file__).resolve()], (
            f"{literal!r} is spelled in {[p.name for p in spellings]}"
        )

    def test_the_dead_refresh_window_is_gone(self):
        """`_clone_needs_refresh` and `_profile_refresh_days` had no callers and
        carried the second spelling of the marker name (review M2). The knob
        they read, `BROWSER_PROFILE_REFRESH_DAYS`, KEEPS its Settings field —
        a `.env` naming a field the model no longer has is an `extra="forbid"`
        crash — but nothing presents it as live: it is COMMENTED OUT in
        `.env.example` (the name stays, which is all
        `test_env_example_documents_every_field` asks) and REMOVED from both
        shipped example configs, which used to set it."""
        assert not hasattr(clone_storage, "_clone_needs_refresh")
        assert not hasattr(clone_storage, "_profile_refresh_days")
        assert hasattr(get_settings(), "browser_profile_refresh_days")
        repo_root = Path(__file__).resolve().parent.parent
        env_example = (repo_root / ".env.example").read_text(encoding="utf-8")
        assert "#BROWSER_PROFILE_REFRESH_DAYS" in env_example
        for example in ("examples/claude.mcp.json", "examples/codex.config.toml"):
            text = (repo_root / example).read_text(encoding="utf-8")
            assert "BROWSER_PROFILE_REFRESH_DAYS" not in text, example


# ---------------------------------------------------------------------------
# F-893 — a refresh that copied nothing must not report success
# ---------------------------------------------------------------------------


class TestRefusedRefreshIsNotSuccess:
    def test_held_snapshot_reports_the_refusal(self, real_layout_root):
        """MEASURED precondition: on 2026-09-20 a Chrome was running ON
        ``C:\\stealth-mcp-browser-sessions\\master-snapshot`` and its
        ``Default/Network/Cookies`` was 7 s NEWER than the master's, with a
        ``Local State`` 133,607 B against the master's 90,406 — a divergence a
        copy from master cannot produce. Every refresh in that window returned
        ``snapshot_refreshed: True`` having moved nothing."""
        held_profile(real_layout_root["snapshot"])
        result = clone_storage._refresh_master_snapshot_if_safe("test")
        assert result["snapshot_refreshed"] is False
        assert result["snapshot_error"] == "snapshot-in-use"

    def test_held_master_still_reports_master_in_use(self, real_layout_root):
        """The existing arm is unchanged: the SOURCE being live is a different
        refusal, decided before the copy is attempted at all."""
        held_profile(real_layout_root["master"])
        result = clone_storage._refresh_master_snapshot_if_safe("test")
        assert result["snapshot_refreshed"] is False
        assert result["snapshot_error"] == "master-in-use"

    def test_a_copy_that_ran_still_reports_success(self, real_layout_root):
        result = clone_storage._refresh_master_snapshot_if_safe("test")
        assert result["snapshot_refreshed"] is True
        assert "snapshot_error" not in result

    def test_the_guard_raises_rather_than_handing_back_an_empty_profile(self, tmp_path):
        """`_require_copied` is unreachable from its two callers TODAY — both
        copy into a directory they have just found free, with no `await` between
        the check and the copy (review m4). It is pinned directly because it is
        one inserted `await` away from being reachable, and what it would hand
        back is an empty directory the caller is told is their profile."""
        assert clone_storage._require_copied(None, tmp_path) is None
        with pytest.raises(ToolError, match="refused"):
            clone_storage._require_copied(clone_storage.TARGET_IN_USE, tmp_path)

    def test_every_copy_site_either_reports_or_guards(self):
        """No third site may discard the answer. Exactly one call to
        `_copy_profile_tree` is allowed to be unguarded — the refresh, which
        READS the refusal into its result — and every other must be wrapped in
        `_require_copied`."""
        import ast

        tree = ast.parse(Path(clone_storage.__file__).read_text(encoding="utf-8"))

        def calls_to(node, name):
            return [
                sub
                for sub in ast.walk(node)
                if isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Name)
                and sub.func.id == name
            ]

        copies = calls_to(tree, "_copy_profile_tree")
        guarded = [
            inner
            for guard in calls_to(tree, "_require_copied")
            for inner in calls_to(guard, "_copy_profile_tree")
        ]
        unguarded = [c for c in copies if c not in guarded]
        assert len(copies) == 3, [c.lineno for c in copies]
        assert [c.lineno for c in unguarded] == [
            c.lineno
            for c in calls_to(
                next(
                    n
                    for n in tree.body
                    if isinstance(n, ast.FunctionDef)
                    and n.name == "_refresh_master_snapshot_if_safe"
                ),
                "_copy_profile_tree",
            )
        ]

    def test_copy_tree_returns_the_refusal(self, real_layout_root):
        """The report is the copier's answer, not a second liveness read at the
        caller — one home for "did this copy run"."""
        target = real_layout_root["sessions"] / "held-clone"
        target.mkdir()
        held_profile(target)
        refused = clone_storage._copy_profile_tree(
            real_layout_root["master"], target, real_layout_root["sessions"], "test"
        )
        assert refused == clone_storage.TARGET_IN_USE
        assert not (target / MARKER).exists()


# ---------------------------------------------------------------------------
# F-894 — a reserved name never silently means a different profile
# ---------------------------------------------------------------------------


class TestReservedProfileNames:
    @pytest.mark.parametrize("name", sorted(profile_seed.RESERVED_NAMES))
    @pytest.mark.asyncio
    async def test_reserved_name_raises_and_creates_nothing(
        self, real_layout_root, name
    ):
        with pytest.raises(ToolError) as excinfo:
            await clone_storage.resolve_profile_selection(name)
        assert name in str(excinfo.value)
        assert not (real_layout_root["sessions"] / name).exists()

    @pytest.mark.asyncio
    async def test_reserved_name_is_case_insensitive(self, real_layout_root):
        with pytest.raises(ToolError):
            await clone_storage.resolve_profile_selection("Master")
        assert not (real_layout_root["sessions"] / "Master").exists()

    @pytest.mark.asyncio
    async def test_snapshot_path_is_refused(self, real_layout_root):
        """A browser driven on the snapshot writes into the seed — F-893's
        precondition, closed at the door it came in through."""
        with pytest.raises(ToolError, match="seed"):
            await clone_storage.resolve_profile_selection(
                str(real_layout_root["snapshot"])
            )

    @pytest.mark.asyncio
    async def test_master_path_selects_the_master_role(self, real_layout_root):
        """The master by absolute path is the MASTER, not an explicit clone of
        itself: driving it directly is how a human logs in, and the role is what
        makes ``close_instance`` refresh the seed afterwards."""
        result = await clone_storage.resolve_profile_selection(
            str(real_layout_root["master"])
        )
        assert result["profile_role"] == "master"
        assert Path(result["user_data_dir"]) == real_layout_root["master"]

    @pytest.mark.asyncio
    async def test_drive_relative_path_is_refused(self, real_layout_root):
        """MEASURED: ``sessions/stealth-mcp-browser-sessionssessionsstealth-
        chrome-devtools-mcp-f876e3d7f2ec`` (0.35 GB) is exactly what the
        resolver produces from a drive-qualified path whose separators were
        eaten — ``Path.is_absolute()`` is False for ``C:foo``, so it was
        anchored as a bare session NAME."""
        mangled = "C:stealth-mcp-browser-sessionssessionsproject-f876e3d7f2ec"
        with pytest.raises(ToolError, match="absolute"):
            await clone_storage.resolve_profile_selection(mangled)
        assert list(real_layout_root["sessions"].iterdir()) == []

    def test_the_drive_refusal_does_not_depend_on_the_host_flavour(
        self, monkeypatch, tmp_path
    ):
        """CI RED at 6e3424e, all six Linux/macOS cells, Windows green.

        MEASURED: ``PureWindowsPath("C:foo").drive`` is ``"C:"`` and
        ``PurePosixPath("C:foo").drive`` is ``""``, so a rule that reads the
        HOST's flavour refuses the mangled path on Windows and lets the identical
        string through on every other platform. The production fix reads the
        drive under the flavour that HAS drives and "would this be anchored"
        under the flavour the resolver actually anchors with — so the refusal is
        the same answer on both, which is what this pin holds.

        Patching ``profile_seed.Path`` to ``PurePosixPath`` is what a POSIX host
        IS for this function: the only thing it builds from the caller's string
        is ``Path(requested)``. ``reserved_reason`` is called directly because
        ``anchor`` needs a CONCRETE path (``expanduser``), and it is the anchored
        directory, not the flavour, that ``resolved`` carries.
        """
        monkeypatch.setattr(profile_seed, "Path", PurePosixPath)
        snapshot = tmp_path / "master-snapshot"
        mangled = "C:stealth-mcp-browser-sessionssessionsproject-f876e3d7f2ec"
        reason = profile_seed.reserved_reason(mangled, tmp_path / mangled, snapshot)
        assert reason is not None and "absolute" in reason
        # And it must not over-refuse on that same host: an ordinary session
        # name and a POSIX absolute path name no drive under either flavour.
        assert profile_seed.reserved_reason("acme", tmp_path / "acme", snapshot) is None
        assert profile_seed.reserved_reason("/srv/p", tmp_path / "p", snapshot) is None

    @pytest.mark.asyncio
    async def test_a_held_snapshot_is_refused_before_any_re_attach(
        self, real_layout_root, call_tool, patched_server, monkeypatch
    ):
        """The refusal must be asked at the SPAWN, not only in the resolver.

        F-888's `adopt_held_profile` runs in front of profile selection and
        matches the requested directory against live browsers, so at 6e3424e a
        snapshot path WITH A BROWSER ON IT was re-attached to and the resolver
        never saw it — which is precisely the machine state F-893 measured. The
        resolver-only pin passed because its fixture had no browser there.

        Both halves matter: the raise, and that no adoption was ATTEMPTED.
        Without the second, this can pass again for the same wrong reason —
        which is also why the patch below is `raising=True` (the default):
        `raising=False` would have let a RENAMED `adopt_held_profile` satisfy
        `attempts == []` by never having been patched at all (review n4).

        `headless=True` is load-bearing and not tidiness: at eed4ae9 this pin was
        RED on every Linux cell (run 35532848939) and green on Windows and macOS,
        because a headed spawn on a runner with no desktop answered F-808's "this
        context cannot display a window" and never reached the reservation. The
        production fix is the ORDER — the caller-shaped refusal now runs ahead of
        the host-shaped one — and asking headless here is what keeps the pin's
        subject the reservation rather than the runner's desktop.
        """
        held_profile(real_layout_root["snapshot"])
        attempts = []

        async def never_reached(*args, **kwargs):
            attempts.append(kwargs.get("user_data_dir") or args)
            raise AssertionError("adopt_held_profile must not be reached")

        monkeypatch.setattr(
            clone_storage.profile_lock,
            "profile_hold",
            lambda *a, **k: SimpleNamespace(pid=4321, reason="held-by-test"),
        )
        srv = patched_server(browser_manager=FakeBrowserManager())
        monkeypatch.setattr(rt.browser_reattach, "adopt_held_profile", never_reached)

        with pytest.raises(ToolError, match="seed"):
            await call_tool(
                srv,
                "spawn_browser",
                user_data_dir=str(real_layout_root["snapshot"]),
                headless=True,
                sandbox=False,
            )
        assert attempts == []

    @pytest.mark.asyncio
    async def test_a_reserved_name_is_refused_where_no_window_can_be_shown(
        self, real_layout_root, call_tool, patched_server, monkeypatch
    ):
        """The CALLER-shaped refusal runs ahead of the HOST-shaped one.

        CI RED at eed4ae9 on every Linux cell (run 35532848939) and green on
        Windows and macOS: the F-808 headed-visibility guard stood first, so on a
        runner with no desktop a reserved ``user_data_dir`` was answered with
        "this context cannot display a window" and the reservation was never
        asked at all. Both guards are pre-flight and side-effect-free, so their
        order decides nothing but which message the caller gets — and a reserved
        path is refused on EVERY host, while "no desktop here" is a fact about
        this one, so answering with the second sends a caller looking for a
        display they do not need.

        Patching ``can_deliver_headed_window`` to False is what that runner IS
        for this handler, which is what lets the order be pinned on every
        platform instead of only where a desktop happens to be absent. The spawn
        is deliberately HEADED, so the guard being ordered would fire.
        """
        monkeypatch.setattr(desktop_launch, "can_deliver_headed_window", lambda: False)
        srv = patched_server(browser_manager=FakeBrowserManager())

        with pytest.raises(ToolError) as excinfo:
            await call_tool(srv, "spawn_browser", user_data_dir="master", sandbox=False)

        message = str(excinfo.value)
        assert "user_data_dir rejected" in message
        assert "F-808" not in message

    @pytest.mark.asyncio
    async def test_an_ordinary_name_is_unaffected(self, real_layout_root):
        result = await clone_storage.resolve_profile_selection("acme")
        assert result["profile_role"] == "explicit"
        assert Path(result["user_data_dir"]).name == "acme"

    @pytest.mark.asyncio
    async def test_an_existing_reserved_dir_is_told_how_to_reach_it(
        self, real_layout_root
    ):
        """MEASURED: `sessions/master` exists on this machine, 0.46 GB. "pick
        another name" is advice for a NEW session and useless to an operator who
        already has that directory, so the message names the escape (review M3).
        """
        existing = real_layout_root["sessions"] / "master"
        existing.mkdir()
        with pytest.raises(ToolError) as excinfo:
            await clone_storage.resolve_profile_selection("master")
        message = str(excinfo.value)
        assert "absolute path" in message
        assert str(existing) in message

    def test_the_profiles_verb_still_lists_a_refused_name(
        self, real_layout_root, capsys
    ):
        """Refusing the NAME must not hide the DIRECTORY (review M3).

        The refusal message, CHANGELOG and RUNBOOK all tell an operator their
        existing ``sessions/master`` is untouched and still listed — so the
        listing is a CLAIM the docs make and is pinned as one. It is the only
        way to see the size before choosing between renaming it and opening it
        by absolute path. MEASURED: that directory exists on this machine at
        0.46 GB, marker ``explicit-master-snapshot``.
        """
        from stealth_chrome_devtools_mcp import cli

        existing = real_chrome_profile(real_layout_root["sessions"] / "master")
        rows = cli._collect_profiles(clone_storage)
        assert [row["path"] for row in rows].count(existing) == 1

        assert cli.main(["profiles"]) == 0
        out = capsys.readouterr().out
        # Two rows are named `master`: the master profile itself and the
        # session directory the reserved name refuses to create a second time.
        named_master = [
            line for line in out.splitlines() if line.strip().startswith("master ")
        ]
        assert len(named_master) == 2, out

    @pytest.mark.asyncio
    async def test_a_reserved_name_with_no_directory_says_nothing_about_one(
        self, real_layout_root
    ):
        with pytest.raises(ToolError) as excinfo:
            await clone_storage.resolve_profile_selection("default")
        assert "still openable" not in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_a_reserved_name_never_walks_to_master_2(self, real_layout_root):
        """The F-871 walk must not be reachable from a reserved name: a refusal
        that turned into ``master-2`` would be the same silent substitution."""
        (real_layout_root["sessions"] / "master").mkdir()
        held_profile(real_layout_root["sessions"] / "master")
        with pytest.raises(ToolError):
            await clone_storage.resolve_profile_selection("master")
        assert not (real_layout_root["sessions"] / "master-2").exists()


# ---------------------------------------------------------------------------
# F-895 — seed provenance is recorded and reported
# ---------------------------------------------------------------------------


class TestSeedProvenance:
    @pytest.mark.asyncio
    async def test_a_new_session_records_its_seed(self, real_layout_root):
        await clone_storage.resolve_profile_selection("acme")
        marker = json.loads(
            (real_layout_root["sessions"] / "acme" / MARKER).read_text(encoding="utf-8")
        )
        assert marker["seeded_from"] == "master-snapshot"
        assert marker["seeded_at"] == marker["created_at"]
        # The legacy keys a 2.1.10 reader looks for are still written.
        assert marker["source"] == str(real_layout_root["snapshot"])
        assert marker["source_kind"] == "explicit-master-snapshot"

    @pytest.mark.asyncio
    async def test_diagnostics_carry_seed_and_staleness(self, real_layout_root):
        selection = await clone_storage.resolve_profile_selection("acme")
        public = clone_storage._public_profile_selection(selection)
        assert public["seeded_from"] == "master-snapshot"
        assert public["seeded_at"]
        assert public["seed_changed_since"] is False

    @pytest.mark.asyncio
    async def test_a_login_in_the_seed_shows_as_changed(self, real_layout_root):
        selection = await clone_storage.resolve_profile_selection("acme")
        jar = real_layout_root["snapshot"] / "Default" / "Network" / "Cookies"
        later = time.time() + 120
        jar.write_bytes(b"new-login")
        os.utime(jar, (later, later))
        public = clone_storage._public_profile_selection(selection)
        assert public["seed_changed_since"] is True

    def test_a_legacy_marker_reads_as_unknown_never_as_fresh(self, tmp_path):
        """``created_at`` is NOT substituted for ``seeded_at``: when a directory
        was made is not a claim about which seed it was made from."""
        legacy = tmp_path / "old-session"
        legacy.mkdir()
        (legacy / MARKER).write_text(
            json.dumps(
                {
                    "source": str(tmp_path / "master-snapshot"),
                    "source_kind": "explicit-master-snapshot",
                    "created_at": "2026-08-04T10:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        provenance = profile_seed.provenance(legacy)
        assert provenance["seeded_from"] == profile_seed.UNKNOWN_SEED
        assert provenance["seeded_at"] is None
        assert provenance["seed_changed_since"] is None

    def test_a_profile_with_no_marker_reads_as_unknown(self, tmp_path):
        bare = tmp_path / "master"
        bare.mkdir()
        assert profile_seed.provenance(bare) == {
            "seeded_from": profile_seed.UNKNOWN_SEED,
            "seeded_at": None,
            "seed_changed_since": None,
        }

    def test_the_profiles_verb_prints_seed_and_staleness(
        self, real_layout_root, capsys
    ):
        """The CLI says out loud what the marker knows — the one line an
        operator can read to see that a session is nine days behind its seed."""
        from stealth_chrome_devtools_mcp import cli

        session = real_layout_root["sessions"] / "acme"
        real_chrome_profile(session)
        profile_seed.write_marker(
            session,
            source=real_layout_root["snapshot"],
            source_kind="explicit-master-snapshot",
            seeded_from="master-snapshot",
        )
        jar = real_layout_root["snapshot"] / "Default" / "Network" / "Cookies"
        later = time.time() + 120
        jar.write_bytes(b"new-login")
        os.utime(jar, (later, later))

        assert cli.main(["profiles"]) == 0
        out = capsys.readouterr().out
        assert "seeded from master-snapshot at" in out
        assert "SEED CHANGED SINCE" in out

    def test_the_profiles_verb_says_unknown_for_a_legacy_marker(
        self, real_layout_root, capsys
    ):
        from stealth_chrome_devtools_mcp import cli

        session = real_layout_root["sessions"] / "old"
        session.mkdir()
        (session / MARKER).write_text(
            json.dumps({"source_kind": "explicit-master-snapshot"}), encoding="utf-8"
        )
        assert cli.main(["profiles"]) == 0
        out = capsys.readouterr().out
        assert "seeded from unknown (when: unknown)" in out
        assert "SEED CHANGED SINCE" not in out

    def test_a_seed_with_no_witnesses_cannot_claim_staleness(self, tmp_path):
        """A seed that is not a Chrome profile answers None, never False — an
        absent witness is no evidence, and False would read as "still fresh"."""
        seed = tmp_path / "seed"
        seed.mkdir()
        assert profile_seed.changed_since(seed, "2026-01-01T00:00:00Z") is None
