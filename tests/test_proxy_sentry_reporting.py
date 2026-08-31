"""F-827 — the stdio PROXY is invisible to Sentry, and it is the component
that decides to disconnect.

``sentry_init()`` was called by exactly two roles: the backend
(``embedded/server.py``'s ``__main__``) and the ops CLI (``cli.main``). The thin
entrypoint's stdio branch returns after ``run_stdio_proxy`` and never reaches
the ``runpy`` load, so no proxy process has ever paid for error reporting. Every
proxy-side decision — the watchdog condemning a backend, a heal succeeding or
failing, an eviction for a source change — happened with no telemetry at all.
The entire disconnect saga (F-820 / F-829 / F-838 / F-839) was diagnosed from
local log files because the component making the calls shipped nothing.

What this file pins:

  * the stdio branch — and ONLY the stdio branch — wires error reporting, so
    the ``runpy`` backend path cannot double-init inside one process (this repo
    has already been burned by a runpy double-load: 282 == 3 x 94 tools);
  * reporting is wired WITHOUT blocking the bridge coming up: ``sentry_sdk``
    costs ~1.5-2.5s to import+init, and the proxy's local ``initialize`` answer
    is what keeps Claude Code's connection timeout from firing;
  * the four disconnect-relevant transitions each ship exactly one capture with
    the fields an investigation needs;
  * every capture path no-ops when reporting is off and never raises — and a
    capture seam that DOES raise cannot break the proxy flow.

Hermetic throughout: no DSN, no network, no real backend, no real
``~/.stealth-mcp``, no Chrome. The Sentry seam is always a spy.
"""

from __future__ import annotations

import json
import logging
import runpy
import sys
from contextlib import contextmanager

import anyio
import pytest

from stealth_chrome_devtools_mcp import observability
from stealth_chrome_devtools_mcp import server as entrypoint
from stealth_chrome_devtools_mcp.embedded import (
    logging_setup,
    proxy_selfheal,
    singleton,
)

PORT_A = 41827
PORT_B = 41828

#: The one message the eviction site reports. Deliberately a literal here (and
#: a literal in ``singleton``): that file has no LOC headroom for a constant.
EVICTED_EVENT = "proxy: backend evicted (source changed)"


class _Sink:
    """A ``client_write`` stand-in that records what the proxy sends back."""

    def __init__(self):
        self.sent = []

    async def send(self, msg):
        self.sent.append(msg)


@pytest.fixture()
def captured(monkeypatch):
    """Spy the ONE capture seam, wherever it is reached from.

    ``proxy_selfheal`` imports it lazily (module attribute read at call time);
    ``singleton`` binds it at import, so both names are patched.
    """
    events = []

    def _spy(message, *, level="warning", **fields):
        events.append((message, level, fields))
        return True

    monkeypatch.setattr(observability, "capture_lifecycle", _spy)
    monkeypatch.setattr(singleton, "capture_lifecycle", _spy, raising=False)
    return events


@pytest.fixture()
def isolated_state(tmp_path, monkeypatch):
    """Point singleton state at tmp_path so tests never touch ~/.stealth-mcp."""
    monkeypatch.setattr(singleton, "STATE_DIR", tmp_path)
    monkeypatch.setattr(singleton, "PORT_FILE", tmp_path / "server.port")
    monkeypatch.setattr(
        singleton, "SERVER_STATE_FILE", tmp_path / "server.json", raising=False
    )
    return tmp_path


