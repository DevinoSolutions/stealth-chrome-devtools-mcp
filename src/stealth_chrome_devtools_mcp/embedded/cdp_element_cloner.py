"""
Enhanced Element Cloner using proper CDP methods
=================================================

This module provides comprehensive element extraction using the full power of
Chrome DevTools Protocol (CDP) through nodriver. It extracts:

1. Complete computed styles using CDP CSS.getComputedStyleForNode
2. Matched CSS rules using CDP CSS.getMatchedStylesForNode
3. Event listeners using CDP DOMDebugger.getEventListeners
4. All stylesheet information via CDP CSS domain
5. Complete DOM structure and attributes

This provides 100% accurate element cloning by using CDP's native capabilities
instead of limited JavaScript-based extraction.
"""

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import nodriver as uc
import requests

from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.embedded.element_resolution import (
    query_selector_all,
    resolve_element,
)


class CDPElementCloner:
    """Enhanced element cloner using proper CDP methods for complete accuracy."""

    def __init__(self):
        """Initialize the CDP element cloner."""
        self.extracted_files = {}
        self.framework_patterns = {
            "react": [r"_react", r"__reactInternalInstance", r"__reactFiber"],
            "vue": [r"__vue__", r"_vnode", r"$el"],
            "angular": [r"ng-", r"__ngContext__", r"ɵ"],
            "jquery": [r"jQuery", r"\$\.", r"__jquery"],
        }

    async def extract_complete_element_cdp(
        self, tab, selector: str, include_children: bool = True
    ) -> dict[str, Any]:
        """
        Extract complete element data using proper CDP methods.

        Args:
            tab (Any): The nodriver tab object for CDP communication.
            selector (str): CSS selector for the target element.
            include_children (bool): Whether to include child elements.

        Returns:
            Dict[str, Any]: Extraction result containing element data, styles, event listeners, and stats.

        This provides 100% accurate element cloning by using CDP's native
        capabilities for CSS rules, event listeners, and style information.
        """
        try:
            debug_logger.log_info(
                "cdp_cloner",
                "extract_complete",
                f"Starting CDP extraction for {selector}",
            )
            await tab.send(uc.cdp.dom.enable())
            await tab.send(uc.cdp.css.enable())
            await tab.send(uc.cdp.runtime.enable())
            nodes = await query_selector_all(tab, selector)
            if not nodes:
                return {"error": f"Element not found: {selector}"}
            node_id = nodes[0]
            element_html = await self._get_element_html(tab, node_id)
            computed_styles = await self._get_computed_styles_cdp(tab, node_id)
            matched_styles = await self._get_matched_styles_cdp(tab, node_id)
            event_listeners = await self._get_event_listeners_cdp(tab, node_id)
            children = []
            if include_children:
                children = await self._get_children_cdp(tab, node_id)
            result = {
                "extraction_method": "CDP",
                "timestamp": datetime.now().isoformat(),
                "selector": selector,
                "url": tab.target.url,
                "element": {
                    "html": element_html,
                    "computed_styles": computed_styles,
                    "matched_styles": matched_styles,
                    "event_listeners": event_listeners,
                    "children": children,
                },
                "extraction_stats": {
                    "computed_styles_count": len(computed_styles),
                    "css_rules_count": len(matched_styles.get("matchedCSSRules", [])),
                    "event_listeners_count": len(event_listeners),
                    "children_count": len(children),
                },
            }
            debug_logger.log_info(
                "cdp_cloner",
                "extract_complete",
                "CDP extraction completed successfully",
            )
            return result
        except Exception as e:
            debug_logger.log_error(
                "cdp_cloner", "extract_complete", f"CDP extraction failed: {e!s}"
            )
            return {"error": f"CDP extraction failed: {e!s}"}

    async def _get_element_html(self, tab, node_id) -> dict[str, Any]:
        """
        Get element's HTML structure and attributes.

        Args:
            tab (Any): The nodriver tab object for CDP communication.
            node_id (Any): Node ID of the target element.

        Returns:
            Dict[str, Any]: Dictionary containing tag name, node info, outer HTML, and attributes.
        """
        try:
            node_details = await tab.send(uc.cdp.dom.describe_node(node_id=node_id))
            outer_html = await tab.send(uc.cdp.dom.get_outer_html(node_id=node_id))
            return {
                "tagName": node_details.tag_name,
                "nodeId": int(node_id),
                "nodeName": node_details.node_name,
                "localName": node_details.local_name,
                "nodeValue": node_details.node_value,
                "outerHTML": outer_html,
                "attributes": [
                    {
                        "name": node_details.attributes[i],
                        "value": node_details.attributes[i + 1],
                    }
                    for i in range(0, len(node_details.attributes or []), 2)
                ]
                if node_details.attributes
                else [],
            }
        except Exception as e:
            debug_logger.log_error("cdp_cloner", "_get_element_html", f"Failed: {e!s}")
            return {"error": str(e)}

    async def _get_computed_styles_cdp(self, tab, node_id) -> dict[str, str]:
        """
        Get complete computed styles using CDP CSS.getComputedStyleForNode.

        Args:
            tab (Any): The nodriver tab object for CDP communication.
            node_id (Any): Node ID of the target element.

        Returns:
            Dict[str, str]: Dictionary of computed style properties and their values.
        """
        try:
            computed_styles_list = await tab.send(
                uc.cdp.css.get_computed_style_for_node(node_id)
            )
            styles = {}
            for style_prop in computed_styles_list:
                styles[style_prop.name] = style_prop.value
            debug_logger.log_info(
                "cdp_cloner",
                "_get_computed_styles",
                f"Got {len(styles)} computed styles",
            )
            return styles
        except Exception as e:
            debug_logger.log_error(
                "cdp_cloner", "_get_computed_styles", f"Failed: {e!s}"
            )
            return {}

    async def _get_matched_styles_cdp(self, tab, node_id) -> dict[str, Any]:
        """
        Get matched CSS rules using CDP CSS.getMatchedStylesForNode.

        Args:
            tab (Any): The nodriver tab object for CDP communication.
            node_id (Any): Node ID of the target element.

        Returns:
            Dict[str, Any]: Dictionary containing inline style, attribute style, matched rules, pseudo elements, and inherited styles.
        """
        try:
            matched_result = await tab.send(
                uc.cdp.css.get_matched_styles_for_node(node_id)
            )
            (
                inline_style,
                attributes_style,
                matched_rules,
                pseudo_elements,
                inherited,
            ) = matched_result[:5]
            result = {
                "inlineStyle": self._css_style_to_dict(inline_style)
                if inline_style
                else None,
                "attributesStyle": self._css_style_to_dict(attributes_style)
                if attributes_style
                else None,
                "matchedCSSRules": [
                    self._rule_match_to_dict(rule) for rule in (matched_rules or [])
                ],
                "pseudoElements": [
                    self._pseudo_element_to_dict(pe) for pe in (pseudo_elements or [])
                ],
                "inherited": [
                    self._inherited_style_to_dict(inh) for inh in (inherited or [])
                ],
            }
            debug_logger.log_info(
                "cdp_cloner",
                "_get_matched_styles",
                f"Got {len(result['matchedCSSRules'])} CSS rules",
            )
            return result
        except Exception as e:
            debug_logger.log_error(
                "cdp_cloner", "_get_matched_styles", f"Failed: {e!s}"
            )
            return {}

    async def _get_event_listeners_cdp(self, tab, node_id) -> list[dict[str, Any]]:
        """
        Get event listeners using CDP DOMDebugger.getEventListeners.

        Args:
            tab (Any): The nodriver tab object for CDP communication.
            node_id (Any): Node ID of the target element.

        Returns:
            List[Dict[str, Any]]: List of dictionaries describing event listeners.
        """
        try:
            remote_object = await tab.send(uc.cdp.dom.resolve_node(node_id=node_id))
            if not remote_object or not remote_object.object_id:
                return []
            event_listeners = await tab.send(
                uc.cdp.dom_debugger.get_event_listeners(remote_object.object_id)
            )
            result = []
            for listener in event_listeners:
                result.append(
                    {
                        "type": listener.type_,
                        "useCapture": listener.use_capture,
                        "passive": listener.passive,
                        "once": listener.once,
                        "scriptId": str(listener.script_id),
                        "lineNumber": listener.line_number,
                        "columnNumber": listener.column_number,
                        "hasHandler": listener.handler is not None,
                        "hasOriginalHandler": listener.original_handler is not None,
                        "backendNodeId": int(listener.backend_node_id)
                        if listener.backend_node_id
                        else None,
                    }
                )
            debug_logger.log_info(
                "cdp_cloner",
                "_get_event_listeners",
                f"Got {len(result)} event listeners",
            )
            return result
        except Exception as e:
            debug_logger.log_error(
                "cdp_cloner", "_get_event_listeners", f"Failed: {e!s}"
            )
            return []

    async def _get_children_cdp(self, tab, node_id) -> list[dict[str, Any]]:
        """
        Get child elements using CDP.

        Args:
            tab (Any): The nodriver tab object for CDP communication.
            node_id (Any): Node ID of the parent element.

        Returns:
            List[Dict[str, Any]]: List of dictionaries containing child element HTML and computed styles.
        """
        try:
            await tab.send(uc.cdp.dom.request_child_nodes(node_id=node_id, depth=1))
            node_details = await tab.send(
                uc.cdp.dom.describe_node(node_id=node_id, depth=1)
            )
            children = []
            if node_details.children:
                for child in node_details.children:
                    if child.node_type == 1:
                        child_html = await self._get_element_html(tab, child.node_id)
                        child_computed = await self._get_computed_styles_cdp(
                            tab, child.node_id
                        )
                        children.append(
                            {
                                "html": child_html,
                                "computed_styles": child_computed,
                                "depth": 1,
                            }
                        )
            return children
        except Exception as e:
            debug_logger.log_error("cdp_cloner", "_get_children", f"Failed: {e!s}")
            return []

    def _css_style_to_dict(self, css_style) -> dict[str, Any]:
        """
        Convert CDP CSSStyle to dictionary.

        Args:
            css_style (Any): CDP CSSStyle object.

        Returns:
            Dict[str, Any]: Dictionary containing cssText and list of properties.
        """
        if not css_style:
            return {}
        return {
            "cssText": css_style.css_text_ or "",
            "properties": [
                {
                    "name": prop.name,
                    "value": prop.value,
                    "important": prop.important,
                    "implicit": prop.implicit,
                    "text": prop.text or "",
                    "parsedOk": prop.parsed_ok,
                    "disabled": prop.disabled,
                }
                for prop in css_style.css_properties_
            ],
        }

    def _rule_match_to_dict(self, rule_match) -> dict[str, Any]:
        """
        Convert CDP RuleMatch to dictionary.

        Args:
            rule_match (Any): CDP RuleMatch object.

        Returns:
            Dict[str, Any]: Dictionary describing the rule match.
        """
        return {
            "matchingSelectors": rule_match.matching_selectors,
            "rule": {
                "selectorText": rule_match.rule.selector_list.text
                if rule_match.rule.selector_list
                else "",
                "origin": str(rule_match.rule.origin),
                "style": self._css_style_to_dict(rule_match.rule.style),
                "styleSheetId": str(rule_match.rule.style_sheet_id_)
                if rule_match.rule.style_sheet_id_
                else None,
            },
        }

    def _pseudo_element_to_dict(self, pseudo_element) -> dict[str, Any]:
        """
        Convert CDP PseudoElementMatches to dictionary.

        Args:
            pseudo_element (Any): CDP PseudoElementMatches object.

        Returns:
            Dict[str, Any]: Dictionary describing the pseudo element matches.
        """
        return {
            "pseudoType": str(pseudo_element.pseudo_type),
            "pseudoIdentifier": pseudo_element.pseudo_identifier_,
            "matches": [
                self._rule_match_to_dict(match) for match in pseudo_element.matches_
            ],
        }

    def _inherited_style_to_dict(self, inherited_style) -> dict[str, Any]:
        """
        Convert CDP InheritedStyleEntry to dictionary.

        Args:
            inherited_style (Any): CDP InheritedStyleEntry object.

        Returns:
            Dict[str, Any]: Dictionary describing inherited styles.
        """
        return {
            "inlineStyle": self._css_style_to_dict(inherited_style.inline_style)
            if inherited_style.inline_style
            else None,
            "matchedCSSRules": [
                self._rule_match_to_dict(rule)
                for rule in inherited_style.matched_css_rules
            ],
        }

    # =====================================================================
    # M5b canonical extraction surface — the ONE home the 5 engines converge
    # onto. Transport per aspect (§2.1 + 2026-07-18 structure ruling): styles
    # via CDP; structure/events/animations/assets/related_files via JS-eval
    # (bounded by _with_cdp_timeout; zero capability loss). Every public aspect
    # method is a thin ``try/except -> {"error": ...}`` delegator.
    # =====================================================================

    async def _resolve_node_id(self, tab, element=None, selector: str | None = None):
        """Resolve a CDP nodeId from an element handle or a selector — the one
        impedance-match every CDP call needs (factored from the former
        ``ElementCloner.extract_element_styles_cdp``). Returns ``None`` when the
        element cannot be resolved."""
        if element is None and selector:
            element = await resolve_element(tab, selector)
        if not element:
            return None
        if hasattr(element, "node_id"):
            return element.node_id
        if hasattr(element, "backend_node_id"):
            node_info = await tab.send(
                uc.cdp.dom.describe_node(backend_node_id=element.backend_node_id)
            )
            return node_info.node.node_id
        return None

    def _encode_into(self, js_code: str, name: str, value: object) -> str:
        # Sub json.dumps(value) into "$NAME$"/'$NAME'/bare $NAME, eating any quotes.
        enc = json.dumps(value)
        return re.sub(rf"""(["']?)\${name}\$?\1""", lambda _: enc, js_code)

    def _load_js_file(self, filename: str, selector: str, options: dict) -> str:
        """Load and prepare a JavaScript file with template substitution."""
        js_dir = Path(__file__).parent / "js"
        js_file = js_dir / filename
        if not js_file.exists():
            raise FileNotFoundError(f"JavaScript file not found: {js_file}")

        with js_file.open(encoding="utf-8") as f:
            js_code = f.read()
        js_code = self._encode_into(js_code, "SELECTOR", selector)
        js_code = self._encode_into(js_code, "OPTIONS", options)
        for key, value in options.items():
            placeholder_key = f"${key.upper()}"
            placeholder_value = "true" if value else "false"
            js_code = js_code.replace(placeholder_key, placeholder_value)

        return js_code

    def _convert_nodriver_result(self, data):
        """Convert nodriver's array result format back to a dict."""
        if isinstance(data, list) and len(data) > 0 and isinstance(data[0], list):
            result = {}
            for item in data:
                if isinstance(item, list) and len(item) == 2:
                    key = item[0]
                    value_obj = item[1]
                    if isinstance(value_obj, dict) and "type" in value_obj:
                        if value_obj["type"] == "string":
                            result[key] = value_obj.get("value", "")
                        elif value_obj["type"] == "number":
                            result[key] = value_obj.get("value", 0)
                        elif value_obj["type"] == "null":
                            result[key] = None
                        elif value_obj["type"] == "array":
                            result[key] = value_obj.get("value", [])
                        elif value_obj["type"] == "object":
                            result[key] = self._convert_nodriver_result(
                                value_obj.get("value", [])
                            )
                        else:
                            result[key] = value_obj.get("value")
                    else:
                        result[key] = value_obj
            return result
        return data

    async def extract_element_styles(
        self,
        tab,
        element=None,
        selector: str | None = None,
        include_computed: bool = True,
        include_css_rules: bool = True,
        include_pseudo: bool = True,
        include_inheritance: bool = False,
    ) -> dict[str, Any]:
        """Extract complete styling via direct CDP calls (no JavaScript).

        The canonical CDP styles path (the ``method == "cdp_direct"`` schema),
        moved from ``ElementCloner`` so the two former CDP-styles copies live in
        one place. Behaviour-identical to the path it replaces.
        """
        try:
            await tab.send(uc.cdp.dom.enable())
            await tab.send(uc.cdp.css.enable())

            node_id = await self._resolve_node_id(tab, element, selector)
            if node_id is None:
                return {"error": "Element not found"}

            result = {"method": "cdp_direct"}

            if include_computed:
                computed_styles_list = await tab.send(
                    uc.cdp.css.get_computed_style_for_node(node_id)
                )
                result["computed_styles"] = {
                    prop.name: prop.value for prop in computed_styles_list
                }
            if include_css_rules or include_pseudo or include_inheritance:
                matched_styles = await tab.send(
                    uc.cdp.css.get_matched_styles_for_node(node_id)
                )
            if include_css_rules:
                result["css_rules"] = []
                if matched_styles[2]:
                    for rule_match in matched_styles[2]:
                        if rule_match.rule and rule_match.rule.style:
                            result["css_rules"].append(
                                {
                                    "selector": rule_match.rule.selector_list.text
                                    if rule_match.rule.selector_list
                                    else "unknown",
                                    "css_text": rule_match.rule.style.css_text or "",
                                    "source": rule_match.rule.origin.value
                                    if rule_match.rule.origin
                                    else "unknown",
                                }
                            )
                if matched_styles[0]:
                    result["inline_style"] = {
                        "css_text": matched_styles[0].css_text or "",
                        "properties": len(matched_styles[0].css_properties)
                        if matched_styles[0].css_properties
                        else 0,
                    }
                if matched_styles[1]:
                    result["attributes_style"] = {
                        "css_text": matched_styles[1].css_text or "",
                        "properties": len(matched_styles[1].css_properties)
                        if matched_styles[1].css_properties
                        else 0,
                    }

            if include_pseudo and len(matched_styles) > 3 and matched_styles[3]:
                result["pseudo_elements"] = {}
                for pseudo_match in matched_styles[3]:
                    if pseudo_match.pseudo_type:
                        result["pseudo_elements"][pseudo_match.pseudo_type.value] = {
                            "matches": len(pseudo_match.matches)
                            if pseudo_match.matches
                            else 0
                        }

            if include_inheritance and len(matched_styles) > 4 and matched_styles[4]:
                result["inheritance_chain"] = []
                for inherited_entry in matched_styles[4]:
                    if inherited_entry.inline_style:
                        result["inheritance_chain"].append(
                            {
                                "inline_css": inherited_entry.inline_style.css_text
                                or "",
                                "properties": len(
                                    inherited_entry.inline_style.css_properties
                                )
                                if inherited_entry.inline_style.css_properties
                                else 0,
                            }
                        )

            return result
        except Exception as e:
            debug_logger.log_error("cdp_cloner", "extract_styles", e)
            return {"error": f"CDP extraction failed: {e!s}"}

    async def extract_element_structure(
        self,
        tab,
        element=None,
        selector: str | None = None,
        include_children: bool = False,
        include_attributes: bool = True,
        include_data_attributes: bool = True,
        max_depth: int = 3,
    ) -> dict[str, Any]:
        """Extract HTML structure + DOM info via JS-eval (verbatim move from
        ``ElementCloner``; kept on JS per the 2026-07-18 ruling — zero loss)."""
        try:
            if not selector:
                return {"error": "Selector is required"}

            options = {
                "include_children": include_children,
                "include_attributes": include_attributes,
                "include_data_attributes": include_data_attributes,
                "max_depth": max_depth,
            }

            js_code = self._load_js_file("extract_structure.js", selector, options)
            structure_data = await tab.evaluate(js_code)

            if hasattr(structure_data, "exception_details"):
                return {
                    "error": f"JavaScript error: {structure_data.exception_details}"
                }
            if isinstance(structure_data, dict):
                return structure_data
            if isinstance(structure_data, list):
                return self._convert_nodriver_result(structure_data)
            return {
                "error": f"Unexpected return type: {type(structure_data)}",
                "raw_data": str(structure_data),
            }
        except Exception as e:
            debug_logger.log_error("cdp_cloner", "extract_structure", e)
            return {"error": str(e)}

    async def extract_element_events(
        self,
        tab,
        element=None,
        selector: str | None = None,
        include_inline: bool = True,
        include_listeners: bool = True,
        include_framework: bool = True,
        analyze_handlers: bool = True,
    ) -> dict[str, Any]:
        """Extract event listeners + JS handlers via JS-eval (verbatim move;
        KEEP-JS per gate Q1 — CDP cannot see inline/framework handlers)."""
        try:
            if not selector:
                return {"error": "Selector is required"}

            options = {
                "include_inline": include_inline,
                "include_listeners": include_listeners,
                "include_framework": include_framework,
                "analyze_handlers": analyze_handlers,
            }

            js_code = self._load_js_file("extract_events.js", selector, options)
            event_data = await tab.evaluate(js_code)

            if hasattr(event_data, "exception_details"):
                return {"error": f"JavaScript error: {event_data.exception_details}"}
            if isinstance(event_data, dict):
                return event_data
            if isinstance(event_data, list):
                return self._convert_nodriver_result(event_data)
            return {
                "error": f"Unexpected return type: {type(event_data)}",
                "raw_data": str(event_data),
            }
        except Exception as e:
            debug_logger.log_error("cdp_cloner", "extract_events", e)
            return {"error": str(e)}

    async def extract_element_animations(
        self,
        tab,
        element=None,
        selector: str | None = None,
        include_css_animations: bool = True,
        include_transitions: bool = True,
        include_transforms: bool = True,
        analyze_keyframes: bool = True,
    ) -> dict[str, Any]:
        """Extract CSS animations/transitions/transforms via JS-eval (verbatim
        move; KEEP-JS — CDP has no synchronous per-node ``@keyframes`` read)."""
        try:
            if not selector:
                return {"error": "Selector is required"}

            options = {
                "include_css_animations": include_css_animations,
                "include_transitions": include_transitions,
                "include_transforms": include_transforms,
                "analyze_keyframes": analyze_keyframes,
            }

            js_code = self._load_js_file("extract_animations.js", selector, options)
            animation_data = await tab.evaluate(js_code)

            if hasattr(animation_data, "exception_details"):
                return {
                    "error": f"JavaScript error: {animation_data.exception_details}"
                }
            if isinstance(animation_data, dict):
                return animation_data
            if isinstance(animation_data, list):
                return self._convert_nodriver_result(animation_data)
            return {
                "error": f"Unexpected return type: {type(animation_data)}",
                "raw_data": str(animation_data),
            }
        except Exception as e:
            debug_logger.log_error("cdp_cloner", "extract_animations", e)
            return {"error": str(e)}

    async def extract_element_assets(
        self,
        tab,
        element=None,
        selector: str | None = None,
        include_images: bool = True,
        include_backgrounds: bool = True,
        include_fonts: bool = True,
        fetch_external: bool = False,
    ) -> dict[str, Any]:
        """Extract element assets (images/backgrounds/fonts) via JS-eval
        (verbatim move; KEEP-JS — CDP has no media/font enumerator)."""
        try:
            if not selector:
                return {"error": "Selector is required"}

            js_dir = Path(__file__).parent / "js"
            js_file = js_dir / "extract_assets.js"

            if not js_file.exists():
                return {"error": f"JavaScript file not found: {js_file}"}

            with js_file.open(encoding="utf-8") as f:
                js_code = f.read()

            js_code = self._encode_into(js_code, "SELECTOR", selector)
            js_code = js_code.replace(
                "$INCLUDE_IMAGES", "true" if include_images else "false"
            )
            js_code = js_code.replace(
                "$INCLUDE_BACKGROUNDS", "true" if include_backgrounds else "false"
            )
            js_code = js_code.replace(
                "$INCLUDE_FONTS", "true" if include_fonts else "false"
            )
            js_code = js_code.replace(
                "$FETCH_EXTERNAL", "true" if fetch_external else "false"
            )

            asset_data = await tab.evaluate(js_code)
            if hasattr(asset_data, "exception_details"):
                return {"error": f"JavaScript error: {asset_data.exception_details}"}
            if isinstance(asset_data, dict):
                pass
            elif isinstance(asset_data, list):
                asset_data = self._convert_nodriver_result(asset_data)
            else:
                return {
                    "error": f"Unexpected return type: {type(asset_data)}",
                    "raw_data": str(asset_data),
                }

            if fetch_external and isinstance(asset_data, dict):
                asset_data["external_assets"] = {}
                for bg_img in asset_data.get("background_images", []):
                    try:
                        url = bg_img.get("url", "")
                        if url.startswith("http"):
                            response = requests.get(url, timeout=5)
                            asset_data["external_assets"][url] = {
                                "content_type": response.headers.get("content-type"),
                                "size": len(response.content),
                                "status": response.status_code,
                            }
                    except Exception as e:
                        debug_logger.log_warning(
                            "cdp_cloner",
                            "extract_assets",
                            f"Could not fetch asset {url}: {e}",
                        )

            return asset_data
        except Exception as e:
            debug_logger.log_error("cdp_cloner", "extract_assets", e)
            return {"error": str(e)}

    async def extract_related_files(
        self,
        tab,
        element=None,
        selector: str | None = None,
        analyze_css: bool = True,
        analyze_js: bool = True,
        follow_imports: bool = False,
        max_depth: int = 2,
    ) -> dict[str, Any]:
        """Discover related CSS/JS files via JS-eval (+ optional HTTP fetch);
        verbatim move — no CDP analogue, rehomed not rewritten."""
        try:
            js_dir = Path(__file__).parent / "js"
            js_file = js_dir / "extract_related_files.js"

            if not js_file.exists():
                return {"error": f"JavaScript file not found: {js_file}"}

            with js_file.open(encoding="utf-8") as f:
                js_code = f.read()

            js_code = js_code.replace(
                "$ANALYZE_CSS", "true" if analyze_css else "false"
            )
            js_code = js_code.replace("$ANALYZE_JS", "true" if analyze_js else "false")
            js_code = js_code.replace(
                "$FOLLOW_IMPORTS", "true" if follow_imports else "false"
            )
            js_code = js_code.replace("$MAX_DEPTH", str(max_depth))

            file_data = await tab.evaluate(js_code)
            if hasattr(file_data, "exception_details"):
                return {"error": f"JavaScript error: {file_data.exception_details}"}
            if isinstance(file_data, dict):
                pass
            elif isinstance(file_data, list):
                file_data = self._convert_nodriver_result(file_data)
            else:
                return {
                    "error": f"Unexpected return type: {type(file_data)}",
                    "raw_data": str(file_data),
                }

            if follow_imports and max_depth > 0 and isinstance(file_data, dict):
                await self._fetch_and_analyze_files(file_data)

            return file_data
        except Exception as e:
            debug_logger.log_error("cdp_cloner", "extract_related_files", e)
            return {"error": str(e)}

    async def _fetch_and_analyze_files(self, file_data: dict) -> None:
        """Fetch + analyze external CSS/JS for extra context (verbatim move)."""
        for stylesheet in file_data["stylesheets"]:
            if (
                stylesheet.get("href")
                and stylesheet["href"] not in self.extracted_files
            ):
                try:
                    response = requests.get(stylesheet["href"], timeout=10)
                    if response.status_code == 200:
                        content = response.text
                        self.extracted_files[stylesheet["href"]] = content
                        imports = re.findall(r'@import\s+["\']([^"\']+)["\']', content)
                        stylesheet["imports"] = []
                        for imp in imports:
                            absolute_url = urljoin(stylesheet["href"], imp)
                            stylesheet["imports"].append(absolute_url)
                        css_vars = re.findall(r"--[\w-]+:\s*[^;]+", content)
                        stylesheet["custom_properties"] = css_vars
                except Exception as e:
                    debug_logger.log_warning(
                        "cdp_cloner",
                        "fetch_css",
                        f"Could not fetch CSS file {stylesheet.get('href')}: {e}",
                    )
        for script in file_data["scripts"]:
            if script.get("src") and script["src"] not in self.extracted_files:
                try:
                    response = requests.get(script["src"], timeout=10)
                    if response.status_code == 200:
                        content = response.text
                        self.extracted_files[script["src"]] = content
                        script["detected_frameworks"] = []
                        for framework, patterns in self.framework_patterns.items():
                            for pattern in patterns:
                                if (
                                    re.search(pattern, content, re.IGNORECASE)
                                    and framework not in script["detected_frameworks"]
                                ):
                                    script["detected_frameworks"].append(framework)
                        imports = re.findall(
                            r'import.*from\s+["\']([^"\']+)["\']', content
                        )
                        script["module_imports"] = imports
                except Exception as e:
                    debug_logger.log_warning(
                        "cdp_cloner",
                        "fetch_js",
                        f"Could not fetch JS file {script.get('src')}: {e}",
                    )

    async def extract_complete_element(
        self,
        tab,
        element=None,
        selector: str | None = None,
        extraction_options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Canonical complete-element extraction — the ONE schema (F-140 3->1).

        Composes the six per-aspect methods on this engine. Unlike the retired
        ``ElementCloner.clone_element_complete``, ``selector`` is forwarded to
        every sub-extractor (fixes F-142), so all aspects populate.
        """
        try:
            default_options = {
                "styles": {
                    "include_computed": True,
                    "include_css_rules": True,
                    "include_pseudo": True,
                },
                "structure": {"include_children": False, "include_attributes": True},
                "events": {"include_framework": True, "analyze_handlers": False},
                "animations": {"analyze_keyframes": True},
                "assets": {"fetch_external": False},
                "related_files": {"follow_imports": False},
            }
            if extraction_options:
                for key, value in extraction_options.items():
                    if key in default_options:
                        default_options[key].update(value)
                    else:
                        default_options[key] = value
            result = {
                "url": tab.url,
                "timestamp": datetime.now().isoformat(),
                "selector": selector,
                "extraction_options": default_options,
            }
            tasks = []
            if "styles" in default_options:
                tasks.append(
                    (
                        "styles",
                        self.extract_element_styles(
                            tab,
                            element=element,
                            selector=selector,
                            **default_options["styles"],
                        ),
                    )
                )
            if "structure" in default_options:
                tasks.append(
                    (
                        "structure",
                        self.extract_element_structure(
                            tab,
                            element=element,
                            selector=selector,
                            **default_options["structure"],
                        ),
                    )
                )
            if "events" in default_options:
                tasks.append(
                    (
                        "events",
                        self.extract_element_events(
                            tab,
                            element=element,
                            selector=selector,
                            **default_options["events"],
                        ),
                    )
                )
            if "animations" in default_options:
                tasks.append(
                    (
                        "animations",
                        self.extract_element_animations(
                            tab,
                            element=element,
                            selector=selector,
                            **default_options["animations"],
                        ),
                    )
                )
            if "assets" in default_options:
                tasks.append(
                    (
                        "assets",
                        self.extract_element_assets(
                            tab,
                            element=element,
                            selector=selector,
                            **default_options["assets"],
                        ),
                    )
                )
            if "related_files" in default_options:
                tasks.append(
                    (
                        "related_files",
                        self.extract_related_files(
                            tab, **default_options["related_files"]
                        ),
                    )
                )
            results = await asyncio.gather(
                *[task[1] for task in tasks], return_exceptions=True
            )
            for i, (name, _) in enumerate(tasks):
                if isinstance(results[i], Exception):
                    result[name] = {"error": str(results[i])}
                else:
                    result[name] = results[i]
            debug_logger.log_info(
                "cdp_cloner",
                "extract_complete_element",
                f"Canonical complete clone with {len(tasks)} aspects",
            )
            return result
        except Exception as e:
            debug_logger.log_error("cdp_cloner", "extract_complete_element", e)
            return {"error": str(e)}


cdp_element_cloner = CDPElementCloner()
