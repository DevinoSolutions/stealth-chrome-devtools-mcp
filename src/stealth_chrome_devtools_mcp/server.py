"""Entrypoint for the self-contained Stealth Chrome DevTools MCP server."""

from __future__ import annotations

import argparse
import logging
import runpy
import threading
from pathlib import Path

EMBEDDED_DIR = Path(__file__).with_name("embedded")


def _start_proxy_error_reporting() -> threading.Thread:
    """Give the stdio proxy its own Sentry, off the critical path (F-827).

    Until this existed, ``sentry_init()`` was called by exactly two roles — the
    backend (``embedded/server.py``'s ``__main__``) and the ops CLI — and the
    stdio branch below returns before the ``runpy`` load, so no proxy process
    ever paid for it. Every proxy-side decision (a watchdog condemnation, a heal
    outcome, an eviction) was invisible to the maintainer, which is why the
    whole 2026-08-30 disconnect saga was diagnosed from local log files.

    **Off-thread, deliberately.** ``import sentry_sdk`` plus ``sentry_sdk.init``
    measures ~1.5-2.5s on this machine. The proxy's value is that it answers the
    client's ``initialize`` locally and instantly, so that cost paid inline
    would be added to EVERY session start, ahead of the handshake it exists to
    keep fast — and there is no "after the handshake, before the serve loop"
    seam to use instead, because the handshake happens inside the serve loop.
    A daemon thread overlaps it with the backend discovery that follows and can
    never hold the process open. The residual is stated, not hidden: a failure
    in the proxy's first ~2s is not reported.
    """
    from stealth_chrome_devtools_mcp.observability import sentry_init

    thread = threading.Thread(target=sentry_init, name="proxy-sentry-init", daemon=True)
    thread.start()
    return thread


def main() -> None:
    """Run the embedded stealth browser MCP server from this package.

    The whole body is guarded, not just the ``runpy`` call: ``serve`` without
    ``--http`` is the DEFAULT verb and returns from the stdio-proxy branch
    without ever reaching ``runpy``, and Ctrl+C is the only way to stop a
    foreground serve. One guard on the one entry point, so there is no second
    way out of ``main()`` (F-809).
    """
    try:
        from stealth_chrome_devtools_mcp.embedded.singleton import DEFAULT_PORT

        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("--transport", default="stdio")
        parser.add_argument("--standalone", action="store_true")
        parser.add_argument("--singleton-port", type=int, default=DEFAULT_PORT)
        known, _ = parser.parse_known_args()

        if known.transport == "stdio" and not known.standalone:
            from stealth_chrome_devtools_mcp.embedded.logging_setup import (
                configure_logging,
            )
            from stealth_chrome_devtools_mcp.embedded.singleton import (
                ensure_server_running,
                run_stdio_proxy,
            )

            # Both of the proxy's observability channels, wired HERE and only
            # here: this is the one branch that never reaches runpy, so the
            # backend cannot inherit a second init when it loads this module as
            # __main__ (a runpy double-load has already cost this repo a 3x tool
            # registration). The log handler must precede ensure_server_running,
            # whose cold-start daemon thread logs — including "backend cold start
            # failed" — otherwise race the handler install.
            configure_logging("proxy")
            _start_proxy_error_reporting()
            port = ensure_server_running(port=known.singleton_port)
            if port is not None:
                run_stdio_proxy(port)
                return

        runpy.run_path(str(EMBEDDED_DIR / "server.py"), run_name="__main__")
    except KeyboardInterrupt:
        # Ctrl+C on the HTTP backend shuts down cleanly and THEN escapes: on the
        # way out uvicorn's ``capture_signals`` restores the pre-serve signal
        # dispositions and re-raises every signal it captured, so SIGINT lands on
        # Python's own interrupt handler with nothing left to catch it. Unhandled,
        # that prints a traceback and reaches ``sys.excepthook`` — which Sentry
        # ships as an unhandled error on every Ctrl+C. ``SystemExit`` does
        # neither; 130 is the conventional exit code for a SIGINT stop.
        #
        # An interrupt can also land mid-startup (a wedged orphan sweep, a
        # blocked bind), where the frame is the whole diagnosis — so keep it,
        # at DEBUG: below Sentry's ERROR event level and its INFO breadcrumb
        # level, and silent unless someone turns debug logging on.
        logging.getLogger(__name__).debug("interrupted (SIGINT)", exc_info=True)
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
