/**
 * Extracts animation, transition and transform information from a DOM element.
 * 
 * @const selector {string} - CSS selector for the target element.
 * @const options {object} - Extraction options.
 * @const element {Element|null} - The DOM element found by selector.
 * @const result {object} - Object containing extracted animation data.
 * @returns {object} - Extracted animation and transition data, or error if not found.
 */
(function() {
    const selector = "$SELECTOR$";
    const options = $OPTIONS$;
    const element = document.querySelector(selector);
    if (!element) return { error: 'Element not found' };

    const result = {
        css_animations: [],
        css_transitions: [],
        css_transforms: {},
        keyframe_rules: []
    };

    const computed = window.getComputedStyle(element);

    if (options.include_css_animations) {
        result.css_animations = {
            name: computed.animationName || 'none',
            duration: computed.animationDuration || '0s',
            timing_function: computed.animationTimingFunction || 'ease',
            delay: computed.animationDelay || '0s',
            iteration_count: computed.animationIterationCount || '1',
            direction: computed.animationDirection || 'normal',
            fill_mode: computed.animationFillMode || 'none',
            play_state: computed.animationPlayState || 'running'
        };
    }

    if (options.include_transitions) {
        result.css_transitions = {
            property: computed.transitionProperty || 'all',
            duration: computed.transitionDuration || '0s',
            timing_function: computed.transitionTimingFunction || 'ease',
            delay: computed.transitionDelay || '0s'
        };
    }

    if (options.include_transforms) {
        result.css_transforms = {
            transform: computed.transform || 'none',
            transform_origin: computed.transformOrigin || '50% 50% 0px',
            transform_style: computed.transformStyle || 'flat',
            perspective: computed.perspective || 'none',
            perspective_origin: computed.perspectiveOrigin || '50% 50%',
            backface_visibility: computed.backfaceVisibility || 'visible'
        };
    }

    if (options.analyze_keyframes && computed.animationName !== 'none') {
        try {
            for (let i = 0; i < document.styleSheets.length; i++) {
                const stylesheet = document.styleSheets[i];
                try {
                    const rules = stylesheet.cssRules || stylesheet.rules;
                    for (let j = 0; j < rules.length; j++) {
                        const rule = rules[j];
                        if (rule.type === 7 && rule.name === computed.animationName) { // CSSKeyframesRule
                            result.keyframe_rules.push({
                                name: rule.name,
                                keyframes: Array.from(rule.cssRules).map(keyframe => ({
                                    key_text: keyframe.keyText,
                                    css_text: keyframe.style.cssText
                                }))
                            });
                        }
                    }
                } catch (e) {
                    // Cross-origin or other access issues
                }
            }
        } catch (e) {
            // Error accessing stylesheets
        }
    }

    return result;
})();