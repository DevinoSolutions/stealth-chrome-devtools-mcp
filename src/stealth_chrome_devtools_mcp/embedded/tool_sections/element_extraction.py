"""The ``element-extraction`` tools. See ``tool_sections/__init__.py`` for the
contract.

plan_SERVERSPLIT slice 8, and the second STRAY relocation of the plan (after
``validate_browser_environment_tool`` in slice 3). ``extract_complete_element_cdp``
was physically filed among the ``file-extraction`` bodies in ``server.py`` —
wedged between ``extract_complete_element_to_file`` and
``extract_element_styles_to_file`` — while registering into ``element-extraction``
all along. It lands here, in its own section's module and in its own section's
surface order (last), which is what keeps module <-> section a clean 1:1.

This is the inline half of the cloner surface: every body hands a tab to the
canonical engine, ``cdp_element_cloner``. The to-file twins moved in slice 7 and
the progressive slices in slice 5, so all three transports now read from one
engine through three thin modules — the shape DESIGN §5 describes.

Three bodies return through ``response_handler.handle_response``, which is
SYNCHRONOUS: awaiting its dict return raises ``TypeError`` and silently broke
these very tools once (F-202). The comment that says so moves with each call
site, and ``tests/test_server_call_conventions.py`` re-derives its AST scan from
``tests/source_scan.py``, so the guard follows the bodies into this file rather
than passing vacuously over an emptier ``server.py``.

Three goldens sit under this section's subsystem —
``tests/goldens/extract_element_styles.json``, ``cdp_complete_element.json`` and
``canonical_engine.json`` — and none MOVES or CHANGES here:
``tests/test_cloner_schemas.py`` and ``tests/test_cdp_element_cloner.py`` drive
``cdp_element_cloner`` DIRECTLY, one layer below the tool bodies, so a tool-body
move physically cannot reach them. They are re-run as part of the slice to show
that rather than assert it.

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

SECTION = "element-extraction"


async def extract_element_styles(
    instance_id: str,
    selector: str,
    include_computed: bool = True,
    include_css_rules: bool = True,
    include_pseudo: bool = True,
    include_inheritance: bool = False,
) -> dict[str, Any]:
    """
    Extract complete styling information from an element.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector for the element.
        include_computed (bool): Include computed styles.
        include_css_rules (bool): Include matching CSS rules.
        include_pseudo (bool): Include pseudo-element styles (::before, ::after).
        include_inheritance (bool): Include style inheritance chain.

    Returns:
        Dict[str, Any]: Complete styling data including computed styles, CSS rules, pseudo-elements.
    """
    tab = await _require_tab(rt.browser_manager, instance_id)
    return await rt._with_cdp_timeout(
        rt.cdp_element_cloner.extract_element_styles(
            tab,
            selector=selector,
            include_computed=include_computed,
            include_css_rules=include_css_rules,
            include_pseudo=include_pseudo,
            include_inheritance=include_inheritance,
        ),
        instance_id=instance_id,
    )


async def extract_element_structure(
    instance_id: str,
    selector: str,
    include_children: bool = False,
    include_attributes: bool = True,
    include_data_attributes: bool = True,
    max_depth: int = 3,
) -> dict[str, Any]:
    """
    Extract complete HTML structure and DOM information.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector for the element.
        include_children (bool): Include child elements.
        include_attributes (bool): Include all attributes.
        include_data_attributes (bool): Include data-* attributes specifically.
        max_depth (int): Maximum depth for children extraction.

    Returns:
        Dict[str, Any]: HTML structure, attributes, position, and children data.
    """
    tab = await _require_tab(rt.browser_manager, instance_id)
    return await rt._with_cdp_timeout(
        rt.cdp_element_cloner.extract_element_structure(
            tab,
            selector=selector,
            include_children=include_children,
            include_attributes=include_attributes,
            include_data_attributes=include_data_attributes,
            max_depth=max_depth,
        ),
        instance_id=instance_id,
    )


async def extract_element_events(
    instance_id: str,
    selector: str,
    include_inline: bool = True,
    include_listeners: bool = True,
    include_framework: bool = True,
    analyze_handlers: bool = False,
) -> dict[str, Any]:
    """
    Extract complete event listener and JavaScript handler information.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector for the element.
        include_inline (bool): Include inline event handlers (onclick, etc.).
        include_listeners (bool): Include addEventListener attached handlers.
        include_framework (bool): Include framework-specific handlers (React, Vue, etc.).
        analyze_handlers (bool): Analyze handler functions for full details (can be large).

    Returns:
        Dict[str, Any]: Event listeners, inline handlers, framework handlers, detected frameworks.
    """
    tab = await _require_tab(rt.browser_manager, instance_id)
    return await rt._with_cdp_timeout(
        rt.cdp_element_cloner.extract_element_events(
            tab,
            selector=selector,
            include_inline=include_inline,
            include_listeners=include_listeners,
            include_framework=include_framework,
            analyze_handlers=analyze_handlers,
        ),
        instance_id=instance_id,
    )


async def extract_element_animations(
    instance_id: str,
    selector: str,
    include_subtree: bool = True,
    include_waapi: bool = True,
) -> dict[str, Any]:
    """
    Extract everything needed to retime or restyle an element's motion (schema v2).

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector for the element.
        include_subtree (bool): Include descendant and pseudo-element animations.
        include_waapi (bool): Include live getAnimations() records (element.animate).

    Returns:
        Dict[str, Any]: `schema_version: 2`, `has_motion`, `overview` prose,
        `animations[]`, `transitions[]`, `interactions[]` (conflicts + remedies),
        `sources[]` (href, rule path, author text), `transforms`, `warnings[]`.
        Per animation: `summary`, `kind`, `target`, `trigger`, `semantics` (each
        a `{value, confidence}` claim), `timeline` (time|scroll|view — duration
        edits are IGNORED on scroll/view), `timing` (`*_ms` numbers beside
        `*_raw` tokens; `iterations` may be "infinite"), `derived` (cycle_ms,
        active window, total_ms, stagger_group), `keyframes[]` (numeric `offset`
        + parsed properties), `checkpoints[]` (declared values, never
        interpolated), `edits[]` (per knob: the author's declaration as `find`,
        the one `token` in it this knob owns, and `replace` — that declaration
        with only the token swapped for `edit_protocol.placeholder`; no `find`
        means a pointer, not an edit). Capped at 25 animations / 20 keyframes;
        subtree records carry `detail_level: "summary"`. Undecidable: omitted.
    """
    tab = await _require_tab(rt.browser_manager, instance_id)
    return await rt._with_cdp_timeout(
        rt.cdp_element_cloner.extract_element_animations(
            tab,
            selector=selector,
            include_subtree=include_subtree,
            include_waapi=include_waapi,
        ),
        instance_id=instance_id,
    )


async def extract_element_assets(
    instance_id: str,
    selector: str,
    include_images: bool = True,
    include_backgrounds: bool = True,
    include_fonts: bool = True,
    fetch_external: bool = False,
) -> dict[str, Any]:
    """
    Extract all assets related to an element (images, fonts, etc.).

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector for the element.
        include_images (bool): Include img src and related images.
        include_backgrounds (bool): Include background images.
        include_fonts (bool): Include font information.
        fetch_external (bool): Whether to fetch external assets for analysis.

    Returns:
        Dict[str, Any]: Images, background images, fonts, icons, videos, audio assets.
    """
    tab = await _require_tab(rt.browser_manager, instance_id)
    result = await rt._with_cdp_timeout(
        rt.cdp_element_cloner.extract_element_assets(
            tab,
            selector=selector,
            include_images=include_images,
            include_backgrounds=include_backgrounds,
            include_fonts=include_fonts,
            fetch_external=fetch_external,
        ),
        instance_id=instance_id,
    )
    # handle_response is synchronous — awaiting its dict return raises TypeError.
    return rt.response_handler.handle_response(
        result, f"element_assets_{instance_id}_{selector.replace(' ', '_')}"
    )


async def extract_element_styles_cdp(
    instance_id: str,
    selector: str,
    include_computed: bool = True,
    include_css_rules: bool = True,
    include_pseudo: bool = True,
    include_inheritance: bool = False,
) -> dict[str, Any]:
    """
    Extract element styles using direct CDP calls (no JavaScript evaluation).
    This prevents hanging issues by using nodriver's native CDP methods.

    Args:
        instance_id (str): Browser instance ID
        selector (str): CSS selector for the element
        include_computed (bool): Include computed styles
        include_css_rules (bool): Include matching CSS rules
        include_pseudo (bool): Include pseudo-element styles
        include_inheritance (bool): Include style inheritance chain

    Returns:
        Dict[str, Any]: Styling data extracted using CDP
    """
    tab = await _require_tab(rt.browser_manager, instance_id)
    return await rt._with_cdp_timeout(
        rt.cdp_element_cloner.extract_element_styles(
            tab,
            selector=selector,
            include_computed=include_computed,
            include_css_rules=include_css_rules,
            include_pseudo=include_pseudo,
            include_inheritance=include_inheritance,
        ),
        instance_id=instance_id,
    )


async def extract_related_files(
    instance_id: str,
    analyze_css: bool = True,
    analyze_js: bool = True,
    follow_imports: bool = False,
    max_depth: int = 2,
) -> dict[str, Any]:
    """
    Discover and analyze related CSS/JS files for context.

    Args:
        instance_id (str): Browser instance ID.
        analyze_css (bool): Analyze linked CSS files.
        analyze_js (bool): Analyze linked JS files.
        follow_imports (bool): Follow @import and module imports (uses network).
        max_depth (int): Maximum depth for following imports.

    Returns:
        Dict[str, Any]: Stylesheets, scripts, imports, modules, framework detection.
    """
    tab = await _require_tab(rt.browser_manager, instance_id)
    result = await rt._with_cdp_timeout(
        rt.cdp_element_cloner.extract_related_files(
            tab,
            analyze_css=analyze_css,
            analyze_js=analyze_js,
            follow_imports=follow_imports,
            max_depth=max_depth,
        ),
        instance_id=instance_id,
    )
    # handle_response is synchronous — awaiting its dict return raises TypeError.
    return rt.response_handler.handle_response(result, f"related_files_{instance_id}")


async def clone_element_complete(
    instance_id: str, selector: str, extraction_options: str | None = None
) -> dict[str, Any]:
    """
    Extract ALL element data inline as one canonical flat clone (the authoritative
    "complete" extractor).

    Composes the six aspects on the canonical engine — styles via direct CDP, and
    structure/events/animations/assets/related_files via JS-eval — into a flat,
    aspect-keyed dict with ``selector`` forwarded to every sub-extractor. Prefer
    this for complete fidelity when the payload can be returned inline. The three
    siblings differ only in delivery/transport: ``clone_element_to_file`` and
    ``extract_complete_element_to_file`` write this same clone to disk (path +
    summary) instead of returning it; ``extract_complete_element_cdp`` returns the
    pure-CDP *nested* variant (``element`` block) for CDP-native fidelity.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector for the element.
        extraction_options (Optional[str]): Dict specifying what to extract and options for each.
            Example: {
                'styles': {'include_computed': True, 'include_pseudo': True},
                'structure': {'include_children': True, 'max_depth': 2},
                'events': {'include_framework': True, 'analyze_handlers': False},
                'animations': {'analyze_keyframes': True},
                'assets': {'fetch_external': False},
                'related_files': {'follow_imports': True, 'max_depth': 1}
            }

    Returns:
        Dict[str, Any]: Complete element clone with styles, structure, events, animations, assets, related files.
    """
    parsed_options = None
    if extraction_options:
        try:
            parsed_options = json.loads(extraction_options)
        except json.JSONDecodeError:
            raise ToolError(f"Invalid JSON in extraction_options: {extraction_options}")
    tab = await _require_tab(rt.browser_manager, instance_id)
    result = await rt._with_cdp_timeout(
        rt.cdp_element_cloner.extract_complete_element(
            tab,
            selector=selector,
            extraction_options=parsed_options,
        ),
        instance_id=instance_id,
    )

    return rt.response_handler.handle_response(
        result,
        fallback_filename_prefix="complete_clone",
        metadata={
            "selector": selector,
            "extraction_options": parsed_options,
            "url": getattr(tab, "url", "unknown"),
        },
    )


async def extract_complete_element_cdp(
    instance_id: str, selector: str, include_children: bool = True
) -> dict[str, Any]:
    """
    Extract a complete element inline via native CDP for every aspect — the
    pure-CDP variant with a *nested* schema (data under an ``element`` block).

    Unlike ``clone_element_complete`` (canonical flat schema; styles CDP + the rest
    JS-eval), this bypasses JavaScript entirely and uses CDP directly for all
    aspects:
    - Computed styles via CSS.getComputedStyleForNode
    - Matched CSS rules via CSS.getMatchedStylesForNode
    - Event listeners via DOMDebugger.getEventListeners
    - DOM structure and attributes via DOM.describeNode

    Prefer this when you specifically need CDP-native fidelity (e.g. matched CSS
    rules, CDP event listeners) and can consume the nested shape; prefer
    ``clone_element_complete`` for the flat, per-aspect default.

    Args:
        instance_id (str): Browser instance ID.
        selector (str): CSS selector for the element.
        include_children (bool): Whether to include child elements.

    Returns:
        Dict[str, Any]: Complete element data with 100% accuracy.
    """
    tab = await _require_tab(rt.browser_manager, instance_id)
    return await rt._with_cdp_timeout(
        rt.cdp_element_cloner.extract_complete_element_cdp(
            tab, selector, include_children
        ),
        instance_id=instance_id,
    )


#: Surface order, which is the order ``server.py``'s binding loop registers them
#: in and therefore the order they appear in
#: ``SECTION_TOOLS["element-extraction"]``. ``extract_complete_element_cdp``
#: stays LAST: that is where it registered from its misfiled position among the
#: file-extraction bodies, and the wire golden is order-insensitive only because
#: nothing here changes it.
TOOLS = (
    extract_element_styles,
    extract_element_structure,
    extract_element_events,
    extract_element_animations,
    extract_element_assets,
    extract_element_styles_cdp,
    extract_related_files,
    clone_element_complete,
    extract_complete_element_cdp,
)