# --------------------------------------------------------------------------
# 1. where sentry_init runs (and where it must NOT)
# --------------------------------------------------------------------------
class TestInitPlacement:
    def test_the_proxy_bootstrap_calls_sentry_init_exactly_once(self, monkeypatch):
        calls = []
        monkeypatch.setattr(observability, "sentry_init", lambda: calls.append(1))

        entrypoint._start_proxy_error_reporting().join(timeout=30)

        assert calls == [1]

    def test_init_does_not_block_the_bridge_coming_up(self, monkeypatch):
        """sentry_sdk costs ~1.5-2.5s to import+init. Paid inline it is added to
        EVERY session start, ahead of the local ``initialize`` answer that keeps
        the client's connection timeout from firing — so it runs off-thread,
        exactly like the cold-start ``_start_backend_holding_lock`` does."""
        released = anyio.Event()

        def _slow_init():
            # Would deadlock the test if the caller waited for it.
            while not released.is_set():
                pass

        monkeypatch.setattr(observability, "sentry_init", _slow_init)

        thread = entrypoint._start_proxy_error_reporting()
        try:
            assert thread.is_alive(), "init must not have been awaited inline"
            assert thread.daemon, "a stuck reporter must never keep the proxy alive"
        finally:
            released.set()
            thread.join(timeout=30)

    def test_the_stdio_branch_wires_reporting_before_the_cold_start(self, monkeypatch):
        order = []
        monkeypatch.setattr(
            logging_setup, "configure_logging", lambda role: order.append(("log", role))
        )
        monkeypatch.setattr(
            entrypoint,
            "_start_proxy_error_reporting",
            lambda: order.append("sentry"),
        )
        monkeypatch.setattr(
            singleton,
            "ensure_server_running",
            lambda port: order.append(("ensure", port)) or PORT_A,
        )
        monkeypatch.setattr(
            singleton, "run_stdio_proxy", lambda port: order.append(("proxy", port))
        )
        monkeypatch.setattr(
            runpy,
            "run_path",
            lambda *a, **k: pytest.fail("the stdio branch must never reach runpy"),
        )
        monkeypatch.setattr(sys, "argv", ["stealth-chrome-devtools-mcp"])

        entrypoint.main()

        assert order == [
            ("log", "proxy"),
            "sentry",
            ("ensure", singleton.DEFAULT_PORT),
            ("proxy", PORT_A),
        ]

    @pytest.mark.parametrize(
        "argv",
        [
            ["x", "--transport", "http"],
            ["x", "--standalone"],
        ],
    )
    def test_the_runpy_backend_path_never_initializes_from_the_entrypoint(
        self, monkeypatch, argv
    ):
        """The structural half of the fix. ``embedded/server.py`` does its own
        ``sentry_init()``; an init at the top of ``main()`` would run a SECOND
        one in the same process the moment runpy loads the backend."""
        inits = []
        started = []
        loaded = []
        monkeypatch.setattr(observability, "sentry_init", lambda: inits.append(1))
        monkeypatch.setattr(
            entrypoint, "_start_proxy_error_reporting", lambda: started.append(1)
        )
        monkeypatch.setattr(
            runpy, "run_path", lambda path, run_name=None: loaded.append(run_name)
        )
        monkeypatch.setattr(sys, "argv", argv)

        entrypoint.main()
        entrypoint.main()  # a second load must not accumulate either

        assert inits == [], "the entrypoint must not init on the backend path"
        assert started == []
        assert loaded == ["__main__", "__main__"]


# --------------------------------------------------------------------------
# 2. the four lifecycle transitions
# --------------------------------------------------------------------------
def _drive_kwargs(**overrides):
    async def connect(_url, _replay, armed):
        armed.set()
        await anyio.sleep_forever()

    async def watch(_port):
        await anyio.sleep_forever()

    kwargs = {
        "port": PORT_A,
        "url_for": lambda p: f"http://127.0.0.1:{p}/mcp/",
        "connect": connect,
        "watch": watch,
        # F-843: default "dead", the premise of the death cases below — and it
        # keeps the real ~/.stealth-mcp record out of the hermetic lane.
        "confirm_alive": lambda _port: False,
        "replay": lambda: None,
        "pending": proxy_selfheal.PendingCalls(),
        "client_write": _Sink(),
        "ensure_running": lambda p: p,
        "await_ready": None,
    }
    kwargs.update(overrides)
    return kwargs


