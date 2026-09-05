"""plan_SERVERSPLIT slices 0 and 12 — the mechanism guards.

Four things this file pins, none of which any existing test covers:

  §5.3 SURFACE IDENTITY — every registered tool name is an attribute of the
       ``server`` module. ``tests/fakes.py``'s ``call_tool`` and
       ``tests/e2e_helpers.py``'s ``get_fn`` both reach a tool by ``getattr`` on
       that module, so a name that registers without binding there is invisible
       to the entire hermetic and E2E tiers — a silent, green-looking hole.
       Its slice-12 counterpart is the NEGATIVE half: the four constructed
       singletons must NOT be ``server`` attributes, because an import alias
       binds at import time and re-creates the second patchable home the
       migration spent twelve slices removing.

  §5.4 SECTION-MODULE CONTRACT — rules 2 and 3 of
       ``tool_sections/__init__.py``, enforced by AST. Rule 2 (never decorate)
       keeps the runpy ``__main__`` load from getting a zero-tool app; rule 3
       (``rt.<name>`` at call time, never a ``from ... import`` binding) keeps
       ``tool_runtime`` the ONE patchable home.

  §4.1/6 SOURCE-SCAN FLOOR — ``tests/source_scan.py``'s non-empty assertion, which
       is what converts "a source-text guard went vacuous" from a silent pass into
       a red test.

  §5.5 ALIAS-IDENTITY PIN — **gone.** While the tool bodies still lived in
       ``server.py`` and reached the singletons through an alias import, the two
       homes had to be the SAME OBJECT or ``conftest.patched_server``'s
       dual-patch would have papered over a divergence. Slice 12 deleted the
       alias block, the dual-patch and this pin together; what stands in its
       place is the one-home guard under §5.3, which asserts the second home
       cannot come back rather than that the two agree.

``SECTION_MODULES`` was empty in slice 0 — the mechanism shipped before any tool
moved — and fills one section per slice. A guard whose subject set can be empty is
exactly the vacuous pass this plan is most afraid of, so the checker is factored
into ``_check_section_module_source`` and driven here BOTH ways: against synthetic
compliant / violating modules (which is what proves the walk actually detects
something) and against whatever real modules have landed. The synthetic pair stays
after the last slice; it is what keeps the checker from rotting into a no-op.

One consequence of ``ast.dump`` worth knowing before you write a section module:
it renders string CONSTANTS too, so the rule-2 check sees docstrings. A module
whose docstring narrates its own dropped registration decorator by name fails the
guard. Say it in a ``#`` comment, which the AST does not carry, or paraphrase.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

from source_scan import (
    MIN_TOOL_SOURCE_FILES,
    collect_tool_source_files,
    tool_source_files,
)
from stealth_chrome_devtools_mcp.embedded import server, tool_runtime
from stealth_chrome_devtools_mcp.embedded.tool_registry import SECTION_TOOLS
from stealth_chrome_devtools_mcp.embedded.tool_sections import SECTION_MODULES

# ---------------------------------------------------------------------------
# §5.3 — the binding loop's contract
# ---------------------------------------------------------------------------


def test_every_registered_tool_is_a_server_attribute():
    """Registered and bound are two different things; both are required."""
    assert SECTION_TOOLS, "SECTION_TOOLS is empty — this guard would be vacuous"
    for section, names in SECTION_TOOLS.items():
        for name in names:
            bound = getattr(server, name, None)
            assert bound is not None, f"{section}/{name} is registered but not bound"
            assert hasattr(bound, "fn"), f"{name} is not a FastMCP tool object"


#: The four singletons ``tool_runtime`` CONSTRUCTS (as opposed to the module
#: objects it re-exports). Derived from nothing on purpose: these are the names
#: whose "one home" the guard below is about, and listing them here is what makes
#: the failure message say which one came back.
CONSTRUCTED_SINGLETONS = (
    "browser_manager",
    "network_interceptor",
    "dom_handler",
    "cdp_function_executor",
)


@pytest.mark.parametrize("name", CONSTRUCTED_SINGLETONS)
def test_a_constructed_singleton_has_exactly_one_home(name):
    """plan_SERVERSPLIT slice 12 — the closing guard.

    Through slices 0-11 ``server.py`` re-exported these as import aliases, so a
    fake handed to ``conftest.patched_server`` had to be written into two places
    at once, and the dual-patch's correctness rested on an identity pin. Slice 12
    deleted the alias block: ``server.py``'s own readers (``app_lifespan``, the
    four ``@mcp.resource`` handlers, the ``__main__`` block) resolve ``rt.<name>``
    at call time like every tool body does.

    This turns "there is one home" from a convention into a guard. An alias that
    comes back would not fail anything else — every test would keep passing while
    a ``setattr`` on ``tool_runtime`` silently stopped reaching whatever read the
    ``server`` copy. That is the exact shape of the divergence the migration's
    identity pin existed to catch, and this is what replaces it.
    """
    assert not hasattr(server, name), (
        f"server.{name} is back. The one patchable home is tool_runtime; an "
        "import alias here binds the object at import time and re-creates the "
        "second home plan_SERVERSPLIT slice 12 removed (read it as rt."
        f"{name} instead)."
    )
    assert hasattr(tool_runtime, name), f"tool_runtime.{name} went missing"


def test_the_binding_loop_is_driven_from_server_not_from_a_section_module():
    """``server.py`` must own the ``section_tool`` application.

    Source-level, because the property only shows up on a SECOND execution of
    ``server.py``'s body (``tests/test_tool_module_reload.py`` is where that is
    exercised end to end); here we pin the shape that makes it hold.
    """
    tree = ast.parse(Path(server.__file__).read_text(encoding="utf-8"))
    applied = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "section_tool"
    ]
    assert applied, (
        "server.py applies section_tool nowhere — either the decorators and the "
        "binding loop both vanished, or registration moved into a section module "
        "(tool_sections contract rule 2), which leaves the runpy __main__ load "
        "with a zero-tool app."
    )


# ---------------------------------------------------------------------------
# §5.2 — the wire-surface snapshot. A HARD golden for the whole migration.
# ---------------------------------------------------------------------------


async def test_the_served_tool_surface_matches_the_golden():
    """A pure move must not change one byte of what a client sees.

    ``tests/goldens/tool_surface.json`` records every served tool's description,
    input schema, output schema, tags and enabled flag. It is a **HARD** golden
    for the duration of plan_SERVERSPLIT (CONTRIBUTING.md golden discipline): a
    diff during any slice is a real regression, never something to regenerate.
    It is what catches a docstring lost in a copy-paste, an annotation whose
    meaning changed when a section module gained
    ``from __future__ import annotations``, or a tool that silently failed to
    register.

    Regenerate ONLY with an explicit surface change and a justification:
        PYTHONUTF8=1 python tools/dump_tool_surface.py --write
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    try:
        from dump_tool_surface import GOLDEN, _render, _surface
    finally:
        sys.path.pop(0)

    assert GOLDEN.exists(), "the wire-surface baseline is missing"
    actual = _render(await _surface())
    expected = GOLDEN.read_text(encoding="utf-8")
    if actual == expected:
        return
    a, b = json.loads(expected), json.loads(actual)
    added, removed = sorted(set(b) - set(a)), sorted(set(a) - set(b))
    changed = sorted(k for k in set(a) & set(b) if a[k] != b[k])
    raise AssertionError(
        f"served tool surface drifted from the HARD golden — "
        f"added={added} removed={removed} changed={changed}"
    )


