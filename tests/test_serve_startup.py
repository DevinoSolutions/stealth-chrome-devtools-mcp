"""F-856 — readiness before reaping: startup work must not delay first serve.

The second half of the 2026-09-02 incident. A cold-started backend bound its
socket within seconds and then answered nothing for ~114 of them, because
``app_lifespan`` — which FastMCP runs on the first MCP session — called
``process_cleanup.activate()`` synchronously, and under that load the orphan
reap took ~90 seconds (a 5s force-kill wait per stubborn browser). The proxy
healing above it had a 45s readiness budget, so the heal could not have
succeeded no matter how patient the verdict before it had been.

Two things are pinned here: the leaf that runs a startup job off the serving
path, and the one caller that had to start using it — including the part of
``activate()`` that must STAY synchronous, because a signal handler installed
after the first browser exists is a handler that was not there when it counted.
"""

from __future__ import annotations

import threading

import pytest

from stealth_chrome_devtools_mcp.embedded import process_cleanup as cleanup_module
from stealth_chrome_devtools_mcp.embedded import serve_startup


class TestAfterServingLeavesTheCallerAlone:
    def test_the_job_runs_off_the_calling_thread(self):
        """The whole point: the caller returns while the work is still going."""
        ran = threading.Event()
        where: list[int] = []

        def job():
            where.append(threading.get_ident())
            ran.set()

        thread = serve_startup.after_serving(job, name="probe")
        thread.join(timeout=5)

        assert ran.is_set()
        assert where == [thread.ident]
        assert where[0] != threading.get_ident()

    def test_the_caller_does_not_wait_for_a_slow_job(self):
        """A 90-second reap must not become 90 seconds of an unanswered
        ``initialize``: ``after_serving`` returns while the job is blocked."""
        release = threading.Event()
        started = threading.Event()

        def job():
            started.set()
            release.wait(timeout=5)

        thread = serve_startup.after_serving(job, name="slow")
        assert started.wait(timeout=5)
        assert thread.is_alive()  # the caller is already back

        release.set()
        thread.join(timeout=5)

    def test_a_raising_job_never_reaches_the_caller(self):
        """Startup work is best-effort by contract — a reap that throws must not
        take down the backend it was supposed to make usable."""
        done = threading.Event()

        def job():
            done.set()
            raise RuntimeError("orphan reaping went wrong")

        thread = serve_startup.after_serving(job, name="angry")
        thread.join(timeout=5)

        assert done.is_set()
        assert not thread.is_alive()

    def test_the_worker_is_a_daemon(self):
        """Interpreter exit must never block on a half-finished sweep; the reap
        is idempotent and the next start redoes it."""
        thread = serve_startup.after_serving(lambda: None, name="daemon")
        thread.join(timeout=5)

        assert thread.daemon is True


class TestActivateDefersTheReap:
    @pytest.fixture
    def cleanup(self, monkeypatch, tmp_path):
        """A ``ProcessCleanup`` whose two activation steps are recorded rather
        than performed."""
        instance = cleanup_module.ProcessCleanup.__new__(cleanup_module.ProcessCleanup)
        order: list[str] = []
        monkeypatch.setattr(
            instance,
            "_setup_cleanup_handlers",
            lambda: order.append("handlers"),
            raising=False,
        )
        monkeypatch.setattr(
            cleanup_module, "get_settings", lambda: _Settings(no_auto_recovery=False)
        )
        return instance, order

    def test_activate_returns_before_the_reap_finishes(self, cleanup, monkeypatch):
        """THE F-856 pin for this half: with the reap blocked, ``activate()``
        must still return — that is the difference between a backend that
        answers its first ``initialize`` in seconds and one that answers in
        minutes."""
        instance, order = cleanup
        release = threading.Event()
        reaping = threading.Event()

        def slow_reap(force=False):
            order.append("reap-start")
            reaping.set()
            release.wait(timeout=5)
            order.append("reap-done")

        monkeypatch.setattr(
            instance, "_recover_orphaned_processes", slow_reap, raising=False
        )

        instance.activate()

        assert reaping.wait(timeout=5), "the reap must have been handed off"
        # The load-bearing assertion: the reap is still BLOCKED when the caller
        # is already back. A synchronous activate() would have "reap-done" here.
        assert order == ["handlers", "reap-start"]
        release.set()

    def test_no_auto_recovery_still_skips_everything(self, cleanup, monkeypatch):
        """The existing opt-out is not a deferral: nothing is handed off either."""
        instance, order = cleanup
        handed: list[object] = []
        monkeypatch.setattr(
            serve_startup, "after_serving", lambda job, **kw: handed.append(job)
        )
        monkeypatch.setattr(
            instance, "_recover_orphaned_processes", lambda force=False: None
        )
        monkeypatch.setattr(
            cleanup_module, "get_settings", lambda: _Settings(no_auto_recovery=True)
        )

        instance.activate()

        assert order == []
        assert handed == []


class _Settings:
    def __init__(self, *, no_auto_recovery: bool) -> None:
        self.no_auto_recovery = no_auto_recovery