class TestCondemnationIsReported:
    async def test_the_watchdogs_verdict_ships_with_port_and_strike_timing(
        self, captured
    ):
        async def connect(_url, _replay, armed):
            armed.set()
            await anyio.sleep_forever()

        async def watch(_port):
            return  # the F-820 confirmed-dead verdict

        with anyio.fail_after(5):
            cause = await proxy_selfheal._one_generation(
                url="http://127.0.0.1/mcp/",
                replay_msg=None,
                port=PORT_A,
                connect=connect,
                watch=watch,
                confirm_alive=lambda _port: False,
                pending=proxy_selfheal.PendingCalls(),
                client_write=_Sink(),
            )

        assert cause == proxy_selfheal.WATCHDOG_CAUSE
        assert [e[0] for e in captured] == [proxy_selfheal.CONDEMNED_EVENT]
        fields = captured[0][2]
        assert fields["port"] == PORT_A
        assert fields["cause"] == proxy_selfheal.WATCHDOG_CAUSE
        assert "strike_seconds" in fields, "the timing is what dates the verdict"

    async def test_a_bridge_first_death_is_condemned_under_its_own_cause(
        self, captured
    ):
        """F-843. The same incident seen by the FAST witness must ship too, and
        must be distinguishable: a ``connection_lost`` condemnation is a backend
        that died under a live call, ~11s before the watchdog could have said
        so. Same event name (the user-visible thing is identical), different
        cause — one query still answers "how often is a backend condemned"."""

        async def connect(_url, _replay, armed):
            armed.set()
            raise ConnectionResetError("backend went away mid-call")

        async def watch(_port):
            await anyio.sleep_forever()  # mid-first-strike: no verdict of its own

        with anyio.fail_after(5):
            cause = await proxy_selfheal._one_generation(
                url="http://127.0.0.1/mcp/",
                replay_msg=None,
                port=PORT_A,
                connect=connect,
                watch=watch,
                confirm_alive=lambda _port: False,
                pending=proxy_selfheal.PendingCalls(),
                client_write=_Sink(),
            )

        assert cause == proxy_selfheal.CONNECTION_LOST_CAUSE
        assert [e[0] for e in captured] == [proxy_selfheal.CONDEMNED_EVENT]
        fields = captured[0][2]
        assert fields["port"] == PORT_A
        assert fields["cause"] == proxy_selfheal.CONNECTION_LOST_CAUSE

    async def test_a_live_backend_behind_a_broken_leg_is_never_condemned(
        self, captured
    ):
        """The confirmation's whole point: a leg that broke over a backend the
        one gate still calls alive is a reset, not a death. Condemning it would
        make every F-827 condemnation count a lie."""

        async def connect(_url, _replay, armed):
            armed.set()
            raise ConnectionResetError("the leg broke, the backend did not")

        async def watch(_port):
            await anyio.sleep_forever()

        with anyio.fail_after(5):
            cause = await proxy_selfheal._one_generation(
                url="http://127.0.0.1/mcp/",
                replay_msg=None,
                port=PORT_A,
                connect=connect,
                watch=watch,
                confirm_alive=lambda _port: True,
                pending=proxy_selfheal.PendingCalls(),
                client_write=_Sink(),
            )

        assert cause == proxy_selfheal.CONNECTION_RESET_CAUSE
        assert captured == [], "nothing was condemned; nothing may be reported"


