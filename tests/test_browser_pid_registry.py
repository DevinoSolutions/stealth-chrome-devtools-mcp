"""Pins for the browser_pids.json tracking record and the F-808 fratricide fix.

The record is shared by every backend on the machine, so the two properties
pinned here are the ones a single-backend design never needed: a write may only
replace the entries it is writing (never the whole file), and startup recovery
may only reap entries no LIVE owner still holds.

Every pin passes an EXPLICIT path — a tmp_path-derived pid_file, no
monkeypatching of module globals — which is the same property
TestNoDefaultPaths enforces on the production signatures, and the only thing
keeping a test run off the developer's live ~/.stealth-mcp/browser_pids.json.
"""

import inspect
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from stealth_chrome_devtools_mcp.embedded import browser_pid_registry as registry
from stealth_chrome_devtools_mcp.embedded.process_cleanup import ProcessCleanup


def _entry(
    pid,
    *,
    owner_pid=None,
    owner_create_time=None,
    user_data_dir=None,
    auto_clone=False,
    uses_custom_data_dir=None,
):
    """A tracked-browser entry as it is written to disk.

    owner_pid=None means the entry carries NO owner keys at all — the shape
    every release up to 2.0.3 wrote, and a different thing from an owner
    recorded as null.
    """
    entry = {
        "pid": pid,
        "create_time": None,
        "user_data_dir": user_data_dir,
        "uses_custom_data_dir": uses_custom_data_dir,
        "auto_clone": auto_clone,
        "timestamp": 0,
    }
    if owner_pid is not None:
        entry["owner_pid"] = owner_pid
        entry["owner_create_time"] = owner_create_time
    return entry


def _seed(path: Path, entries: dict) -> None:
    path.write_text(json.dumps({"browser_processes": entries, "timestamp": 0}))


def _read(path: Path) -> dict:
    return json.loads(path.read_text())["browser_processes"]


def _cleanup(tmp_path: Path, name: str = "browser_pids.json") -> ProcessCleanup:
    """A ProcessCleanup whose record is in tmp_path, built without __init__.

    Mirrors test_process_cleanup.py's helper, but never the sibling that points
    pid_file at a real home path: everything here writes.
    """
    pc = ProcessCleanup.__new__(ProcessCleanup)
    pc.pid_file = tmp_path / name
    pc.tracked_pids = set()
    pc.browser_processes = {}
    # 0 disables the temp-profile sweep, which would otherwise walk the real
    # system temp dir at the end of every recovery.
    pc.orphan_profile_max_age_seconds = 0
    pc._init_time = 1700000100.0
    return pc


# ---------------------------------------------------------------------------
# P7: the guard that keeps every other pin honest
# ---------------------------------------------------------------------------


def _public_functions():
    for name, obj in vars(registry).items():
        if name.startswith("_") or not inspect.isfunction(obj):
            continue
        if obj.__module__ == registry.__name__:
            yield name, obj


class TestNoDefaultPaths:
    def test_no_public_function_defaults_its_path_parameter(self):
        """The module docstring's corollary, enforced: the caller's binding is
        what selects the file. A default would bind a module global at def-time
        and silently ignore the pid_file redirection every pin here relies on.
        """
        offenders = [
            f"{name}({param})"
            for name, func in _public_functions()
            for param in inspect.signature(func).parameters.values()
            if (
                param.name in ("path", "paths")
                and param.default is not inspect.Parameter.empty
            )
            or isinstance(param.default, Path)
        ]
        assert offenders == [], (
            f"path parameters must stay required, but {offenders} default theirs"
        )

    def test_guard_is_not_vacuous(self):
        assert len(list(_public_functions())) >= 3


# ---------------------------------------------------------------------------
# P6: the schema, including the owner keys the normalizer must learn
# ---------------------------------------------------------------------------


class TestOwnerFieldsRoundTrip:
    def test_recorded_owner_survives_normalization(self, tmp_path):
        """The normalizer builds a fixed key set and drops everything else, so
        owner fields written by track are only real if it knows them by name.
        """
        record = tmp_path / "pids.json"
        _seed(record, {"inst": _entry(1234, owner_pid=4242, owner_create_time=99.5)})

        loaded = registry.read_entries(record)["inst"]

        assert loaded["owner_pid"] == 4242
        assert loaded["owner_create_time"] == 99.5

    def test_legacy_int_entry_has_no_owner_key_at_all(self):
        """Absent, not None: `is_reapable` decides on the KEY, and a recorded
        null owner_create_time is a legitimate value (psutil could not read it).
        """
        normalized = registry.normalize_entries({"legacy": 1111})["legacy"]

        assert normalized["pid"] == 1111
        assert "owner_pid" not in normalized
        assert "owner_create_time" not in normalized

    def test_legacy_dict_entry_has_no_owner_key_at_all(self):
        raw = {"legacy": {"pid": 2222, "user_data_dir": "/x"}}

        normalized = registry.normalize_entries(raw)["legacy"]

        assert "owner_pid" not in normalized


# ---------------------------------------------------------------------------
# P1: legacy entries are reapable — the 2.0.3 upgrade path
# ---------------------------------------------------------------------------


