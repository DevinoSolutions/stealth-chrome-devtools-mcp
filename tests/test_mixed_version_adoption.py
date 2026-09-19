"""F-889 (d) — a NEWER backend of ours is ADOPTED, and never killed.

A mixed-version fleet had one eviction loop left after F-886. Two identities on
one desktop each read the other as stale (``fingerprint_mismatch`` answers "these
two digests differ", never "mine is newer"), so each evicted the other on every
proxy start — five waves in one measured session, every browser killed
(``mixed-install-fleet-evicts-in-a-loop``). F-886 stops that only when the loser
owns live browsers, and a fleet MID-UPGRADE is precisely the population where
neither does yet.

**Two questions, two predicates.** F-889's first pass answered both with
``singleton._identity_matches``, and that is the defect the review measured:

* "would I ADOPT what is on this port instead of spawning?" —
  :func:`singleton._adoptable_identity`, the forward-looking one. A recorded
  version strictly NEWER than ours answers yes;
* "is this entry MINE?" — :func:`singleton._identity_matches`, unchanged since
  F-886. ``backend_eviction.protected`` asks it to decide whether the backend it
  is about to terminate is a STRANGER's, and the operator verbs ask it to decide
  whose backend ``restart`` targets.

Widening the second one to mean the first inverted F-886 for the exact
population F-889 (d) exists to protect: a NEWER stranger's backend — momentarily
unreachable, still holding its user's logged-in Chrome — read as "ours", so
condition 2 of the protection rule returned no browsers and the kill site
terminated it. Adopting forward must mean *step aside*, never *take the port*.

These pins therefore drive the PRODUCTION composition — ``_clear_stale_backend``
over a real ``server.json``, a real ``browser_pids.json`` and the real
``backend_eviction`` rule — rather than the predicate with a double in place of
the collaborators. A ``protecting=lambda: pytest.fail(...)`` double is strictly
stronger than production and cannot see this class of defect at all.

Hermetic: no sockets, no HTTP, nothing outside ``tmp_path``. The one live pid
used is this test process's own, so "the browser is still running" is a fact
rather than a patch — and nothing here can terminate anything (the kill is
replaced by a recorder at ``singleton._terminate_backend``, the seam the suite
already owns).
"""

from __future__ import annotations

import json
import os

import pytest

from stealth_chrome_devtools_mcp.embedded import (
    backend_registry,
    browser_pid_registry,
    build_identity,
    singleton,
)

OURS = "2.1.9"
NEWER = "2.2.0"
OLDER = "2.1.8"
DIGEST = "a" * 64
THEIRS = "b" * 64

OUR_PORT = 51999
THEIR_PORT = 51998
THEIR_PID = 4242
OUR_PID = 4343
CONTEXT = "win-session-1"


def _entry(version: str, *, digest: str = DIGEST, port: int = OUR_PORT, pid: int = 1):
    return {
        "port": port,
        "version": version,
        "pid": pid,
        "source_fingerprint": digest,
        "display_context": CONTEXT,
    }


@pytest.fixture()
def our_build(monkeypatch):
    """This process is ``OURS`` running the source ``DIGEST`` hashes to."""
    monkeypatch.setattr(singleton, "_server_version", lambda: OURS)
    monkeypatch.setattr(singleton, "_source_fingerprint", lambda: DIGEST)


@pytest.fixture()
def machine(monkeypatch, tmp_path):
    """A state dir of our own, with the kill replaced by a recorder.

    Returns the list terminations land in, so every node below can assert on
    what production WOULD have killed without anything dying.
    """
    record = tmp_path / "server.json"
    monkeypatch.setattr(singleton, "SERVER_STATE_FILE", record)
    monkeypatch.setattr(singleton, "STATE_DIR", tmp_path)
    monkeypatch.setattr(singleton, "_ensure_state_dir", lambda: None)
    killed: list[int] = []
    monkeypatch.setattr(
        singleton, "_terminate_backend", lambda port: bool(killed.append(port)) or True
    )
    return killed


def _record(version: str, *, digest: str, port: int, pid: int) -> None:
    backend_registry.record_backend(
        singleton.SERVER_STATE_FILE,
        port=port,
        version=version,
        pid=pid,
        source_fingerprint=digest,
        display_context=CONTEXT,
    )


def _owns_a_live_browser(owner_pid: int) -> int:
    """Give ``owner_pid`` one browser in ``browser_pids.json`` that IS running.

    The browser pid is this test process. ``backend_eviction.owned_browsers``
    asks ``psutil.pid_exists`` of it, so nothing has to be patched for the
    liveness half — and the one thing the rule protects is a fact.
    """
    alive = os.getpid()
    (singleton.STATE_DIR / browser_pid_registry.RECORD_NAME).write_text(
        json.dumps(
            {
                "browser_processes": {
                    f"instance-{alive}": {
                        "pid": alive,
                        browser_pid_registry.OWNER_PID: owner_pid,
                        browser_pid_registry.OWNER_CREATE_TIME: 1.0,
                    }
                }
            }
        )
    )
    return alive