class TestHealOutcomesAreReported:
    async def test_a_successful_heal_ships_old_port_new_port_and_generation(
        self, captured, monkeypatch
    ):
        deaths = [True, False]

        async def watch(_port):
            if deaths.pop(0):
                return
            await anyio.sleep_forever()

        async def fake_heal(_dead_port, **_kw):
            return PORT_B

        monkeypatch.setattr(proxy_selfheal, "heal_backend", fake_heal)

        with anyio.move_on_after(5):
            await proxy_selfheal.drive(**_drive_kwargs(watch=watch))

        healed = [e for e in captured if e[0] == proxy_selfheal.HEALED_EVENT]
        assert len(healed) == 1
        assert healed[0][2]["old_port"] == PORT_A
        assert healed[0][2]["new_port"] == PORT_B
        assert healed[0][2]["generation"] == 2
        # F-843: and WHICH witness saw the death this heal answered.
        assert healed[0][2]["cause"] == proxy_selfheal.WATCHDOG_CAUSE

    async def test_an_unhealable_backend_ships_the_user_visible_teardown(
        self, captured, monkeypatch
    ):
        async def watch(_port):
            return

        async def fake_heal(_dead_port, **_kw):
            return None

        monkeypatch.setattr(proxy_selfheal, "heal_backend", fake_heal)

        with anyio.fail_after(5):
            await proxy_selfheal.drive(**_drive_kwargs(watch=watch))

        teardowns = [e for e in captured if e[0] == proxy_selfheal.TEARDOWN_EVENT]
        assert len(teardowns) == 1
        assert teardowns[0][1] == "error", "the disconnect the user feels is an error"
        assert teardowns[0][2]["reason"] == "unhealable"
        assert teardowns[0][2]["port"] == PORT_A

    async def test_a_flapping_backend_ships_the_teardown_too(
        self, captured, monkeypatch
    ):
        async def watch(_port):
            return

        async def fake_heal(dead_port, **_kw):
            return dead_port + 1

        monkeypatch.setattr(proxy_selfheal, "heal_backend", fake_heal)

        with anyio.fail_after(5):
            await proxy_selfheal.drive(**_drive_kwargs(watch=watch))

        teardowns = [e for e in captured if e[0] == proxy_selfheal.TEARDOWN_EVENT]
        assert len(teardowns) == 1
        assert teardowns[0][2]["reason"] == "flapping"
        assert (
            teardowns[0][2]["consecutive_heals"] > proxy_selfheal.MAX_CONSECUTIVE_HEALS
        )

    async def test_a_backend_that_never_became_ready_reports_nothing(
        self, captured, monkeypatch
    ):
        """SOFT golden, updated deliberately with F-843.

        This node used to arm the generation and then let ``connect`` return,
        asserting silence — which is exactly the shape F-843 turned out to be:
        an armed backend whose leg ends IS an incident, and reporting nothing
        was the defect, not the contract. What the node was really protecting is
        that a generation with nothing to lose stays silent, so it now states
        that premise properly: readiness never came, ``armed`` never set."""

        async def connect(_url, _replay, _armed):
            return  # never armed: no backend was ever serving this proxy

        with anyio.fail_after(5):
            await proxy_selfheal.drive(**_drive_kwargs(connect=connect))

        assert captured == []

    async def test_the_client_going_away_reports_nothing(self, captured, monkeypatch):
        """The client's own exit is not a backend disconnect. It reaches
        ``drive`` as a cancellation from the proxy's OUTER task group, so no
        verdict is ever reached — and nothing may be shipped."""

        async def connect(_url, _replay, armed):
            armed.set()
            await anyio.sleep_forever()

        async def run():
            await proxy_selfheal.drive(**_drive_kwargs(connect=connect))

        async with anyio.create_task_group() as outer:
            outer.start_soon(run)
            await anyio.sleep(0.05)
            outer.cancel_scope.cancel()

        assert captured == []


class TestEvictionIsReported:
    def test_a_genuine_source_change_ships_alongside_its_log_line(
        self, captured, isolated_state, monkeypatch, caplog
    ):
        """Mirrors ``test_singleton_version_aware`` 's eviction stubbing: the
        version matches and the digests differ, which is the ONE reading that
        means 'someone edited the source' (never F-829's unreadable digest)."""

        @contextmanager
        def fake_lock():
            yield True

        monkeypatch.setattr(singleton, "_exclusive_lock", fake_lock)
        monkeypatch.setattr(singleton, "_server_version", lambda: "1.2.1")
        monkeypatch.setattr(singleton, "_source_fingerprint", lambda: "NEW")
        (isolated_state / "server.json").write_text(
            json.dumps(
                {
                    "port": 19222,
                    "version": "1.2.1",
                    "pid": 4242,
                    "source_fingerprint": "OLD",
                }
            )
        )
        monkeypatch.setattr(singleton, "_clear_stale_backend", lambda port: None)
        monkeypatch.setattr(singleton, "_start_server_process", lambda port: None)
        monkeypatch.setattr(singleton, "_wait_for_server", lambda port: True)

        with caplog.at_level(logging.INFO, logger="stealth.proxy"):
            singleton._start_backend_holding_lock(19222)

        # The log line is untouched: the capture PIGGYBACKS, it never replaces.
        assert caplog.messages.count("backend stale (source changed), evicting") == 1
        evictions = [e for e in captured if e[0] == EVICTED_EVENT]
        assert len(evictions) == 1
        assert evictions[0][2]["port"] == 19222

    def test_an_unreadable_fingerprint_is_never_reported_as_an_eviction(
        self, captured, isolated_state, monkeypatch
    ):
        """F-829's whole point, now also on the wire: a digest we could not read
        is unknown, not an edit, so it must not ship an eviction event."""

        @contextmanager
        def fake_lock():
            yield True

        monkeypatch.setattr(singleton, "_exclusive_lock", fake_lock)
        monkeypatch.setattr(singleton, "_server_version", lambda: "1.2.1")
        monkeypatch.setattr(singleton, "_source_fingerprint", lambda: None)
        monkeypatch.setattr(singleton, "_same_identity_backend_ready", lambda *a: False)
        (isolated_state / "server.json").write_text(
            json.dumps(
                {
                    "port": 19222,
                    "version": "1.2.1",
                    "pid": 4242,
                    "source_fingerprint": "OLD",
                }
            )
        )
        monkeypatch.setattr(singleton, "_clear_stale_backend", lambda port: None)
        monkeypatch.setattr(singleton, "_start_server_process", lambda port: None)
        monkeypatch.setattr(singleton, "_wait_for_server", lambda port: True)

        singleton._start_backend_holding_lock(19222)

        assert [e for e in captured if e[0] == EVICTED_EVENT] == []


