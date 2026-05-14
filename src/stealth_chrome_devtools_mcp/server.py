"""Entrypoint for the self-contained Stealth Chrome DevTools MCP server."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path


EMBEDDED_DIR = Path(__file__).with_name("embedded")


def main() -> None:
    """Run the embedded stealth browser MCP server from this package."""
    embedded_path = str(EMBEDDED_DIR)
    if embedded_path not in sys.path:
        sys.path.insert(0, embedded_path)
    runpy.run_path(str(EMBEDDED_DIR / "server.py"), run_name="__main__")


if __name__ == "__main__":
    main()
