(function() {
    const selector = "$SELECTOR$";
    const options = $OPTIONS$;
    const element = document.querySelector(selector);
    if (!element) return { error: 'Element not found' };

    const result = {};

    if (options.include_computed) {
        const computed = window.getComputedStyle(element);
        result.computed_styles = {};
        for (let i = 0; i < computed.length; i++) {
            const prop = computed[i];
            result.computed_styles[prop] = computed.getPropertyValue(prop);
        }
    }

    if (options.include_css_rules) {
        result.css_rules = [];
        try {
            const styleSheets = document.styleSheets;
            for (let i = 0; i < styleSheets.length; i++) {
                try {
                    const sheet = styleSheets[i];
                    const rules = sheet.cssRules || sheet.rules;
                    for (let j = 0; j < rules.length; j++) {
                        try {
                            const rule = rules[j];
                            if (rule.selectorText && element.matches(rule.selectorText)) {
                                result.css_rules.push({
                                    selectorText: rule.selectorText,
                                    cssText: rule.cssText,
                                    specificity: calculateSpecificity(rule.selectorText),
                                    href: sheet.href || 'inline'
                                });
                            }
                        } catch (e) {}
                    }
                } catch (e) {}
            }
        } catch (e) {}
    }

    if (options.include_pseudo) {
        result.pseudo_elements = {};
        ['::before', '::after', '::first-line', '::first-letter'].forEach(pseudo => {
            try {
                const pseudoStyles = window.getComputedStyle(element, pseudo);
                const content = pseudoStyles.getPropertyValue('content');
                if (content && content !== 'none') {
                    result.pseudo_elements[pseudo] = {
                        content: content,
                        styles: {}
                    };
                    for (let i = 0; i < pseudoStyles.length; i++) {
                        const prop = pseudoStyles[i];
                        result.pseudo_elements[pseudo].styles[prop] = pseudoStyles.getPropertyValue(prop);
                    }
                }
            } catch (e) {}
        });
    }

    result.custom_properties = {};
    const computedStyles = window.getComputedStyle(element);
    for (let i = 0; i < computedStyles.length; i++) {
        const prop = computedStyles[i];
        if (prop.startsWith('--')) {
            result.custom_properties[prop] = computedStyles.getPropertyValue(prop);
        }
    }

    /**
     * Calculates CSS selector specificity.
     *
     * @param {string} selector - The CSS selector string.
     * @returns {number} Specificity value calculated as:
     *   ids * 100 + (classes + attrs + pseudos) * 10 + elements
     *   - ids: number of ID selectors (#id)
     *   - classes: number of class selectors (.class)
     *   - attrs: number of attribute selectors ([attr])
     *   - pseudos: number of pseudo-class selectors (:pseudo)
     *   - elements: number of element selectors (div, span, etc.)
     */
    function calculateSpecificity(selector) {
        const ids = (selector.match(/#[a-z_-]+/gi) || []).length;
        const classes = (selector.match(/\.[a-z_-]+/gi) || []).length;
        const attrs = (selector.match(/\[[^\]]+\]/gi) || []).length;
        const pseudos = (selector.match(/:[a-z_-]+/gi) || []).length;
        const elements = (selector.match(/^[a-z]+|\s+[a-z]+/gi) || []).length;
        return ids * 100 + (classes + attrs + pseudos) * 10 + elements;
    }

    return result;
})();