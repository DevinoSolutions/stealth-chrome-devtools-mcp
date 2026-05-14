/**
 * Extracts structure and metadata from a DOM element.
 * 
 * @const selector {string} - CSS selector for the target element.
 * @const options {object} - Extraction options.
 * @const element {Element|null} - The DOM element found by selector.
 * @const result {object} - Object containing extracted element data.
 * @const rect {DOMRect} - Bounding rectangle of the element.
 * @returns {object} - Extracted structure and metadata, or error if not found.
 */
(function() {
    const selector = "$SELECTOR$";
    const options = $OPTIONS$;
    const element = document.querySelector(selector);
    if (!element) return { error: 'Element not found' };

    const result = {
        tag_name: element.tagName.toLowerCase(),
        id: element.id || null,
        class_name: element.className || null,
        class_list: Array.from(element.classList),
        text_content: element.textContent ? element.textContent.substring(0, 500) : '',
        inner_html: element.innerHTML ? element.innerHTML.substring(0, 2000) : '',
        outer_html: element.outerHTML ? element.outerHTML.substring(0, 3000) : ''
    };

    if (options.include_attributes) {
        result.attributes = {};
        result.data_attributes = {};
        for (let i = 0; i < element.attributes.length; i++) {
            const attr = element.attributes[i];
            if (attr.name.startsWith('data-')) {
                result.data_attributes[attr.name] = attr.value;
            } else {
                result.attributes[attr.name] = attr.value;
            }
        }
    }

    const rect = element.getBoundingClientRect();
    result.dimensions = {
        width: rect.width,
        height: rect.height,
        top: rect.top,
        left: rect.left,
        right: rect.right,
        bottom: rect.bottom
    };

    if (options.include_children) {
        result.children = [];
        for (let i = 0; i < Math.min(options.max_depth || 3, element.children.length); i++) {
            const child = element.children[i];
            result.children.push({
                tag_name: child.tagName.toLowerCase(),
                id: child.id || null,
                class_name: child.className || null,
                text_content: child.textContent ? child.textContent.substring(0, 100) : ''
            });
        }
    }

    result.scroll_info = {
        scroll_width: element.scrollWidth,
        scroll_height: element.scrollHeight,
        scroll_top: element.scrollTop,
        scroll_left: element.scrollLeft
    };

    return result;
})();