def _unreachable(monkeypatch) -> None:
    """The backend on the port does not answer, but its process IS alive.

    That is the outage shape: a starved or busy backend whose probe times out.
    ``_same_identity_backend_ready`` must therefore fall through to the window
    and answer "not reusable" without concluding the process is gone.
    """
    monkeypatch.setattr(singleton, "_backend_http_ready", lambda port, **_kw: False)
    monkeypatch.setattr(singleton, "_server_is_healthy", lambda port: True)


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


class TestTheTwoPredicatesAreDifferentQuestions:
    """The split the review's H1 asked for, stated directly."""

    def test_a_newer_backend_is_adoptable(self, our_build):
        """THE F-889 (d) pin. Its digest differs too — it is a different build —
        and that must no longer read as 'stale'."""
        assert singleton._adoptable_identity(_entry(NEWER, digest=THEIRS)) is True

    def test_a_newer_backend_is_not_ours(self, our_build):
        """...and this is H1. ``_identity_matches`` is what tells the protection
        rule whether the backend it is about to kill belongs to someone else. A
        newer build is someone else's — that is the entire reason we adopt it
        rather than replace it."""
        assert singleton._identity_matches(_entry(NEWER, digest=THEIRS)) is False

    def test_an_older_backend_is_neither(self, our_build):
        assert singleton._adoptable_identity(_entry(OLDER, digest=THEIRS)) is False
        assert singleton._identity_matches(_entry(OLDER, digest=THEIRS)) is False

    def test_same_version_different_digest_is_neither(self, our_build):
        """Issue #14 / F-206, preserved exactly: an in-place source edit on an
        editable install is invisible to the version key, so the digest is the
        only thing that can see it. This is the case the eviction flow exists
        for and F-889 must not touch it."""
        assert singleton._adoptable_identity(_entry(OURS, digest=THEIRS)) is False
        assert singleton._identity_matches(_entry(OURS, digest=THEIRS)) is False

    def test_our_own_build_is_both(self, our_build):
        assert singleton._adoptable_identity(_entry(OURS)) is True
        assert singleton._identity_matches(_entry(OURS)) is True

    def test_an_unreadable_digest_on_a_newer_build_is_still_adoptable(self, our_build):
        """F-829's sentinel is about the DIGEST question, which a newer version
        never reaches."""
        assert singleton._adoptable_identity(_entry(NEWER, digest=None)) is True


class TestTheKillSiteAsProductionComposesIt:
    """``singleton._clear_stale_backend`` with every collaborator REAL.

    This is the composition ``_start_backend_holding_lock`` calls under the
    cold-start lock, and the one the review measured killing a newer sibling.
    """

    def test_a_newer_backend_owning_a_live_browser_is_spared(
        self, our_build, machine, monkeypatch
    ):
        """H1. A newer build of ours, momentarily unreachable, still holding its
        user's logged-in Chrome. Step aside; never kill."""
        _record(NEWER, digest=THEIRS, port=THEIR_PORT, pid=THEIR_PID)
        alive = _owns_a_live_browser(THEIR_PID)
        monkeypatch.setattr(singleton, "_is_our_backend", lambda pid: pid == THEIR_PID)
        _unreachable(monkeypatch)

        spared = singleton._clear_stale_backend(THEIR_PORT)

        assert spared == [alive], "the protection rule must see its live browser"
        assert machine == [], "a newer backend of ours must never be terminated"

    def test_a_newer_backend_that_answers_is_adopted_before_the_rule_is_asked(
        self, our_build, machine, monkeypatch
    ):
        """The other half of adopting forward, and the cheaper path: a newer
        backend that is reachable is REUSABLE, so ``clear_stale`` returns at its
        first answer and no browser record is ever consulted."""
        _record(NEWER, digest=THEIRS, port=THEIR_PORT, pid=THEIR_PID)
        monkeypatch.setattr(singleton, "_is_our_backend", lambda pid: pid == THEIR_PID)
        monkeypatch.setattr(singleton, "_backend_http_ready", lambda port, **_kw: True)

        assert singleton._clear_stale_backend(THEIR_PORT) == []
        assert machine == []

    def test_a_newer_backend_with_no_live_browser_is_still_evicted(
        self, our_build, machine, monkeypatch
    ):
        """The asymmetry F-886 chose and F-889 keeps. Protecting every newer
        backend would accumulate one per upgrade forever; browsers are the only
        state reconnecting cannot rebuild, so they are what buys the refusal."""
        _record(NEWER, digest=THEIRS, port=THEIR_PORT, pid=THEIR_PID)
        monkeypatch.setattr(singleton, "_is_our_backend", lambda pid: pid == THEIR_PID)
        _unreachable(monkeypatch)

        assert singleton._clear_stale_backend(THEIR_PORT) == []
        assert machine == [THEIR_PORT]

    def test_an_older_backend_owning_a_live_browser_is_also_spared(
        self, our_build, machine, monkeypatch
    ):
        """F-886, byte-for-byte. An OLDER stranger was already protected and
        must stay so — the split must not have narrowed the rule either."""
        _record(OLDER, digest=THEIRS, port=THEIR_PORT, pid=THEIR_PID)
        alive = _owns_a_live_browser(THEIR_PID)
        monkeypatch.setattr(singleton, "_is_our_backend", lambda pid: pid == THEIR_PID)
        _unreachable(monkeypatch)

        assert singleton._clear_stale_backend(THEIR_PORT) == [alive]
        assert machine == []

    def test_our_own_wedged_backend_is_evicted_even_holding_browsers(
        self, our_build, machine, monkeypatch
    ):
        """Condition 2 of the protection rule, and the reason ``_identity_matches``
        must keep meaning exactly 'mine': terminating a wedged backend of our own
        is the recovery both ``restart`` and the cold-start lock exist to perform.
        Protecting it would turn a wedge into a permanent one."""
        _record(OURS, digest=DIGEST, port=OUR_PORT, pid=OUR_PID)
        _owns_a_live_browser(OUR_PID)
        monkeypatch.setattr(singleton, "_is_our_backend", lambda pid: pid == OUR_PID)
        _unreachable(monkeypatch)

        assert singleton._clear_stale_backend(OUR_PORT) == []
        assert machine == [OUR_PORT]


