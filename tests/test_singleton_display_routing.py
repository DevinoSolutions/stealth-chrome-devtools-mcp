"""F-808 Task 4 — discovery routes a client to a backend that can show windows.

The defect: every client adopted whichever backend happened to be recorded
first. A Claude Code session running where windows CAN be shown would adopt a
backend started from a blind context (a service session, an SSH login), and
every `spawn_browser(headless=False)` on it launched a browser nobody could
see — Chrome inherits its parent's window station, so the backend's context,
not the caller's flag, decides visibility.

The fix, pinned here: candidates come in adoption order (window-capable first,
proven-HEADLESS excluded when we ourselves can show windows), and IDENTITY —
version plus source fingerprint plus a live `initialize` — stays the only gate.
Display context is a preference, never an equality test: the headless client
adopting the desktop backend is the point, not a leak.

Two more properties get pins here because they are what makes the preference
safe rather than merely nice:

* iteration continues past a candidate the identity gate refuses, so one stale
  entry can no longer shadow a live backend behind it (the residual first-entry
  weakness the old single-candidate read had);
* a cheap socket pre-filter runs before the app probe, so a dead recorded
  backend costs milliseconds on every proxy start instead of a 2s HTTP timeout;
* `_select_backend_port` never picks a port another context has recorded —
  `record_backend` supersedes by port at Popen time, so binding there would
  evict a live sibling's record before our own backend was even ready.

Everything runs against the record and stubbed probes: no processes, no
sockets. The CLIENT's own context is controlled by patching
`display_context.display_context` (the module attribute singleton reads at call
time), the same sentinel-token idiom `test_singleton_version_aware.py`'s
`stubbed_context` fixture uses.
"""

from __future__ import annotations

import pytest

from stealth_chrome_devtools_mcp.embedded import (
    backend_registry as reg,
)
from stealth_chrome_devtools_mcp.embedded import (
    display_context,
    proxy_forwarder,
    singleton,
)
from stealth_chrome_devtools_mcp.embedded.display_context import HEADLESS, UNVERIFIED

DESKTOP = "win-session-1"
OTHER_DESKTOP = "win-session-2"
VERSION = "9.9.9"
FINGERPRINT = "fp-same"


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    """Redirect every singleton state path into tmp_path.

    The singleton globals are what gets patched, never backend_registry's: the
    registry functions take the path from their caller, so patching the
    registry's own globals would be inert.
    """
    monkeypatch.setattr(singleton, "STATE_DIR", tmp_path)
    monkeypatch.setattr(
        singleton, "SERVER_STATE_FILE", tmp_path / "server.json", raising=False
    )
    monkeypatch.setattr(singleton, "PORT_FILE", tmp_path / "server.port", raising=False)
    return tmp_path / "server.json"


@pytest.fixture
def our_identity(monkeypatch):
    """Make every recorded backend pass the identity+readiness gate, so the
    assertions below are about ROUTING and nothing else."""
    monkeypatch.setattr(singleton, "_server_version", lambda: VERSION)
    monkeypatch.setattr(singleton, "_source_fingerprint", lambda: FINGERPRINT)
    monkeypatch.setattr(singleton, "_backend_http_ready", lambda port, **kw: True)
    monkeypatch.setattr(singleton, "_server_is_healthy", lambda port: True)


@pytest.fixture
def client_context(monkeypatch):
    """Set the context of the CLIENT doing the discovering (not of anything
    recorded). Patching the module attribute is what lands: singleton reads
    `display_context.display_context()` at call time."""

    def _set(ctx: str) -> None:
        monkeypatch.setattr(display_context, "display_context", lambda: ctx)

    return _set


def _record(path, port, ctx, version=VERSION):
    reg.record_backend(
        path,
        port=port,
        version=version,
        pid=port,
        source_fingerprint=FINGERPRINT,
        display_context=ctx,
    )


