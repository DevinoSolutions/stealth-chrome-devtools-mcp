"""Embedded backend package for stealth-chrome-devtools-mcp.

Holds the self-contained MCP server (``server.py``) plus the browser manager,
cloners, hooks, storage, and their supporting modules. Every intra-package
import uses the canonical absolute form
``from stealth_chrome_devtools_mcp.embedded.X import Y`` so each module resolves
to exactly one identity no matter how the process is launched (installed entry
point, ``python -m``, or ``runpy.run_path`` of ``server.py``).

Sanctioned compatibility shim
-----------------------------
The block below is THE single sanctioned place that puts this ``embedded/``
directory on ``sys.path``. It exists only so ad-hoc scratch scripts that use
bare-name imports (``from debug_logger import debug_logger``) keep working once
they import this package. Package-internal code must never rely on it -- it uses
the absolute form above. Do not reintroduce ``sys.path`` inserts elsewhere; add
imports via the absolute package form instead.
"""

import sys
from pathlib import Path

_EMBEDDED_DIR = str(Path(__file__).resolve().parent)
if _EMBEDDED_DIR not in sys.path:
    sys.path.insert(0, _EMBEDDED_DIR)