class TestTheOperatorVerbsStillTargetOurOwn:
    """M4. Adoption is a PROXY-START decision; ``restart``/``stop`` are not.

    On a two-identity desktop the stranger's entry is FIRST by construction — it
    was recorded first, which is why we stepped aside from it. F-886 made both
    verbs pick ours out of that record by identity; reading a newer stranger as
    "ours" handed ``restart`` the stranger's port back.
    """

    @pytest.fixture()
    def two_identities(self, our_build, machine):
        """A newer stranger recorded FIRST, then our own backend beside it."""
        _record(NEWER, digest=THEIRS, port=THEIR_PORT, pid=THEIR_PID)
        _record(OURS, digest=DIGEST, port=OUR_PORT, pid=OUR_PID)
        ports = [
            e.get("port")
            for e in backend_registry.read_backends(singleton.SERVER_STATE_FILE)
        ]
        assert ports == [THEIR_PORT, OUR_PORT], "the stranger must be listed first"
        return machine

    def test_restarts_seed_is_our_own_port(self, two_identities, monkeypatch):
        """``restart_backend``'s first line. It terminates exactly what selection
        returns, so a seed naming the stranger is a restart that walks away from
        its own wedged backend."""
        monkeypatch.setattr(
            singleton.display_context, "display_context", lambda: CONTEXT
        )
        assert (
            backend_registry.own_or_first_port(
                singleton.SERVER_STATE_FILE,
                CONTEXT,
                matches=singleton._identity_matches,
            )
            == OUR_PORT
        )

    def test_port_selection_chooses_our_own_port(self, two_identities, monkeypatch):
        """The same question at the other site, which is what makes the seed and
        the choice agree."""
        monkeypatch.setattr(
            singleton.display_context, "display_context", lambda: CONTEXT
        )
        assert (
            backend_registry.port_for_context(
                singleton.SERVER_STATE_FILE,
                CONTEXT,
                matches=singleton._identity_matches,
            )
            == OUR_PORT
        )

    def test_stop_targets_the_backend_that_answers_and_spares_the_siblings_entry(
        self, two_identities, monkeypatch
    ):
        """``stop`` reads its target through the adoption walk, never through the
        identity predicate — unchanged by F-889 — and forgets exactly the ONE
        entry it stopped (F-886). With the newer stranger down and ours
        responsive, ours is what the walk names."""
        monkeypatch.setattr(
            singleton.display_context, "display_context", lambda: CONTEXT
        )
        monkeypatch.setattr(
            singleton, "_server_is_healthy", lambda port: port == OUR_PORT
        )
        monkeypatch.setattr(
            singleton, "_backend_http_ready", lambda port, **_kw: port == OUR_PORT
        )
        monkeypatch.setattr(singleton, "_is_our_backend", lambda pid: pid == OUR_PID)

        result, pid = singleton.stop_backend()

        assert (result, pid) == ("stopped", OUR_PID)
        assert two_identities == [OUR_PORT]
        assert [
            e.get("port")
            for e in backend_registry.read_backends(singleton.SERVER_STATE_FILE)
        ] == [THEIR_PORT], "the sibling's entry must survive its neighbour's stop"
