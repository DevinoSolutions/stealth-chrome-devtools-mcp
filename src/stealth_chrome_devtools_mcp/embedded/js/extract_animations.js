/**
 * animation-facts collector — the browser half of the animations aspect.
 *
 * This script COLLECTS FACTS ONLY. Every derivation (per-animation records,
 * resolved keyframes, derived timing, semantics, edit recipes, checkpoints,
 * summaries, triggers, interactions) happens in Python, in
 * ``embedded/animation_analysis.py``. Keeping the injected script a pure
 * collector keeps it small and makes every derived field unit-testable
 * hermetically from a captured JSON string, with no browser.
 *
 * TRANSPORT (F-846, same hazard class as the fixed F-844 viewport bug):
 * ``tab.evaluate`` of a non-primitive returns CDP deep-serialization
 * (``{"type":"object","value":[["name",{...}]]}``), which corrupts every nested
 * array/object. A STRING is the one shape that survives, so this returns
 * ``JSON.stringify(facts)`` and the engine ``json.loads`` it.
 *
 * INFINITY: ``JSON.stringify`` turns ``Infinity`` into ``null`` silently, and
 * ``null`` reads as "unknown" rather than "forever". ``finiteOr`` normalizes
 * Infinity -> "infinite" and NaN -> undefined (omitted) BEFORE stringify, in
 * one place. Python never re-interprets.
 *
 * @const selector {string} - CSS selector for the target element.
 * @const options {object} - Extraction options.
 * @returns {string} - JSON string of the fact payload (or of an error object).
 */
