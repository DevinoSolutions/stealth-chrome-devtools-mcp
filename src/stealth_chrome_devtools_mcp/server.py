"""Entrypoint for the self-contained Stealth Chrome DevTools MCP server."""

from __future__ import annotations

import argparse
import runpy
from pathlib import Path

EMBEDDED_DIR = Path(__file__).with_name("embedded")


def main() -> None:
    """Run the embedded stealth browser MCP server from this package."""
    from stealth_chrome_devtools_mcp.embedded.singleton import DEFAULT_PORT

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--transport", default="stdio")
    parser.add_argument("--standalone", action="store_true")
    parser.add_argument("--singleton-port", type=int, default=DEFAULT_PORT)
    known, _ = parser.parse_known_args()

    if known.transport == "stdio" and not known.standalone:
        from stealth_chrome_devtools_mcp.embedded.singleton import (
            ensure_server_running,
            run_stdio_proxy,
        )

        port = ensure_server_running(port=known.singleton_port)
        if port is not None:
            run_stdio_proxy(port)
            return

    try:
        runpy.run_path(str(EMBEDDED_DIR / "server.py"), run_name="__main__")
    except KeyboardInterrupt:
        # Ctrl+C on the HTTP backend shuts down cleanly and THEN escapes: on the
        # way out uvicorn's ``capture_signals`` restores the pre-serve signal
        # dispositions and re-raises every signal it captured, so SIGINT lands on
        # Python's own interrupt handler with nothing left to catch it. Unhandled,
        # that prints a traceback and reaches ``sys.excepthook`` — which Sentry
        # ships as an unhandled error on every Ctrl+C. ``SystemExit`` does
        # neither; 130 is the conventional exit code for a SIGINT stop (F-809).
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
