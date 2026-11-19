"""``atomic_file``: the one atomic write the registries, the supervisor state and
the launch marker publish through (T-19641).

Every guarantee a replaced site had is pinned here once: a reader sees the whole
old file or the whole new one, a Windows sharing refusal is retried and a
permanent one surfaces, and the staged file never outlives a failed write.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from stealth_chrome_devtools_mcp.embedded import atomic_file


def _names(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


class TestWrite:
    def test_text_lands_whole_and_leaves_nothing_staged(self, tmp_path):
        target = tmp_path / "state.txt"
        atomic_file.write_text(target, "héllo")
        assert target.read_text(encoding="utf-8") == "héllo"
        assert _names(tmp_path) == ["state.txt"]

    def test_json_round_trips_and_indent_is_honoured(self, tmp_path):
        target = tmp_path / "state.json"
        atomic_file.write_json(target, {"a": [1, 2]}, indent=2)
        assert json.loads(target.read_text(encoding="utf-8")) == {"a": [1, 2]}
        assert "\n  " in target.read_text(encoding="utf-8")

    def test_it_replaces_an_existing_file(self, tmp_path):
        target = tmp_path / "state.json"
        atomic_file.write_json(target, {"v": 1})
        atomic_file.write_json(target, {"v": 2})
        assert json.loads(target.read_text(encoding="utf-8")) == {"v": 2}

    def test_the_staged_name_carries_the_pid(self, tmp_path, monkeypatch):
        """Two processes staging at once must not collide on the temp name."""
        staged: list[str] = []
        real = Path.write_text

        def spy(self, *args, **kwargs):
            staged.append(self.name)
            return real(self, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", spy)
        atomic_file.write_text(tmp_path / "x.json", "{}")
        assert staged == [f"x.json.{os.getpid()}.tmp"]

    def test_the_parent_is_created_unless_told_not_to(self, tmp_path):
        atomic_file.write_text(tmp_path / "a" / "b.json", "{}")
        assert (tmp_path / "a" / "b.json").exists()
        gone = tmp_path / "deleted-profile"
        with pytest.raises(OSError):
            atomic_file.write_text(gone / "marker", "owner", make_parents=False)
        assert not gone.exists(), "a write must not bring a deleted directory back"

    def test_an_unserialisable_payload_stages_nothing(self, tmp_path):
        with pytest.raises(TypeError):
            atomic_file.write_json(tmp_path / "x.json", {"bad": object()})
        assert _names(tmp_path) == []


class TestSharingRefusal:
    """Windows raises PermissionError from ``replace`` while ANOTHER process has the
    target open; a bounded retry outlasts the microsecond window."""

    @pytest.fixture(autouse=True)
    def _fast_retries(self, monkeypatch):
        monkeypatch.setattr(atomic_file, "COMMIT_RETRY_SECONDS", 0)

    def test_a_refusal_that_clears_is_retried_into_a_success(
        self, tmp_path, monkeypatch
    ):
        target = tmp_path / "state.json"
        atomic_file.write_json(target, {"v": 1})
        real = Path.replace
        attempts: list[int] = []

        def busy_twice(self, destination):
            attempts.append(1)
            if len(attempts) <= 2:
                raise PermissionError(32, "used by another process")
            return real(self, destination)

        monkeypatch.setattr(Path, "replace", busy_twice)
        atomic_file.write_json(target, {"v": 2})
        assert len(attempts) == 3
        assert json.loads(target.read_text(encoding="utf-8")) == {"v": 2}
        assert _names(tmp_path) == ["state.json"]

    def test_a_permanent_refusal_surfaces_after_the_bound_with_the_old_file_whole(
        self, tmp_path, monkeypatch
    ):
        target = tmp_path / "state.json"
        atomic_file.write_json(target, {"v": 1})
        attempts: list[int] = []

        def always_busy(self, destination):
            attempts.append(1)
            raise PermissionError(5, "used by another process")

        monkeypatch.setattr(Path, "replace", always_busy)
        with pytest.raises(PermissionError):
            atomic_file.write_json(target, {"v": 2})
        assert len(attempts) == atomic_file.COMMIT_ATTEMPTS
        assert json.loads(target.read_text(encoding="utf-8")) == {"v": 1}
        assert _names(tmp_path) == ["state.json"], "the staged file is removed"

    def test_only_a_sharing_refusal_is_retried(self, tmp_path, monkeypatch):
        attempts: list[int] = []

        def broken(self, destination):
            attempts.append(1)
            raise OSError(28, "no space left on device")

        monkeypatch.setattr(Path, "replace", broken)
        with pytest.raises(OSError, match="no space"):
            atomic_file.write_json(tmp_path / "state.json", {})
        assert attempts == [1]


class TestCrashBetweenWriteAndReplace:
    """The reader's guarantee: whatever stops the writer, the old file is whole."""

    @pytest.mark.parametrize("stop", [RuntimeError, KeyboardInterrupt, SystemExit])
    def test_the_old_file_survives_and_the_staged_one_is_removed(
        self, tmp_path, monkeypatch, stop
    ):
        target = tmp_path / "state.json"
        atomic_file.write_json(target, {"v": "old"})

        def die(self, destination):
            raise stop("killed between the write and the replace")

        monkeypatch.setattr(Path, "replace", die)
        with pytest.raises(stop):
            atomic_file.write_json(target, {"v": "new"})
        assert json.loads(target.read_text(encoding="utf-8")) == {"v": "old"}
        assert _names(tmp_path) == ["state.json"]

    def test_a_write_that_dies_halfway_never_reaches_the_target(
        self, tmp_path, monkeypatch
    ):
        target = tmp_path / "state.json"
        atomic_file.write_json(target, {"v": "old"})
        real = Path.write_text

        def half_then_die(self, text, *args, **kwargs):
            real(self, text[: len(text) // 2], *args, **kwargs)
            raise OSError(28, "no space left on device")

        monkeypatch.setattr(Path, "write_text", half_then_die)
        with pytest.raises(OSError, match="no space"):
            atomic_file.write_json(target, {"v": "new" * 50})
        assert json.loads(target.read_text(encoding="utf-8")) == {"v": "old"}
        assert _names(tmp_path) == ["state.json"]

    def test_a_process_killed_for_real_leaves_the_old_file_whole(self, tmp_path):
        """No cleanup runs in a killed process: only the target's own integrity
        can be promised, and the leftover is the accepted, bounded gap."""
        target = tmp_path / "state.json"
        atomic_file.write_json(target, {"v": "old"})
        code = (
            "import os, sys\n"
            "from pathlib import Path\n"
            "from stealth_chrome_devtools_mcp.embedded import atomic_file\n"
            "def die(self, destination): os._exit(9)\n"
            "Path.replace = die\n"
            "atomic_file.write_json(Path(sys.argv[1]), {'v': 'new'})\n"
        )
        proc = subprocess.run(  # noqa: S603  PERMANENT(test child, fixed argv)
            [sys.executable, "-c", code, str(target)], timeout=120, check=False
        )
        assert proc.returncode == 9
        assert json.loads(target.read_text(encoding="utf-8")) == {"v": "old"}
        leftovers = [n for n in _names(tmp_path) if n != "state.json"]
        assert len(leftovers) <= 1
        assert all(n.endswith(".tmp") for n in leftovers)


class TestPrivate:
    """``narrow=``: the vault's owner-only publish goes through the one writer
    (audit 7, F2), with the guarantees it had as a private copy."""

    def test_the_temp_is_narrowed_before_the_first_byte_and_flushed(
        self, tmp_path, monkeypatch
    ):
        target = tmp_path / "vault.json"
        seen: list[tuple[str, int]] = []
        synced: list[int] = []
        real_fsync = os.fsync

        def narrow(path: Path) -> None:
            seen.append((path.name, path.stat().st_size))

        def fsync(descriptor: int) -> None:
            synced.append(descriptor)
            real_fsync(descriptor)

        monkeypatch.setattr(atomic_file.os, "fsync", fsync)
        atomic_file.write_text(target, '{"a":1}', narrow=narrow)
        assert target.read_text(encoding="utf-8") == '{"a":1}'
        assert seen == [(f"vault.json.{os.getpid()}.tmp", 0)]
        assert len(synced) == 1
        assert _names(tmp_path) == ["vault.json"]

    def test_the_temp_is_created_exclusively_with_mode_0600(
        self, tmp_path, monkeypatch
    ):
        """The POSIX mode check, scripted so it runs on every platform: what the
        file is created with is what ``os.open`` is asked for."""
        asked: list[tuple[int, int]] = []
        real_open = os.open

        def spy(path, flags, mode=0o777, **kwargs):
            asked.append((flags, mode))
            return real_open(path, flags, mode, **kwargs)

        monkeypatch.setattr(atomic_file.os, "open", spy)
        atomic_file.write_text(tmp_path / "v.json", "{}", narrow=lambda path: None)
        [(flags, mode)] = asked
        assert mode == 0o600
        assert flags & os.O_EXCL
        assert flags & os.O_CREAT
        assert flags & os.O_WRONLY

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
    def test_the_published_file_has_no_group_or_other_bits(self, tmp_path):
        target = tmp_path / "v.json"
        atomic_file.write_text(target, "{}", narrow=lambda path: None)
        assert target.stat().st_mode & 0o077 == 0

    def test_the_real_narrowing_publishes_a_readable_file(self, tmp_path):
        """On Windows this is the DACL call; the file is still ours to read."""
        from stealth_chrome_devtools_mcp.embedded import backend_token

        target = tmp_path / "v.json"
        atomic_file.write_text(target, '{"k":"v"}', narrow=backend_token.owner_only)
        atomic_file.write_text(target, '{"k":"w"}', narrow=backend_token.owner_only)
        assert json.loads(target.read_text(encoding="utf-8")) == {"k": "w"}
        assert _names(tmp_path) == ["v.json"]

    def test_a_leftover_of_a_dead_writer_with_this_pid_is_replaced_not_followed(
        self, tmp_path
    ):
        target = tmp_path / "v.json"
        stale = tmp_path / f"v.json.{os.getpid()}.tmp"
        stale.write_text("old leftover", encoding="utf-8")
        atomic_file.write_text(target, "new", narrow=lambda path: None)
        assert target.read_text(encoding="utf-8") == "new"
        assert _names(tmp_path) == ["v.json"]

    def test_a_failing_narrowing_leaves_the_old_file_whole_and_nothing_staged(
        self, tmp_path
    ):
        target = tmp_path / "v.json"
        atomic_file.write_text(target, "old")

        def refuse(path: Path) -> None:
            raise OSError("cannot restrict")

        with pytest.raises(OSError, match="cannot restrict"):
            atomic_file.write_text(target, "new", narrow=refuse)
        assert target.read_text(encoding="utf-8") == "old"
        assert _names(tmp_path) == ["v.json"]

    def test_a_write_with_no_narrowing_is_as_it_was_and_is_not_flushed(
        self, tmp_path, monkeypatch
    ):
        def no_fsync(descriptor: int) -> None:
            raise AssertionError("an ordinary state write is not fsynced")

        monkeypatch.setattr(atomic_file.os, "fsync", no_fsync)
        atomic_file.write_json(tmp_path / "s.json", {"v": 1})
        assert json.loads((tmp_path / "s.json").read_text(encoding="utf-8")) == {"v": 1}

    def test_the_vault_file_is_published_through_the_one_writer(
        self, tmp_path, monkeypatch
    ):
        from stealth_chrome_devtools_mcp.embedded import backend_token, vault_crypto

        calls: list[dict[str, object]] = []
        real = atomic_file.write_text

        def spy(path, text, **kwargs):
            calls.append(kwargs)
            real(path, text, **kwargs)

        monkeypatch.setattr(atomic_file, "write_text", spy)
        vault_crypto.write_file(tmp_path / "v.json", {"v": 1})
        assert calls == [
            {"make_parents": False, "narrow": backend_token.owner_only},
        ]
