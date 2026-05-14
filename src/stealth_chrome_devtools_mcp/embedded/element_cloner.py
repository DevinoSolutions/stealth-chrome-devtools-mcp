"""Advanced element cloning system with complete styling and JS extraction."""

import asyncio
import json
import re
from typing import Dict, List, Any, Optional, Set, Union
from urllib.parse import urljoin, urlparse
from pathlib import Path
import requests

try:
    from .debug_logger import debug_logger
except ImportError:
    from debug_logger import debug_logger

class ElementCloner:
    """Advanced element cloning with full fidelity extraction."""

    def __init__(self):
        self.extracted_files = {}
        self.framework_patterns = {
            'react': [r'_react', r'__reactInternalInstance', r'__reactFiber'],
            'vue': [r'__vue__', r'_vnode', r'$el'],
            'angular': [r'ng-', r'__ngContext__', r'ɵ'],
            'jquery': [r'jQuery', r'\$\.', r'__jquery']
        }

    async def extract_element_styles(
        self,
        tab,
        element=None,
        selector: str = None,
        include_computed: bool = True,
        include_css_rules: bool = True,
        include_pseudo: bool = True,
        include_inheritance: bool = False
    ) -> Dict[str, Any]:
        """
        Extract complete styling information from an element.

        Args:
            tab (Any): Browser tab instance
            element (Any): Element object or None to use selector
            selector (str): CSS selector if element is None
            include_computed (bool): Include computed styles
            include_css_rules (bool): Include matching CSS rules
            include_pseudo (bool): Include pseudo-element styles
            include_inheritance (bool): Include style inheritance chain

        Returns:
            Dict[str, Any]: Dict with styling data
        """
        try:
            return await self.extract_element_styles_cdp(
                tab=tab,
                element=element,
                selector=selector,
                include_computed=include_computed,
                include_css_rules=include_css_rules,
                include_pseudo=include_pseudo,
                include_inheritance=include_inheritance
            )
        except Exception as e:
            debug_logger.log_error("element_cloner", "extract_styles", e)
            return {"error": str(e)}

    def _load_js_file(self, filename: str, selector: str, options: dict) -> str:
        """Load and prepare JavaScript file with template substitution"""
        js_dir = Path(__file__).parent / "js"
        js_file = js_dir / filename
        
        if not js_file.exists():
            raise FileNotFoundError(f"JavaScript file not found: {js_file}")
            
        with open(js_file, 'r', encoding='utf-8') as f:
            js_code = f.read()
            
        js_code = js_code.replace('$SELECTOR$', selector)
        js_code = js_code.replace('$SELECTOR', selector)
        js_code = js_code.replace('$OPTIONS$', json.dumps(options))
        js_code = js_code.replace('$OPTIONS', json.dumps(options))
        
        for key, value in options.items():
            placeholder_key = f'${key.upper()}'
            placeholder_value = 'true' if value else 'false'
            js_code = js_code.replace(placeholder_key, placeholder_value)
        
        return js_code

    def _convert_nodriver_result(self, data):
        """Convert nodriver's array format back to dict"""
        if isinstance(data, list) and len(data) > 0 and isinstance(data[0], list):
            result = {}
            for item in data:
                if isinstance(item, list) and len(item) == 2:
                    key = item[0]
                    value_obj = item[1]
                    if isinstance(value_obj, dict) and 'type' in value_obj:
                        if value_obj['type'] == 'string':
                            result[key] = value_obj.get('value', '')
                        elif value_obj['type'] == 'number':
                            result[key] = value_obj.get('value', 0)
                        elif value_obj['type'] == 'null':
                            result[key] = None
                        elif value_obj['type'] == 'array':
                            result[key] = value_obj.get('value', [])
                        elif value_obj['type'] == 'object':
                            result[key] = self._convert_nodriver_result(value_obj.get('value', []))
                        else:
                            result[key] = value_obj.get('value')
                    else:
                        result[key] = value_obj
            return result
        return data

    async def extract_element_structure(
        self,
        tab,
        element=None,
        selector: str = None,
        include_children: bool = False,
        include_attributes: bool = True,
        include_data_attributes: bool = True,
        max_depth: int = 3
    ) -> Dict[str, Any]:
        """
        Extract complete HTML structure and DOM information.

        Args:
            tab (Any): Browser tab instance
            element (Any): Element object or None to use selector
            selector (str): CSS selector if element is None
            include_children (bool): Include child elements
            include_attributes (bool): Include all attributes
            include_data_attributes (bool): Include data-* attributes specifically
            max_depth (int): Maximum depth for children extraction

        Returns:
            Dict[str, Any]: Dict with structure data
        """
        try:
            if not selector:
                return {"error": "Selector is required"}
                
            options = {
                'include_children': include_children,
                'include_attributes': include_attributes,
                'include_data_attributes': include_data_attributes,
                'max_depth': max_depth
            }
            
            js_code = self._load_js_file('extract_structure.js', selector, options)
            structure_data = await tab.evaluate(js_code)
            
            if hasattr(structure_data, 'exception_details'):
                return {"error": f"JavaScript error: {structure_data.exception_details}"}
            elif isinstance(structure_data, dict):
                debug_logger.log_info("element_cloner", "extract_structure", f"Extracted structure for {structure_data.get('tag_name', 'unknown')} element")
                return structure_data
            elif isinstance(structure_data, list):
                result = self._convert_nodriver_result(structure_data)
                debug_logger.log_info("element_cloner", "extract_structure", f"Extracted structure for {result.get('tag_name', 'unknown')} element")
                return result
            else:
                debug_logger.log_warning("element_cloner", "extract_structure", f"Got unexpected type: {type(structure_data)}")
                return {"error": f"Unexpected return type: {type(structure_data)}", "raw_data": str(structure_data)}
        except Exception as e:
            debug_logger.log_error("element_cloner", "extract_structure", e)
            return {"error": str(e)}

    async def extract_element_events(
        self,
        tab,
        element=None,
        selector: str = None,
        include_inline: bool = True,
        include_listeners: bool = True,
        include_framework: bool = True,
        analyze_handlers: bool = True
    ) -> Dict[str, Any]:
        """
        Extract complete event listener and JavaScript handler information.

        Args:
            tab (Any): Browser tab instance
            element (Any): Element object or None to use selector
            selector (str): CSS selector if element is None
            include_inline (bool): Include inline event handlers (onclick, etc.)
            include_listeners (bool): Include addEventListener attached handlers
            include_framework (bool): Include framework-specific handlers (React, Vue, etc.)
            analyze_handlers (bool): Analyze handler functions for details

        Returns:
            Dict[str, Any]: Dict with event data
        """
        try:
            if not selector:
                return {"error": "Selector is required"}
                
            options = {
                'include_inline': include_inline,
                'include_listeners': include_listeners,
                'include_framework': include_framework,
                'analyze_handlers': analyze_handlers
            }
            
            js_code = self._load_js_file('extract_events.js', selector, options)
            event_data = await tab.evaluate(js_code)
            
            if hasattr(event_data, 'exception_details'):
                return {"error": f"JavaScript error: {event_data.exception_details}"}
            elif isinstance(event_data, dict):
                debug_logger.log_info("element_cloner", "extract_events", f"Extracted events for element")
                return event_data
            elif isinstance(event_data, list):
                result = self._convert_nodriver_result(event_data)
                debug_logger.log_info("element_cloner", "extract_events", f"Extracted events for element")
                return result
            else:
                debug_logger.log_warning("element_cloner", "extract_events", f"Got unexpected type: {type(event_data)}")
                return {"error": f"Unexpected return type: {type(event_data)}", "raw_data": str(event_data)}
        except Exception as e:
            debug_logger.log_error("element_cloner", "extract_events", e)
            return {"error": str(e)}

    async def extract_element_animations(
        self,
        tab,
        element=None,
        selector: str = None,
        include_css_animations: bool = True,
        include_transitions: bool = True,
        include_transforms: bool = True,
        analyze_keyframes: bool = True
    ) -> Dict[str, Any]:
        """
        Extract CSS animations, transitions, and transforms.

        Args:
            tab (Any): Browser tab instance
            element (Any): Element object or None to use selector
            selector (str): CSS selector if element is None
            include_css_animations (bool): Include CSS @keyframes animations
            include_transitions (bool): Include CSS transitions
            include_transforms (bool): Include CSS transforms
            analyze_keyframes (bool): Analyze keyframe rules

        Returns:
            Dict[str, Any]: Dict with animation data
        """
        try:
            if not selector:
                return {"error": "Selector is required"}
                
            options = {
                'include_css_animations': include_css_animations,
                'include_transitions': include_transitions,
                'include_transforms': include_transforms,
                'analyze_keyframes': analyze_keyframes
            }
            
            js_code = self._load_js_file('extract_animations.js', selector, options)
            animation_data = await tab.evaluate(js_code)
            
            if hasattr(animation_data, 'exception_details'):
                return {"error": f"JavaScript error: {animation_data.exception_details}"}
            elif isinstance(animation_data, dict):
                debug_logger.log_info("element_cloner", "extract_animations", f"Extracted animations for element")
                return animation_data
            elif isinstance(animation_data, list):
                result = self._convert_nodriver_result(animation_data)
                debug_logger.log_info("element_cloner", "extract_animations", f"Extracted animations for element")
                return result
            else:
                debug_logger.log_warning("element_cloner", "extract_animations", f"Got unexpected type: {type(animation_data)}")
                return {"error": f"Unexpected return type: {type(animation_data)}", "raw_data": str(animation_data)}
        except Exception as e:
            debug_logger.log_error("element_cloner", "extract_animations", e)
            return {"error": str(e)}

    async def extract_element_assets(
        self,
        tab,
        element=None,
        selector: str = None,
        include_images: bool = True,
        include_backgrounds: bool = True,
        include_fonts: bool = True,
        fetch_external: bool = False
    ) -> Dict[str, Any]:
        """
        Extract all assets related to an element (images, fonts, etc.).

        Args:
            tab (Any): Browser tab instance
            element (Any): Element object or None to use selector
            selector (str): CSS selector if element is None
            include_images (bool): Include img src and related images
            include_backgrounds (bool): Include background images
            include_fonts (bool): Include font information
            fetch_external (bool): Whether to fetch external assets for analysis

        Returns:
            Dict[str, Any]: Dict with asset data
        """
        try:
            if not selector:
                return {"error": "Selector is required"}
                
            js_dir = Path(__file__).parent / "js"
            js_file = js_dir / "extract_assets.js"
            
            if not js_file.exists():
                return {"error": f"JavaScript file not found: {js_file}"}
                
            with open(js_file, 'r', encoding='utf-8') as f:
                js_code = f.read()
                
            js_code = js_code.replace('$SELECTOR', selector)
            js_code = js_code.replace('$INCLUDE_IMAGES', 'true' if include_images else 'false')
            js_code = js_code.replace('$INCLUDE_BACKGROUNDS', 'true' if include_backgrounds else 'false')
            js_code = js_code.replace('$INCLUDE_FONTS', 'true' if include_fonts else 'false')
            js_code = js_code.replace('$FETCH_EXTERNAL', 'true' if fetch_external else 'false')
            
            asset_data = await tab.evaluate(js_code)
            if hasattr(asset_data, 'exception_details'):
                return {"error": f"JavaScript error: {asset_data.exception_details}"}
            elif isinstance(asset_data, dict):
                pass
            elif isinstance(asset_data, list):
                # Convert nodriver's array format back to dict
                asset_data = self._convert_nodriver_result(asset_data)
            else:
                debug_logger.log_warning("element_cloner", "extract_assets", f"Got unexpected type: {type(asset_data)}")
                return {"error": f"Unexpected return type: {type(asset_data)}", "raw_data": str(asset_data)}
            
            if fetch_external and isinstance(asset_data, dict):
                asset_data['external_assets'] = {}
                for bg_img in asset_data.get('background_images', []):
                    try:
                        url = bg_img.get('url', '')
                        if url.startswith('http'):
                            response = requests.get(url, timeout=5)
                            asset_data['external_assets'][url] = {
                                'content_type': response.headers.get('content-type'),
                                'size': len(response.content),
                                'status': response.status_code
                            }
                    except Exception as e:
                        debug_logger.log_warning("element_cloner", "extract_assets", f"Could not fetch asset {url}: {e}")
            
            debug_logger.log_info("element_cloner", "extract_assets", f"Extracted assets for element")
            return asset_data
        except Exception as e:
            debug_logger.log_error("element_cloner", "extract_assets", e)
            return {"error": str(e)}

    async def extract_related_files(
        self,
        tab,
        element=None,
        selector: str = None,
        analyze_css: bool = True,
        analyze_js: bool = True,
        follow_imports: bool = False,
        max_depth: int = 2
    ) -> Dict[str, Any]:
        """
        Discover and analyze related CSS/JS files for context.

        Args:
            tab (Any): Browser tab instance
            element (Any): Element object or None to use selector
            selector (str): CSS selector if element is None
            analyze_css (bool): Analyze linked CSS files
            analyze_js (bool): Analyze linked JS files
            follow_imports (bool): Follow @import and module imports
            max_depth (int): Maximum depth for following imports

        Returns:
            Dict[str, Any]: Dict with related file data
        """
        try:
            js_dir = Path(__file__).parent / "js"
            js_file = js_dir / "extract_related_files.js"
            
            if not js_file.exists():
                return {"error": f"JavaScript file not found: {js_file}"}
                
            with open(js_file, 'r', encoding='utf-8') as f:
                js_code = f.read()
                
            js_code = js_code.replace('$ANALYZE_CSS', 'true' if analyze_css else 'false')
            js_code = js_code.replace('$ANALYZE_JS', 'true' if analyze_js else 'false')
            js_code = js_code.replace('$FOLLOW_IMPORTS', 'true' if follow_imports else 'false')
            js_code = js_code.replace('$MAX_DEPTH', str(max_depth))
            
            file_data = await tab.evaluate(js_code)
            if hasattr(file_data, 'exception_details'):
                return {"error": f"JavaScript error: {file_data.exception_details}"}
            elif isinstance(file_data, dict):
                pass
            elif isinstance(file_data, list):
                file_data = self._convert_nodriver_result(file_data)
            else:
                debug_logger.log_warning("element_cloner", "extract_related_files", f"Got unexpected type: {type(file_data)}")
                return {"error": f"Unexpected return type: {type(file_data)}", "raw_data": str(file_data)}
            
            if follow_imports and max_depth > 0 and isinstance(file_data, dict):
                await self._fetch_and_analyze_files(file_data, tab.url, max_depth)
            
            debug_logger.log_info("element_cloner", "extract_related_files", f"Found related files")
            return file_data
        except Exception as e:
            debug_logger.log_error("element_cloner", "extract_related_files", e)
            return {"error": str(e)}

    async def _fetch_and_analyze_files(self, file_data: Dict, base_url: str, max_depth: int) -> None:
        """
        Fetch and analyze external CSS/JS files for additional context.

        Args:
            file_data (Dict): Data structure containing file info
            base_url (str): Base URL for resolving relative paths
            max_depth (int): Maximum depth for following imports

        Returns:
            None
        """
        for stylesheet in file_data['stylesheets']:
            if stylesheet.get('href') and stylesheet['href'] not in self.extracted_files:
                try:
                    response = requests.get(stylesheet['href'], timeout=10)
                    if response.status_code == 200:
                        content = response.text
                        self.extracted_files[stylesheet['href']] = content
                        imports = re.findall(r'@import\s+["\']([^"\']+)["\']', content)
                        stylesheet['imports'] = []
                        for imp in imports:
                            absolute_url = urljoin(stylesheet['href'], imp)
                            stylesheet['imports'].append(absolute_url)
                        css_vars = re.findall(r'--[\w-]+:\s*[^;]+', content)
                        stylesheet['custom_properties'] = css_vars
                except Exception as e:
                    debug_logger.log_warning("element_cloner", "fetch_css", f"Could not fetch CSS file {stylesheet.get('href')}: {e}")
        for script in file_data['scripts']:
            if script.get('src') and script['src'] not in self.extracted_files:
                try:
                    response = requests.get(script['src'], timeout=10)
                    if response.status_code == 200:
                        content = response.text
                        self.extracted_files[script['src']] = content
                        script['detected_frameworks'] = []
                        for framework, patterns in self.framework_patterns.items():
                            for pattern in patterns:
                                if re.search(pattern, content, re.IGNORECASE):
                                    if framework not in script['detected_frameworks']:
                                        script['detected_frameworks'].append(framework)
                        imports = re.findall(r'import.*from\s+["\']([^"\']+)["\']', content)
                        script['module_imports'] = imports
                except Exception as e:
                    debug_logger.log_warning("element_cloner", "fetch_js", f"Could not fetch JS file {script.get('src')}: {e}")

    async def clone_element_complete(
        self,
        tab,
        element=None,
        selector: str = None,
        extraction_options: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """
        Master function that extracts all element data using specialized functions.

        Args:
            tab (Any): Browser tab instance
            element (Any): Element object or None to use selector
            selector (str): CSS selector if element is None
            extraction_options (Dict[str, Any]): Dict specifying what to extract and options for each
                Example: {
                    'styles': {'include_computed': True, 'include_pseudo': True},
                    'structure': {'include_children': True, 'max_depth': 2},
                    'events': {'include_framework': True, 'analyze_handlers': True},
                    'animations': {'analyze_keyframes': True},
                    'assets': {'fetch_external': True},
                    'related_files': {'follow_imports': True, 'max_depth': 1}
                }

        Returns:
            Dict[str, Any]: Complete element clone data
        """
        try:
            default_options = {
                'styles': {'include_computed': True, 'include_css_rules': True, 'include_pseudo': True},
                'structure': {'include_children': False, 'include_attributes': True},
                'events': {'include_framework': True, 'analyze_handlers': False},
                'animations': {'analyze_keyframes': True},
                'assets': {'fetch_external': False},
                'related_files': {'follow_imports': False}
            }
            if extraction_options:
                for key, value in extraction_options.items():
                    if key in default_options:
                        default_options[key].update(value)
                    else:
                        default_options[key] = value
            if element is None and selector:
                element = await tab.select(selector)
            if not element:
                return {"error": "Element not found"}
            result = {
                "url": tab.url,
                "timestamp": asyncio.get_event_loop().time(),
                "selector": selector,
                "extraction_options": default_options
            }
            tasks = []
            if 'styles' in default_options:
                tasks.append(('styles', self.extract_element_styles(tab, element, **default_options['styles'])))
            if 'structure' in default_options:
                tasks.append(('structure', self.extract_element_structure(tab, element, **default_options['structure'])))
            if 'events' in default_options:
                tasks.append(('events', self.extract_element_events(tab, element, **default_options['events'])))
            if 'animations' in default_options:
                tasks.append(('animations', self.extract_element_animations(tab, element, **default_options['animations'])))
            if 'assets' in default_options:
                tasks.append(('assets', self.extract_element_assets(tab, element, **default_options['assets'])))
            if 'related_files' in default_options:
                tasks.append(('related_files', self.extract_related_files(tab, **default_options['related_files'])))
            results = await asyncio.gather(*[task[1] for task in tasks], return_exceptions=True)
            for i, (name, _) in enumerate(tasks):
                if isinstance(results[i], Exception):
                    result[name] = {"error": str(results[i])}
                else:
                    result[name] = results[i]
            debug_logger.log_info("element_cloner", "clone_complete", f"Complete element clone extracted with {len(tasks)} data types")
            return result
        except Exception as e:
            debug_logger.log_error("element_cloner", "clone_complete", e)
            return {"error": str(e)}

    async def extract_element_styles_cdp(
        self,
        tab,
        element=None,
        selector: str = None,
        include_computed: bool = True,
        include_css_rules: bool = True,
        include_pseudo: bool = True,
        include_inheritance: bool = False
    ) -> Dict[str, Any]:
        """
        Extract complete styling information using direct CDP calls (no JavaScript evaluation).
        This prevents hanging issues by using nodriver's native CDP methods.

        Args:
            tab (Any): Browser tab instance
            element (Any): Element object or None to use selector
            selector (str): CSS selector if element is None
            include_computed (bool): Include computed styles
            include_css_rules (bool): Include matching CSS rules
            include_pseudo (bool): Include pseudo-element styles
            include_inheritance (bool): Include style inheritance chain

        Returns:
            Dict[str, Any]: Dict with styling data
        """
        try:
            import nodriver.cdp as cdp
            
            await tab.send(cdp.dom.enable())
            await tab.send(cdp.css.enable())
            
            if element is None and selector:
                element = await tab.select(selector)
            if not element:
                return {"error": "Element not found"}
            
            if hasattr(element, 'node_id'):
                node_id = element.node_id
            elif hasattr(element, 'backend_node_id'):
                node_info = await tab.send(cdp.dom.describe_node(backend_node_id=element.backend_node_id))
                node_id = node_info.node.node_id
            else:
                return {"error": "Could not get node ID from element"}
            
            result = {"method": "cdp_direct"}
            
            if include_computed:
                debug_logger.log_info("element_cloner", "extract_styles_cdp", "Getting computed styles via CDP")
                computed_styles_list = await tab.send(cdp.css.get_computed_style_for_node(node_id))
                result["computed_styles"] = {prop.name: prop.value for prop in computed_styles_list}
                
            if include_css_rules:
                debug_logger.log_info("element_cloner", "extract_styles_cdp", "Getting matched styles via CDP")
                matched_styles = await tab.send(cdp.css.get_matched_styles_for_node(node_id))
                
                # Extract CSS rules from matched styles
                result["css_rules"] = []
                if matched_styles[2]:  # matchedCSSRules
                    for rule_match in matched_styles[2]:
                        if rule_match.rule and rule_match.rule.style:
                            result["css_rules"].append({
                                "selector": rule_match.rule.selector_list.text if rule_match.rule.selector_list else "unknown",
                                "css_text": rule_match.rule.style.css_text or "",
                                "source": rule_match.rule.origin.value if rule_match.rule.origin else "unknown"
                            })
                
                # Add inline styles if present
                if matched_styles[0]:  # inlineStyle
                    result["inline_style"] = {
                        "css_text": matched_styles[0].css_text or "",
                        "properties": len(matched_styles[0].css_properties) if matched_styles[0].css_properties else 0
                    }
                    
                # Add attribute styles if present  
                if matched_styles[1]:  # attributesStyle
                    result["attributes_style"] = {
                        "css_text": matched_styles[1].css_text or "",
                        "properties": len(matched_styles[1].css_properties) if matched_styles[1].css_properties else 0
                    }
            
            # Handle pseudo elements (if available in matched_styles)
            if include_pseudo and len(matched_styles) > 3 and matched_styles[3]:
                result["pseudo_elements"] = {}
                for pseudo_match in matched_styles[3]:
                    if pseudo_match.pseudo_type:
                        result["pseudo_elements"][pseudo_match.pseudo_type.value] = {
                            "matches": len(pseudo_match.matches) if pseudo_match.matches else 0
                        }
            
            # Handle inheritance (if available in matched_styles)
            if include_inheritance and len(matched_styles) > 4 and matched_styles[4]:
                result["inheritance_chain"] = []
                for inherited_entry in matched_styles[4]:
                    if inherited_entry.inline_style:
                        result["inheritance_chain"].append({
                            "inline_css": inherited_entry.inline_style.css_text or "",
                            "properties": len(inherited_entry.inline_style.css_properties) if inherited_entry.inline_style.css_properties else 0
                        })
            
            debug_logger.log_info("element_cloner", "extract_styles_cdp", f"CDP extraction completed with {len(result.get('css_rules', []))} CSS rules")
            return result
            
        except Exception as e:
            debug_logger.log_error("element_cloner", "extract_styles_cdp", e)
            return {"error": f"CDP extraction failed: {str(e)}"}

element_cloner = ElementCloner()
