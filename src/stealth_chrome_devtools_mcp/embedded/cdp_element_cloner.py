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
from datetime import datetime
from typing import Dict, List, Any, Optional

import nodriver as uc
from debug_logger import debug_logger


class CDPElementCloner:
    """Enhanced element cloner using proper CDP methods for complete accuracy."""

    def __init__(self):
        """Initialize the CDP element cloner."""

    async def extract_complete_element_cdp(
        self,
        tab,
        selector: str,
        include_children: bool = True
    ) -> Dict[str, Any]:
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
            debug_logger.log_info("cdp_cloner", "extract_complete", f"Starting CDP extraction for {selector}")
            await tab.send(uc.cdp.dom.enable())
            await tab.send(uc.cdp.css.enable())
            await tab.send(uc.cdp.runtime.enable())
            doc = await tab.send(uc.cdp.dom.get_document())
            nodes = await tab.send(uc.cdp.dom.query_selector_all(doc.node_id, selector))
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
                    "children": children
                },
                "extraction_stats": {
                    "computed_styles_count": len(computed_styles),
                    "css_rules_count": len(matched_styles.get("matchedCSSRules", [])),
                    "event_listeners_count": len(event_listeners),
                    "children_count": len(children)
                }
            }
            debug_logger.log_info("cdp_cloner", "extract_complete", f"CDP extraction completed successfully")
            return result
        except Exception as e:
            debug_logger.log_error("cdp_cloner", "extract_complete", f"CDP extraction failed: {str(e)}")
            return {"error": f"CDP extraction failed: {str(e)}"}

    async def _get_element_html(self, tab, node_id) -> Dict[str, Any]:
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
                    {"name": node_details.attributes[i], "value": node_details.attributes[i+1]}
                    for i in range(0, len(node_details.attributes or []), 2)
                ] if node_details.attributes else []
            }
        except Exception as e:
            debug_logger.log_error("cdp_cloner", "_get_element_html", f"Failed: {str(e)}")
            return {"error": str(e)}

    async def _get_computed_styles_cdp(self, tab, node_id) -> Dict[str, str]:
        """
        Get complete computed styles using CDP CSS.getComputedStyleForNode.

        Args:
            tab (Any): The nodriver tab object for CDP communication.
            node_id (Any): Node ID of the target element.

        Returns:
            Dict[str, str]: Dictionary of computed style properties and their values.
        """
        try:
            computed_styles_list = await tab.send(uc.cdp.css.get_computed_style_for_node(node_id))
            styles = {}
            for style_prop in computed_styles_list:
                styles[style_prop.name] = style_prop.value
            debug_logger.log_info("cdp_cloner", "_get_computed_styles", f"Got {len(styles)} computed styles")
            return styles
        except Exception as e:
            debug_logger.log_error("cdp_cloner", "_get_computed_styles", f"Failed: {str(e)}")
            return {}

    async def _get_matched_styles_cdp(self, tab, node_id) -> Dict[str, Any]:
        """
        Get matched CSS rules using CDP CSS.getMatchedStylesForNode.

        Args:
            tab (Any): The nodriver tab object for CDP communication.
            node_id (Any): Node ID of the target element.

        Returns:
            Dict[str, Any]: Dictionary containing inline style, attribute style, matched rules, pseudo elements, and inherited styles.
        """
        try:
            matched_result = await tab.send(uc.cdp.css.get_matched_styles_for_node(node_id))
            inline_style, attributes_style, matched_rules, pseudo_elements, inherited = matched_result[:5]
            result = {
                "inlineStyle": self._css_style_to_dict(inline_style) if inline_style else None,
                "attributesStyle": self._css_style_to_dict(attributes_style) if attributes_style else None,
                "matchedCSSRules": [self._rule_match_to_dict(rule) for rule in (matched_rules or [])],
                "pseudoElements": [self._pseudo_element_to_dict(pe) for pe in (pseudo_elements or [])],
                "inherited": [self._inherited_style_to_dict(inh) for inh in (inherited or [])]
            }
            debug_logger.log_info("cdp_cloner", "_get_matched_styles", 
                                  f"Got {len(result['matchedCSSRules'])} CSS rules")
            return result
        except Exception as e:
            debug_logger.log_error("cdp_cloner", "_get_matched_styles", f"Failed: {str(e)}")
            return {}

    async def _get_event_listeners_cdp(self, tab, node_id) -> List[Dict[str, Any]]:
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
                result.append({
                    "type": listener.type_,
                    "useCapture": listener.use_capture,
                    "passive": listener.passive,
                    "once": listener.once,
                    "scriptId": str(listener.script_id),
                    "lineNumber": listener.line_number,
                    "columnNumber": listener.column_number,
                    "hasHandler": listener.handler is not None,
                    "hasOriginalHandler": listener.original_handler is not None,
                    "backendNodeId": int(listener.backend_node_id) if listener.backend_node_id else None
                })
            debug_logger.log_info("cdp_cloner", "_get_event_listeners", 
                                  f"Got {len(result)} event listeners")
            return result
        except Exception as e:
            debug_logger.log_error("cdp_cloner", "_get_event_listeners", f"Failed: {str(e)}")
            return []

    async def _get_children_cdp(self, tab, node_id) -> List[Dict[str, Any]]:
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
            node_details = await tab.send(uc.cdp.dom.describe_node(node_id=node_id, depth=1))
            children = []
            if node_details.children:
                for child in node_details.children:
                    if child.node_type == 1:
                        child_html = await self._get_element_html(tab, child.node_id)
                        child_computed = await self._get_computed_styles_cdp(tab, child.node_id)
                        children.append({
                            "html": child_html,
                            "computed_styles": child_computed,
                            "depth": 1
                        })
            return children
        except Exception as e:
            debug_logger.log_error("cdp_cloner", "_get_children", f"Failed: {str(e)}")
            return []

    def _css_style_to_dict(self, css_style) -> Dict[str, Any]:
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
                    "disabled": prop.disabled
                }
                for prop in css_style.css_properties_
            ]
        }

    def _rule_match_to_dict(self, rule_match) -> Dict[str, Any]:
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
                "selectorText": rule_match.rule.selector_list.text if rule_match.rule.selector_list else "",
                "origin": str(rule_match.rule.origin),
                "style": self._css_style_to_dict(rule_match.rule.style),
                "styleSheetId": str(rule_match.rule.style_sheet_id_) if rule_match.rule.style_sheet_id_ else None
            }
        }

    def _pseudo_element_to_dict(self, pseudo_element) -> Dict[str, Any]:
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
            "matches": [self._rule_match_to_dict(match) for match in pseudo_element.matches_]
        }

    def _inherited_style_to_dict(self, inherited_style) -> Dict[str, Any]:
        """
        Convert CDP InheritedStyleEntry to dictionary.

        Args:
            inherited_style (Any): CDP InheritedStyleEntry object.

        Returns:
            Dict[str, Any]: Dictionary describing inherited styles.
        """
        return {
            "inlineStyle": self._css_style_to_dict(inherited_style.inline_style) if inherited_style.inline_style else None,
            "matchedCSSRules": [self._rule_match_to_dict(rule) for rule in inherited_style.matched_css_rules]
        }