# ---------------------------------------------------------------------------
# §5.4 — the section-module contract, one checker, driven both ways
# ---------------------------------------------------------------------------


def _check_section_module_source(name: str, source: str) -> None:
    """Raise ``AssertionError`` if *source* breaks contract rule 2 or 3."""
    tree = ast.parse(source)
    assert "section_tool" not in ast.dump(tree), (
        f"{name} decorates its own tools; a section module is imported ONCE per "
        "process, so it would register into the first server.py execution only"
    )
    assert "mcp" not in {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}, (
        f"{name} reaches for the FastMCP app"
    )
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.endswith("tool_runtime")
        ):
            raise AssertionError(
                f"{name} binds {[a.name for a in node.names]} from tool_runtime at "
                "import time; use `import tool_runtime as rt` and resolve "
                "`rt.<name>` at call time, or conftest's patched_server becomes a "
                "silent no-op for this module"
            )


_COMPLIANT = '''
"""The ``cookies-storage`` tools."""

from stealth_chrome_devtools_mcp.embedded import tool_runtime as rt
from stealth_chrome_devtools_mcp.embedded.tool_errors import _require_tab

SECTION = "cookies-storage"


async def get_cookies(instance_id: str):
    """Doc."""
    tab = await _require_tab(rt.browser_manager, instance_id)
    return await rt._with_cdp_timeout(rt.network_interceptor.get_cookies(tab))


TOOLS = (get_cookies,)
'''

