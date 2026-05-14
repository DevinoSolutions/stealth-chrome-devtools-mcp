"""
Comprehensive Element Cloner - CopyIt-CDP v3 Style
===================================================

This module provides comprehensive element extraction capabilities matching CopyIt-CDP v3
functionality using proper nodriver API without JSON.stringify wrappers.

Key features:
- Complete computed styles extraction
- Event listener detection (inline, addEventListener, React/framework)
- CSS rules matching from all stylesheets
- Pseudo-element styles (::before, ::after, etc.)
- Animation and transition properties
- Framework detection and handler extraction
- Child element extraction with depth tracking
"""

import os
import sys
from typing import Dict, Any, Optional
from pathlib import Path

project_root = Path(__file__).parent
sys.path.append(str(project_root))

from debug_logger import debug_logger


class ComprehensiveElementCloner:
    """
    Comprehensive element cloner that extracts complete element data
    matching CopyIt-CDP v3 functionality using proper nodriver APIs.
    """
    
    def __init__(self):
        """Initialize the comprehensive element cloner."""
        pass
    
    async def extract_complete_element(
        self,
        tab,
        selector: str,
        include_children: bool = True
    ) -> Dict[str, Any]:
        """
        Extract complete element data matching CopyIt-CDP v3 functionality.
        
        This method extracts:
        - HTML structure with all attributes
        - Complete computed styles (all CSS properties)
        - Event listeners (inline, addEventListener, React handlers)
        - CSS rules from stylesheets that match the element
        - Pseudo-elements (::before, ::after) with their styles
        - Animations, transitions, and transforms
        - Font information
        - Child elements with depth tracking (if requested)
        - Framework detection (React, Vue, Angular handlers)
        """
        try:
            debug_logger.log_info("element_cloner", "extract_complete", f"Starting comprehensive extraction for {selector}")
            
            js_code = f"""
            (async function() {{
                async function extractSingleElement(element) {{
                    const computedStyles = window.getComputedStyle(element);
                    const styles = {{}};
                    for (let i = 0; i < computedStyles.length; i++) {{
                        const prop = computedStyles[i];
                        styles[prop] = computedStyles.getPropertyValue(prop);
                    }}
                    
                    const html = {{
                        outerHTML: element.outerHTML,
                        innerHTML: element.innerHTML,
                        tagName: element.tagName,
                        id: element.id,
                        className: element.className,
                        attributes: Array.from(element.attributes).map(attr => ({{
                            name: attr.name,
                            value: attr.value
                        }}))
                    }};
                    
                    const eventListeners = [];
                    
                    for (const attr of element.attributes) {{
                        if (attr.name.startsWith('on')) {{
                            eventListeners.push({{
                                type: attr.name.substring(2),
                                handler: attr.value,
                                source: 'inline'
                            }});
                        }}
                    }}
                    
                    if (typeof getEventListeners === 'function') {{
                        try {{
                            const listeners = getEventListeners(element);
                            for (const eventType in listeners) {{
                                listeners[eventType].forEach(listener => {{
                                    eventListeners.push({{
                                        type: eventType,
                                        handler: listener.listener.toString().substring(0, 200) + '...',
                                        useCapture: listener.useCapture,
                                        passive: listener.passive,
                                        once: listener.once,
                                        source: 'addEventListener'
                                    }});
                                }});
                            }}
                        }} catch (e) {{}}
                    }}
                    
                    const commonEvents = ['click', 'mousedown', 'mouseup', 'mouseover', 'mouseout', 'focus', 'blur', 'change', 'input', 'submit'];
                    commonEvents.forEach(eventType => {{
                        if (element[`on${{eventType}}`] && typeof element[`on${{eventType}}`] === 'function') {{
                            const handler = element[`on${{eventType}}`].toString();
                            if (!eventListeners.some(l => l.type === eventType && l.source === 'inline')) {{
                                eventListeners.push({{
                                    type: eventType,
                                    handler: handler,
                                    handlerPreview: handler.substring(0, 100) + (handler.length > 100 ? '...' : ''),
                                    source: 'property'
                                }});
                            }}
                        }}
                    }});
                    
                    try {{
                        const reactKeys = Object.keys(element).filter(key => key.startsWith('__react'));
                        if (reactKeys.length > 0) {{
                            const reactDetails = [];
                            reactKeys.forEach(key => {{
                                try {{
                                    const reactData = element[key];
                                    if (reactData && reactData.memoizedProps) {{
                                        const props = reactData.memoizedProps;
                                        Object.keys(props).forEach(prop => {{
                                            if (prop.startsWith('on') && typeof props[prop] === 'function') {{
                                                const funcStr = props[prop].toString();
                                                reactDetails.push({{
                                                    event: prop.substring(2).toLowerCase(),
                                                    handler: funcStr,
                                                    handlerPreview: funcStr.substring(0, 100) + (funcStr.length > 100 ? '...' : '')
                                                }});
                                            }}
                                        }});
                                    }}
                                }} catch (e) {{}}
                            }});
                            
                            eventListeners.push({{
                                type: 'framework',
                                handler: 'React event handlers detected',
                                source: 'react',
                                details: `Found ${{reactKeys.length}} React properties`,
                                reactHandlers: reactDetails
                            }});
                        }}
                    }} catch (e) {{}}
                    
                    const cssRules = [];
                    const sheets = document.styleSheets;
                    for (let i = 0; i < sheets.length; i++) {{
                        try {{
                            const rules = sheets[i].cssRules || sheets[i].rules;
                            for (let j = 0; j < rules.length; j++) {{
                                const rule = rules[j];
                                if (rule.type === 1 && element.matches(rule.selectorText)) {{
                                    cssRules.push({{
                                        selector: rule.selectorText,
                                        css: rule.style.cssText,
                                        source: sheets[i].href || 'inline'
                                    }});
                                }}
                            }}
                        }} catch (e) {{
                        }}
                    }}
                    
                    const pseudoElements = {{}};
                    ['::before', '::after', '::first-line', '::first-letter'].forEach(pseudo => {{
                        const pseudoStyles = window.getComputedStyle(element, pseudo);
                        const content = pseudoStyles.getPropertyValue('content');
                        if (content && content !== 'none') {{
                            pseudoElements[pseudo] = {{
                                content: content,
                                styles: {{}}
                            }};
                            for (let i = 0; i < pseudoStyles.length; i++) {{
                                const prop = pseudoStyles[i];
                                pseudoElements[pseudo].styles[prop] = pseudoStyles.getPropertyValue(prop);
                            }}
                        }}
                    }});
                    
                    const animations = {{
                        animation: styles.animation || 'none',
                        transition: styles.transition || 'none',
                        transform: styles.transform || 'none'
                    }};
                    
                    const fonts = {{
                        computed: styles.fontFamily,
                        fontSize: styles.fontSize,
                        fontWeight: styles.fontWeight
                    }};
                    
                    return {{
                        html,
                        styles,
                        eventListeners,
                        cssRules,
                        pseudoElements,
                        animations,
                        fonts
                    }};
                }}
                
                function getElementDepth(child, parent) {{
                    let depth = 0;
                    let current = child;
                    while (current && current !== parent) {{
                        depth++;
                        current = current.parentElement;
                    }}
                    return depth;
                }}
                
                function getElementPath(child, parent) {{
                    const path = [];
                    let current = child;
                    while (current && current !== parent) {{
                        const tag = current.tagName.toLowerCase();
                        const index = Array.from(current.parentElement.children)
                            .filter(el => el.tagName === current.tagName)
                            .indexOf(current);
                        path.unshift(index > 0 ? `${{tag}}[${{index}}]` : tag);
                        current = current.parentElement;
                    }}
                    return path.join(' > ');
                }}
                
                const element = document.querySelector('{selector}');
                if (!element) return null;
                
                const result = {{
                    element: await extractSingleElement(element),
                    children: []
                }};
                
                if ({str(include_children).lower()}) {{
                    let targetElement = element;
                    const children = element.querySelectorAll('*');
                    
                    if (children.length === 0 && element.parentElement) {{
                        console.log('No children found, extracting from parent element instead');
                        targetElement = element.parentElement;
                        result.extractedFrom = 'parent';
                        result.originalElement = await extractSingleElement(element);
                        result.element = await extractSingleElement(targetElement);
                    }}
                    
                    const allChildren = targetElement.querySelectorAll('*');
                    for (let i = 0; i < allChildren.length; i++) {{
                        const childData = await extractSingleElement(allChildren[i]);
                        childData.depth = getElementDepth(allChildren[i], targetElement);
                        childData.path = getElementPath(allChildren[i], targetElement);
                        if (allChildren[i] === element) {{
                            childData.isOriginallySelected = true;
                        }}
                        result.children.push(childData);
                    }}
                }}
                
                return result;
            }})()
            """
            
            debug_logger.log_info("element_cloner", "extract_complete", "Executing comprehensive JavaScript extraction")
            
            result = await tab.evaluate(js_code, return_by_value=True, await_promise=True)
            
            debug_logger.log_info("element_cloner", "extract_complete", f"Raw result type: {type(result)}")
            
            if isinstance(result, dict):
                extracted_data = result
            elif result is None:
                debug_logger.log_error("element_cloner", "extract_complete", "Element not found")
                return {"error": "Element not found", "selector": selector}
            elif hasattr(result, '__class__') and 'RemoteObject' in str(type(result)):
                debug_logger.log_info("element_cloner", "extract_complete", "Got RemoteObject, extracting value")
                if hasattr(result, 'value') and result.value is not None:
                    extracted_data = result.value
                elif hasattr(result, 'deep_serialized_value') and result.deep_serialized_value is not None:
                    deep_val = result.deep_serialized_value.value
                    debug_logger.log_info("element_cloner", "extract_complete", f"Deep serialized value type: {type(deep_val)}")
                    debug_logger.log_info("element_cloner", "extract_complete", f"Deep serialized value sample: {str(deep_val)[:300]}")
                    
                    if isinstance(deep_val, list) and len(deep_val) > 0:
                        try:
                            extracted_data = {}
                            for item in deep_val:
                                if isinstance(item, list) and len(item) == 2:
                                    key, val = item
                                    extracted_data[key] = val
                            debug_logger.log_info("element_cloner", "extract_complete", f"Converted deep serialized to dict with {len(extracted_data)} keys")
                        except Exception as e:
                            debug_logger.log_error("element_cloner", "extract_complete", f"Failed to convert deep serialized value: {e}")
                            extracted_data = {"error": f"Failed to convert deep serialized value: {e}"}
                    else:
                        extracted_data = deep_val
                else:
                    debug_logger.log_error("element_cloner", "extract_complete", "RemoteObject has no accessible value")
                    return {"error": "RemoteObject has no accessible value", "remote_object": str(result)[:200]}
            else:
                debug_logger.log_error("element_cloner", "extract_complete", f"Unexpected result type: {type(result)}")
                return {"error": f"Unexpected result type: {type(result)}", "result": str(result)[:200]}
            
            if not isinstance(extracted_data, dict):
                debug_logger.log_error("element_cloner", "extract_complete", f"Extracted data is not dict: {type(extracted_data)}")
                return {"error": f"Extracted data is not dict: {type(extracted_data)}"}
            
            final_result = {
                **extracted_data,
                "url": tab.url,
                "selector": selector,
                "timestamp": "now",
                "includesChildren": include_children
            }
            
            debug_logger.log_info("element_cloner", "extract_complete", "Comprehensive extraction completed successfully")
            return final_result
            
        except Exception as e:
            debug_logger.log_error("element_cloner", "extract_complete", f"Error during extraction: {str(e)}")
            return {
                "error": f"Extraction failed: {str(e)}",
                "selector": selector,
                "url": getattr(tab, 'url', 'unknown'),
                "timestamp": "now"
            }

comprehensive_element_cloner = ComprehensiveElementCloner()