# --------------------------------------------------------------------------
# 3. the capture seam itself: off is silent, and it never raises
# --------------------------------------------------------------------------
class TestCaptureSeamContract:
    def test_it_no_ops_without_raising_when_reporting_is_disabled(self, monkeypatch):
        # conftest sets STEALTH_MCP_NO_ERROR_REPORTING for the whole session;
        # make the dependency explicit rather than inherited.
        monkeypatch.setenv("STEALTH_MCP_NO_ERROR_REPORTING", "true")
        import sentry_sdk

        monkeypatch.setattr(
            sentry_sdk,
            "capture_message",
            lambda *a, **k: pytest.fail("a disabled reporter must not reach the SDK"),
        )

        assert observability.capture_lifecycle("x", port=1) is False

    def test_it_returns_false_instead_of_raising_when_the_sdk_fails(self, monkeypatch):
        monkeypatch.delenv("STEALTH_MCP_NO_ERROR_REPORTING", raising=False)
        import sentry_sdk

        def _boom(*_a, **_k):
            raise RuntimeError("transport exploded")

        monkeypatch.setattr(sentry_sdk, "new_scope", _boom)

        assert observability.capture_lifecycle("x", port=1) is False

    def test_the_fields_reach_the_event_as_one_scrubbable_context(self, monkeypatch):
        monkeypatch.delenv("STEALTH_MCP_NO_ERROR_REPORTING", raising=False)
        import sentry_sdk

        seen = {}

        class _Scope:
            def set_context(self, name, value):
                seen["context"] = (name, value)

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

        monkeypatch.setattr(sentry_sdk, "new_scope", _Scope)
        monkeypatch.setattr(
            sentry_sdk,
            "capture_message",
            lambda message, level=None: seen.update(message=message, level=level),
        )

        assert observability.capture_lifecycle("hi", level="error", port=7) is True
        assert seen["message"] == "hi"
        assert seen["level"] == "error"
        assert seen["context"][1] == {"port": 7}

    async def test_a_raising_seam_cannot_break_the_proxy_flow(self, monkeypatch):
        """The reporter is a backstop's backstop: even a capture that throws
        must leave the heal loop's control flow exactly as it was."""

        def _boom(*_a, **_k):
            raise RuntimeError("reporting exploded")

        monkeypatch.setattr(observability, "capture_lifecycle", _boom)

        heals = []

        async def watch(_port):
            return

        async def fake_heal(dead_port, **_kw):
            heals.append(dead_port)

        monkeypatch.setattr(proxy_selfheal, "heal_backend", fake_heal)

        with anyio.fail_after(5):
            await proxy_selfheal.drive(**_drive_kwargs(watch=watch))

        assert heals == [PORT_A], "the flow must be untouched by a broken reporter"
