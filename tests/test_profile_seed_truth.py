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
from pathlib import Path

import pytest

from fakes import held_profile
from stealth_chrome_devtools_mcp.embedded import clone_storage, profile_seed
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

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

    def test_witness_paths_have_exactly_one_home(self):
        """One home: no module under the package may spell a witnessed path a
        second time. A second copy is how the shipped list came to name a file
        Chrome stopped writing four years ago and nobody noticed."""
        for rel in profile_seed.LOGIN_WITNESSES:
            spellings = [
                path
                for path in PACKAGE.rglob("*.py")
                if rel in path.read_text(encoding="utf-8")
            ]
            assert spellings == [
                profile_seed.__file__ and Path(profile_seed.__file__)
            ], f"{rel!r} is spelled in {[p.name for p in spellings]}"


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

    @pytest.mark.asyncio
    async def test_an_ordinary_name_is_unaffected(self, real_layout_root):
        result = await clone_storage.resolve_profile_selection("acme")
        assert result["profile_role"] == "explicit"
        assert Path(result["user_data_dir"]).name == "acme"

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
