"""F-904: importing ``__main__`` by name must never start a backend.

``src/stealth_chrome_devtools_mcp/__main__.py`` existed to serve exactly one
purpose: ``python -m stealth_chrome_devtools_mcp`` runs it as ``__main__`` and
that call reaches ``server.main()``. Before this fix its body was

    from stealth_chrome_devtools_mcp.server import main
    main()

with no ``if __name__ == "__main__":`` guard, so *any* import of the module by
its dotted name — ``importlib.import_module("stealth_chrome_devtools_mcp.__main__")``,
``pkgutil.walk_packages`` + ``import_module`` (which tooling may do while
enumerating a package), a stray ``import stealth_chrome_devtools_mcp.__main__``
in a REPL or a test — ran ``main()`` as an ordinary side effect of the import
statement. ``main()`` cold-starts a REAL backend into the operator's
``~/.stealth-mcp`` (measured on 2026-09-21: pid 189088, port 64986, from
nothing more than an import).

Two nodes below pin the fix. ``TestImportingDoesNotRun`` is the RED-then-GREEN
case: importing the module by name must be inert. ``TestRunningAsMainStillRuns``
is the node that guarantees the fix did not also break the one thing this file
exists for — ``python -m stealth_chrome_devtools_mcp`` must still reach
``main()``, simulated here with ``runpy.run_module(..., run_name="__main__")``,
the same mechanism ``python -m`` itself uses. ``TestGuardShape`` reads the
source with ``ast`` so a future edit cannot reintroduce a second, unguarded
top-level call beside the guarded one.

Both dynamic nodes patch ``stealth_chrome_devtools_mcp.server.main`` to a
tripwire BEFORE importing/running ``__main__`` — ``__main__.py`` binds its own
``main`` name via ``from stealth_chrome_devtools_mcp.server import main``,
which resolves at import time, so the patch must be in place first for either
node to observe (or safely tolerate) a call.
"""

from __future__ import annotations

import ast
import importlib
import runpy
import sys
from pathlib import Path
from unittest.mock import patch

MODULE_NAME = "stealth_chrome_devtools_mcp.__main__"
MAIN_SOURCE = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "stealth_chrome_devtools_mcp"
    / "__main__.py"
)


def _unload(name: str) -> None:
    sys.modules.pop(name, None)


class TestImportingDoesNotRun:
    def test_importing_by_name_never_calls_main(self):
        """A bare import must be inert: no backend, no side effect."""
        calls: list[None] = []
        _unload(MODULE_NAME)
        try:
            with patch(
                "stealth_chrome_devtools_mcp.server.main",
                side_effect=lambda: calls.append(None),
            ):
                importlib.import_module(MODULE_NAME)
        finally:
            _unload(MODULE_NAME)
        assert calls == [], (
            "importing stealth_chrome_devtools_mcp.__main__ by name called "
            "main() — an import started a real backend (F-904)"
        )


class TestRunningAsMainStillRuns:
    def test_python_dash_m_still_reaches_main(self):
        """The fix must not cost the module its one real job.

        ``runpy.run_module(pkg, run_name="__main__")`` is the same mechanism
        ``python -m stealth_chrome_devtools_mcp`` uses to resolve and execute
        the package's ``__main__`` submodule.
        """
        calls: list[None] = []
        _unload(MODULE_NAME)
        _unload("stealth_chrome_devtools_mcp")
        try:
            with patch(
                "stealth_chrome_devtools_mcp.server.main",
                side_effect=lambda: calls.append(None),
            ):
                runpy.run_module("stealth_chrome_devtools_mcp", run_name="__main__")
        finally:
            _unload(MODULE_NAME)
            _unload("stealth_chrome_devtools_mcp")
        assert calls == [None], (
            "running the package as __main__ must call main() exactly once"
        )


class TestGuardShape:
    def test_the_only_top_level_call_is_guarded(self):
        """AST guard: the module's only top-level statement calling anything
        must sit under ``if __name__ == "__main__":`` — never a bare call
        beside it."""
        tree = ast.parse(MAIN_SOURCE.read_text(encoding="utf-8"))
        bare_calls = [
            node
            for node in tree.body
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
        ]
        assert bare_calls == [], (
            f"{MAIN_SOURCE} has a top-level call not under "
            '`if __name__ == "__main__":` — importing it would run that call'
        )

        guards = [
            node
            for node in tree.body
            if isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "__name__"
        ]
        assert len(guards) == 1, (
            f'{MAIN_SOURCE} must have exactly one `if __name__ == "__main__":` guard'
        )
        assert any(
            isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
            for stmt in guards[0].body
        ), f"{MAIN_SOURCE}'s __name__ guard has no call in its body"
