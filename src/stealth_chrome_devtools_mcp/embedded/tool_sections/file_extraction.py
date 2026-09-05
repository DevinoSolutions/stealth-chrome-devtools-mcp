"""The ``file-extraction`` tools. See ``tool_sections/__init__.py`` for the
contract.

plan_SERVERSPLIT slice 7. This is the to-file twin of ``element-extraction``:
seven bodies that hand a tab to ``file_based_element_cloner`` — the thin to-file
adapter that owns ``output_dir`` and nothing else — plus the two that inventory
and reap what those wrote (``list_clone_files`` / ``cleanup_clone_files``, the
only bodies here that need neither a tab nor the CDP timeout).

Two goldens sit under this section's subsystem —
``tests/goldens/file_based_structure_to_file.json`` and
``extract_element_structure_list_convert.json`` — and neither MOVES nor CHANGES
here: ``tests/test_cloner_schemas.py`` drives ``file_based_element_cloner``
DIRECTLY, one layer below the tool bodies, so a tool-body move physically cannot
reach them. They are re-run as part of the slice to show that rather than assert
it, and ``git diff -- tests/goldens/`` is empty in this commit.

The seven adapter-driving bodies were physically SPLIT in ``server.py``: two sat
above ``extract_complete_element_cdp`` and five below it, because that
element-extraction body was misfiled among them. Slice 8 relocates that stray to
its own section; here the nine file-extraction bodies simply close up, in the
surface order they registered in.

Bodies moved verbatim: the only edits are the dropped registration decorator
(contract rule 2 — registration is driven from ``server.py``'s binding loop, once
per execution of that module body) and the rewrite of the singleton/helper reads
to ``rt.<name>``, resolved at CALL time against the one patchable home (contract
rule 3). Docstrings and signatures are byte-identical — FastMCP surfaces them and
``tests/goldens/tool_surface.json`` is a HARD golden for this migration.
"""

import json
from typing import Any

from stealth_chrome_devtools_mcp.embedded import tool_runtime as rt
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError, _require_tab

SECTION = "file-extraction"


async def clone_element_to_file(
    instance_id: str, selector: str, extraction_options: str | None = None
) -> dict[str, Any]:
    """
    Save the complete canonical clone to a file — the to-file twin of
    ``clone_element_complete`` (same engine, same per-aspect transport, accepts the
    same ``extraction_options``).

    Returns ``{file_path, extraction_type, summary}`` instead of the full data, so
    a large clone never overwhelms the response. Use
    ``extract_complete_element_to_file`` for the lighter variant that takes only an
    ``include_children`` toggle (no per-aspect options).

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector for the element.
        extraction_options (Optional[str]): JSON string with extraction options.

    Returns:
        Dict[str, Any]: File path and summary information about the cloned element.
    """
    tab = await _require_tab(rt.browser_manager, instance_id)
    parsed_options = None
    if extraction_options:
        try:
            parsed_options = json.loads(extraction_options)
        except json.JSONDecodeError as exc:
            raise ToolError("Invalid extraction_options JSON") from exc
    return await rt._with_cdp_timeout(
        rt.file_based_element_cloner.clone_element_complete_to_file(
            tab, selector=selector, extraction_options=parsed_options
        ),
        instance_id=instance_id,
    )


async def extract_complete_element_to_file(
    instance_id: str, selector: str, include_children: bool = True
) -> dict[str, Any]:
    """
    Save a complete canonical clone to a file — the lightweight to-file variant.

    Same canonical engine as ``clone_element_complete`` (styles via CDP, the rest
    via JS-eval) but exposes only an ``include_children`` toggle rather than
    per-aspect ``extraction_options``. Returns ``{file_path, extraction_type,
    summary}``. Use ``clone_element_to_file`` when you need the full per-aspect
    option set.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector for the element.
        include_children (bool): Whether to include child elements.

    Returns:
        Dict[str, Any]: File path and concise summary instead of massive data dump.
    """
    tab = await _require_tab(rt.browser_manager, instance_id)
    return await rt._with_cdp_timeout(
        rt.file_based_element_cloner.extract_complete_element_to_file(
            tab, selector, include_children
        ),
        instance_id=instance_id,
    )


async def extract_element_styles_to_file(
    instance_id: str,
    selector: str,
    include_computed: bool = True,
    include_css_rules: bool = True,
    include_pseudo: bool = True,
    include_inheritance: bool = False,
) -> dict[str, Any]:
    """
    Extract element styles and save to file, returning file path.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector for the element.
        include_computed (bool): Include computed styles.
        include_css_rules (bool): Include matching CSS rules.
        include_pseudo (bool): Include pseudo-element styles.
        include_inheritance (bool): Include style inheritance chain.

    Returns:
        Dict[str, Any]: File path and summary of extracted styles.
    """
    tab = await _require_tab(rt.browser_manager, instance_id)
    return await rt._with_cdp_timeout(
        rt.file_based_element_cloner.extract_element_styles_to_file(
            tab,
            selector=selector,
            include_computed=include_computed,
            include_css_rules=include_css_rules,
            include_pseudo=include_pseudo,
            include_inheritance=include_inheritance,
        ),
        instance_id=instance_id,
    )