class TestLegacyEntriesAreReapable:
    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param(1111, id="legacy-bare-int"),
            pytest.param({"pid": 1111}, id="legacy-dict-without-owner"),
        ],
    )
    def test_entry_without_owner_fields_is_reapable(self, raw):
        """No owner recorded means unowned, decided BEFORE the liveness
        callable is consulted — so a deliberately generous callable cannot
        rescue a legacy entry. Without this, upgrading from 2.0.3 would adopt
        every orphan the old release left behind and never reap one again.
        """
        entry = registry.normalize_entries({"legacy": raw})["legacy"]
        consulted = []

        def never_reachable(owner_pid, owner_create_time):
            consulted.append(owner_pid)
            return True

        assert registry.is_reapable(entry, never_reachable) is True
        assert consulted == []

    def test_entry_owned_by_a_live_process_is_not_reapable(self):
        entry = registry.normalize_entries(
            {"live": _entry(1, owner_pid=4242, owner_create_time=7.0)}
        )["live"]

        assert registry.is_reapable(entry, lambda pid, ct: True) is False
        assert registry.is_reapable(entry, lambda pid, ct: False) is True

    def test_liveness_callable_receives_the_recorded_owner(self):
        entry = registry.normalize_entries(
            {"live": _entry(1, owner_pid=4242, owner_create_time=7.0)}
        )["live"]
        seen = []

        registry.is_reapable(entry, lambda pid, ct: seen.append((pid, ct)) or True)

        assert seen == [(4242, 7.0)]


# ---------------------------------------------------------------------------
# P4/P5: the write protocol
# ---------------------------------------------------------------------------


class TestWritesMerge:
    def test_save_merges_instead_of_replacing(self, tmp_path):
        """Two backends, one record. Before F-808 each save serialised its whole
        in-memory table over the file, so B's save erased A's entry and A's
        browser was left running with nobody tracking it.
        """
        record = tmp_path / "pids.json"
        backend_a = _cleanup(tmp_path)
        backend_b = _cleanup(tmp_path)
        backend_a.pid_file = record
        backend_b.pid_file = record

        backend_a.browser_processes = {"inst-a": _entry(101)}
        backend_a._save_tracked_pids()
        backend_b.browser_processes = {"inst-b": _entry(202)}
        backend_b._save_tracked_pids()

        recorded = registry.read_entries(record)
        assert set(recorded) == {"inst-a", "inst-b"}
        assert recorded["inst-a"]["pid"] == 101
        assert recorded["inst-b"]["pid"] == 202

    def test_untrack_drops_only_the_named_entry(self, tmp_path):
        """Untracking deletes by id. It cannot delete by omission — that is the
        same whole-file replacement, and it would take every other backend's
        entries with it.
        """
        record = tmp_path / "pids.json"
        _seed(record, {"theirs": _entry(303, owner_pid=4242)})
        pc = _cleanup(tmp_path)
        pc.pid_file = record
        pc.browser_processes = {"mine": _entry(101)}
        pc._save_tracked_pids()

        pc.untrack_browser_process("mine")

        recorded = registry.read_entries(record)
        assert set(recorded) == {"theirs"}


class TestFailedLockLeavesTheRecordIntact:
    def test_failed_lock_does_not_destroy_existing_entries(self, tmp_path, monkeypatch):
        """The pre-F-808 writer opened the record with mode "w" — which
        truncates — and only THEN reached for the lock, swallowing the failure
        with the file already empty. Contention alone was enough to lose every
        tracked browser on the machine.
        """
        record = tmp_path / "pids.json"
        _seed(record, {"theirs": _entry(303, owner_pid=4242)})
        pc = _cleanup(tmp_path)
        pc.pid_file = record
        pc.browser_processes = {"mine": _entry(101)}

        def refuse(_handle):
            raise OSError("lock held elsewhere")

        monkeypatch.setattr(registry, "_LOCK_RETRY_DELAY", 0)
        monkeypatch.setattr(registry, "_acquire", refuse)

        pc._save_tracked_pids()

        assert _read(record) == {"theirs": _entry(303, owner_pid=4242)}

    def test_a_lock_that_cannot_be_had_raises_rather_than_yielding(
        self, tmp_path, monkeypatch
    ):
        """F-607's shape, kept: the lock helper must not yield as if acquired.
        Its callers log and degrade, so raising turns a silent race into a
        skipped write.
        """
        monkeypatch.setattr(registry, "_LOCK_RETRY_DELAY", 0)
        monkeypatch.setattr(
            registry, "_acquire", MagicMock(side_effect=OSError("held"))
        )

        with pytest.raises(OSError):
            registry.update_entries(tmp_path / "pids.json", lambda entries: entries)


class TestRecordParentDir:
    def test_update_creates_the_missing_parent_dir(self, tmp_path):
        """The writer makes the record's OWN parent rather than relying on
        singleton._ensure_state_dir having run first, so a redirected path
        lands where the caller asked.
        """
        record = tmp_path / "sub" / "pids.json"

        registry.update_entries(record, lambda entries: {"inst": _entry(1)})

        assert registry.read_entries(record)["inst"]["pid"] == 1

    def test_read_of_an_absent_record_is_empty_not_an_error(self, tmp_path):
        assert registry.read_entries(tmp_path / "nope.json") == {}

    def test_read_of_a_corrupt_record_is_empty_not_an_error(self, tmp_path):
        record = tmp_path / "pids.json"
        record.write_text("{not json")

        assert registry.read_entries(record) == {}
