/**
 * Extracts event handlers and framework information from a DOM element.
 * 
 * selector: string - CSS selector for the target element.
 * options: object - Extraction options:
 *   include_inline: boolean - Whether to include inline event handlers.
 *   include_framework: boolean - Whether to detect framework-specific handlers.
 *   include_listeners: boolean - Whether to detect event listeners via attributes.
 * 
 * Returns:
 *   object - {
 *     inline_handlers: Array<{event: string, handler: string}>,
 *     event_listeners: Array<{event: string, type: string, detected: boolean}>,
 *     framework_handlers: Object,
 *     detected_frameworks: Array<string>
 *   }
 */
(function() {
    const selector = "$SELECTOR$";
    const options = $OPTIONS$;
    const element = document.querySelector(selector);
    if (!element) return {error: 'Element not found'};

    const result = {
        inline_handlers: [],
        event_listeners: [],
        framework_handlers: {},
        detected_frameworks: []
    };

    if (options.include_inline) {
        const inlineEvents = [
            'onclick',
            'onmouseover',
            'onmouseout',
            'onkeydown',
            'onkeyup',
            'onchange',
            'onsubmit',
            'onfocus',
            'onblur'
        ];
        inlineEvents.forEach(event => {
            if (element[event]) {
                result.inline_handlers.push({
                    event: event,
                    handler: element[event].toString()
                });
            }
        });
    }

    if (options.include_framework) {
        const reactKeys = Object.keys(element).filter(key => key.startsWith('__react'));
        if (reactKeys.length > 0) {
            result.detected_frameworks.push('React');
            result.framework_handlers.react = {
                keys: reactKeys,
                fiber_node: reactKeys.length > 0 ? 'detected' : null
            };
        }

        if (element.__vue__ || element._vnode) {
            result.detected_frameworks.push('Vue');
            result.framework_handlers.vue = {
                instance: element.__vue__ ? 'detected' : null,
                vnode: element._vnode ? 'detected' : null
            };
        }

        if (element.ng339 || window.angular) {
            result.detected_frameworks.push('Angular');
            result.framework_handlers.angular = {
                scope: element.ng339 ? 'detected' : null
            };
        }

        if (window.jQuery && window.jQuery(element).data()) {
            result.detected_frameworks.push('jQuery');
            result.framework_handlers.jquery = {
                data: Object.keys(window.jQuery(element).data())
            };
        }
    }

    if (options.include_listeners) {
        const commonEvents = [
            'click',
            'mouseover',
            'keydown',
            'submit',
            'change'
        ];
        commonEvents.forEach(eventType => {
            try {
                if (element.hasAttribute(`on${eventType}`)) {
                    result.event_listeners.push({
                        event: eventType,
                        type: 'attribute',
                        detected: true
                    });
                }
            } catch(e) {}
        });
    }

    return result;
})();