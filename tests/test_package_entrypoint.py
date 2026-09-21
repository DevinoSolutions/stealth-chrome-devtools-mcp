"""THE one home for "what entering this package as a module does" (F-903).

Two doors and they must not behave alike. ``python -m
stealth_chrome_devtools_mcp`` runs ``__main__.py`` under the name ``__main__``
and MUST start the server; ``import stealth_chrome_devtools_mcp.__main__`` is a
plain import and must run NOTHING.

Until F-903 the file had no guard, so the two were the same door. That is not a
theoretical tidiness point: on 2026-09-21 this finding's own census probe walked
the package with ``pkgutil.walk_packages`` + ``importlib.import_module``, reached
``__main__``, and cold-started a real backend (pid 189088, port 64986) into the
operator's live ``~/.stealth-mcp``. Any tool that imports a package module by
module -- a doc generator, a coverage sweep, an import linter, an IDE -- is that
probe.

Nothing here starts a server. The two in-process nodes patch
``server.main`` to a tripwire BEFORE the module body can reach it, so the RED
state of the first node is a tripwire firing rather than a backend launching, and
the subprocess node takes the ``--transport http`` path to ``--help``, which
argparse answers and exits (see ``TestBareHelpFallsThroughToTheColdStart`` for
why the BARE spelling is not safe to run).
"""

from __future__ import annotations

import importlib
import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

from stealth_chrome_devtools_mcp import server as shim

_DUNDER_MAIN = "stealth_chrome_devtools_mcp.__main__"


class _Tripwire:
    """Records that it was called; raises so nothing downstream can proceed."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("main() ran")


@pytest.fixture()
def tripwired_main(monkeypatch):
    """``server.main`` replaced, and ``__main__`` evicted so it re-executes.

    The patch must land BEFORE the import: ``__main__.py`` binds ``main`` with a
    ``from`` import, which copies the object, so patching afterwards would reach
    a different binding than the module body called.
    """
    wire = _Tripwire()
    monkeypatch.setattr(shim, "main", wire)
    monkeypatch.delitem(sys.modules, _DUNDER_MAIN, raising=False)
    return wire


class TestImportingItRunsNothing:
    def test_importing_dunder_main_by_name_does_not_run_main(self, tripwired_main):
        """The RED half of F-903's product fix.

        Without the ``if __name__`` guard the module body calls ``main()`` at
        import, the tripwire fires, and the ``import_module`` below raises.
        """
        importlib.import_module(_DUNDER_MAIN)

        assert tripwired_main.calls == 0, (
            "importing the package's __main__ ran main() -- "
            "this is how a census probe cold-started a real backend"
        )

    def test_the_guard_is_the_reason_and_not_a_coincidence(self):
        """Pins the mechanism, so a future edit cannot lose it silently."""
        import stealth_chrome_devtools_mcp as pkg

        source = (Path(pkg.__file__).parent / "__main__.py").read_text(encoding="utf-8")
        assert 'if __name__ == "__main__":' in source


class TestNoModuleBodyDoesWork:
    """The whole-package rule that replaces F-903's ``_NEVER_IMPORT`` deny-list.

    A deny-list protects against the module someone remembered. What actually
    makes ``operator_fence.derived_globals()`` -- and every doc generator, import
    linter and coverage sweep -- safe is the stronger property: importing any
    module in this package does nothing but define things. So this walks the
    package with AST and refuses a bare module-level CALL, which is the shape
    ``main()`` had.

    There is exactly ONE allowed call and it is named with its reason rather than
    pattern-matched, so adding a second is a decision someone writes down.
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
        import ast

        import stealth_chrome_devtools_mcp as pkg

        root = Path(pkg.__file__).parent
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
            "(F-903). Move it behind a function or an `if __name__` guard:\n"
            + "\n".join(offenders)
        )

    def test_the_allowance_still_describes_something_real(self):
        """An allow-list entry for code that moved is an allowance for nothing."""
        import stealth_chrome_devtools_mcp as pkg

        root = Path(pkg.__file__).parent
        for rel, call in self.ALLOWED:
            assert call in (root / rel).read_text(encoding="utf-8"), (
                f"{rel} no longer contains {call} -- drop the allowance"
            )


class TestRunningItAsAModuleStillWorks:
    def test_run_module_under_the_dunder_main_name_calls_main(self, tripwired_main):
        """``python -m pkg`` IS ``runpy`` with ``run_name="__main__"``.

        So this is the ``-m`` contract measured in-process: the guard must let
        ``main()`` through on exactly the path the console user takes. The
        tripwire raises, which is what keeps this node from starting anything.
        """
        with pytest.raises(AssertionError, match="main\\(\\) ran"):
            runpy.run_module("stealth_chrome_devtools_mcp", run_name="__main__")

        assert tripwired_main.calls == 1

    def test_python_dash_m_help_exits_zero(self, tmp_path):
        """End to end, in a real child, with the operator's HOME redirected.

        ``--transport http`` is what makes this safe: it routes ``main()`` past
        the stdio-proxy branch into the ``runpy`` load of ``embedded/server.py``,
        whose own parser HAS ``--help`` and answers it before anything binds a
        port or spawns a browser.
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

        done = subprocess.run(  # noqa: S603  PERMANENT(F-903: our own interpreter, fixed argv)
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


class TestBareHelpFallsThroughToTheColdStart:
    """A MEASURED residual, pinned so it is not rediscovered the hard way.

    ``server.main`` builds its parser with ``add_help=False`` and reads it with
    ``parse_known_args``, so ``--help`` is an UNKNOWN argument to it. With the
    default ``--transport stdio`` that means a bare ``python -m
    stealth_chrome_devtools_mcp --help`` does not print help and does not exit --
    it enters the stdio-proxy branch and calls ``ensure_server_running``, which
    cold-starts a backend. Asking for help starts a server.

    This node does not fix that; it records it, and it proves it WITHOUT starting
    anything by tripwiring the cold start itself. Deleting the pin when the
    behaviour is fixed is the point.
    """

    def test_bare_help_reaches_ensure_server_running(self, monkeypatch):
        from stealth_chrome_devtools_mcp.embedded import singleton

        reached = _Tripwire()
        monkeypatch.setattr(sys, "argv", ["stealth-chrome-devtools-mcp", "--help"])
        monkeypatch.setattr(singleton, "ensure_server_running", reached)
        monkeypatch.setattr(
            runpy,
            "run_path",
            lambda *a, **k: pytest.fail("the runpy branch must not be reached"),
        )
        monkeypatch.setattr(
            shim,
            "_start_proxy_error_reporting",
            lambda: None,
        )

        with pytest.raises(AssertionError, match="main\\(\\) ran"):
            shim.main()

        assert reached.calls == 1
