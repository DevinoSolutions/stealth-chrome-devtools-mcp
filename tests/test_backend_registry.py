"""Pins for the server.json record's one home.

Schema v2 (plan_F808 Task 3) keys the record by display context, so one machine
can hold a headless backend and a desktop backend at once. Every pin here passes
an EXPLICIT path - no monkeypatching of the module's own globals - which is the
same property TestNoDefaultPaths enforces on the production signatures.
"""

import json

import pytest

from fakes import assert_no_default_paths
from stealth_chrome_devtools_mcp.embedded import backend_registry as reg
from stealth_chrome_devtools_mcp.embedded.display_context import HEADLESS, UNVERIFIED


class TestNoDefaultPaths:
    def test_no_public_function_defaults_its_path_parameter(self):
        """This module's docstring states the corollary; fakes.py enforces it
        for both record modules, including the companion non-vacuity assertion.
        """
        assert_no_default_paths(reg)


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

    def test_a_reused_port_supersedes_whatever_context_claimed_it(self, tmp_path):
        """Supersede-by-port is not a v1 rule. A context token can change under
        a backend that did not - a Windows session id is reassigned across an
        RDP reconnect - and the old key would then keep a dead entry on the live
        backend's port forever, resurrecting the kill-respawn loop. Only one
        process can hold a loopback listener, so the newest write on a port is
        by construction the only entry that can describe a live backend."""
        p = tmp_path / "server.json"
        reg.record_backend(
            p,
            port=19222,
            version="v",
            pid=42,
            source_fingerprint="fp",
            display_context="win-session-1",
        )

        reg.record_backend(
            p,
            port=19222,
            version="v",
            pid=999,
            source_fingerprint="fp",
            display_context="win-session-3",
        )

        assert reg.read_backends(p) == [
            {
                "port": 19222,
                "version": "v",
                "pid": 999,
                "source_fingerprint": "fp",
                "display_context": "win-session-3",
            }
        ]
        state = reg.read_record(p)
        assert reg.first_backend(state)["pid"] == 999
        assert reg.backend_on_port(state, 19222)["pid"] == 999

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

    def test_a_transient_windows_sharing_refusal_is_retried(
        self, tmp_path, monkeypatch
    ):
        """On Windows, replacing a file another process has OPEN raises
        PermissionError - Python readers do not pass FILE_SHARE_DELETE, so a
        concurrent read_record is enough to lose the rename. The window is
        microseconds, so the commit retries rather than failing the write.
        """
        p = tmp_path / "server.json"
        reg.record_backend(
            p,
            port=1,
            version="v",
            pid=10,
            source_fingerprint="fp",
            display_context="headless",
        )

        real_replace = reg.Path.replace
        attempts = []

        def _busy_twice(self, target):
            attempts.append(1)
            if len(attempts) <= 2:
                raise PermissionError(5, "used by another process")
            return real_replace(self, target)

        monkeypatch.setattr(reg.Path, "replace", _busy_twice)

        reg.record_backend(
            p,
            port=2,
            version="v",
            pid=20,
            source_fingerprint="fp",
            display_context="headless",
        )

        assert len(attempts) == 3  # two refusals, then the write lands
        assert [e["port"] for e in reg.read_backends(p)] == [2]
        assert sorted(f.name for f in tmp_path.iterdir()) == ["server.json"]

    def test_a_permanent_sharing_refusal_still_fails_and_cleans_up(
        self, tmp_path, monkeypatch
    ):
        """The retry is a bounded grace, not an infinite one: a target that is
        never releasable must surface as an error, with the previous record
        whole and no staged temp left behind."""
        p = tmp_path / "server.json"
        reg.record_backend(
            p,
            port=1,
            version="v",
            pid=10,
            source_fingerprint="fp",
            display_context="headless",
        )

        attempts = []

        def _always_busy(self, target):
            attempts.append(1)
            raise PermissionError(5, "used by another process")

        monkeypatch.setattr(reg.Path, "replace", _always_busy)

        with pytest.raises(PermissionError):
            reg.record_backend(
                p,
                port=2,
                version="v",
                pid=20,
                source_fingerprint="fp",
                display_context="headless",
            )

        assert len(attempts) == reg._COMMIT_ATTEMPTS
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


def _record(path, port, ctx, version="v"):
    """One recorded backend. The F-808 adoption and conflict rules read only
    the port and the display context, so the identity fields are fixed noise
    here — `version` is exposed only for the one pin that needs two identities.
    """
    reg.record_backend(
        path,
        port=port,
        version=version,
        pid=port,
        source_fingerprint="fp",
        display_context=ctx,
    )


