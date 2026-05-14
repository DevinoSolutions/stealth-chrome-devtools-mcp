(function() {
    const result = {
        stylesheets: [],
        scripts: [],
        imports: [],
        modules: []
    };
    
    const analyzeCss = $ANALYZE_CSS;
    const analyzeJs = $ANALYZE_JS;
    const followImports = $FOLLOW_IMPORTS;
    const maxDepth = $MAX_DEPTH;
    
    if (analyzeCss) {
        // Extract stylesheets
        const links = document.querySelectorAll('link[rel="stylesheet"]');
        links.forEach(link => {
            result.stylesheets.push({
                href: link.href,
                media: link.media,
                disabled: link.disabled,
                crossOrigin: link.crossOrigin,
                integrity: link.integrity
            });
        });
        
        // Extract style tags
        const styles = document.querySelectorAll('style');
        styles.forEach((style, index) => {
            result.stylesheets.push({
                type: 'inline',
                index: index,
                content: style.textContent,
                media: style.media
            });
        });
    }
    
    if (analyzeJs) {
        // Extract script tags
        const scripts = document.querySelectorAll('script');
        scripts.forEach((script, index) => {
            if (script.src) {
                result.scripts.push({
                    src: script.src,
                    type: script.type || 'text/javascript',
                    async: script.async,
                    defer: script.defer,
                    crossOrigin: script.crossOrigin,
                    integrity: script.integrity,
                    noModule: script.noModule
                });
            } else {
                result.scripts.push({
                    type: 'inline',
                    index: index,
                    content: script.textContent,
                    scriptType: script.type || 'text/javascript'
                });
            }
        });
    }
    
    // Try to detect ES6 modules and imports
    if (followImports) {
        const moduleScripts = document.querySelectorAll('script[type="module"]');
        moduleScripts.forEach((script, index) => {
            if (script.src) {
                result.modules.push({
                    src: script.src,
                    type: 'module'
                });
            } else {
                // Parse inline module for imports
                const content = script.textContent;
                const importRegex = /import\s+.*?\s+from\s+['"]([^'"]+)['"]/g;
                let match;
                while ((match = importRegex.exec(content)) !== null) {
                    result.imports.push({
                        module: match[1],
                        type: 'es6-import',
                        source: 'inline-module',
                        index: index
                    });
                }
            }
        });
    }
    
    return result;
})();