"""The ``progressive-cloning`` tools. See ``tool_sections/__init__.py`` for
the contract.

plan_SERVERSPLIT slice 5, the first golden-backed section. Two goldens sit under
this section's subsystem — ``tests/goldens/progressive_expand_styles.json`` and
``progressive_list_stored_elements.json`` — and neither MOVES or CHANGES here:
``tests/test_cloner_schemas.py`` drives ``progressive_element_cloner`` directly,
one layer below the tool bodies, so this pure move cannot touch them. They are
re-run as part of the slice precisely to prove that.

The section is one tool that CAPTURES (``clone_element_progressive``, the only
body here that needs a tab and the CDP timeout) plus nine that slice or clear the
already-cached extraction, so the adapter — not this module — remains the one
home for what a slice means.

Bodies moved verbatim: the only edits are the dropped registration decorator
(contract rule 2 — registration is driven from ``server.py``'s binding loop, once
per execution of that module body) and the rewrite of the singleton/helper reads
to ``rt.<name>``, resolved at CALL time against the one patchable home (contract
rule 3). Docstrings and signatures are byte-identical — FastMCP surfaces them and
``tests/goldens/tool_surface.json`` is a HARD golden for this migration.
``expand_children``'s two ``{"error": ...}`` returns are carried, not authored,
here: converting them is a behaviour change and plan_SERVERSPLIT §8 forbids one
inside a slice whose whole verification is a byte-identical surface.
"""

from typing import Any

from stealth_chrome_devtools_mcp.embedded import tool_runtime as rt
from stealth_chrome_devtools_mcp.embedded.tool_errors import _require_tab

SECTION = "progressive-cloning"


async def clone_element_progressive(
    instance_id: str, selector: str, include_children: bool = True
) -> dict[str, Any]:
    """
    Clone element progressively - returns lightweight base structure with element_id.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector for the element.
        include_children (bool): Whether to extract child elements.

    Returns:
        Dict[str, Any]: Base structure with element_id for progressive expansion.
    """
    tab = await _require_tab(rt.browser_manager, instance_id)
    return await rt._with_cdp_timeout(
        rt.progressive_element_cloner.clone_element_progressive(
            tab, selector, include_children
        ),
        instance_id=instance_id,
    )


async def expand_styles(
    element_id: str,
    categories: list[str] | None = None,
    properties: list[str] | None = None,
) -> dict[str, Any]:
    """
    Expand styles data for a stored element.

    Args:
        element_id (str): Element ID from clone_element_progressive().
        categories (Optional[List[str]]): Style categories to include (layout, typography, colors, spacing, borders, backgrounds, effects, animation).
        properties (Optional[List[str]]): Specific CSS property names to include.

    Returns:
        Dict[str, Any]: Filtered styles data.
    """
    return rt.progressive_element_cloner.expand_styles(
        element_id, categories, properties
    )


async def expand_events(
    element_id: str, event_types: list[str] | None = None
) -> dict[str, Any]:
    """
    Expand event listeners data for a stored element.

    Args:
        element_id (str): Element ID from clone_element_progressive().
        event_types (Optional[List[str]]): Event types or sources to include (click, react, inline, addEventListener).

    Returns:
        Dict[str, Any]: Filtered event listeners data.
    """
    return rt.progressive_element_cloner.expand_events(element_id, event_types)


async def expand_children(
    element_id: str, depth_range: list | None = None, max_count: Any | None = None
) -> dict[str, Any]:
    """
    Expand children data for a stored element.

    Args:
        element_id (str): Element ID from clone_element_progressive().
        depth_range (Optional[List]): [min_depth, max_depth] range to include.
        max_count (Optional[Any]): Maximum number of children to return.

    Returns:
        Dict[str, Any]: Filtered children data.
    """
    if isinstance(max_count, str):
        try:
            max_count = int(max_count) if max_count else None
        except ValueError:
            return {"error": f"Invalid max_count value: {max_count}"}

    if isinstance(depth_range, list):
        try:
            depth_range = [int(x) if isinstance(x, str) else x for x in depth_range]
        except ValueError:
            return {"error": f"Invalid depth_range values: {depth_range}"}

    depth_tuple = tuple(depth_range) if depth_range else None

    result = rt.progressive_element_cloner.expand_children(
        element_id, depth_tuple, max_count
    )
    return rt.response_handler.handle_response(result, f"expand_children_{element_id}")


async def expand_css_rules(
    element_id: str, source_types: list[str] | None = None
) -> dict[str, Any]:
    """
    Expand CSS rules data for a stored element.

    Args:
        element_id (str): Element ID from clone_element_progressive().
        source_types (Optional[List[str]]): CSS rule sources to include (inline, external stylesheet URLs).

    Returns:
        Dict[str, Any]: Filtered CSS rules data.
    """
    return rt.progressive_element_cloner.expand_css_rules(element_id, source_types)


async def expand_pseudo_elements(element_id: str) -> dict[str, Any]:
    """
    Expand pseudo-elements data for a stored element.

    Args:
        element_id (str): Element ID from clone_element_progressive().

    Returns:
        Dict[str, Any]: Pseudo-elements data (::before, ::after, etc.).
    """
    return rt.progressive_element_cloner.expand_pseudo_elements(element_id)


async def expand_animations(element_id: str) -> dict[str, Any]:
    """
    Expand the animations slice (schema v2) and fonts for a stored element.

    Args:
        element_id (str): Element ID from clone_element_progressive().

    Returns:
        Dict[str, Any]: `animations` — the same schema-v2 object
        extract_element_animations() documents — plus `fonts`.
    """
    return rt.progressive_element_cloner.expand_animations(element_id)


async def list_stored_elements() -> dict[str, Any]:
    """
    List all stored elements with their basic info.

    Returns:
        Dict[str, Any]: List of stored elements with metadata.
    """
    return rt.progressive_element_cloner.list_stored_elements()


async def clear_stored_element(element_id: str) -> dict[str, Any]:
    """
    Clear a specific stored element.

    Args:
        element_id (str): Element ID to clear.

    Returns:
        Dict[str, Any]: Success/error message.
    """
    return rt.progressive_element_cloner.clear_stored_element(element_id)


async def clear_all_elements() -> dict[str, Any]:
    """
    Clear all stored elements.

    Returns:
        Dict[str, Any]: Success message.
    """
    return rt.progressive_element_cloner.clear_all_elements()


#: Surface order, which is the order ``server.py``'s binding loop registers them
#: in and therefore the order they appear in
#: ``SECTION_TOOLS["progressive-cloning"]``.
TOOLS = (
    clone_element_progressive,
    expand_styles,
    expand_events,
    expand_children,
    expand_css_rules,
    expand_pseudo_elements,
    expand_animations,
    list_stored_elements,
    clear_stored_element,
    clear_all_elements,
)