class TestAdoptionCandidates:
    """plan_F808 Task 4: the adoption POLICY — which recorded backends a client
    in a given display context may reuse, and in what order. It lives here
    rather than in singleton so the rule is stated once, as a pure function of
    the record, and `_find_running_server` stays a probe loop.
    """

    def test_a_headless_client_is_offered_the_desktop_backend_first(self, tmp_path):
        """THE F-808 fix at the policy level: an SSH/service-session client
        adopting the desktop backend is exactly what makes its headed spawns
        visible, so the window-capable entry must come first even though the
        headless one matches the client's own context exactly."""
        p = tmp_path / "server.json"
        _record(p, 1111, HEADLESS)
        _record(p, 2222, "win-session-1")

        assert [e["port"] for e in reg.adoption_candidates(p, HEADLESS)] == [2222, 1111]

    def test_a_headless_client_still_falls_back_to_a_headless_backend(self, tmp_path):
        """Preference, not requirement: with no desktop backend recorded, the
        headless one is still adoptable — refusing it would spawn a second
        blind backend beside a perfectly good one."""
        p = tmp_path / "server.json"
        _record(p, 1111, HEADLESS)

        assert [e["port"] for e in reg.adoption_candidates(p, HEADLESS)] == [1111]

    def test_a_capable_client_is_never_offered_a_proven_headless_backend(
        self, tmp_path
    ):
        """The asymmetry. Adopting a proven-blind backend would strand THIS
        desktop session's headed browsing on it — F-808 again with the roles
        swapped — so a proven-capable client is offered nothing here and cold
        starts its own instead."""
        p = tmp_path / "server.json"
        _record(p, 1111, HEADLESS)

        assert reg.adoption_candidates(p, "win-session-7") == []

    def test_an_unverified_client_is_offered_everything(self, tmp_path):
        """UNVERIFIED means "we could not classify this platform", not "we can
        show windows". Applying the capable-client exclusion on that basis
        would evict healthy backends on platforms we simply cannot read, so an
        unverified client keeps maximal continuity and adopts anything."""
        p = tmp_path / "server.json"
        _record(p, 1111, HEADLESS)

        assert [e["port"] for e in reg.adoption_candidates(p, UNVERIFIED)] == [1111]

    def test_an_unverified_entry_survives_the_capable_clients_filter(self, tmp_path):
        """The exclusion is on PROVEN-headless entries only. An UNVERIFIED
        entry is what every <= 2.0.3 record reads as, so dropping it would make
        a capable client evict the running backend on the first upgrade."""
        p = tmp_path / "server.json"
        _record(p, 1111, UNVERIFIED)

        assert [e["port"] for e in reg.adoption_candidates(p, "win-session-1")] == [
            1111
        ]

    def test_a_capable_client_is_never_offered_a_foreign_desktop(self, tmp_path):
        """PIN MOVED, deliberately (F-808 step 4c): this case previously
        asserted both desktops were offered, recorded order deciding. That was
        wrong, and identity cannot catch it — a sibling desktop runs the same
        install, so version and fingerprint both match and the backend is
        adopted. A browser spawned on it then renders on a window station THIS
        user cannot see, which is the F-808 headline symptom, and Task 5 cannot
        guard it either because that backend's own can_show_windows() is True.
        The invariant is one backend per (fingerprint, display context); the
        only safe answer for a proven client is its OWN context.
        """
        p = tmp_path / "server.json"
        _record(p, 1111, "win-session-1")
        _record(p, 2222, "win-session-2")

        ports = [e["port"] for e in reg.adoption_candidates(p, "win-session-2")]
        assert ports == [2222]

    def test_a_capable_client_gets_its_own_entry_and_unverified_ones(self, tmp_path):
        """What survives the filter, in order. UNVERIFIED stays adoptable (a
        pre-2.0.4 record reads as exactly that), so a proven client can see two
        entries — and among survivors the stable sort keeps recorded order, so
        discovery stays deterministic."""
        p = tmp_path / "server.json"
        _record(p, 1111, UNVERIFIED)
        _record(p, 2222, "win-session-1")
        _record(p, 3333, "win-session-9")

        ports = [e["port"] for e in reg.adoption_candidates(p, "win-session-1")]
        assert ports == [1111, 2222]

    def test_an_absent_record_offers_no_candidates(self, tmp_path):
        assert reg.adoption_candidates(tmp_path / "server.json", HEADLESS) == []


class TestPortForContext:
    def test_returns_the_port_recorded_for_that_context(self, tmp_path):
        p = tmp_path / "server.json"
        _record(p, 1111, HEADLESS)
        _record(p, 2222, "win-session-1")

        assert reg.port_for_context(p, "win-session-1") == 2222

    def test_an_unrecorded_context_has_no_port(self, tmp_path):
        p = tmp_path / "server.json"
        _record(p, 1111, HEADLESS)

        assert reg.port_for_context(p, "win-session-1") is None

    def test_a_record_naming_no_usable_port_has_no_port(self, tmp_path):
        """Hand-edited or truncated records must read as "no port", never as a
        string that a caller would then try to bind."""
        p = tmp_path / "server.json"
        p.write_text(json.dumps({"schema": 2, "backends": {"ctx": {"port": "nope"}}}))

        assert reg.port_for_context(p, "ctx") is None


