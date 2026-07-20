"""
Progressive Element Cloner System
=================================

Stores comprehensive element clone data in memory and returns a compact handle
(`element_id`) so clients can progressively expand specific portions later.
"""

import time
import uuid
from typing import Any

from stealth_chrome_devtools_mcp.embedded.cdp_element_cloner import cdp_element_cloner
from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.embedded.in_memory_storage import in_memory_storage


class ProgressiveElementCloner:
    """Progressive element cloner with in-memory store."""

    def __init__(self):
        self.STORAGE_KEY = "progressive_elements"

    def _get_store(self) -> dict[str, dict[str, Any]]:
        return in_memory_storage.get(self.STORAGE_KEY, {})

    def _save_store(self, data: dict[str, dict[str, Any]]) -> None:
        in_memory_storage.set(self.STORAGE_KEY, data)

    async def clone_element_progressive(
        self,
        tab,
        selector: str,
        include_children: bool = True,
    ) -> dict[str, Any]:
        try:
            element_id = f"elem_{uuid.uuid4().hex[:12]}"
            debug_logger.log_info(
                "progressive_cloner",
                "clone_progressive",
                f"Cloning {selector} -> {element_id}",
            )

            full_data = await cdp_element_cloner.extract_complete_element(
                tab,
                selector=selector,
                extraction_options={
                    "structure": {"include_children": include_children}
                },
            )
            if not isinstance(full_data, dict) or "error" in full_data:
                return {
                    "error": "Element not found or extraction failed",
                    "selector": selector,
                }

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
                "tagName": full_data.get("structure", {}).get("tag_name", "unknown"),
                "attributes_count": len(
                    full_data.get("structure", {}).get("attributes", {})
                ),
                "children_count": len(
                    full_data.get("structure", {}).get("children", [])
                ),
                "summary": {
                    "styles_count": len(
                        full_data.get("styles", {}).get("computed_styles", {})
                    ),
                    "event_listeners_count": len(
                        full_data.get("events", {}).get("event_listeners", [])
                    ),
                    "css_rules_count": len(
                        full_data.get("styles", {}).get("css_rules", [])
                    ),
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
        self,
        element_id: str,
        categories: list[str] | None = None,
        properties: list[str] | None = None,
    ) -> dict[str, Any]:
        store = self._get_store()
        if element_id not in store:
            return {"error": f"Element {element_id} not found"}
        data = store[element_id]["full_data"]
        styles = data.get("styles", {}).get("computed_styles", {})
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

    def expand_events(
        self, element_id: str, event_types: list[str] | None = None
    ) -> dict[str, Any]:
        store = self._get_store()
        if element_id not in store:
            return {"error": f"Element {element_id} not found"}
        data = store[element_id]["full_data"]
        events = data.get("events", {}).get("event_listeners", [])
        if event_types:
            events = [
                e
                for e in events
                if e.get("type") in event_types or e.get("source") in event_types
            ]
        return {
            "element_id": element_id,
            "data_type": "events",
            "event_listeners": events,
            "total_available": len(events),
            "returned_count": len(events),
        }

    def expand_children(
        self,
        element_id: str,
        depth_range: tuple[int, int] | None = None,
        max_count: int | None = None,
    ) -> dict[str, Any]:
        store = self._get_store()
        if element_id not in store:
            return {"error": f"Element {element_id} not found"}
        data = store[element_id]["full_data"]
        children = data.get("structure", {}).get("children", [])

        # Ensure children is a list that can be sliced
        if not isinstance(children, list):
            children = list(children) if hasattr(children, "__iter__") else []

        if depth_range:
            min_d, max_d = depth_range
            children = [
                c
                for c in children
                if isinstance(c, dict) and min_d <= c.get("depth", 0) <= max_d
            ]

        if isinstance(max_count, int) and max_count > 0:
            try:
                children = children[:max_count]
            except (TypeError, AttributeError) as e:
                debug_logger.log_error(
                    "progressive_cloner",
                    "expand_children",
                    f"Slicing error: {e}, children type: {type(children)}",
                )
                children = []
        return {
            "element_id": element_id,
            "data_type": "children",
            "children": children,
            "total_available": len(data.get("structure", {}).get("children", [])),
            "returned_count": len(children),
        }

    def expand_css_rules(
        self, element_id: str, source_types: list[str] | None = None
    ) -> dict[str, Any]:
        store = self._get_store()
        if element_id not in store:
            return {"error": f"Element {element_id} not found"}
        data = store[element_id]["full_data"]
        rules = data.get("styles", {}).get("css_rules", [])
        if source_types:
            rules = [
                r for r in rules if any(s in r.get("source", "") for s in source_types)
            ]
        return {
            "element_id": element_id,
            "data_type": "css_rules",
            "css_rules": rules,
            "total_available": len(data.get("styles", {}).get("css_rules", [])),
            "returned_count": len(rules),
        }

    def expand_pseudo_elements(self, element_id: str) -> dict[str, Any]:
        store = self._get_store()
        if element_id not in store:
            return {"error": f"Element {element_id} not found"}
        data = store[element_id]["full_data"]
        pseudos = data.get("styles", {}).get("pseudo_elements", {})
        return {
            "element_id": element_id,
            "data_type": "pseudo_elements",
            "pseudo_elements": pseudos,
            "available_pseudos": list(pseudos.keys()),
        }

    def expand_animations(self, element_id: str) -> dict[str, Any]:
        store = self._get_store()
        if element_id not in store:
            return {"error": f"Element {element_id} not found"}
        data = store[element_id]["full_data"]
        animations = data.get("animations", {})
        fonts = data.get("assets", {}).get("fonts", {})
        return {
            "element_id": element_id,
            "data_type": "animations",
            "animations": animations,
            "fonts": fonts,
        }

    def list_stored_elements(self) -> dict[str, Any]:
        store = self._get_store()
        items = []
        for element_id, meta in store.items():
            fd = meta.get("full_data", {})
            items.append(
                {
                    "element_id": element_id,
                    "selector": meta.get("selector"),
                    "url": meta.get("url"),
                    "tagName": fd.get("structure", {}).get("tag_name", "unknown"),
                    "children_count": len(fd.get("structure", {}).get("children", [])),
                    "styles_count": len(
                        fd.get("styles", {}).get("computed_styles", {})
                    ),
                    "timestamp": meta.get("timestamp"),
                }
            )
        return {"stored_elements": items, "total_count": len(items)}

    def clear_stored_element(self, element_id: str) -> dict[str, Any]:
        store = self._get_store()
        if element_id in store:
            del store[element_id]
            self._save_store(store)
            return {"success": True, "message": f"Element {element_id} cleared"}
        return {"error": f"Element {element_id} not found"}

    def clear_all_elements(self) -> dict[str, Any]:
        self._save_store({})
        return {"success": True, "message": "All stored elements cleared"}


progressive_element_cloner = ProgressiveElementCloner()