class TestDiscoveryPrefersAWindowCapableBackend:
    def test_headless_client_adopts_the_desktop_backend(
        self, state_file, our_identity, client_context
    ):
        """THE F-808 fix as a test: a client that cannot show windows itself
        reuses the DESKTOP backend, so the browsers it spawns appear on the
        user's screen instead of on a station nobody is looking at."""
        _record(state_file, 1111, HEADLESS)
        _record(state_file, 2222, DESKTOP)
        client_context(HEADLESS)

        assert singleton._find_running_server() == 2222

    def test_falls_back_to_the_headless_backend_when_no_desktop_one_exists(
        self, state_file, our_identity, client_context
    ):
        """Preference, not requirement — otherwise a headless-only machine
        would cold start a second backend on every proxy start."""
        _record(state_file, 1111, HEADLESS)
        client_context(HEADLESS)

        assert singleton._find_running_server() == 1111

    def test_capable_client_never_adopts_a_proven_headless_backend(
        self, state_file, our_identity, client_context
    ):
        """The asymmetry: adopting a blind backend would strand THIS desktop
        session's headed browsing on it. None here means "cold start your
        own", which is the correct outcome, not a failure."""
        _record(state_file, 1111, HEADLESS)
        client_context("win-session-7")

        assert singleton._find_running_server() is None

    def test_unverified_client_adopts_anything(
        self, state_file, our_identity, client_context
    ):
        """UNVERIFIED is "could not classify", not "capable". Excluding
        headless backends on that basis would evict healthy backends on every
        platform we cannot read."""
        _record(state_file, 1111, HEADLESS)
        client_context(UNVERIFIED)

        assert singleton._find_running_server() == 1111


class TestCandidateIterationIsNotSingleShot:
    def test_a_stale_entry_does_not_shadow_a_live_one(
        self, state_file, our_identity, client_context
    ):
        """Recorded FIRST and version-stale, so the old read-one-entry
        discovery would have stopped here and returned None — evicting and
        respawning over a perfectly live backend recorded behind it."""
        _record(state_file, 1111, DESKTOP, version="0.0.1-stale")
        _record(state_file, 2222, OTHER_DESKTOP)
        client_context(DESKTOP)

        assert singleton._find_running_server() == 2222

    def test_dead_recorded_backend_is_skipped_cheaply(
        self, state_file, our_identity, client_context, monkeypatch
    ):
        """The socket pre-filter. Without it every dead record on the way to a
        live one costs a full HTTP timeout on the proxy's hot path, so the
        assertion is not just "found 2222" but "never even probed 1111"."""
        probed: list[int] = []
        monkeypatch.setattr(singleton, "_server_is_healthy", lambda port: port == 2222)
        monkeypatch.setattr(
            singleton,
            "_backend_http_ready",
            lambda port, **kw: bool(probed.append(port)) or True,
        )
        _record(state_file, 1111, DESKTOP)
        _record(state_file, 2222, OTHER_DESKTOP)
        client_context(DESKTOP)

        assert singleton._find_running_server() == 2222
        assert 1111 not in probed


class TestPortSelectionKeepsContextsApart:
    def test_two_contexts_never_fight_over_one_port(
        self, state_file, client_context, monkeypatch
    ):
        """A desktop client cold-starting while a healthy headless backend
        holds DEFAULT_PORT must pick a different port. Binding there would
        record OUR backend on that port at Popen time, and `record_backend`
        supersedes by port — silently evicting the live sibling's entry before
        our own backend was even ready."""
        _record(state_file, singleton.DEFAULT_PORT, HEADLESS)
        client_context(DESKTOP)
        # Only the CONFLICT may cause the fallback here: the port is not
        # foreign-held, and the fallback picker is a sentinel.
        monkeypatch.setattr(singleton, "_port_is_foreign_held", lambda port: False)
        monkeypatch.setattr(proxy_forwarder, "_free_port", lambda: 54321)

        assert singleton._select_backend_port(singleton.DEFAULT_PORT) == 54321

    def test_own_context_port_is_preferred(
        self, state_file, client_context, monkeypatch
    ):
        """Our own context's recorded port is where eviction/restart should
        land — and it must win over a sibling's entry that merely happens to
        be recorded first."""
        _record(state_file, 5555, HEADLESS)
        _record(state_file, 4444, DESKTOP)
        client_context(DESKTOP)
        monkeypatch.setattr(singleton, "_port_is_foreign_held", lambda port: False)

        assert singleton._select_backend_port(singleton.DEFAULT_PORT) == 4444
