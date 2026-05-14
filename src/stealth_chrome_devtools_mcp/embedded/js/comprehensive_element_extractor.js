(function() {
    const selector = "$SELECTOR$";
    const includeChildren = $INCLUDE_CHILDREN$;

    /**
     * Extracts comprehensive information from a single DOM element.
     * @param {Element} element - The DOM element to extract data from.
     * @returns {Object} An object containing:
     *   - html: {Object} HTML details (outerHTML, innerHTML, tagName, id, className, attributes)
     *   - styles: {Object} Computed CSS styles
     *   - eventListeners: {Array} Event listeners detected by multiple methods
     *   - cssRules: {Array} CSS rules matching the element
     *   - pseudoElements: {Object} Styles and content for pseudo-elements
     *   - animations: {Object} Animation, transition, and transform properties
     *   - fonts: {Object} Font family, size, and weight
     */
    async function extractSingleElement(element) {
        const computedStyles = window.getComputedStyle(element);
        const styles = {};
        for (let i = 0; i < computedStyles.length; i++) {
            const prop = computedStyles[i];
            styles[prop] = computedStyles.getPropertyValue(prop);
        }

        const html = {
            outerHTML: element.outerHTML,
            innerHTML: element.innerHTML,
            tagName: element.tagName,
            id: element.id,
            className: element.className,
            attributes: Array.from(element.attributes).map(attr => ({
                name: attr.name,
                value: attr.value
            }))
        };

        const eventListeners = [];

        for (const attr of element.attributes) {
            if (attr.name.startsWith('on')) {
                eventListeners.push({
                    type: attr.name.substring(2),
                    handler: attr.value,
                    source: 'inline'
                });
            }
        }

        if (typeof getEventListeners === 'function') {
            try {
                const listeners = getEventListeners(element);
                for (const eventType in listeners) {
                    listeners[eventType].forEach(listener => {
                        eventListeners.push({
                            type: eventType,
                            handler: listener.listener.toString().substring(0, 200) + '...',
                            useCapture: listener.useCapture,
                            passive: listener.passive,
                            once: listener.once,
                            source: 'addEventListener'
                        });
                    });
                }
            } catch (e) {}
        }

        const commonEvents = ['click', 'mousedown', 'mouseup', 'mouseover', 'mouseout', 'focus', 'blur', 'change', 'input', 'submit'];
        commonEvents.forEach(eventType => {
            if (element[`on${eventType}`] && typeof element[`on${eventType}`] === 'function') {
                const handler = element[`on${eventType}`].toString();
                if (!eventListeners.some(l => l.type === eventType && l.source === 'inline')) {
                    eventListeners.push({
                        type: eventType,
                        handler: handler,
                        handlerPreview: handler.substring(0, 100) + (handler.length > 100 ? '...' : ''),
                        source: 'property'
                    });
                }
            }
        });

        try {
            const reactKeys = Object.keys(element).filter(key => key.startsWith('__react'));
            if (reactKeys.length > 0) {
                const reactDetails = [];
                reactKeys.forEach(key => {
                    try {
                        const reactData = element[key];
                        if (reactData && reactData.memoizedProps) {
                            const props = reactData.memoizedProps;
                            Object.keys(props).forEach(prop => {
                                if (prop.startsWith('on') && typeof props[prop] === 'function') {
                                    const funcStr = props[prop].toString();
                                    reactDetails.push({
                                        event: prop.substring(2).toLowerCase(),
                                        handler: funcStr,
                                        handlerPreview: funcStr.substring(0, 100) + (funcStr.length > 100 ? '...' : '')
                                    });
                                }
                            });
                        }
                    } catch (e) {}
                });

                eventListeners.push({
                    type: 'framework',
                    handler: 'React event handlers detected',
                    source: 'react',
                    details: `Found ${reactKeys.length} React properties`,
                    reactHandlers: reactDetails
                });
            }
        } catch (e) {}

        try {
            if (element._events || element.__events) {
                const events = element._events || element.__events;
                Object.keys(events).forEach(eventType => {
                    const handlers = Array.isArray(events[eventType]) ? events[eventType] : [events[eventType]];
                    handlers.forEach(handler => {
                        if (typeof handler === 'function') {
                            const funcStr = handler.toString();
                            eventListeners.push({
                                type: eventType,
                                handler: funcStr,
                                handlerPreview: funcStr.substring(0, 100) + (funcStr.length > 100 ? '...' : ''),
                                source: 'registry'
                            });
                        }
                    });
                });
            }
        } catch (e) {}

        const cssRules = [];
        const sheets = document.styleSheets;
        for (let i = 0; i < sheets.length; i++) {
            try {
                const rules = sheets[i].cssRules || sheets[i].rules;
                for (let j = 0; j < rules.length; j++) {
                    const rule = rules[j];
                    if (rule.type === 1 && element.matches(rule.selectorText)) {
                        cssRules.push({
                            selector: rule.selectorText,
                            css: rule.style.cssText,
                            source: sheets[i].href || 'inline'
                        });
                    }
                }
            } catch (e) {}
        }

        const pseudoElements = {};
        ['::before', '::after', '::first-line', '::first-letter'].forEach(pseudo => {
            const pseudoStyles = window.getComputedStyle(element, pseudo);
            const content = pseudoStyles.getPropertyValue('content');
            if (content && content !== 'none') {
                pseudoElements[pseudo] = {
                    content: content,
                    styles: {}
                };
                for (let i = 0; i < pseudoStyles.length; i++) {
                    const prop = pseudoStyles[i];
                    pseudoElements[pseudo].styles[prop] = pseudoStyles.getPropertyValue(prop);
                }
            }
        });

        const animations = {
            animation: styles.animation || 'none',
            transition: styles.transition || 'none',
            transform: styles.transform || 'none'
        };

        const fonts = {
            computed: styles.fontFamily,
            fontSize: styles.fontSize,
            fontWeight: styles.fontWeight
        };

        return {
            html,
            styles,
            eventListeners,
            cssRules,
            pseudoElements,
            animations,
            fonts
        };
    }

    /**
     * Calculates the depth of a child element relative to a parent element.
     * @param {Element} child - The child DOM element.
     * @param {Element} parent - The parent DOM element.
     * @returns {number} The depth (number of levels) between child and parent.
     */
    function getElementDepth(child, parent) {
        let depth = 0;
        let current = child;
        while (current && current !== parent) {
            depth++;
            current = current.parentElement;
        }
        return depth;
    }

    /**
     * Generates a CSS-like path from a child element to a parent element.
     * @param {Element} child - The child DOM element.
     * @param {Element} parent - The parent DOM element.
     * @returns {string} The path string (e.g., "div > span[1] > a").
     */
    function getElementPath(child, parent) {
        const path = [];
        let current = child;
        while (current && current !== parent) {
            const tag = current.tagName.toLowerCase();
            const index = Array.from(current.parentElement.children)
                .filter(el => el.tagName === current.tagName)
                .indexOf(current);
            path.unshift(index > 0 ? `${tag}[${index}]` : tag);
            current = current.parentElement;
        }
        return path.join(' > ');
    }

    const element = document.querySelector(selector);
    if (!element) return { error: 'Element not found' };

    const result = {
        element: null,
        children: []
    };

    result.element = extractSingleElement(element);

    if (includeChildren) {
        let targetElement = element;
        const children = element.querySelectorAll('*');

        if (children.length === 0 && element.parentElement) {
            targetElement = element.parentElement;
            result.extractedFrom = 'parent';
            result.originalElement = extractSingleElement(element);
            result.element = extractSingleElement(targetElement);
        }

        const allChildren = targetElement.querySelectorAll('*');
        for (let i = 0; i < allChildren.length; i++) {
            const childData = extractSingleElement(allChildren[i]);
            childData.depth = getElementDepth(allChildren[i], targetElement);
            childData.path = getElementPath(allChildren[i], targetElement);
            if (allChildren[i] === element) {
                childData.isOriginallySelected = true;
            }
            result.children.push(childData);
        }
    }

    return result;
})();