async def extract_element_structure_to_file(
    instance_id: str,
    selector: str,
    include_children: bool = False,
    include_attributes: bool = True,
    include_data_attributes: bool = True,
    max_depth: int = 3,
) -> dict[str, Any]:
    """
    Extract element structure and save to file, returning file path.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector for the element.
        include_children (bool): Include child elements.
        include_attributes (bool): Include all attributes.
        include_data_attributes (bool): Include data-* attributes.
        max_depth (int): Maximum depth for children extraction.

    Returns:
        Dict[str, Any]: File path and summary of extracted structure.
    """
    tab = await _require_tab(rt.browser_manager, instance_id)
    return await rt._with_cdp_timeout(
        rt.file_based_element_cloner.extract_element_structure_to_file(
            tab,
            selector=selector,
            include_children=include_children,
            include_attributes=include_attributes,
            include_data_attributes=include_data_attributes,
            max_depth=max_depth,
        ),
        instance_id=instance_id,
    )


async def extract_element_events_to_file(
    instance_id: str,
    selector: str,
    include_inline: bool = True,
    include_listeners: bool = True,
    include_framework: bool = True,
    analyze_handlers: bool = True,
) -> dict[str, Any]:
    """
    Extract element events and save to file, returning file path.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector for the element.
        include_inline (bool): Include inline event handlers.
        include_listeners (bool): Include addEventListener handlers.
        include_framework (bool): Include framework-specific handlers.
        analyze_handlers (bool): Analyze handler functions.

    Returns:
        Dict[str, Any]: File path and summary of extracted events.
    """
    tab = await _require_tab(rt.browser_manager, instance_id)
    return await rt._with_cdp_timeout(
        rt.file_based_element_cloner.extract_element_events_to_file(
            tab,
            selector=selector,
            include_inline=include_inline,
            include_listeners=include_listeners,
            include_framework=include_framework,
            analyze_handlers=analyze_handlers,
        ),
        instance_id=instance_id,
    )


async def extract_element_animations_to_file(
    instance_id: str,
    selector: str,
    include_subtree: bool = True,
    include_waapi: bool = True,
) -> dict[str, Any]:
    """
    Extract element animations (schema v2) to a file, returning the file path.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector for the element.
        include_subtree (bool): Include descendant and pseudo-element animations.
        include_waapi (bool): Include live getAnimations() records.

    Returns:
        Dict[str, Any]: File path plus a summary (has_motion and the animation,
        transition, keyframe and interaction counts). The file holds the full
        schema-v2 payload extract_element_animations() documents.
    """
    tab = await _require_tab(rt.browser_manager, instance_id)
    return await rt._with_cdp_timeout(
        rt.file_based_element_cloner.extract_element_animations_to_file(
            tab,
            selector=selector,
            include_subtree=include_subtree,
            include_waapi=include_waapi,
        ),
        instance_id=instance_id,
    )


async def extract_element_assets_to_file(
    instance_id: str,
    selector: str,
    include_images: bool = True,
    include_backgrounds: bool = True,
    include_fonts: bool = True,
    fetch_external: bool = False,
) -> dict[str, Any]:
    """
    Extract element assets and save to file, returning file path.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector for the element.
        include_images (bool): Include images.
        include_backgrounds (bool): Include background images.
        include_fonts (bool): Include font information.
        fetch_external (bool): Fetch external assets.

    Returns:
        Dict[str, Any]: File path and summary of extracted assets.
    """
    tab = await _require_tab(rt.browser_manager, instance_id)
    return await rt._with_cdp_timeout(
        rt.file_based_element_cloner.extract_element_assets_to_file(
            tab,
            selector=selector,
            include_images=include_images,
            include_backgrounds=include_backgrounds,
            include_fonts=include_fonts,
            fetch_external=fetch_external,
        ),
        instance_id=instance_id,
    )


async def list_clone_files() -> list[dict[str, Any]]:
    """
    List all element clone files saved to disk.

    Returns:
        List[Dict[str, Any]]: List of clone files with metadata and file information.
    """
    return rt.file_based_element_cloner.list_clone_files()


async def cleanup_clone_files(max_age_hours: int = 24) -> dict[str, int]:
    """
    Clean up old clone files to save disk space.

    Args:
        max_age_hours (int): Maximum age in hours for files to keep.

    Returns:
        Dict[str, int]: Number of files deleted.
    """
    deleted_count = rt.file_based_element_cloner.cleanup_old_files(max_age_hours)
    return {"deleted_count": deleted_count}


#: Surface order, which is the order ``server.py``'s binding loop registers them
#: in and therefore the order they appear in ``SECTION_TOOLS["file-extraction"]``.
TOOLS = (
    clone_element_to_file,
    extract_complete_element_to_file,
    extract_element_styles_to_file,
    extract_element_structure_to_file,
    extract_element_events_to_file,
    extract_element_animations_to_file,
    extract_element_assets_to_file,
    list_clone_files,
    cleanup_clone_files,
)
