"""F-889 (d) — a NEWER backend of ours is adopted, never evicted.

A mixed-version fleet had one eviction loop left after F-886. Two identities on
one desktop each read the other as stale (``fingerprint_mismatch`` answers "these
two digests differ", never "mine is newer"), so each evicted the other on every
proxy start — five waves in one measured session, every browser killed
(``mixed-install-fleet-evicts-in-a-loop``). F-886 stops that only when the loser
owns live browsers, and a fleet MID-UPGRADE is precisely the population where
neither does yet.

The rule is one clause on ``singleton._identity_matches``: a recorded version
strictly NEWER than ours MATCHES. Everything downstream follows from that single
predicate — ``_same_identity_backend_ready`` passes for it, so
``backend_eviction.clear_stale`` returns before the kill site,
``_select_backend_port`` never steps on its port, and the watchdog's confirmation
phase accepts it. That is why these pins sit on the predicate and on the two
consumers, and not on a new rule of their own: there isn't one.

Hermetic — no sockets, no HTTP, no state file beyond ``tmp_path``.
"""

from __future__ import annotations

import pytest

from stealth_chrome_devtools_mcp.embedded import (
    backend_eviction,
    backend_registry,
    build_identity,
    singleton,
)

OURS = "2.1.9"
NEWER = "2.2.0"
OLDER = "2.1.8"
DIGEST = "a" * 64


def _entry(version: str, *, digest: str = DIGEST, port: int = 51999, pid: int = 4242):
    return {
        "port": port,
        "version": version,
        "pid": pid,
        "source_fingerprint": digest,
        "display_context": "win-session-1",
    }


@pytest.fixture()
def our_build(monkeypatch):
    """This process is ``OURS`` running the source ``DIGEST`` hashes to."""
    monkeypatch.setattr(singleton, "_server_version", lambda: OURS)
    monkeypatch.setattr(singleton, "_source_fingerprint", lambda: DIGEST)


class TestNewerVersionOrdering:
    """``build_identity.newer`` on its ordering table."""

    @pytest.mark.parametrize(
        ("recorded", "ours", "expected"),
        [
            ("2.2.0", "2.1.9", True),
            ("2.1.10", "2.1.9", True),  # segment-wise, never lexicographic
            ("3.0.0", "2.9.9", True),
            ("2.1.9", "2.1.9", False),
            ("2.1.8", "2.1.9", False),
            ("2.2", "2.2.0", False),  # zero-extended: the same build
            ("2.2.0", "2.2", False),
            ("2.2.0.1", "2.2.0", True),
        ],
    )
    def test_the_ordering(self, recorded, ours, expected):
        assert build_identity.newer(recorded, ours) is expected

    @pytest.mark.parametrize(
        ("recorded", "ours"),
        [
            (build_identity.UNKNOWN_VERSION, "2.1.9"),
            ("2.1.9", build_identity.UNKNOWN_VERSION),
            ("2.2.0rc1", "2.1.9"),  # unparseable: not newer
            ("v2.2.0", "2.1.9"),
            ("", "2.1.9"),
            (None, "2.1.9"),
            (220, "2.1.9"),  # a hand-edited record's integer
            ("2.1.9", "not-a-version"),
        ],
    )
    def test_everything_uncomparable_fails_closed_onto_todays_behaviour(
        self, recorded, ours
    ):
        """A process that could not resolve its own build has no claim to make
        about someone else's, and an unparseable recorded version must not be
        adoptable by accident."""
        assert build_identity.newer(recorded, ours) is False

    def test_unknown_is_never_newer_even_than_unknown(self):
        assert (
            build_identity.newer(
                build_identity.UNKNOWN_VERSION, build_identity.UNKNOWN_VERSION
            )
            is False
        )


class TestIdentityAdoptsForward:
    def test_a_newer_recorded_backend_matches_identity(self, our_build):
        """THE F-889 (d) pin. Its digest differs too — it is a different build —
        and that must no longer read as 'stale'."""
        assert singleton._identity_matches(_entry(NEWER, digest="b" * 64)) is True

    def test_an_older_recorded_backend_does_not(self, our_build):
        assert singleton._identity_matches(_entry(OLDER, digest="b" * 64)) is False

    def test_same_version_different_digest_still_does_not(self, our_build):
        """Issue #14 / F-206, preserved exactly: an in-place source edit on an
        editable install is invisible to the version key, so the digest is the
        only thing that can see it. This is the case the eviction flow exists
        for and F-889 must not touch it."""
        assert singleton._identity_matches(_entry(OURS, digest="b" * 64)) is False

    def test_same_version_same_digest_still_matches(self, our_build):
        assert singleton._identity_matches(_entry(OURS)) is True

    def test_an_unreadable_recorded_digest_on_a_newer_build_still_matches(
        self, our_build
    ):
        """F-829's sentinel is about the DIGEST question, which a newer version
        never reaches."""
        assert singleton._identity_matches(_entry(NEWER, digest=None)) is True


class TestTheTwoConsumersFollowTheOnePredicate:
    def test_the_reuse_gate_probes_a_newer_backend_instead_of_refusing_it(
        self, our_build, tmp_path, monkeypatch
    ):
        """``_same_identity_backend_ready`` is what ``clear_stale`` asks before
        the kill and what the watchdog's confirmation asks before condemning.
        For a newer backend it must get as far as the PROBE."""
        record = tmp_path / "server.json"
        backend_registry.record_backend(
            record,
            port=51999,
            version=NEWER,
            pid=4242,
            source_fingerprint="b" * 64,
            display_context="win-session-1",
        )
        monkeypatch.setattr(singleton, "SERVER_STATE_FILE", record)
        probed = []
        monkeypatch.setattr(
            singleton,
            "_backend_http_ready",
            lambda port, **_kw: bool(probed.append(port)) or True,
        )

        assert singleton._same_identity_backend_ready(51999, patience=0.0) is True
        assert probed == [51999], "a newer backend must be probed, not refused"

    def test_a_newer_backend_is_spared_at_the_kill_site(self, our_build):
        """``clear_stale``'s first answer — reusable, nothing to clear — so
        ``terminate_backend`` is never reached. The eviction loop ends here."""
        killed = []

        spared = backend_eviction.clear_stale(
            51999,
            reusable=lambda: singleton._identity_matches(_entry(NEWER)),
            protecting=lambda: pytest.fail("protection must not even be consulted"),
            terminate_backend=lambda: killed.append(51999),
        )

        assert spared == []
        assert killed == [], "a newer backend of ours must never be terminated"

    def test_an_older_backend_is_still_evicted_when_unprotected(self, our_build):
        """The asymmetry is the whole point: converging forward must not become
        'never evict anything', or issue #14's upgrade flow stops working."""
        killed = []

        spared = backend_eviction.clear_stale(
            51999,
            reusable=lambda: singleton._identity_matches(_entry(OLDER)),
            protecting=list,
            terminate_backend=lambda: killed.append(51999),
        )

        assert spared == []
        assert killed == [51999]
