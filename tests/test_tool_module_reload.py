"""The runpy/spec double-load must leave every loaded identity with a full
94-tool app, and must not accumulate in the shared ``SECTION_TOOLS`` map.

``embedded/server.py``'s module body can execute up to three times in ONE process,
under three different identities:

  1. ``stealth_chrome_devtools_mcp.embedded.server`` — the canonical import,
  2. ``server`` — the bare-name identity ``importlib.util.spec_from_file_location``
     creates (``tests/e2e_helpers.py``, ``tests/test_browser_integration.py``),
  3. ``__main__`` — ``stealth_chrome_devtools_mcp/server.py``'s ``runpy.run_path``.

Each execution builds its OWN ``mcp``/``registry``; ``SECTION_TOOLS`` lives in
``tool_registry`` and is SHARED. That asymmetry has two opposite failure modes,
both fatal, and only ONE of them is visible to the existing count tripwire:

  * 3x ACCUMULATION (the historical bug): ``SECTION_TOOLS`` grows to 282 == 3 x 94
    because the map is shared across executions. Cured by the idempotent append at
    ``tool_registry.ToolRegistry.section_tool``; this file keeps it cured.

  * 0x REGISTRATION (the hazard plan_SERVERSPLIT introduces): a section module
    decorates at ITS OWN module scope, so it registers into the FIRST execution's
    ``mcp`` and never runs again — a section module is imported once per process.
    ``SECTION_TOOLS`` still says 94, so ``tests/test_tool_registry.py``'s
    ``TestCountTripwire`` stays GREEN — but the second and third identities carry
    an EMPTY app, and ``tests/e2e_helpers.py``'s ``getattr`` lookups would skip the
    whole E2E tier into vacuous green. Only the PER-IDENTITY assertions below see
    it. This is why ``tool_sections/__init__.py`` contract rule 2 forbids a section
    module from decorating, and why registration is driven from ``server.py``'s
    binding loop instead.

RED CONDITIONS, both proved live by ``TestTheDetectorDetects`` at the bottom: the
four checks are factored into ``_assert_*`` helpers so the self-tests exercise the
SAME implementation the live checks do, rather than a restatement of it.

Measured in-place (plan_SERVERSPLIT slice 0), with ``__pycache__`` cleared between
mutation and revert so no stale bytecode could mask the result:

  * deleting the ``if func.__name__ not in names`` guard in
    ``tool_registry.ToolRegistry.section_tool`` (the non-idempotent append) turns
    ``test_section_tools_does_not_accumulate_across_loads`` RED with
    "282 tools in SECTION_TOOLS after three executions (3x registration)".
  * making ``server.py``'s binding loop skip on re-execution (an ``if
    not SECTION_TOOLS`` guard around it, i.e. the "decorate once" shape a section
    module would have) turns ``test_every_loaded_identity_owns_a_full_app`` RED
    while the count tripwire stays green — the 0x mode, exactly as described.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

from stealth_chrome_devtools_mcp.embedded import server as canonical
from stealth_chrome_devtools_mcp.embedded import tool_registry

#: Deliberately a literal, NOT derived from ``SECTION_TOOLS``. The whole point of
#: this file is that ``SECTION_TOOLS`` itself may be wrong (282, or right-but-lying
#: at 94 while two apps are empty), so an expectation derived from it would move
#: with the bug. ``tests/test_tool_registry.py::TestCountTripwire`` is the home for
#: the derived count; this is the independent witness.
EXPECTED_TOOL_COUNT = 94

_PROBE_ALIASES = ("server", "_split_reload_probe")


def _exec_copy(alias: str):
    """Execute ``embedded/server.py``'s body again under a fresh module identity —
    exactly what ``tests/e2e_helpers.py`` does to create the bare-name ``server``."""
    spec = importlib.util.spec_from_file_location(alias, Path(canonical.__file__))
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def three_identities():
    """The canonical import plus two more executions == the 3x load that made 282.

    Module-scoped: executing ``server.py``'s body builds a whole FastMCP app, so
    doing it once for the file is enough and keeps the lane fast. ``sys.modules``
    is restored so no later test inherits the probe identities.
    """
    saved = {a: sys.modules.get(a) for a in _PROBE_ALIASES}
    try:
        yield (canonical, _exec_copy(_PROBE_ALIASES[0]), _exec_copy(_PROBE_ALIASES[1]))
    finally:
        for alias, previous in saved.items():
            if previous is None:
                sys.modules.pop(alias, None)
            else:
                sys.modules[alias] = previous


# ---------------------------------------------------------------------------
# The four checks, factored so the self-tests below drive the real implementation
# ---------------------------------------------------------------------------


def _assert_no_accumulation(section_tools, expected=EXPECTED_TOOL_COUNT):
    total = sum(len(v) for v in section_tools.values())
    assert total == expected, (
        f"{total} tools in SECTION_TOOLS after three executions "
        f"({total / expected:.0f}x registration)"
    )
    for section, names in section_tools.items():
        assert len(names) == len(set(names)), f"duplicate names in {section}: {names}"


def _assert_full_app(identity, tool_count, expected=EXPECTED_TOOL_COUNT):
    assert tool_count == expected, (
        f"{identity}'s FastMCP app has {tool_count} tools, not {expected}. A "
        "section module that decorates at its own module scope registers into "
        "the first execution only, leaving every later load with an empty app."
    )


def _assert_names_are_attributes(identity, module, expected_names):
    missing = sorted(n for n in expected_names if not hasattr(module, n))
    assert not missing, (
        f"{identity} is missing {missing} — tests/fakes.py's call_tool and "
        "tests/e2e_helpers.py's get_fn both reach a tool by getattr on this "
        "module, so an unbound name is invisible to the hermetic and E2E tiers."
    )


def _assert_apps_are_independent(apps):
    assert len({id(a) for a in apps}) == len(apps), (
        "each execution must build its own FastMCP app; a shared one means the "
        "runpy __main__ load is serving another identity's registrations"
    )


# ---------------------------------------------------------------------------
# The live checks
# ---------------------------------------------------------------------------


def test_section_tools_does_not_accumulate_across_loads(three_identities):
    _assert_no_accumulation(tool_registry.SECTION_TOOLS)


async def test_every_loaded_identity_owns_a_full_app(three_identities):
    for module in three_identities:
        tools = await module.mcp.get_tools()
        _assert_full_app(module.__name__, len(tools))


def test_every_registered_name_is_an_attribute_of_every_identity(three_identities):
    expected = {n for names in tool_registry.SECTION_TOOLS.values() for n in names}
    assert len(expected) == EXPECTED_TOOL_COUNT
    for module in three_identities:
        _assert_names_are_attributes(module.__name__, module, expected)


def test_the_apps_are_independent(three_identities):
    _assert_apps_are_independent([m.mcp for m in three_identities])


def test_the_probe_identities_share_the_one_tool_runtime(three_identities):
    """The singletons must NOT be rebuilt per execution: ``tool_runtime`` is a
    normal module, imported once, so all three identities drive one
    ``BrowserManager`` (plan_SERVERSPLIT §7 R4) and ONE patch reaches all of them.
    """
    from stealth_chrome_devtools_mcp.embedded import tool_runtime

    for module in three_identities:
        assert module.browser_manager is tool_runtime.browser_manager
        assert module.network_interceptor is tool_runtime.network_interceptor


# ---------------------------------------------------------------------------
# The detector's own RED conditions — a guard that cannot fail guards nothing
# ---------------------------------------------------------------------------


class TestTheDetectorDetects:
    """Drive the four checks with the exact shapes the two failure modes produce.

    Without these, a refactor could neuter an assertion (a ``>=`` for an ``==``,
    a swallowed comparison) and every check above would keep passing.
    """

    def test_accumulation_shape_is_caught(self):
        """3x mode: the shape a non-idempotent append leaves behind."""
        accumulated = {"tabs": ["a", "a", "a"], "debugging": ["b", "b", "b"]}
        with pytest.raises(AssertionError, match="3x registration"):
            _assert_no_accumulation(accumulated, expected=2)

    def test_duplicate_names_are_caught_even_at_the_right_total(self):
        """A map that totals correctly can still carry a duplicate."""
        with pytest.raises(AssertionError, match="duplicate names"):
            _assert_no_accumulation({"tabs": ["a", "a"]}, expected=2)

    def test_zero_registration_shape_is_caught(self):
        """0x mode: the count map is fine, the second app is empty."""
        with pytest.raises(AssertionError, match="has 0 tools"):
            _assert_full_app("_split_reload_probe", 0)

    def test_an_unbound_tool_name_is_caught(self):
        """The getattr seam: registered but not bound onto the module."""

        class _Bare:
            pass

        with pytest.raises(AssertionError, match="spawn_browser"):
            _assert_names_are_attributes("probe", _Bare(), {"spawn_browser"})

    def test_a_shared_app_is_caught(self):
        """One FastMCP app handed to two identities."""
        shared = object()
        with pytest.raises(AssertionError, match="own FastMCP app"):
            _assert_apps_are_independent([shared, shared])
