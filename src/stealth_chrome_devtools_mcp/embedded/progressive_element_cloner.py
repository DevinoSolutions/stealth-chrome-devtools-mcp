"""
Progressive Element Cloner System
=================================

Stores comprehensive element clone data in memory and returns a compact handle
(`element_id`) so clients can progressively expand specific portions later.
"""

import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from debug_logger import debug_logger
from persistent_storage import persistent_storage
from comprehensive_element_cloner import comprehensive_element_cloner


class ProgressiveElementCloner:
    """Progressive element cloner with in-memory store."""

    def __init__(self):
        self.STORAGE_KEY = "progressive_elements"

    def _get_store(self) -> Dict[str, Dict[str, Any]]:
        return persistent_storage.get(self.STORAGE_KEY, {})

    def _save_store(self, data: Dict[str, Dict[str, Any]]) -> None:
        persistent_storage.set(self.STORAGE_KEY, data)

    async def clone_element_progressive(
        self,
        tab,
        selector: str,
        include_children: bool = True,
    ) -> Dict[str, Any]:
        try:
            element_id = f"elem_{uuid.uuid4().hex[:12]}"
            debug_logger.log_info("progressive_cloner", "clone_progressive", f"Cloning {selector} -> {element_id}")

            full_data = await comprehensive_element_cloner.extract_complete_element(
                tab, selector, include_children
            )
            if not isinstance(full_data, dict) or "error" in full_data:
                return {"error": "Element not found or extraction failed", "selector": selector}

            store = self._get_store()
            store[element_id] = {
                "full_data": full_data,
                "url": getattr(tab, "url", ""),
                "selector": selector,
                "timestamp": time.time(),
                "include_children": include_children,
            }
            self._save_store(store)

            base = {
                "tagName": full_data.get("element", {}).get("html", {}).get("tagName")
                or full_data.get("tagName", "unknown"),
                "attributes_count": len(full_data.get("element", {}).get("html", {}).get("attributes", [])),
                "children_count": len(full_data.get("children", [])),
                "summary": {
                    "styles_count": len(full_data.get("element", {}).get("computed_styles", {}))
                    or len(full_data.get("styles", {})),
                    "event_listeners_count": len(full_data.get("element", {}).get("event_listeners", []))
                    or len(full_data.get("eventListeners", [])),
                    "css_rules_count": len(full_data.get("element", {}).get("matched_styles", {}).get("matchedCSSRules", []))
                    if isinstance(full_data.get("element", {}).get("matched_styles"), dict)
                    else len(full_data.get("cssRules", [])),
                },
            }

            return {
                "element_id": element_id,
                "base": base,
                "available_data": [
                    "styles",
                    "events",
                    "children",
                    "css_rules",
                    "pseudo_elements",
                    "animations",
                    "fonts",
                    "html",
                ],
                "url": getattr(tab, "url", ""),
                "selector": selector,
                "timestamp": time.time(),
            }
        except Exception as e:
            debug_logger.log_error("progressive_cloner", "clone_progressive", e)
            return {"error": str(e)}

    def expand_styles(
        self, element_id: str, categories: Optional[List[str]] = None, properties: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        store = self._get_store()
        if element_id not in store:
            return {"error": f"Element {element_id} not found"}
        data = store[element_id]["full_data"]
        styles = (
            data.get("element", {}).get("computed_styles", {})
            if isinstance(data.get("element", {}).get("computed_styles"), dict)
            else data.get("styles", {})
        )
        if properties:
            filtered = {k: v for k, v in styles.items() if k in properties}
        elif categories:
            category_map = {
                "layout": [
                    "display",
                    "position",
                    "width",
                    "height",
                    "max-width",
                    "max-height",
                    "min-width",
                    "min-height",
                ],
                "typography": [
                    "font-family",
                    "font-size",
                    "font-weight",
                    "font-style",
                    "line-height",
                    "text-align",
                ],
                "colors": ["color", "background-color", "border-color"],
            }
            keys = set(k for c in categories for k in category_map.get(c, []))
            filtered = {k: v for k, v in styles.items() if k in keys}
        else:
            filtered = styles
        return {
            "element_id": element_id,
            "data_type": "styles",
            "styles": filtered,
            "total_available": len(styles),
            "returned_count": len(filtered),
        }

    def expand_events(self, element_id: str, event_types: Optional[List[str]] = None) -> Dict[str, Any]:
        store = self._get_store()
        if element_id not in store:
            return {"error": f"Element {element_id} not found"}
        data = store[element_id]["full_data"]
        events = data.get("eventListeners", []) or data.get("element", {}).get("event_listeners", [])
        if event_types:
            events = [e for e in events if e.get("type") in event_types or e.get("source") in event_types]
        return {
            "element_id": element_id,
            "data_type": "events",
            "event_listeners": events,
            "total_available": len(events),
            "returned_count": len(events),
        }

    def expand_children(
        self, element_id: str, depth_range: Optional[Tuple[int, int]] = None, max_count: Optional[int] = None
    ) -> Dict[str, Any]:
        store = self._get_store()
        if element_id not in store:
            return {"error": f"Element {element_id} not found"}
        data = store[element_id]["full_data"]
        children = data.get("children", [])
        
        # Ensure children is a list that can be sliced
        if not isinstance(children, list):
            children = list(children) if hasattr(children, '__iter__') else []
            
        if depth_range:
            min_d, max_d = depth_range
            children = [c for c in children if isinstance(c, dict) and min_d <= c.get("depth", 0) <= max_d]
            
        if isinstance(max_count, int) and max_count > 0:
            try:
                children = children[:max_count]
            except (TypeError, AttributeError) as e:
                debug_logger.log_error("progressive_cloner", "expand_children", f"Slicing error: {e}, children type: {type(children)}")
                children = []
        return {
            "element_id": element_id,
            "data_type": "children",
            "children": children,
            "total_available": len(data.get("children", [])),
            "returned_count": len(children),
        }

    def expand_css_rules(self, element_id: str, source_types: Optional[List[str]] = None) -> Dict[str, Any]:
        store = self._get_store()
        if element_id not in store:
            return {"error": f"Element {element_id} not found"}
        data = store[element_id]["full_data"]
        rules = data.get("cssRules", [])
        if source_types:
            rules = [r for r in rules if any(s in r.get("source", "") for s in source_types)]
        return {
            "element_id": element_id,
            "data_type": "css_rules",
            "css_rules": rules,
            "total_available": len(data.get("cssRules", [])),
            "returned_count": len(rules),
        }

    def expand_pseudo_elements(self, element_id: str) -> Dict[str, Any]:
        store = self._get_store()
        if element_id not in store:
            return {"error": f"Element {element_id} not found"}
        data = store[element_id]["full_data"]
        pseudos = data.get("pseudoElements", {})
        return {
            "element_id": element_id,
            "data_type": "pseudo_elements",
            "pseudo_elements": pseudos,
            "available_pseudos": list(pseudos.keys()),
        }

    def expand_animations(self, element_id: str) -> Dict[str, Any]:
        store = self._get_store()
        if element_id not in store:
            return {"error": f"Element {element_id} not found"}
        data = store[element_id]["full_data"]
        animations = data.get("animations", {})
        fonts = data.get("fonts", {})
        return {
            "element_id": element_id,
            "data_type": "animations",
            "animations": animations,
            "fonts": fonts,
        }

    def list_stored_elements(self) -> Dict[str, Any]:
        store = self._get_store()
        items = []
        for element_id, meta in store.items():
            fd = meta.get("full_data", {})
            items.append(
                {
                    "element_id": element_id,
                    "selector": meta.get("selector"),
                    "url": meta.get("url"),
                    "tagName": fd.get("tagName") or fd.get("element", {}).get("html", {}).get("tagName", "unknown"),
                    "children_count": len(fd.get("children", [])),
                    "styles_count": len(fd.get("styles", {}))
                    or len(fd.get("element", {}).get("computed_styles", {})),
                    "timestamp": meta.get("timestamp"),
                }
            )
        return {"stored_elements": items, "total_count": len(items)}

    def clear_stored_element(self, element_id: str) -> Dict[str, Any]:
        store = self._get_store()
        if element_id in store:
            del store[element_id]
            self._save_store(store)
            return {"success": True, "message": f"Element {element_id} cleared"}
        return {"error": f"Element {element_id} not found"}

    def clear_all_elements(self) -> Dict[str, Any]:
        self._save_store({})
        return {"success": True, "message": "All stored elements cleared"}


progressive_element_cloner = ProgressiveElementCloner()