class TestPortConflict:
    """A spawn must not bind a port another display context has recorded:
    `record_backend` supersedes by port, and the record happens at Popen time
    (before the new backend is even ready), so binding there would drop a live
    sibling's entry and make it undiscoverable. Only a PROVEN context counts —
    an UNVERIFIED entry we would already have adopted if it were healthy.
    """

    def test_an_unverified_entry_is_never_a_conflict(self, tmp_path):
        """The missing pin (F-808 step 4b). UNVERIFIED is what every <= 2.0.3
        record reads as, and adoption offers it to EVERY client — so reaching
        port selection at all proves this entry is stale or dead. Calling it a
        conflict diverts the spawn to a random port, aims the eviction at the
        WRONG one, and leaks the live old backend and its Chrome forever.
        """
        p = tmp_path / "server.json"
        _record(p, 19222, UNVERIFIED)

        assert reg.port_conflict(p, 19222, "win-session-1") is False

    def test_a_port_recorded_by_another_context_conflicts(self, tmp_path):
        p = tmp_path / "server.json"
        _record(p, 19222, HEADLESS)

        assert reg.port_conflict(p, 19222, "win-session-1") is True

    def test_our_own_recorded_port_never_conflicts(self, tmp_path):
        """Rebinding the port our own context last used is the normal
        eviction/restart path — our record supersedes only itself."""
        p = tmp_path / "server.json"
        _record(p, 19222, "win-session-1")

        assert reg.port_conflict(p, 19222, "win-session-1") is False

    @pytest.mark.parametrize("client", [HEADLESS, UNVERIFIED])
    def test_a_client_that_cannot_prove_a_desktop_still_conflicts(
        self, tmp_path, client
    ):
        """The asymmetry's other side, pinned so it cannot drift silently. Both
        unproven client tokens behave the same, which is the point: the rule is
        about what the CLIENT can prove, not which token it carries. Such a
        client adopts anything, but does not get to BIND on top of a proven
        sibling — it has not earned the right to evict a backend that may be
        alive and serving.

        Two costs are accepted here and named in the docstring: an old backend
        on that port can linger, and on this one path adoption and selection
        disagree (harmless — discovery runs first and returns). The argument
        for revisiting is that the same soundness reasoning that makes an
        UNVERIFIED ENTRY safe to evict applies to an unproven CLIENT too.
        """
        p = tmp_path / "server.json"
        _record(p, 19222, "win-session-1")

        assert reg.port_conflict(p, 19222, client) is True

    def test_an_unrecorded_port_never_conflicts(self, tmp_path):
        p = tmp_path / "server.json"
        _record(p, 19222, HEADLESS)

        assert reg.port_conflict(p, 20000, "win-session-1") is False

    def test_an_absent_record_never_conflicts(self, tmp_path):
        assert reg.port_conflict(tmp_path / "server.json", 19222, HEADLESS) is False


class TestOwnOrFirstPort:
    """`restart_backend`'s terminate target. Its two halves must name the same
    port: the spawn half asks port_for_context, so a terminate half reading
    first_backend would kill a sibling desktop's backend on a two-context
    machine and respawn ours somewhere else.
    """

    def test_our_own_context_wins_over_an_entry_recorded_first(self, tmp_path):
        p = tmp_path / "server.json"
        _record(p, 1111, HEADLESS)
        _record(p, 2222, "win-session-1")

        assert reg.own_or_first_port(p, "win-session-1") == 2222

    def test_falls_back_to_the_first_backend_when_our_context_is_absent(self, tmp_path):
        """Keeps the single-backend and pre-v2 cases behaving exactly as they
        did — restart must still find something to terminate."""
        p = tmp_path / "server.json"
        _record(p, 1111, HEADLESS)

        assert reg.own_or_first_port(p, "win-session-1") == 1111

    def test_an_absent_record_has_no_port(self, tmp_path):
        assert reg.own_or_first_port(tmp_path / "server.json", HEADLESS) is None


class TestRecordedInt:
    def test_reads_an_integer_field(self, tmp_path):
        p = tmp_path / "server.json"
        _record(p, 1111, HEADLESS)
        entry = reg.first_backend(reg.read_record(p))

        assert reg.recorded_int(entry, "pid") == 1111

    @pytest.mark.parametrize(
        ("entry", "key"),
        [
            (None, "pid"),
            ({}, "pid"),
            ({"pid": "4242"}, "pid"),
            ({"pid": None}, "pid"),
        ],
    )
    def test_absent_or_ill_typed_reads_as_none(self, entry, key):
        """The record tolerates hand-editing and two schemas, so a field that
        is missing or the wrong type must narrow to None rather than reach a
        caller that annotated it `int | None` and believed itself."""
        assert reg.recorded_int(entry, key) is None