(function () {
    const selector = "$SELECTOR$";
    const options = $OPTIONS$;
    const element = document.querySelector(selector);
    if (!element) return JSON.stringify({ error: 'Element not found' });

    const TRIGGER_SCAN_CAP = 500;
    // Author text is only worth carrying if an edit recipe could use it; a
    // multi-hundred-KB sheet is a transport cost with no payoff.
    const RAW_SOURCE_CAP = 150000;
    const warnings = [];
    const sources = [];
    const rawSources = {};
    const capsHit = {};
    let sourceSeq = 0;
    let rulesScanned = 0;

    /**
     * The stylesheet's AUTHOR text, or null (F-849).
     *
     * Chrome's rule.cssText is a re-serialization: it reorders the animation
     * shorthand, expands `.68` to `0.68`, adds spaces after commas and injects
     * `running`. An edit recipe built from it matches nothing on disk. Only an
     * inline <style> exposes the bytes the author wrote -- a linked sheet's
     * ownerNode is the <link>, whose textContent is empty, and we do NOT
     * re-fetch it (owner ruling Q5: name the href and stop).
     */
    function rawSourceFor(sheet) {
        try {
            const node = sheet.ownerNode;
            if (!node || node.tagName !== 'STYLE') return null;
            const text = node.textContent || '';
            return text.length > RAW_SOURCE_CAP ? null : text;
        } catch (e) {
            return null;
        }
    }

    function warn(code, message, detail) {
        warnings.push({ code: code, message: message, detail: detail || {} });
    }

    /** Infinity -> "infinite"; NaN/non-finite -> undefined (omitted). */
    function finiteOr(value) {
        if (value === Infinity) return 'infinite';
        if (typeof value === 'number' && !isFinite(value)) return undefined;
        return value;
    }

    /** A stable, human-readable in-page path for a node. */
    function selectorPath(node) {
        if (!node || node.nodeType !== 1) return null;
        if (node.id) return '#' + node.id;
        const parts = [];
        let cur = node;
        while (cur && cur.nodeType === 1 && parts.length < 6) {
            let part = cur.localName;
            if (cur.id) { parts.unshift('#' + cur.id); break; }
            const cls = (cur.getAttribute && cur.getAttribute('class') || '').trim();
            if (cls) part += '.' + cls.split(/\s+/).slice(0, 2).join('.');
            const parent = cur.parentElement;
            if (parent) {
                const sibs = Array.prototype.filter.call(
                    parent.children, function (c) { return c.localName === cur.localName; });
                if (sibs.length > 1) part += ':nth-of-type(' + (sibs.indexOf(cur) + 1) + ')';
            }
            parts.unshift(part);
            cur = cur.parentElement;
        }
        return parts.join(' > ');
    }

    function relationTo(node) {
        if (node === element) return 'self';
        if (element.contains && node && element.contains(node)) return 'descendant';
        return 'other';
    }

    // ── computed facts (the declared CSS lists, comma-joined; Python splits) ──
    const computed = window.getComputedStyle(element);
    function cs(prop) { return computed.getPropertyValue(prop) || ''; }

    const facts = {
        facts_version: 1,
        selector: selector,
        url: document.location ? document.location.href : '',
        captured_at_ms: (window.performance && performance.now) ? performance.now() : 0,
        element: {
            tag: element.localName,
            id: element.id || '',
            classes: (element.getAttribute('class') || '').trim().split(/\s+/).filter(Boolean),
            inline_properties: Array.prototype.slice.call(element.style || []),
            is_canvas: element.localName === 'canvas'
        },
        computed: {
            animation_name: cs('animation-name'),
            animation_duration: cs('animation-duration'),
            animation_delay: cs('animation-delay'),
            animation_timing_function: cs('animation-timing-function'),
            animation_iteration_count: cs('animation-iteration-count'),
            animation_direction: cs('animation-direction'),
            animation_fill_mode: cs('animation-fill-mode'),
            animation_play_state: cs('animation-play-state'),
            animation_composition: cs('animation-composition'),
            animation_timeline: cs('animation-timeline'),
            animation_range_start: cs('animation-range-start'),
            animation_range_end: cs('animation-range-end'),
            transition_property: cs('transition-property'),
            transition_duration: cs('transition-duration'),
            transition_delay: cs('transition-delay'),
            transition_timing_function: cs('transition-timing-function'),
            transition_behavior: cs('transition-behavior')
        },
        transforms: {
            transform: cs('transform') || 'none',
            transform_origin: cs('transform-origin'),
            transform_style: cs('transform-style'),
            perspective: cs('perspective'),
            perspective_origin: cs('perspective-origin'),
            backface_visibility: cs('backface-visibility'),
            will_change: cs('will-change')
        },
        keyframe_rules: [],
        waapi: [],
        matched_rules: [],
        candidate_rules: [],
        sources: sources,
        // {sheetIndex: authorText | null} -- Python slices each rule's span out
        // of this, so the text is carried ONCE rather than per rule.
        raw_sources: rawSources,
        warnings: warnings,
        caps_hit: capsHit
    };

    if (facts.element.is_canvas) {
        warn('canvas_element', 'Motion inside a <canvas> is script-rendered and not captured', {});
    }

    // ── CSSOM walk: keyframes + the rules that declare animation/transition ──

    function sheetInfo(sheet, index) {
        const node = sheet.ownerNode;
        return {
            index: index,
            href: sheet.href || null,
            kind: sheet.href ? 'link' : (node ? 'style' : 'constructed'),
            origin: 'author',
            disabled: !!sheet.disabled,
            title: sheet.title || null
        };
    }

    function addSource(kind, sheet, sheetIdx, rulePath, atContext, name, selectorText, cssText) {
        const id = 'src-' + (sourceSeq++);
        sources.push({
            id: id,
            kind: kind,
            stylesheet: sheetInfo(sheet, sheetIdx),
            rule_path: rulePath.slice(),
            at_rule_context: atContext.slice(),
            name: name || null,
            selector_text: selectorText || null,
            // Chrome's re-serialization, NOT the author's text -- named so no
            // reader mistakes it for something to find/replace against (F-849).
            computed_css_text: cssText || '',
            source_text_available: rawSources[sheetIdx] != null
        });
        return id;
    }

    /** Declarations of interest on a style rule, plus the !important ones. */
    const ANIM_PROPS = [
        'animation', 'animation-name', 'animation-duration', 'animation-delay',
        'animation-timing-function', 'animation-iteration-count', 'animation-direction',
        'animation-fill-mode', 'animation-play-state', 'animation-composition',
        'animation-timeline', 'animation-range', 'animation-range-start', 'animation-range-end',
        'transition', 'transition-property', 'transition-duration', 'transition-delay',
        'transition-timing-function', 'transition-behavior'
    ];

    function declaredOn(style) {
        const out = {};
        const important = [];
        for (let i = 0; i < ANIM_PROPS.length; i++) {
            const p = ANIM_PROPS[i];
            const v = style.getPropertyValue(p);
            if (v) out[p] = v;
        }
        for (let i = 0; i < style.length; i++) {
            const p = style[i];
            if (style.getPropertyPriority(p) === 'important') important.push(p);
        }
        return { declares: out, important: important };
    }

    /** Strip interaction/state pseudo-classes so we can ask "would the base match". */
    const PSEUDO_RE = /::?(hover|focus|focus-visible|focus-within|active|target|checked|visited|before|after|placeholder|marker|selection|backdrop|first-line|first-letter)\b(\([^)]*\))?/g;

    function safeMatches(sel) {
        try { return element.matches(sel); } catch (e) { return false; }
    }

    function ruleFacts(rule, sheet, sheetIdx, rulePath, atContext) {
        const info = declaredOn(rule.style);
        if (Object.keys(info.declares).length === 0) return;
        const selText = rule.selectorText || '';
        const matchesNow = safeMatches(selText);
        const base = selText.replace(PSEUDO_RE, '').trim();
        const matchesBase = base && base !== selText ? safeMatches(base) : matchesNow;
        // A rule is a *candidate* when its base form is about this element but it
        // is not currently applying — the class-toggle / :hover trigger cases.
        const mentionsUs = matchesNow || matchesBase ||
            (facts.element.id && selText.indexOf('#' + facts.element.id) !== -1) ||
            facts.element.classes.some(function (c) { return selText.indexOf('.' + c) !== -1; });
        if (!mentionsUs) return;
        const srcId = addSource('rule', sheet, sheetIdx, rulePath, atContext,
            null, selText, rule.cssText || '');
        const rec = {
            source_ref: srcId,
            selector_text: selText,
            css_text: rule.cssText || '',
            declares: info.declares,
            important: info.important,
            matches_now: matchesNow,
            matches_base: matchesBase,
            at_rule_context: atContext.slice()
        };
        if (matchesNow) facts.matched_rules.push(rec);
        else facts.candidate_rules.push(rec);
    }

    function walkRules(rules, sheet, sheetIdx, rulePath, atContext) {
        for (let i = 0; i < rules.length; i++) {
            if (rulesScanned >= TRIGGER_SCAN_CAP) {
                capsHit.trigger_scan = true;
                warn('trigger_scan_cap_reached',
                    'Stylesheet rule scan stopped at the ' + TRIGGER_SCAN_CAP + '-rule cap',
                    { scanned: rulesScanned });
                return;
            }
            rulesScanned++;
            const rule = rules[i];
            const path = rulePath.concat([i]);
            const kfRules = rule.cssRules;
            if (rule.type === 7 || (rule.name && kfRules && !rule.selectorText && rule.appendRule)) {
                // CSSKeyframesRule
                const srcId = addSource('keyframes', sheet, sheetIdx, path, atContext,
                    rule.name, null, rule.cssText || '');
                const frames = [];
                for (let k = 0; k < kfRules.length; k++) {
                    const kf = kfRules[k];
                    frames.push({
                        key_text: kf.keyText || '',
                        css_text: kf.style ? kf.style.cssText : '',
                        easing: kf.style ? (kf.style.getPropertyValue('animation-timing-function') || '') : '',
                        composite: kf.style ? (kf.style.getPropertyValue('animation-composition') || '') : ''
                    });
                }
                facts.keyframe_rules.push({ name: rule.name, source_ref: srcId, keyframes: frames });
            } else if (rule.style && rule.selectorText !== undefined) {
                ruleFacts(rule, sheet, sheetIdx, path, atContext);
            } else if (kfRules) {
                // @media / @supports / @layer / @container — recurse with context.
                const cond = rule.conditionText || rule.media && rule.media.mediaText || '';
                const label = (rule.cssText || '').split('{')[0].trim();
                walkRules(kfRules, sheet, sheetIdx, path,
                    atContext.concat([label || cond]));
            }
        }
    }

    try {
        for (let s = 0; s < document.styleSheets.length; s++) {
            const sheet = document.styleSheets[s];
            let rules = null;
            try {
                rules = sheet.cssRules || sheet.rules;
            } catch (e) {
                warn('cross_origin_stylesheet',
                    'Stylesheet rules unreadable (CORS); its @keyframes and rules are not captured',
                    { index: s, href: sheet.href || null });
                continue;
            }
            if (!rules) continue;
            // Capture BEFORE walking: addSource stamps source_text_available.
            rawSources[s] = rawSourceFor(sheet);
            if (rawSources[s] == null && sheet.href) {
                warn('author_source_unavailable',
                    'Author text is not readable for this linked stylesheet, so edit '
                    + 'recipes for it carry a rule pointer instead of a find literal',
                    { index: s, href: sheet.href });
            }
            if (!sheet.href && !sheet.ownerNode) {
                warn('constructed_stylesheet_no_href',
                    'Constructed stylesheet has no href; edits must target the script that adopts it',
                    { index: s });
            }
            walkRules(rules, sheet, s, [], []);
        }
    } catch (e) {
        warn('keyframes_not_found', 'Stylesheet enumeration failed: ' + e, {});
    }

    // ── WAAPI: the live truth, including element.animate() and subtree ──

    if (options.include_waapi !== false) {
        if (typeof element.getAnimations !== 'function') {
            warn('getanimations_unavailable',
                'Element.getAnimations() is unavailable; only declared CSS was captured', {});
        } else {
            let anims = [];
            try {
                anims = element.getAnimations({ subtree: options.include_subtree !== false });
            } catch (e) {
                try { anims = element.getAnimations(); } catch (e2) { anims = []; }
            }
            for (let a = 0; a < anims.length; a++) {
                const anim = anims[a];
                const eff = anim.effect;
                const rec = {
                    kind: (anim.constructor && anim.constructor.name) || 'Animation',
                    animation_name: anim.animationName || anim.transitionProperty || null,
                    author_id: anim.id || '',
                    play_state: anim.playState,
                    playback_rate: anim.playbackRate,
                    pending: !!anim.pending,
                    replace_state: anim.replaceState || null
                };
                let tl = null;
                try { tl = anim.timeline; } catch (e) { tl = null; }
                if (tl) {
                    const tlRec = {
                        type: (tl.constructor && tl.constructor.name) || 'DocumentTimeline'
                    };
                    if (tl.axis) tlRec.axis = tl.axis;
                    if (tl.source) tlRec.subject_selector = selectorPath(tl.source);
                    if (tl.subject) tlRec.subject_selector = selectorPath(tl.subject);
                    rec.timeline = tlRec;
                }
                if (eff) {
                    let target = null;
                    try { target = eff.target; } catch (e) { target = null; }
                    rec.target = {
                        relation: relationTo(target),
                        selector: selectorPath(target),
                        pseudo: eff.pseudoElement || ''
                    };
                    try {
                        const ct = eff.getComputedTiming();
                        rec.computed_timing = {
                            delay: finiteOr(ct.delay),
                            end_delay: finiteOr(ct.endDelay),
                            fill: ct.fill,
                            iteration_start: finiteOr(ct.iterationStart),
                            iterations: finiteOr(ct.iterations),
                            duration: typeof ct.duration === 'number' ? finiteOr(ct.duration) : ct.duration,
                            direction: ct.direction,
                            easing: ct.easing,
                            active_duration: finiteOr(ct.activeDuration),
                            end_time: finiteOr(ct.endTime),
                            progress: finiteOr(ct.progress),
                            current_iteration: finiteOr(ct.currentIteration)
                        };
                    } catch (e) { /* effect without computed timing */ }
                    try {
                        const tim = eff.getTiming ? eff.getTiming() : null;
                        if (tim) rec.composite = tim.composite || eff.composite || null;
                        else rec.composite = eff.composite || null;
                    } catch (e) { rec.composite = null; }
                    try {
                        rec.keyframes = eff.getKeyframes().map(function (kf) {
                            const out = {};
                            for (const k in kf) {
                                if (kf[k] === undefined || kf[k] === null) continue;
                                out[k] = typeof kf[k] === 'number' ? finiteOr(kf[k]) : kf[k];
                            }
                            return out;
                        });
                    } catch (e) {
                        rec.keyframes = [];
                        warn('keyframes_not_found',
                            'effect.getKeyframes() threw for ' + (rec.animation_name || 'an animation'),
                            { animation_name: rec.animation_name });
                    }
                }
                try {
                    if (anim.rangeStart) rec.range_start = String(anim.rangeStart);
                    if (anim.rangeEnd) rec.range_end = String(anim.rangeEnd);
                } catch (e) { /* not a view/scroll animation */ }
                facts.waapi.push(rec);
            }
        }
    }

    return JSON.stringify(facts);
})();
