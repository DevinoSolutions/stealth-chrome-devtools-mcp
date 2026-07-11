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

    runpy.run_path(str(EMBEDDED_DIR / "server.py"), run_name="__main__")


if __name__ == "__main__":
    main()