_DECORATES = _COMPLIANT.replace(
    "async def get_cookies", '@section_tool("cookies-storage")\nasync def get_cookies'
)
_REACHES_FOR_MCP = _COMPLIANT + "\nmcp.tool(get_cookies)\n"
_IMPORT_BINDS = _COMPLIANT.replace(
    "from stealth_chrome_devtools_mcp.embedded import tool_runtime as rt",
    "from stealth_chrome_devtools_mcp.embedded.tool_runtime import browser_manager",
)


class TestTheContractCheckerDetects:
    """RED conditions for the AST checker, so it cannot rot into a no-op."""

    def test_a_compliant_module_passes(self):
        _check_section_module_source("compliant", _COMPLIANT)

    def test_a_module_that_decorates_is_caught(self):
        with pytest.raises(AssertionError, match="decorates its own tools"):
            _check_section_module_source("decorates", _DECORATES)

    def test_a_module_that_reaches_for_mcp_is_caught(self):
        with pytest.raises(AssertionError, match="reaches for the FastMCP app"):
            _check_section_module_source("mcp_user", _REACHES_FOR_MCP)

    def test_an_import_time_singleton_binding_is_caught(self):
        with pytest.raises(AssertionError, match="at import time"):
            _check_section_module_source("binder", _IMPORT_BINDS)


def test_every_section_module_obeys_the_contract():
    """Live from slice 1, over every section module that has landed."""
    for module in SECTION_MODULES:
        _check_section_module_source(
            module.__name__, Path(module.__file__).read_text(encoding="utf-8")
        )
        assert isinstance(module.SECTION, str) and module.TOOLS


def test_the_section_modules_cover_whole_sections_and_nothing_else():
    """Derived, never typed. A section is whole or it has not moved: a module
    that carried only SOME of its section's tools would leave the rest
    registered from ``server.py`` and the two halves patchable in two places."""
    sections = {m.SECTION for m in SECTION_MODULES}
    assert sections <= set(SECTION_TOOLS), f"unknown section(s): {sections}"
    for module in SECTION_MODULES:
        assert len(module.TOOLS) == len(SECTION_TOOLS[module.SECTION])


# ---------------------------------------------------------------------------
# §4.1 step 6 — the source-scan floor
# ---------------------------------------------------------------------------


def test_the_tool_source_set_is_server_plus_tool_runtime_plus_the_sections():
    files = tool_source_files()
    assert len(files) == 2 + len(SECTION_MODULES)
    assert len(files) >= MIN_TOOL_SOURCE_FILES
    names = {f.name for f in files}
    assert {"server.py", "tool_runtime.py"} <= names


def test_the_floor_fires_when_the_set_collapses():
    """RED condition for the floor itself.

    This models the state slice 11 must never reach: the floor has ratcheted up
    (here, to 3) but the section modules have been dropped from the derived set,
    so every source-text guard would scan only two files and pass over code that
    has already left them. Without this test, the floor is decoration.
    """
    with pytest.raises(AssertionError, match="collapsed to 2 file"):
        collect_tool_source_files(server, tool_runtime, (), floor=3)
