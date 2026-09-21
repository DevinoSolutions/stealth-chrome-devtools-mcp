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
import contextlib
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


PACKAGE = "stealth_chrome_devtools_mcp"


def _unload(name: str) -> None:
    sys.modules.pop(name, None)


def _cached_package_modules() -> dict[str, object]:
    return {
        name: module
        for name, module in sys.modules.items()
        if name == PACKAGE or name.startswith(PACKAGE + ".")
    }


@contextlib.contextmanager
def _pristine_package_modules():
    """Put ``sys.modules`` back exactly as it was, whatever the body did.

    **Popping a PACKAGE while its submodules stay cached is unsound**, and this
    is the one file that has reason to do it. The next ``import
    stealth_chrome_devtools_mcp`` builds a NEW module object, while ``import
    stealth_chrome_devtools_mcp.embedded`` is a ``sys.modules`` HIT that never
    re-binds ``embedded`` as an attribute of that new parent -- not even an
    explicit ``importlib.import_module`` repairs it (measured). The package is
    then permanently un-walkable by attribute, which is how
    ``monkeypatch.setattr("stealth_chrome_devtools_mcp.embedded.<x>.<y>", …)``
    -- pytest resolves a dotted target by ``__import__`` plus a ``getattr``
    walk -- came to fail in ``tests/test_python_exec_timeout.py`` with
    ``module 'stealth_chrome_devtools_mcp' has no attribute 'embedded'``.

    It only ever showed up in a FULL lane: this file sorts before that one, and
    each file alone re-imports the package cleanly. Restoring the mapping here
    is what makes the pops below local to the node that needs them.
    """
    saved = _cached_package_modules()
    try:
        yield
    finally:
        for name in tuple(_cached_package_modules()):
            if name not in saved:
                del sys.modules[name]
        sys.modules.update(saved)


class TestImportingDoesNotRun:
    def test_importing_by_name_never_calls_main(self):
        """A bare import must be inert: no backend, no side effect."""
        calls: list[None] = []
        with _pristine_package_modules():
            _unload(MODULE_NAME)
            with patch(
                "stealth_chrome_devtools_mcp.server.main",
                side_effect=lambda: calls.append(None),
            ):
                importlib.import_module(MODULE_NAME)
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

        The pops are what force a fresh execution, and
        :func:`_pristine_package_modules` is what keeps them from outliving
        this node -- popping the PACKAGE leaves every later attribute walk over
        it broken, for the whole session.
        """
        calls: list[None] = []
        with _pristine_package_modules():
            _unload(MODULE_NAME)
            _unload(PACKAGE)
            with patch(
                "stealth_chrome_devtools_mcp.server.main",
                side_effect=lambda: calls.append(None),
            ):
                runpy.run_module(PACKAGE, run_name="__main__")
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


class TestTheImportTreeSurvivesTheseNodes:
    """Nothing above may leave the package un-walkable by ATTRIBUTE.

    This is the only file in the suite that pops a package module, and it is
    placed here rather than at the top because these nodes must run AFTER the
    two that do it. What it catches is not hypothetical: the pre-push lane went
    red on ``tests/test_python_exec_timeout.py`` with ``module
    'stealth_chrome_devtools_mcp' has no attribute 'embedded'``, from a
    ``monkeypatch.setattr`` on a dotted string, because this file had left a
    fresh package object in ``sys.modules`` that named none of its submodules.

    A pin here covers this file and every file sorted before it, which is where
    the mechanism lives -- the cost of covering the whole session would be a
    per-test teardown hook, and no other file in the tree pops or reloads a
    real package module (``test_element_cloner_output_dir`` and
    ``test_tool_module_reload`` both restore what they take).
    """

    def test_the_named_subpackages_resolve_by_attribute(self):
        """The exact walk pytest does for a dotted monkeypatch target.

        Each target is IMPORTED first and then walked by attribute, which is
        ``monkeypatch.setattr``'s own two steps. Importing is what makes the
        walk mean something: a subpackage nothing has loaded yet is absent for
        an innocent reason, and asserting on it would fail wherever this file
        runs alone.
        """
        for dotted in ("embedded", "embedded.tool_sections"):
            importlib.import_module(f"{PACKAGE}.{dotted}")
            walked = importlib.import_module(PACKAGE)
            for part in dotted.split("."):
                walked = getattr(walked, part, None)
                assert walked is not None, (
                    f"{PACKAGE}.{dotted} is imported but its parent no longer "
                    "names it -- something popped a package module and left "
                    "its submodules cached"
                )

    def test_every_cached_submodule_is_still_named_by_its_parent(self):
        """The general invariant, not just the two spellings above."""
        orphans = []
        for name, module in sorted(_cached_package_modules().items()):
            parent_name, _, leaf = name.rpartition(".")
            if not parent_name:
                continue
            parent = sys.modules.get(parent_name)
            if parent is not None and getattr(parent, leaf, None) is not module:
                orphans.append(name)
        assert not orphans, (
            "cached submodules their own parent no longer names: "
            f"{orphans}. Popping a package from sys.modules while its "
            "submodules stay cached is what does this; restore the mapping "
            "instead (see _pristine_package_modules)."
        )

    def test_a_dotted_monkeypatch_target_still_resolves(self, monkeypatch):
        """The failing operation itself, on the module that reported it."""
        monkeypatch.setattr(
            "stealth_chrome_devtools_mcp.embedded.singleton.DEFAULT_PORT",
            19999,
        )


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
