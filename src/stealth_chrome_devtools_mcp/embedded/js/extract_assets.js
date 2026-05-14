(function(selector, options) {
    const element = document.querySelector(selector);
    if (!element) return {error: 'Element not found'};
    
    const result = {
        images: [],
        background_images: [],
        fonts: {},
        icons: [],
        videos: [],
        audio: []
    };
    
    const includeImages = $INCLUDE_IMAGES;
    const includeBackgrounds = $INCLUDE_BACKGROUNDS;
    const includeFonts = $INCLUDE_FONTS;
    const fetchExternal = $FETCH_EXTERNAL;
    
    // Extract images
    if (includeImages) {
        const images = element.querySelectorAll('img');
        images.forEach(img => {
            if (img.src) {
                result.images.push({
                    src: img.src,
                    alt: img.alt,
                    width: img.naturalWidth,
                    height: img.naturalHeight,
                    loading: img.loading
                });
            }
        });
    }
    
    // Extract background images
    if (includeBackgrounds) {
        const computedStyle = window.getComputedStyle(element);
        const bgImage = computedStyle.backgroundImage;
        if (bgImage && bgImage !== 'none') {
            const urls = bgImage.match(/url\(["']?([^"')]+)["']?\)/g);
            if (urls) {
                urls.forEach(url => {
                    const cleanUrl = url.replace(/url\(["']?([^"')]+)["']?\)/, '$1');
                    result.background_images.push({
                        url: cleanUrl,
                        element_selector: selector
                    });
                });
            }
        }
    }
    
    // Extract font information
    if (includeFonts) {
        const computedStyle = window.getComputedStyle(element);
        result.fonts = {
            family: computedStyle.fontFamily,
            size: computedStyle.fontSize,
            weight: computedStyle.fontWeight,
            style: computedStyle.fontStyle
        };
    }
    
    // Extract videos
    const videos = element.querySelectorAll('video');
    videos.forEach(video => {
        result.videos.push({
            src: video.src,
            poster: video.poster,
            width: video.videoWidth,
            height: video.videoHeight,
            duration: video.duration
        });
    });
    
    // Extract audio
    const audios = element.querySelectorAll('audio');
    audios.forEach(audio => {
        result.audio.push({
            src: audio.src,
            duration: audio.duration
        });
    });
    
    // Extract icons (favicon, apple-touch-icon, etc.)
    const iconLinks = document.querySelectorAll('link[rel*="icon"]');
    iconLinks.forEach(link => {
        result.icons.push({
            href: link.href,
            rel: link.rel,
            sizes: link.sizes ? link.sizes.toString() : null,
            type: link.type
        });
    });
    
    return result;
})('$SELECTOR', {
    include_images: $INCLUDE_IMAGES,
    include_backgrounds: $INCLUDE_BACKGROUNDS, 
    include_fonts: $INCLUDE_FONTS,
    fetch_external: $FETCH_EXTERNAL
});