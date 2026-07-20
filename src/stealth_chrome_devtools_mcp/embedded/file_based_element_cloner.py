import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from stealth_chrome_devtools_mcp.embedded.cdp_element_cloner import cdp_element_cloner
from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.embedded.response_handler import (
    default_clone_output_dir,
)


class FileBasedElementCloner:
    """Thin to-file adapter over the canonical ``cdp_element_cloner`` engine.

    Every ``*_to_file`` method is a call site of the one ``_extract_and_save``
    helper (F-141): extract via the engine, persist to the output dir, return
    the uniform ``{file_path, extraction_type, summary}`` contract. This class
    owns only file-writing + the ``output_dir`` resolution contract; all
    extraction lives in the engine.
    """

    def __init__(self, output_dir: str | None = None):
        """
        Initialize with output directory for clone files.

        Args:
            output_dir (str): Directory to save clone files. When omitted,
                defaults to a per-user location outside the installed package
                (see ``default_clone_output_dir``) so a real install never
                writes into read-only ``site-packages``. An explicit absolute
                path is honored as-is; an explicit relative path stays
                package-relative for backward compatibility.
        """
        if output_dir is None:
            self.output_dir = default_clone_output_dir()
        elif Path(output_dir).is_absolute():
            self.output_dir = Path(output_dir)
        else:
            self.output_dir = Path(__file__).resolve().parent.parent / output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def _safe_process_framework_handlers(self, framework_handlers):
        """Safely process framework handlers that might be dict or list."""
        if isinstance(framework_handlers, dict):
            return {
                k: len(v) if isinstance(v, list) else str(v)
                for k, v in framework_handlers.items()
            }
        if isinstance(framework_handlers, list):
            return {"handlers": len(framework_handlers)}
        return {"value": str(framework_handlers)}

    def _generate_filename(self, prefix: str, extension: str = "json") -> str:
        """
        Generate unique filename with timestamp.

        Args:
            prefix (str): Prefix for the filename.
            extension (str): File extension.

        Returns:
            str: Generated filename.
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        unique_id = str(uuid.uuid4())[:8]
        return f"{prefix}_{timestamp}_{unique_id}.{extension}"

    def _save_to_file(self, data: dict[str, Any], filename: str) -> str:
        """
        Save data to file and return absolute path.

        Args:
            data (Dict[str, Any]): Data to save.
            filename (str): Name of the file.

        Returns:
            str: Absolute path to the saved file.
        """
        file_path = self.output_dir / filename
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return str(file_path.absolute())

    async def _extract_and_save(self, prefix, extraction_coro, summary_fn):
        """The one to-file shape (F-141): extract → save → uniform contract.

        Extract via the engine coroutine, persist the raw payload under
        ``output_dir``, and return ``{file_path, extraction_type, summary}``.
        A delegated ``{"error": ...}`` payload is still written to disk and its
        (empty) summary returned — the to-file layer never propagates a
        delegated extractor error (unified swallow, was inconsistent across the
        8 copies). An exception in extraction/save yields ``{"error": str(e)}``.
        """
        op = f"{prefix}_to_file"
        try:
            data = await extraction_coro
            filename = self._generate_filename(prefix)
            file_path = self._save_to_file(data, filename)
            debug_logger.log_info(
                "file_element_cloner", op, f"Saved {prefix} data to {file_path}"
            )
            return {
                "file_path": file_path,
                "extraction_type": prefix,
                "summary": summary_fn(data),
            }
        except Exception as e:
            debug_logger.log_error("file_element_cloner", op, e)
            return {"error": str(e)}

    async def extract_element_styles_to_file(
        self,
        tab,
        selector: str,
        include_computed: bool = True,
        include_css_rules: bool = True,
        include_pseudo: bool = True,
        include_inheritance: bool = False,
    ) -> dict[str, Any]:
        """Extract element styles (engine, CDP) and save to file."""
        return await self._extract_and_save(
            "styles",
            cdp_element_cloner.extract_element_styles(
                tab,
                selector=selector,
                include_computed=include_computed,
                include_css_rules=include_css_rules,
                include_pseudo=include_pseudo,
                include_inheritance=include_inheritance,
            ),
            lambda d: {
                "selector": selector,
                "url": getattr(tab, "url", "unknown"),
                "computed_styles_count": len(d.get("computed_styles", {})),
                "css_rules_count": len(d.get("css_rules", [])),
                "pseudo_elements_count": len(d.get("pseudo_elements", {})),
                "custom_properties_count": len(d.get("custom_properties", {})),
            },
        )

    async def extract_complete_element_to_file(
        self, tab, selector: str, include_children: bool = True
    ) -> dict[str, Any]:
        """Extract the complete element (engine composer) and save to file."""
        return await self._extract_and_save(
            "complete_comprehensive",
            cdp_element_cloner.extract_complete_element(
                tab,
                selector=selector,
                extraction_options={
                    "structure": {"include_children": include_children}
                },
            ),
            lambda d: {
                "selector": selector,
                "url": d.get("url", "unknown"),
                "tag_name": d.get("structure", {}).get("tag_name", "unknown"),
                "computed_styles_count": len(
                    d.get("styles", {}).get("computed_styles", {})
                ),
                "attributes_count": len(d.get("structure", {}).get("attributes", {})),
                "event_listeners_count": len(
                    d.get("events", {}).get("event_listeners", [])
                ),
                "children_count": len(d.get("structure", {}).get("children", []))
                if include_children
                else 0,
                "has_pseudo_elements": bool(d.get("styles", {}).get("pseudo_elements")),
                "css_rules_count": len(d.get("styles", {}).get("css_rules", [])),
                "animations_count": len(d.get("animations", {})),
                "file_size_kb": round(len(json.dumps(d)) / 1024, 2),
            },
        )

    async def extract_element_structure_to_file(
        self,
        tab,
        element=None,
        selector: str = None,
        include_children: bool = False,
        include_attributes: bool = True,
        include_data_attributes: bool = True,
        max_depth: int = 3,
    ) -> dict[str, Any]:
        """Extract structure (engine, JS) and save to file."""
        return await self._extract_and_save(
            "structure",
            cdp_element_cloner.extract_element_structure(
                tab,
                element,
                selector,
                include_children,
                include_attributes,
                include_data_attributes,
                max_depth,
            ),
            lambda d: {
                "selector": selector,
                "tag_name": d.get("tag_name"),
                "attributes_count": len(d.get("attributes", {})),
                "data_attributes_count": len(d.get("data_attributes", {})),
                "children_count": len(d.get("children", [])),
                "dom_path": d.get("dom_path"),
            },
        )

    async def extract_element_events_to_file(
        self,
        tab,
        element=None,
        selector: str = None,
        include_inline: bool = True,
        include_listeners: bool = True,
        include_framework: bool = True,
        analyze_handlers: bool = True,
    ) -> dict[str, Any]:
        """Extract events (engine, JS) and save to file."""
        return await self._extract_and_save(
            "events",
            cdp_element_cloner.extract_element_events(
                tab,
                element,
                selector,
                include_inline,
                include_listeners,
                include_framework,
                analyze_handlers,
            ),
            lambda d: {
                "selector": selector,
                "inline_handlers_count": len(d.get("inline_handlers", [])),
                "event_listeners_count": len(d.get("event_listeners", [])),
                "detected_frameworks": d.get("detected_frameworks", []),
                "framework_handlers": self._safe_process_framework_handlers(
                    d.get("framework_handlers", {})
                ),
            },
        )

    async def extract_element_animations_to_file(
        self,
        tab,
        element=None,
        selector: str = None,
        include_css_animations: bool = True,
        include_transitions: bool = True,
        include_transforms: bool = True,
        analyze_keyframes: bool = True,
    ) -> dict[str, Any]:
        """Extract animations (engine, JS) and save to file."""
        return await self._extract_and_save(
            "animations",
            cdp_element_cloner.extract_element_animations(
                tab,
                element,
                selector,
                include_css_animations,
                include_transitions,
                include_transforms,
                analyze_keyframes,
            ),
            lambda d: {
                "selector": selector,
                "has_animations": d.get("animations", {}).get("animation_name", "none")
                != "none",
                "has_transitions": d.get("transitions", {}).get(
                    "transition_property", "none"
                )
                != "none",
                "has_transforms": d.get("transforms", {}).get("transform", "none")
                != "none",
                "keyframes_count": len(d.get("keyframes", [])),
            },
        )

    async def extract_element_assets_to_file(
        self,
        tab,
        element=None,
        selector: str = None,
        include_images: bool = True,
        include_backgrounds: bool = True,
        include_fonts: bool = True,
        fetch_external: bool = False,
    ) -> dict[str, Any]:
        """Extract assets (engine, JS) and save to file."""
        return await self._extract_and_save(
            "assets",
            cdp_element_cloner.extract_element_assets(
                tab,
                element,
                selector,
                include_images,
                include_backgrounds,
                include_fonts,
                fetch_external,
            ),
            lambda d: {
                "selector": selector,
                "images_count": len(d.get("images", [])),
                "background_images_count": len(d.get("background_images", [])),
                "font_family": d.get("fonts", {}).get("family"),
                "custom_fonts_count": len(d.get("fonts", {}).get("custom_fonts", [])),
                "icons_count": len(d.get("icons", [])),
                "videos_count": len(d.get("videos", [])),
                "audio_count": len(d.get("audio", [])),
            },
        )

    async def extract_related_files_to_file(
        self,
        tab,
        element=None,
        selector: str = None,
        analyze_css: bool = True,
        analyze_js: bool = True,
        follow_imports: bool = False,
        max_depth: int = 2,
    ) -> dict[str, Any]:
        """Extract related files (engine, JS+HTTP) and save to file."""
        return await self._extract_and_save(
            "related_files",
            cdp_element_cloner.extract_related_files(
                tab,
                element,
                selector,
                analyze_css,
                analyze_js,
                follow_imports,
                max_depth,
            ),
            lambda d: {
                "selector": selector,
                "stylesheets_count": len(d.get("stylesheets", [])),
                "scripts_count": len(d.get("scripts", [])),
                "imports_count": len(d.get("imports", [])),
                "modules_count": len(d.get("modules", [])),
            },
        )

    async def clone_element_complete_to_file(
        self,
        tab,
        element=None,
        selector: str = None,
        extraction_options: dict[str, Any] = None,
    ) -> dict[str, Any]:
        """Extract all element data (engine composer) and save to file."""

        def _summary(d):
            components: dict[str, Any] = {}
            if "styles" in d:
                styles = d["styles"]
                components["styles"] = {
                    "computed_styles_count": len(styles.get("computed_styles", {})),
                    "css_rules_count": len(styles.get("css_rules", [])),
                    "pseudo_elements_count": len(styles.get("pseudo_elements", {})),
                }
            if "structure" in d:
                structure = d["structure"]
                components["structure"] = {
                    "tag_name": structure.get("tag_name"),
                    "attributes_count": len(structure.get("attributes", {})),
                    "children_count": len(structure.get("children", [])),
                }
            if "events" in d:
                events = d["events"]
                components["events"] = {
                    "inline_handlers_count": len(events.get("inline_handlers", [])),
                    "detected_frameworks": events.get("detected_frameworks", []),
                }
            if "animations" in d:
                animations = d["animations"]
                components["animations"] = {
                    "has_animations": animations.get("animations", {}).get(
                        "animation_name", "none"
                    )
                    != "none",
                    "keyframes_count": len(animations.get("keyframes", [])),
                }
            if "assets" in d:
                assets = d["assets"]
                components["assets"] = {
                    "images_count": len(assets.get("images", [])),
                    "background_images_count": len(assets.get("background_images", [])),
                }
            if "related_files" in d:
                files = d["related_files"]
                components["related_files"] = {
                    "stylesheets_count": len(files.get("stylesheets", [])),
                    "scripts_count": len(files.get("scripts", [])),
                }
            return {
                "selector": selector,
                "url": d.get("url"),
                "components": components,
            }

        return await self._extract_and_save(
            "complete_clone",
            cdp_element_cloner.extract_complete_element(
                tab, element, selector, extraction_options
            ),
            _summary,
        )

    def list_clone_files(self) -> list[dict[str, Any]]:
        """
        List all clone files in the output directory.

        Returns:
            List[Dict[str, Any]]: List of file info dictionaries.
        """
        files = []
        for file_path in self.output_dir.glob("*.json"):
            try:
                file_info = {
                    "file_path": str(file_path.absolute()),
                    "filename": file_path.name,
                    "size": file_path.stat().st_size,
                    "created": datetime.fromtimestamp(
                        file_path.stat().st_ctime
                    ).isoformat(),
                    "modified": datetime.fromtimestamp(
                        file_path.stat().st_mtime
                    ).isoformat(),
                }
                try:
                    with open(file_path, encoding="utf-8") as f:
                        data = json.load(f)
                        if "_metadata" in data:
                            file_info["metadata"] = data["_metadata"]
                except (json.JSONDecodeError, KeyError, OSError):
                    pass  # metadata may be missing or file partially written
                files.append(file_info)
            except Exception as e:
                debug_logger.log_warning(
                    "file_element_cloner",
                    "list_files",
                    f"Error reading {file_path}: {e}",
                )
        files.sort(key=lambda x: x["created"], reverse=True)
        return files

    def cleanup_old_files(self, max_age_hours: int = 24) -> int:
        """
        Clean up clone files older than specified hours.

        Args:
            max_age_hours (int): Maximum age of files in hours.

        Returns:
            int: Number of deleted files.
        """
        import time

        cutoff_time = time.time() - (max_age_hours * 3600)
        deleted_count = 0
        for file_path in self.output_dir.glob("*.json"):
            try:
                if file_path.stat().st_ctime < cutoff_time:
                    file_path.unlink()
                    deleted_count += 1
                    debug_logger.log_info(
                        "file_element_cloner",
                        "cleanup",
                        f"Deleted old file: {file_path.name}",
                    )
            except Exception as e:
                debug_logger.log_warning(
                    "file_element_cloner", "cleanup", f"Error deleting {file_path}: {e}"
                )
        return deleted_count


file_based_element_cloner = FileBasedElementCloner()
