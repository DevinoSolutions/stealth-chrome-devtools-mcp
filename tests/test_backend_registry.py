"""Pins for the server.json record's one home.

Schema v2 (plan_F808 Task 3) keys the record by display context, so one machine
can hold a headless backend and a desktop backend at once. Every pin here passes
an EXPLICIT path - no monkeypatching of the module's own globals - which is the
same property TestNoDefaultPaths enforces on the production signatures.
"""

import inspect
import json
from pathlib import Path

from stealth_chrome_devtools_mcp.embedded import backend_registry as reg
from stealth_chrome_devtools_mcp.embedded.display_context import HEADLESS, UNVERIFIED


def _public_functions():
    for name, obj in vars(reg).items():
        if name.startswith("_") or not inspect.isfunction(obj):
            continue
        if obj.__module__ == reg.__name__:
            yield name, obj


class TestNoDefaultPaths:
    def test_no_public_function_defaults_its_path_parameter(self):
        """The module docstring's corollary, enforced: the caller's binding is
        what selects the file. A default would bind this module's own
        SERVER_STATE_FILE at def-time and silently ignore the redirection the
        hermetic fixtures rely on - which is the only thing keeping a test run
        off the developer's live ~/.stealth-mcp record.

        Two ways to offend, both caught: naming the parameter path/paths, and
        defaulting ANY parameter to a Path (a future `record=SERVER_STATE_FILE`
        would escape a name-only check).
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
        # Confirms the sweep actually visits this module's functions rather
        # than an empty iterator (a renamed module or a broken __module__
        # filter would silently pass the guard above) - matches
        # test_no_silent_excepts.py:147's companion-assertion convention.
        assert len(list(_public_functions())) >= 3


class TestRecordParentDir:
    def test_record_backend_creates_the_missing_parent_dir(self, tmp_path):
        """The writer makes the record's OWN parent rather than calling
        singleton's _ensure_state_dir, so a redirected path lands where the
        caller asked instead of forcing the real STATE_DIR into existence.
        """
        record = tmp_path / "sub" / "server.json"

        reg.record_backend(
            record,
            port=19222,
            version="2.0.3",
            pid=4242,
            source_fingerprint="fp",
            display_context="win-session-1",
        )

        assert reg.read_backends(record) == [
            {
                "port": 19222,
                "version": "2.0.3",
                "pid": 4242,
                "source_fingerprint": "fp",
                "display_context": "win-session-1",
            }
        ]


class TestSchemaV1Compatibility:
    def test_v1_record_is_read_as_one_unverified_backend(self, tmp_path):
        """A backend from <=2.0.3 wrote a flat record with no display_context. It
        must still be reusable, classified UNVERIFIED (capable) rather than dropped:
        dropping it would evict a healthy backend on upgrade."""
        p = tmp_path / "server.json"
        p.write_text(
            '{"port": 19222, "version": "2.0.3", "pid": 42, "source_fingerprint": "abc"}'
        )
        entries = reg.read_backends(p)
        assert len(entries) == 1
        assert entries[0]["port"] == 19222
        assert entries[0]["display_context"] == "unverified"

    def test_v1_record_is_superseded_by_a_classified_write_on_its_port(self, tmp_path):
        """THE upgrade path. A real client writes its REAL context token, which
        is a DIFFERENT key from the UNVERIFIED one the v1 record reads as - so
        the v1 entry must be superseded by port, not merely replaced by key.

        Deliberately NOT parametrized over UNVERIFIED: writing under UNVERIFIED
        would pass on key-replacement alone and prove nothing about the rule
        this test exists to pin.
        """
        p = tmp_path / "server.json"
        p.write_text(
            '{"port": 19222, "version": "2.0.3", "pid": 42, '
            '"source_fingerprint": "OLD"}'
        )

        reg.record_backend(
            p,
            port=19222,
            version="2.0.4",
            pid=999,
            source_fingerprint="NEW",
            display_context="win-session-1",
        )

        assert reg.read_backends(p) == [
            {
                "port": 19222,
                "version": "2.0.4",
                "pid": 999,
                "source_fingerprint": "NEW",
                "display_context": "win-session-1",
            }
        ]

    def test_an_unverified_backend_on_another_port_survives(self, tmp_path):
        """The boundary of supersede-by-port. UNVERIFIED is not only the v1
        marker - it is also what a live backend on an unclassifiable platform
        (or one whose probe failed) records for itself. On a DIFFERENT port it
        is a different backend and must never be collected."""
        p = tmp_path / "server.json"
        reg.record_backend(
            p,
            port=5000,
            version="v",
            pid=50,
            source_fingerprint="fp",
            display_context=UNVERIFIED,
        )

        reg.record_backend(
            p,
            port=6000,
            version="v",
            pid=60,
            source_fingerprint="fp",
            display_context="win-session-1",
        )

        assert {e["display_context"]: e["port"] for e in reg.read_backends(p)} == {
            UNVERIFIED: 5000,
            "win-session-1": 6000,
        }

    def test_upgraded_backend_is_what_both_normalizers_return(self, tmp_path):
        """Regression pin for the shadowing defect (F-808 step 3b): the stale
        v1 entry sorted FIRST, so first_backend and backend_on_port both handed
        every caller a <=2.0.3 record whose version can never match the running
        package. _same_identity_backend_ready then refused reuse forever and
        every proxy start kill-respawned the shared backend."""
        p = tmp_path / "server.json"
        p.write_text('{"port": 19222, "version": "2.0.3", "pid": 42}')

        reg.record_backend(
            p,
            port=19222,
            version="2.0.4",
            pid=999,
            source_fingerprint="NEW",
            display_context="win-session-1",
        )

        state = reg.read_record(p)
        assert reg.first_backend(state)["version"] == "2.0.4"
        assert reg.first_backend(state)["pid"] == 999
        assert reg.backend_on_port(state, 19222)["version"] == "2.0.4"
        assert reg.backend_on_port(state, 19222)["pid"] == 999


class TestUnreadableRecords:
    def test_missing_file_reads_as_no_backends(self, tmp_path):
        p = tmp_path / "server.json"
        assert reg.read_backends(p) == []
        assert reg.read_record(p) is None

    def test_corrupt_or_non_dict_record_reads_as_no_backends(self, tmp_path):
        """A truncated write, a hand-edited file, or a JSON top-level that is
        not an object must all read as "no backend" rather than raising into
        discovery - the record is a cache, never a source of failure."""
        for text in ("{not json at all", "[]", '"str"'):
            p = tmp_path / "server.json"
            p.write_text(text)
            assert reg.read_backends(p) == [], text
            assert reg.read_record(p) is None, text

    def test_v2_file_with_a_non_dict_backends_value_reads_as_no_backends(
        self, tmp_path
    ):
        p = tmp_path / "server.json"
        p.write_text(json.dumps({"schema": 2, "backends": ["nope"]}))
        assert reg.read_backends(p) == []


class TestPerContextRecords:
    def test_write_then_read_round_trips_by_context(self, tmp_path):
        p = tmp_path / "server.json"
        reg.record_backend(
            p,
            port=1,
            version="v",
            pid=10,
            source_fingerprint="fp",
            display_context="headless",
        )
        reg.record_backend(
            p,
            port=2,
            version="v",
            pid=11,
            source_fingerprint="fp",
            display_context="win-session-1",
        )
        got = {e["display_context"]: e["port"] for e in reg.read_backends(p)}
        assert got == {"headless": 1, "win-session-1": 2}

    def test_recording_the_same_context_replaces_not_appends(self, tmp_path):
        p = tmp_path / "server.json"
        reg.record_backend(
            p,
            port=1,
            version="v",
            pid=10,
            source_fingerprint="fp",
            display_context="headless",
        )
        reg.record_backend(
            p,
            port=9,
            version="v",
            pid=99,
            source_fingerprint="fp",
            display_context="headless",
        )
        assert [e["port"] for e in reg.read_backends(p)] == [9]

    def test_window_capable_first_orders_the_search(self, tmp_path):
        """Discovery must prefer a backend that can show windows, so an SSH client
        converges on the desktop backend instead of starting a blind one."""
        p = tmp_path / "server.json"
        reg.record_backend(
            p,
            port=1,
            version="v",
            pid=10,
            source_fingerprint="fp",
            display_context="headless",
        )
        reg.record_backend(
            p,
            port=2,
            version="v",
            pid=11,
            source_fingerprint="fp",
            display_context="win-session-1",
        )
        assert [e["port"] for e in reg.window_capable_first(p)] == [2, 1]

    def test_window_capable_first_keeps_unverified_ahead_of_headless(self, tmp_path):
        """UNVERIFIED means "could not classify", which display_context treats as
        capable - so it must sort with the capable group, never with HEADLESS."""
        p = tmp_path / "server.json"
        reg.record_backend(
            p,
            port=1,
            version="v",
            pid=10,
            source_fingerprint="fp",
            display_context=HEADLESS,
        )
        reg.record_backend(
            p,
            port=2,
            version="v",
            pid=11,
            source_fingerprint="fp",
            display_context=UNVERIFIED,
        )
        assert [e["port"] for e in reg.window_capable_first(p)] == [2, 1]

    def test_forget_removes_only_the_named_context(self, tmp_path):
        p = tmp_path / "server.json"
        reg.record_backend(
            p,
            port=1,
            version="v",
            pid=10,
            source_fingerprint="fp",
            display_context="headless",
        )
        reg.record_backend(
            p,
            port=2,
            version="v",
            pid=11,
            source_fingerprint="fp",
            display_context="win-session-1",
        )
        reg.forget_backend(p, "headless")
        assert [e["display_context"] for e in reg.read_backends(p)] == ["win-session-1"]

    def test_forgetting_the_only_context_leaves_a_readable_empty_record(self, tmp_path):
        """forget writes a valid empty v2 file rather than unlinking: only
        clear_record (the `stop` verb) removes the file itself."""
        p = tmp_path / "server.json"
        reg.record_backend(
            p,
            port=1,
            version="v",
            pid=10,
            source_fingerprint="fp",
            display_context="headless",
        )
        reg.forget_backend(p, "headless")
        assert reg.read_backends(p) == []
        assert p.exists()

    def test_forgetting_an_absent_context_is_a_no_op(self, tmp_path):
        p = tmp_path / "server.json"
        reg.record_backend(
            p,
            port=1,
            version="v",
            pid=10,
            source_fingerprint="fp",
            display_context="headless",
        )
        reg.forget_backend(p, "win-session-9")
        assert [e["port"] for e in reg.read_backends(p)] == [1]


class TestSingleRecordNormalizers:
    """The seam cli.py and singleton's readers go through: they hold a RAW
    record (v1 flat or v2), because `singleton._read_server_state` is a
    patch surface several test modules stub with v1-flat dicts."""

    def test_first_backend_of_a_v1_flat_record_is_that_record(self):
        state = {"port": 19222, "version": "2.0.3", "pid": 42}
        assert reg.first_backend(state) == {
            "port": 19222,
            "version": "2.0.3",
            "pid": 42,
            "display_context": UNVERIFIED,
        }

    def test_first_backend_of_a_v2_record_is_a_recorded_entry(self, tmp_path):
        p = tmp_path / "server.json"
        reg.record_backend(
            p,
            port=5,
            version="v",
            pid=50,
            source_fingerprint="fp",
            display_context="win-session-1",
        )
        assert reg.first_backend(reg.read_record(p))["port"] == 5

    def test_first_backend_of_nothing_is_none(self):
        assert reg.first_backend(None) is None
        assert reg.first_backend({}) is None

    def test_backend_on_port_selects_the_entry_for_that_port(self, tmp_path):
        p = tmp_path / "server.json"
        reg.record_backend(
            p,
            port=1,
            version="v",
            pid=10,
            source_fingerprint="fp",
            display_context="headless",
        )
        reg.record_backend(
            p,
            port=2,
            version="v",
            pid=11,
            source_fingerprint="fp",
            display_context="win-session-1",
        )
        state = reg.read_record(p)
        assert reg.backend_on_port(state, 2)["pid"] == 11
        assert reg.backend_on_port(state, 1)["pid"] == 10
        assert reg.backend_on_port(state, 3) is None
        assert reg.backend_on_port(None, 1) is None


class TestAtomicWrite:
    def test_a_write_leaves_no_temp_file_behind(self, tmp_path):
        """The v2 writer stages into a sibling temp file and os.replace()s it.
        A leftover temp would mean a partial write survived."""
        p = tmp_path / "server.json"
        reg.record_backend(
            p,
            port=1,
            version="v",
            pid=10,
            source_fingerprint="fp",
            display_context="headless",
        )
        reg.forget_backend(p, "headless")
        assert sorted(f.name for f in tmp_path.iterdir()) == ["server.json"]

    def test_a_failed_write_leaves_the_previous_record_intact(
        self, tmp_path, monkeypatch
    ):
        """The replace is the commit point: if it fails, the reader still sees
        the whole previous record - never a truncated one - and the staged temp
        file is cleaned up rather than left to accumulate."""
        p = tmp_path / "server.json"
        reg.record_backend(
            p,
            port=1,
            version="v",
            pid=10,
            source_fingerprint="fp",
            display_context="headless",
        )

        def _boom(self, target):
            raise OSError("replace refused")

        monkeypatch.setattr(reg.Path, "replace", _boom)
        try:
            reg.record_backend(
                p,
                port=2,
                version="v",
                pid=20,
                source_fingerprint="fp",
                display_context="headless",
            )
        except OSError:
            pass

        assert [e["port"] for e in reg.read_backends(p)] == [1]
        assert sorted(f.name for f in tmp_path.iterdir()) == ["server.json"]


class TestClearRecord:
    def test_clear_removes_the_files_and_never_raises_on_absent_ones(self, tmp_path):
        p = tmp_path / "server.json"
        port_file = tmp_path / "server.port"
        reg.record_backend(
            p,
            port=1,
            version="v",
            pid=10,
            source_fingerprint="fp",
            display_context="headless",
        )
        reg.clear_record(p, port_file)  # port_file never existed
        assert not p.exists()
        assert reg.read_backends(p) == []
