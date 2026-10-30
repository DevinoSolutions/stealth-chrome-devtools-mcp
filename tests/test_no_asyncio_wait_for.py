"""T-10690: no module under ``src/`` bounds an await with ``asyncio.wait_for``.

On CPython 3.11 (the package supports ``>=3.11``) ``asyncio.wait_for`` hands back
the inner result when the outer task is cancelled AFTER the inner future finished
but BEFORE the waiter resumed: the cancellation is swallowed and the caller goes
on. A ``click_element`` cancelled at that instant (a shutdown) armed its probe or
sent input as if nothing had happened. 3.12+ built ``wait_for`` on
``asyncio.timeout`` and re-raises, so only 3.11 shows it. Measured with a
standalone probe on 3.11.14: ``wait_for`` ends not cancelled, ``asyncio.timeout``
ends cancelled.

The one form is ``async with asyncio.timeout(seconds): await ...`` (T-10284 took
``page_natives._release_bounded`` there first). A second way to bound an await
is the defect, so this scans the source rather than trusting a convention.
"""

from __future__ import annotations

import ast
from pathlib import Path

import stealth_chrome_devtools_mcp as package

PACKAGE = Path(package.__file__).parent

#: Fewer modules than this means the scan is looking at the wrong tree, and a
#: scan of the wrong tree finds nothing and passes.
MIN_MODULES = 100

#: The bounded awaits the package has today. A scan that finds no
#: ``asyncio.timeout`` has not been looking at the code that was converted.
MIN_TIMEOUT_SITES = 40

WHY = (
    "T-10690: on Python 3.11 asyncio.wait_for returns the inner result when the "
    "outer task is cancelled after the inner future finished, so the "
    "cancellation is lost; bound an await with `async with asyncio.timeout(...)`"
)


def _reaches_wait_for(node: ast.AST, modules: set[str], functions: set[str]) -> bool:
    if isinstance(node, ast.Attribute):
        owner = ast.unparse(node.value)
        return node.attr == "wait_for" and (
            owner in modules or owner == "asyncio.tasks"
        )
    if isinstance(node, ast.Name):
        return node.id in functions
    return (
        isinstance(node, ast.Call)
        and ast.unparse(node.func) == "getattr"
        and len(node.args) >= 2
        and ast.unparse(node.args[0]) in modules
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value == "wait_for"
    )


def wait_for_uses(source: str) -> list[int]:
    """Lines that reach ``asyncio.wait_for`` by any spelling: ``asyncio.wait_for``
    (called or only referenced), the module under another name, ``wait_for``
    imported from it, and ``asyncio.tasks.wait_for``."""
    tree = ast.parse(source)
    modules: set[str] = set()
    functions: set[str] = set()
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {a.asname or a.name for a in node.names if a.name == "asyncio"}
        elif isinstance(node, ast.ImportFrom) and node.module in (
            "asyncio",
            "asyncio.tasks",
        ):
            for alias in node.names:
                if alias.name == "wait_for":
                    functions.add(alias.asname or alias.name)
                    lines.append(node.lineno)
    lines += [
        node.lineno
        for node in ast.walk(tree)
        if _reaches_wait_for(node, modules, functions)
    ]
    return sorted(set(lines))


def timeout_sites(source: str) -> int:
    """How many ``async with asyncio.timeout(...)`` the source holds."""
    return sum(
        isinstance(item.context_expr, ast.Call)
        and ast.unparse(item.context_expr.func) == "asyncio.timeout"
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.AsyncWith)
        for item in node.items
    )


def modules() -> list[Path]:
    return sorted(PACKAGE.rglob("*.py"))


class TestTheScannerSeesEverySpelling:
    """A census that cannot see a spelling passes over it."""

    def test_the_call(self):
        assert wait_for_uses("import asyncio\nasyncio.wait_for(x, 1)\n") == [2]

    def test_a_reference_that_is_not_a_call(self):
        assert wait_for_uses("import asyncio\nf = asyncio.wait_for\n") == [2]

    def test_the_module_under_another_name(self):
        assert wait_for_uses("import asyncio as aio\naio.wait_for(x, 1)\n") == [2]

    def test_the_function_imported(self):
        source = "from asyncio import wait_for as bound\nbound(x, 1)\n"
        assert wait_for_uses(source) == [1, 2]

    def test_the_function_imported_from_tasks(self):
        assert wait_for_uses("from asyncio.tasks import wait_for\n") == [1]

    def test_the_module_path_to_it(self):
        assert wait_for_uses("import asyncio\nasyncio.tasks.wait_for(x, 1)\n") == [2]

    def test_getattr_on_the_module(self):
        source = "import asyncio\ngetattr(asyncio, 'wait_for')(x, 1)\n"
        assert wait_for_uses(source) == [2]

    def test_nodrivers_own_wait_for_is_not_asyncios(self):
        """``tab.wait_for(...)`` is nodriver's element wait, a different thing."""
        assert wait_for_uses("import asyncio\nawait tab.wait_for(selector='a')\n") == []

    def test_a_timeout_block_is_counted(self):
        source = (
            "import asyncio\nasync def f():\n"
            "    async with asyncio.timeout(1):\n        await x\n"
        )
        assert timeout_sites(source) == 1


class TestNoModuleBoundsAnAwaitWithWaitFor:
    def test_the_scan_covers_the_package(self):
        assert len(modules()) >= MIN_MODULES, len(modules())

    def test_no_module_calls_asyncio_wait_for(self):
        offenders = [
            f"{path.relative_to(PACKAGE).as_posix()}:{line}"
            for path in modules()
            for line in wait_for_uses(path.read_text(encoding="utf-8"))
        ]
        assert offenders == [], f"{WHY}. Found at {offenders}"

    def test_the_bounded_awaits_are_asyncio_timeout_blocks(self):
        total = sum(
            timeout_sites(path.read_text(encoding="utf-8")) for path in modules()
        )
        assert total >= MIN_TIMEOUT_SITES, (total, WHY)
