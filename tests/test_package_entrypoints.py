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
import os
import runpy
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

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


class TestNoModuleBodyDoesWork:
    """The whole-package rule, folded in from F-903 (which found this defect).

    ``TestGuardShape`` above closes the one module that was broken. This closes
    the CLASS: a deny-list protects against the module someone remembered, and
    F-903's test fence carried one (``_NEVER_IMPORT``) until this replaced it.
    What actually makes ``operator_fence.derived_globals()`` -- and every doc
    generator, import linter and coverage sweep -- safe is the stronger
    property: importing any module in this package only defines things.

    There is exactly ONE allowed call and it is named with its reason rather
    than pattern-matched, so adding a second is a decision someone writes down.
    """

    ALLOWED: frozenset[tuple[str, str]] = frozenset(
        {
            # CLAUDE.md: tool_runtime is THE one call site of cdp_transport
            # .install(). It is idempotent, does no I/O, spawns nothing, and has
            # to run once per process before any tool body -- a module body is
            # the only place with that shape.
            ("embedded/tool_runtime.py", "cdp_transport.install()"),
        }
    )

    def test_no_module_runs_anything_at_import(self):
        root = MAIN_SOURCE.parent
        offenders: list[str] = []
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:
                if not (
                    isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                ):
                    continue
                rel = path.relative_to(root).as_posix()
                if (rel, ast.unparse(node)) in self.ALLOWED:
                    continue
                offenders.append(f"{rel}:{node.lineno}: {ast.unparse(node)}")

        assert not offenders, (
            "a module body that CALLS something runs when the package is merely "
            "imported -- which is how __main__.py cold-started a real backend "
            "(F-904). Move it behind a function or an `if __name__` guard:\n"
            + "\n".join(offenders)
        )

    def test_the_allowance_still_describes_something_real(self):
        """An allow-list entry for code that moved is an allowance for nothing."""
        root = MAIN_SOURCE.parent
        for rel, call in self.ALLOWED:
            assert call in (root / rel).read_text(encoding="utf-8"), (
                f"{rel} no longer contains {call} -- drop the allowance"
            )


class TestTheRealDashMStillWorks:
    def test_python_dash_m_help_exits_zero(self, tmp_path):
        """End to end, in a real child, with the operator's HOME redirected.

        ``runpy.run_module`` above is the mechanism; this is the command. The
        two are not redundant -- an in-process ``run_module`` cannot catch a
        packaging or console-script fault, and a child cannot be given a
        tripwire.

        ``--transport http`` is what makes it safe: it routes ``main()`` past
        the stdio-proxy branch into the ``runpy`` load of ``embedded/server.py``,
        whose own parser HAS ``--help`` and answers it before anything binds a
        port or spawns a browser. The bare spelling is F-905 and must not be run.
        """
        home = tmp_path / "home"
        home.mkdir()
        env = dict(os.environ)
        env.update(
            HOME=str(home),
            USERPROFILE=str(home),
            STEALTH_MCP_NO_ERROR_REPORTING="1",
            STEALTH_MCP_NO_AUTO_RECOVERY="1",
            STEALTH_MCP_BROWSER_SESSION_ROOT=str(tmp_path / "sessions"),
            PYTHONUTF8="1",
        )

        done = subprocess.run(  # noqa: S603  PERMANENT(F-904: our own interpreter, fixed argv)
            [
                sys.executable,
                "-m",
                "stealth_chrome_devtools_mcp",
                "--transport",
                "http",
                "--help",
            ],
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
            check=False,
        )

        assert done.returncode == 0, (
            f"exit={done.returncode}\nstdout={done.stdout[-2000:]}\n"
            f"stderr={done.stderr[-2000:]}"
        )
        assert "--transport" in done.stdout


class TestAskingForHelpStartsNothing:
    """F-905: `--help` must reach a parser, never `ensure_server_running`.

    ``server.main`` builds its own parser with ``add_help=False`` and reads it
    with ``parse_known_args``, and BOTH are deliberate: it decides one thing --
    stdio proxy or ``runpy`` the backend -- from three flags, and every other
    argument belongs to ``embedded/server.py``'s full parser, which is reached
    through ``runpy`` and re-reads ``sys.argv``. An ``add_help=True`` here would
    answer with the SHIM's three-flag usage and hide the real one.

    What that left is a gap rather than a policy: ``--help`` is unknown to the
    shim, so with the default ``--transport stdio`` it fell past the parser into
    the stdio branch and reached ``ensure_server_running`` -- **asking for help
    cold-started a backend**. The fix routes a help request to the branch that
    can answer it, and touches no other argv.

    Neither node here starts anything: the first tripwires the cold start and
    the ``runpy`` load, and the second is the real child that already proves
    ``--transport http --help`` exits 0.
    """

    @pytest.mark.parametrize("flag", ["--help", "-h"])
    def test_a_help_request_never_reaches_the_cold_start(self, monkeypatch, flag):
        from stealth_chrome_devtools_mcp import server as shim
        from stealth_chrome_devtools_mcp.embedded import singleton

        def _must_not_start(*_a, **_k):
            raise AssertionError(
                f"`{flag}` reached ensure_server_running -- asking for help "
                "cold-starts a backend (F-905)"
            )

        loaded: list[str] = []
        monkeypatch.setattr(sys, "argv", ["stealth-chrome-devtools-mcp", flag])
        monkeypatch.setattr(singleton, "ensure_server_running", _must_not_start)
        monkeypatch.setattr(shim, "_start_proxy_error_reporting", lambda: None)
        monkeypatch.setattr(
            runpy, "run_path", lambda path, **_k: loaded.append(str(path))
        )

        shim.main()

        assert loaded, f"`{flag}` reached neither the cold start nor the backend parser"
        assert loaded[0].endswith("server.py")

    def test_a_real_transport_argument_is_untouched(self, monkeypatch):
        """The fix must be keyed on the help request and nothing else.

        Without this, "route help to runpy" could be implemented as "always
        runpy", which would delete the stdio proxy.
        """
        from stealth_chrome_devtools_mcp import server as shim
        from stealth_chrome_devtools_mcp.embedded import singleton

        served: list[int] = []
        monkeypatch.setattr(sys, "argv", ["stealth-chrome-devtools-mcp"])
        monkeypatch.setattr(singleton, "ensure_server_running", lambda port: 4321)
        monkeypatch.setattr(singleton, "run_stdio_proxy", served.append)
        monkeypatch.setattr(shim, "_start_proxy_error_reporting", lambda: None)
        monkeypatch.setattr(
            runpy,
            "run_path",
            lambda *_a, **_k: pytest.fail("an ordinary stdio start must not runpy"),
        )

        shim.main()

        assert served == [4321]
