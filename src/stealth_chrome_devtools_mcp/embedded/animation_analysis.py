"""THE one home for the animations schema (F-848, schema v2).

Facts in, schema-v2 payload out. This module owns ``analyze()`` — the ONE
composition site — and everything that answers *what the motion is*: the
per-animation records, resolved keyframes, derived timing, static checkpoints
and the transition inventory, all read from the DECLARED CSS. What to DO about
it (edit recipes, triggers, conflicts, prose) is ``animation_advice``; what is
actually RUNNING is ``animation_waapi``; reading a CSS token or a JSON field is
``animation_facts``.

**This is derivation, not extraction.** The ONE-cloner-engine convention
(CLAUDE.md §3 / DESIGN §5) is preserved because the only call path into this
pipeline is ``cdp_element_cloner.extract_element_animations`` -> ``analyze()``.
DOM extraction still has exactly one home; this is downstream of it, is called
from nowhere else, and touches no tab, socket or file — which is what makes
every derived field unit-testable hermetically with no browser.

The pipeline is four leaves, none of which imports ``server``:

    animation_facts (read)  ->  animation_advice (what to do)
                            ->  animation_waapi  (what is running)
                            ->  this module

It is four rather than one because the derivation runs past the 1000-line cap in
``tools/check_file_budgets.py``. Each split follows a seam the spec already
draws — fidelity vs actionability, declared vs live — rather than a line count;
the budget is what made us look, not what chose the line.

The governing rule throughout is the spec's **M11 honesty rule**: a derived
field is emitted only when it is mechanically decidable from the captured facts,
and it carries a ``confidence`` or is omitted entirely. Never guessed, never
interpolated by our own code and presented as measured — a weak model quotes a
derived field verbatim and will not sanity-check it, so a field that lies on
exotic input is strictly worse than its absence.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


from stealth_chrome_devtools_mcp.embedded.animation_advice import (
    apply_stagger_groups,
    build_interactions,
    build_overview,
    build_pending,
    summarize,
    trigger_from_rules,
)
from stealth_chrome_devtools_mcp.embedded.animation_edits import (
    EDIT_PROTOCOL,
    applying_rule,
    build_edits,
    build_transition_edits,
    rule_declaring,
    source_by_id,
    source_span,
)
from stealth_chrome_devtools_mcp.embedded.animation_facts import (
    Caps,
    Derived,
    Facts,
    Record,
    as_number,
    as_obj,
    as_rows,
    as_strings,
    as_text,
    cap_message,
    caps_from,
    cycle_get,
    duration_ms,
    easing_class,
    iteration_count,
    keyframe_offsets,
    motion_kind,
    parse_declarations,
    put,
    split_css_list,
    warn,
)
from stealth_chrome_devtools_mcp.embedded.animation_waapi import (
    adopt_live_timelines,
    build_waapi,
)

CHECKPOINT_OFFSETS = (0.0, 0.25, 0.5, 0.75, 1.0)


# ── The confidence invariant (F-850) ───────────────────────────────────────
# Two shapes in the payload, one rule: our code decided it, so it states how
# sure it is, or the field is absent.
#   CLAIM_FIELDS      the field's VALUE is a claim: {"value", "confidence"}
#   JUDGEMENT_BLOCKS  a block (or list of blocks) with more to say than a bare
#                     value, carrying "confidence" alongside its own fields
# ``tests/test_animation_confidence.py`` walks the emitted payload against both,
# so a field added later that forgets its confidence reds with no test of its
# own — which is the point of registering them here rather than in the test.
CLAIM_FIELDS = frozenset({"motion_kind", "easing_class"})
JUDGEMENT_BLOCKS = frozenset({"trigger", "stagger_group", "edits", "interactions"})


# Properties that appear inside a keyframe block but describe the ANIMATION
# rather than being animated themselves.
_KEYFRAME_META = {"animation-timing-function", "animation-composition"}

# NOTE (D4, deferred): this aspect returns ``{"error": ...}`` dicts rather than
# raising ``ToolError``, matching the other five aspects of the cloner engine.
# Per DESIGN §9 the raised form is the convention, but converting ONE aspect
# would create a second error shape across the six — the "a second way is a
# defect" lens. The conversion is a single deliberate sweep across all six
# aspects at the tool boundary, and is not part of this batch (owner ruling Q4).


# ---------------------------------------------------------------------------
# Derived timing (M9 / §3.3) — the arithmetic weak models get wrong
# ---------------------------------------------------------------------------


def usable_edits(record: Record) -> bool:
    """Does this record carry a recipe a model can actually APPLY?

    A degraded recipe is a rule pointer, not an edit: it has no ``find``. Only
    counting the applicable ones keeps ``editable`` honest — a list full of
    pointers is exactly the case that has to say ``editable: false`` and why.
    """
    return any("find" in edit for edit in as_rows(record.get("edits")))


def derive_timing(timing: Record) -> Record:
    """Every number a model would otherwise have to compute, or nothing at all.

    Returns ``{}`` when the duration is not a number (a scroll/view timeline
    reports ``"auto"``): inventing a millisecond figure there is exactly the lie
    class M11 forbids. ``"infinite"`` propagates as the documented string rather
    than becoming a ``null`` that reads as "unknown" instead of "forever".
    """
    duration = as_number(timing.get("duration_ms"))
    if duration is None:
        return {}
    delay = as_number(timing.get("delay_ms")) or 0.0
    direction = as_text(timing.get("direction"), "normal")
    iterations = timing.get("iterations")
    derived: Record = {
        "iteration_ms": round(duration, 3),
        # One full there-and-back is TWO iterations when the direction alternates.
        "cycle_ms": round(
            duration * (2 if direction.startswith("alternate") else 1), 3
        ),
        # A NEGATIVE delay is not a wait that happened in the past: the
        # animation starts immediately, already that far into its first
        # iteration. Reporting -500 here said it began before the page did (R7).
        "active_start_ms": round(max(delay, 0.0), 3),
    }
    if delay < 0:
        derived["starts_at_progress_ms"] = round(-delay, 3)
    count = as_number(iterations)
    if iterations == "infinite" or count is None:
        derived["active_end_ms"] = "infinite"
        derived["total_ms"] = "infinite"
    else:
        end = round(delay + duration * count, 3)
        derived["active_end_ms"] = end
        derived["total_ms"] = end
    return derived


# ---------------------------------------------------------------------------
# Keyframe resolution (M2) and static checkpoints (§3.7)
# ---------------------------------------------------------------------------


def resolve_keyframes(rule: Record, caps: Caps) -> tuple[list[Record], bool]:
    """A captured ``@keyframes`` rule -> resolved, offset-sorted records.

    Returns ``(keyframes, truncated)``. Each record carries a numeric ``offset``,
    the per-keyframe ``easing``/``composite`` when declared, a parsed
    property->value map, and the raw ``cssText`` alongside it — so a model never
    has to re-parse ``key_text: "0%, 50%"`` plus a CSS blob in its head.
    """
    records: list[Record] = []
    for frame in as_rows(rule.get("keyframes")):
        declarations = parse_declarations(as_text(frame.get("css_text")))
        properties = {
            name: value
            for name, value in declarations.items()
            if name not in _KEYFRAME_META
        }
        easing = as_text(frame.get("easing")) or declarations.get(
            "animation-timing-function", ""
        )
        composite = as_text(frame.get("composite")) or declarations.get(
            "animation-composition", ""
        )
        for offset in keyframe_offsets(as_text(frame.get("key_text"))):
            record: Record = {"offset": offset}
            if easing:
                record["easing"] = easing
            if composite:
                record["composite"] = composite
            record["properties"] = properties
            # Chrome's re-serialization, NOT the author's bytes (it expands
            # `.7` to `0.7` and respaces): named so nothing treats it as a
            # find/replace target. Author text lives on the edit recipes.
            record["computed_css_text"] = as_text(frame.get("css_text"))
            records.append(record)
    records.sort(key=lambda r: as_number(r["offset"]) or 0.0)
    return records[: caps.keyframes], len(records) > caps.keyframes


def keyframe_rule_for(facts: Facts, name: str) -> Record | None:
    """The captured ``@keyframes`` block for ONE animation name.

    The whole point of the comma split (F-847): v1 compared the joined
    ``"pulse, spin"`` list against ``CSSKeyframesRule.name``, so the lookup
    failed for every element with more than one animation — precisely the case
    the feature exists for. The LAST matching block wins, as in the cascade.
    """
    found = None
    for rule in as_rows(facts.get("keyframe_rules")):
        if rule.get("name") == name:
            found = rule
    return found


def checkpoint_time(timing: Record) -> Callable[[float], float] | None:
    """How a keyframe OFFSET maps to wall-clock time in the first iteration.

    ``None`` when it does not map at all. The old code assumed
    ``time = duration * offset`` unconditionally, so under
    ``animation-direction: reverse`` it reported that offset 0 renders at t=0 —
    where the element actually shows the 100% keyframe. ``time_ms`` looks
    measured, so being wrong there is worse than being absent (R5).

    A non-zero ``iteration-start`` means the first iteration begins partway
    through and reaches its offsets in an order we are not going to
    reconstruct, so no time is offered for it at all.
    """
    duration = as_number(timing.get("duration_ms"))
    if duration is None or as_number(timing.get("iteration_start")):
        return None
    direction = as_text(timing.get("direction"), "normal")
    # 'alternate' plays its FIRST iteration forwards; 'alternate-reverse'
    # plays its first backwards. Later iterations swap, which is why every
    # time here is explicitly the first iteration's.
    if direction in {"reverse", "alternate-reverse"}:
        return lambda offset: round(duration * (1.0 - offset), 3)
    return lambda offset: round(duration * offset, 3)


def build_checkpoints(keyframes: list[Record], timing: Record) -> list[Record]:
    """What the element looks like at 0/25/50/75/100%, WITHOUT interpolating.

    Either the checkpoint lands on a declared keyframe (``exact: true``, with the
    declared values) or it reports the bracketing keyframes and the segment's
    easing (``exact: false``). We never compute a bezier/matrix/color value and
    present it as a measurement — for keyframe-driven animation the keyframes
    ARE the ground truth, and a model needs to know the element travels
    1 -> 1.08 -> 0.94, not the precise value at t=1500ms.
    """
    if not keyframes:
        return []
    at_time = checkpoint_time(timing)
    by_offset = {as_number(k["offset"]): k for k in keyframes}
    declared = sorted(o for o in by_offset if o is not None)
    checkpoints: list[Record] = []
    for offset in CHECKPOINT_OFFSETS:
        record: Record = {"offset": offset}
        if at_time is not None:
            record["time_ms"] = at_time(offset)
        exact = by_offset.get(offset)
        if exact is not None:
            record["exact"] = True
            record["values"] = exact.get("properties", {})
            checkpoints.append(record)
            continue
        before = [o for o in declared if o < offset]
        after = [o for o in declared if o > offset]
        if not before or not after:
            continue  # outside the declared range: say nothing rather than guess
        start, end = before[-1], after[0]
        between: Record = {"from_offset": start, "to_offset": end}
        segment_easing = by_offset[start].get("easing")
        if segment_easing:
            between["segment_easing"] = segment_easing
        record["exact"] = False
        record["between"] = between
        record["from"] = by_offset[start].get("properties", {})
        record["to"] = by_offset[end].get("properties", {})
        checkpoints.append(record)
    return checkpoints


# ---------------------------------------------------------------------------
# Timeline typing (M12)
# ---------------------------------------------------------------------------


def timeline_from_css(computed: Record) -> Record:
    """``animation-timeline`` -> the typed timeline block."""
    raw = as_text(computed.get("animation_timeline"), "auto")
    lowered = raw.lower()
    kind = "time"
    if "view(" in lowered or lowered.startswith("--view"):
        kind = "view"
    elif "scroll(" in lowered:
        kind = "scroll"
    return {"type": kind, "raw": raw}


# ---------------------------------------------------------------------------
# Per-animation records (M1) — the declared CSS animations
# ---------------------------------------------------------------------------


def _timing_from_computed(computed: Record, index: int) -> Record:
    """The declared CSS timing for animation ``index``, with list cycling."""

    def item(prop: str, default: str = "") -> str:
        return cycle_get(split_css_list(as_text(computed.get(prop))), index, default)

    duration_raw = item("animation_duration", "0s")
    delay_raw = item("animation_delay", "0s")
    timing: Record = {}
    duration = duration_ms(duration_raw)
    if duration is not None:
        timing["duration_ms"] = duration
    timing["duration_raw"] = duration_raw
    delay = duration_ms(delay_raw)
    if delay is not None:
        timing["delay_ms"] = delay
    timing["delay_raw"] = delay_raw
    iterations = iteration_count(item("animation_iteration_count", "1"))
    if iterations is not None:
        timing["iterations"] = iterations
    timing["direction"] = item("animation_direction", "normal")
    timing["fill"] = item("animation_fill_mode", "none")
    timing["easing"] = item("animation_timing_function", "ease")
    timing["play_state"] = item("animation_play_state", "running")
    composition = item("animation_composition", "")
    if composition:
        timing["composition"] = composition
    return timing


def build_animations(facts: Facts, caps: Caps) -> tuple[list[Record], bool]:
    """One record per DECLARED CSS animation on the element itself (M1).

    Today's ``"pulse, spin"`` / ``"2s, 3s"`` parallel strings are zipped HERE,
    where the cycling rule is applied once and correctly, instead of being left
    for a model that cannot know which duration belongs to which name.
    """
    computed = as_obj(facts.get("computed"))
    # The SLOT index is kept, not the position in the filtered list. Every other
    # ``animation-*`` list stays as long as ``animation-name`` and is read
    # positionally, so dropping a ``none`` slot without keeping its index hands
    # every later animation the switched-off slot's duration, delay, easing and
    # iteration count -- and addresses its edit recipe at the wrong comma item.
    # That is F-847's list-cycling defect returning by a side door: the cycling
    # rule applied correctly, to the wrong index.
    declared = split_css_list(as_text(computed.get("animation_name")))
    slots = [
        (slot, name) for slot, name in enumerate(declared) if name and name != "none"
    ]
    truncated = len(slots) > caps.animations
    rule = applying_rule(facts)
    records: list[Record] = []
    # ``id`` counts RECORDS, not slots: ``build_waapi`` numbers the live records
    # from ``len(animations)``, so a hole here would collide with anim-N.
    for position, (index, name) in enumerate(slots[: caps.animations]):
        record: Record = {
            "id": f"anim-{position}",
            "kind": "css-animation",
            "name": name,
            "target": {"relation": "self", "selector": facts.get("selector")},
            "timeline": timeline_from_css(computed),
            "timing": _timing_from_computed(computed, index),
        }
        source_refs = [rule.get("source_ref")] if rule else []
        warnings: list[Record] = []
        keyframe_rule = keyframe_rule_for(facts, name)
        if keyframe_rule is None:
            # M7: empty keyframes with no warning reads as "no keyframes exist",
            # a false negative that makes a model ADD a duplicate block.
            record["keyframes"] = []
            warnings.append(
                {
                    "code": "keyframes_not_found",
                    "message": (
                        f"No @keyframes block named '{name}' was readable; it may "
                        f"live in a cross-origin stylesheet or be injected by JS"
                    ),
                    "detail": {"name": name},
                }
            )
        else:
            keyframes, kf_truncated = resolve_keyframes(keyframe_rule, caps)
            record["keyframes"] = keyframes
            source_refs.append(keyframe_rule.get("source_ref"))
            if kf_truncated:
                warnings.append(
                    {
                        "code": "keyframe_cap_reached",
                        "message": cap_message(
                            "keyframes", caps.keyframes, "max_keyframes"
                        ),
                        "detail": {"name": name},
                    }
                )
        record["warnings"] = warnings
        record["edits"] = build_edits(facts, record, index)
        record["source_refs"] = [ref for ref in source_refs if ref]
        records.append(record)
    return records, truncated


def attach_css_edits(facts: Facts, record: Record, caps: Caps) -> None:
    """Recipes for a LIVE animation that also has a CSS origin, in place.

    A ``::before`` or descendant animation reaches us through
    ``getAnimations()``, not through the element's own computed style, so it
    misses ``build_animations``. Its rule and ``@keyframes`` are readable all
    the same — usually in the very same stylesheet — and handing back an empty
    ``edits`` list with no reason was silence where an answer existed (F-849).
    """
    name = as_text(record.get("name"))
    pseudo = as_text(as_obj(record.get("target")).get("pseudo_element"))
    rule = rule_declaring(facts, name, pseudo)
    keyframe_rule = keyframe_rule_for(facts, name)
    if keyframe_rule is not None and not as_rows(record.get("keyframes")):
        record["keyframes"], _ = resolve_keyframes(keyframe_rule, caps)
    # The declaring rule never MATCHES the element (it is on ::before, or on a
    # descendant), so the cascade scope is the one rule we identified rather
    # than the element's matched rules.
    record["edits"] = build_edits(facts, record, 0, [rule] if rule else [])
    record["source_refs"] = [
        source.get("id")
        for source in (
            source_by_id(facts, rule.get("source_ref") if rule else None),
            source_by_id(
                facts, keyframe_rule.get("source_ref") if keyframe_rule else None
            ),
        )
        if source
    ]


# ---------------------------------------------------------------------------
# WAAPI records (M4/S2) — the live truth CSS alone cannot report
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Transition inventory (S1) + default-noise suppression (M8 / D3)
# ---------------------------------------------------------------------------


def build_transitions(facts: Facts) -> list[Record]:
    """One record per transitioned longhand, with the UA default suppressed.

    D3/M8: ``all 0s ease 0s`` is what every element reports and is not
    information. A transition survives only when its duration or its delay is
    non-zero — a delay-only transition is real (it defers a later change).
    """
    computed = as_obj(facts.get("computed"))

    def parts(prop: str) -> list[str]:
        return split_css_list(as_text(computed.get(prop)))

    properties = parts("transition_property")
    durations = parts("transition_duration")
    delays = parts("transition_delay")
    easings = parts("transition_timing_function")
    behaviors = parts("transition_behavior")
    rule = applying_rule(facts, "transition")
    records: list[Record] = []
    for index, prop in enumerate(properties):
        if not prop or prop == "none":
            continue
        duration_raw = cycle_get(durations, index, "0s")
        delay_raw = cycle_get(delays, index, "0s")
        duration = duration_ms(duration_raw) or 0.0
        delay = duration_ms(delay_raw) or 0.0
        if duration == 0.0 and delay == 0.0:
            continue  # the UA default; silence beats noise
        easing = cycle_get(easings, index, "ease")
        record: Record = {
            "property": prop,
            "summary": (
                f"{prop} transitions over {duration_raw} {easing}"
                + (f" after {delay_raw}" if delay else "")
            ),
            "duration_ms": duration,
            "duration_raw": duration_raw,
            "delay_ms": delay,
            "delay_raw": delay_raw,
            "easing": easing,
        }
        put(record, "easing_class", easing_class(easing))
        behavior = cycle_get(behaviors, index, "")
        if behavior:
            record["behavior"] = behavior
        record["trigger"] = trigger_from_rules(facts, {prop})
        record["edits"] = build_transition_edits(facts, record, index)
        if rule is not None:
            record["source_refs"] = [rule.get("source_ref")]
        if not usable_edits(record):
            # Mandatory on any record with no usable recipes, whatever its kind:
            # an absent verdict reads as "editable" (R10).
            record["editable"] = False
            record["not_editable_reason"] = (
                "no readable CSS rule declares this transition; its stylesheet "
                "is likely cross-origin, so there is nothing here to find/replace"
            )
        records.append(record)
    return records


# ---------------------------------------------------------------------------
# The one composition site
# ---------------------------------------------------------------------------

_FIELD_ORDER = (
    "id",
    "summary",
    "kind",
    "name",
    "author_id",
    "target",
    "detail_level",
    "semantics",
    "timeline",
    "timing",
    "derived",
    "trigger",
    "editable",
    "not_editable_reason",
    "keyframes",
    "checkpoints",
    "edits",
    "animated_properties",
    "source_refs",
    "warnings",
)


def _ordered(animation: Record) -> Record:
    """Summary-first, bulk-last (§3.8 rule 4), so ``json.dumps`` reproduces a
    stable order — it keeps goldens diffable and keeps a model's attention on
    the summary before the raw CSS."""
    ordered = {key: animation[key] for key in _FIELD_ORDER if key in animation}
    ordered.update({k: v for k, v in animation.items() if k not in ordered})
    return ordered


def effective_easing(animation: Record, keyframes: list[Record]) -> Derived:
    """The animation's easing class, taking the KEYFRAMES into account (R6).

    A keyframe may declare its own ``animation-timing-function``, which
    overrides the animation-level curve for the segment starting there. Reading
    only ``timing.easing`` reported a confident ``"linear"`` for an animation
    whose every segment was a different curve — while the payload's own
    ``checkpoints[].between.segment_easing`` said otherwise. When the segments
    disagree, that disagreement IS the answer, and the confident field is the
    one a weak model quotes.

    The last keyframe is excluded: no segment starts there, so its declared
    easing never applies.
    """
    declared = as_text(as_obj(animation.get("timing")).get("easing"))
    segments = {as_text(frame.get("easing")) or declared for frame in keyframes[:-1]}
    if not segments:
        segments = {declared}
    if len(segments) == 1:
        return easing_class(segments.pop())
    return Derived(
        "per-keyframe",
        "high",
        "the keyframes declare their own animation-timing-function, so the "
        "animation-level curve never applies; the per-segment curves are in "
        "checkpoints[].between.segment_easing",
    )


def _enrich(animation: Record) -> None:
    """Attach the derived blocks every animation record carries, in place."""
    keyframes = as_rows(animation.get("keyframes"))
    properties = sorted(
        {prop for frame in keyframes for prop in as_obj(frame.get("properties"))}
    )
    animation["animated_properties"] = properties
    semantics: Record = {}
    put(semantics, "motion_kind", motion_kind(keyframes))
    if "motion_kind" in semantics:
        semantics["motion_properties"] = properties
    put(semantics, "easing_class", effective_easing(animation, keyframes))
    if semantics:
        animation["semantics"] = semantics
    timing = as_obj(animation.get("timing"))
    animation["derived"] = derive_timing(timing)
    animation["checkpoints"] = build_checkpoints(keyframes, timing)
    if animation["checkpoints"] and checkpoint_time(timing) is None:
        # Silence about a missing time_ms would read as "this animation has no
        # timeline"; say which input made it undecidable instead.
        warn(
            animation,
            "checkpoint_time_not_decidable",
            "checkpoints carry no time_ms: this animation's first iteration "
            "does not start at offset 0 (a non-zero iteration-start, or no "
            "numeric duration)",
            {
                "direction": timing.get("direction"),
                "iteration_start": timing.get("iteration_start"),
            },
        )


# Fields a SUMMARY record does not carry. Chosen by measurement: `edits` was
# 19.4KB and `keyframes` 3.7KB of one 24.8KB record, and on a busy page every
# oversized record was a descendant the caller never asked about (R12).
_ELIDED_FOR_SUMMARY = (
    "edits",
    "checkpoints",
    "keyframes",
    "editable",
    "not_editable_reason",
)


def summarize_detail(animation: Record) -> None:
    """Reduce a record for something the caller did not select, in place.

    The blowup case is always the subtree: ask about a grid and every animated
    cell arrives at full weight. A descendant keeps its identity, timing,
    semantics and trigger -- everything needed to decide whether to look closer
    -- and drops the frame-by-frame detail and the edit recipes.

    `detail_level` makes this VISIBLE in the record. A silently different shape
    between records would be worse than the size problem it solves: a model
    seeing no `edits` on one record and edits on another would conclude the
    first is not editable. It is not a claim about the animation, so it does not
    carry `editable` either -- `detail_level` says we did not look.
    """
    animation["detail_level"] = "summary"
    for field in _ELIDED_FOR_SUMMARY:
        animation.pop(field, None)
    # A "keyframes truncated at 20" warning on a record that now carries no
    # keyframes at all describes a cut that is not what limits this reader.
    # detail_level plus the payload's detail_note already say what was elided.
    animation["warnings"] = [
        w
        for w in as_rows(animation.get("warnings"))
        if as_text(w.get("code")) != "keyframe_cap_reached"
    ]


def wants_full_detail(animation: Record) -> bool:
    """Is this record about the element the caller actually asked for?

    Its own pseudo-elements count: ``#hero::before`` is part of ``#hero``.
    """
    relation = as_text(as_obj(animation.get("target")).get("relation"), "self")
    return relation in {"self", "pseudo"}


def _attach_trigger(facts: Facts, animation: Record) -> None:
    timeline_type = as_text(as_obj(animation.get("timeline")).get("type"), "time")
    if timeline_type in {"scroll", "view"}:
        animation["trigger"] = {"kind": timeline_type, "confidence": "high"}
    elif animation.get("kind") == "waapi":
        animation["trigger"] = {"kind": "js", "confidence": "high"}
    else:
        properties = set(as_strings(animation.get("animated_properties")))
        animation["trigger"] = trigger_from_rules(facts, properties)


def _with_author_text(facts: Facts, source: Record) -> Record:
    """A source record carrying the author's own rule text when it is readable.

    ``computed_css_text`` is Chrome's re-serialization; ``source_text`` is what
    is actually on disk. Both are useful, but only one of them is safe to
    find/replace against, so they are never conflated (F-849).
    """
    span = source_span(facts, source)
    if span is None:
        return source
    return {**source, "source_text": span.strip()}


def analyze(facts: Facts, options: Record | None = None) -> Record:
    """Collected facts -> the schema-v2 payload. THE one composition site."""
    options = options or {}
    caps = caps_from(options)
    animations, anim_truncated = build_animations(facts, caps)
    css_names = {as_text(a.get("name")) for a in animations}
    live, live_truncated = build_waapi(facts, css_names, len(animations), caps)
    anim_truncated = anim_truncated or live_truncated
    for record in live:
        if record["kind"] == "waapi":
            # A live animation with no CSS declaration has no CSS to edit, and
            # saying so IS the point (M10's negative case): a weak model handed a
            # stylesheet pointer will edit CSS that cannot affect what is running.
            record["edits"] = []
            record["source_refs"] = []
            record["editable"] = False
            record["not_editable_reason"] = (
                "set by element.animate() in JS; no CSS declaration to edit"
            )
        else:
            # A CSS animation reaching us live (::before, a descendant) still has
            # a rule and a @keyframes block to edit — find them (F-849).
            attach_css_edits(facts, record, caps)
        animations.append(record)

    adopt_live_timelines(facts, animations)
    for animation in animations:
        _enrich(animation)
    # A truncated list makes a stagger group LIE: `members` and `delays_ms`
    # would describe the 25 we kept as if they were all of them, and an
    # off-by-one stagger is exactly what these groups exist to prevent (R10).
    apply_stagger_groups(animations, complete=not anim_truncated)
    for animation in animations:
        _attach_trigger(facts, animation)
        animation["summary"] = summarize(animation)
        if not wants_full_detail(animation):
            summarize_detail(animation)
            continue
        if not usable_edits(animation) and "editable" not in animation:
            # Never hand back an empty edits list with no explanation: silence
            # reads as "nothing to do here", which is the failure M11 forbids.
            animation["editable"] = False
            animation["not_editable_reason"] = (
                "no readable CSS rule declares this animation; its stylesheet is "
                "likely cross-origin, so there is nothing here to find/replace"
            )

    warnings = as_rows(facts.get("warnings"))
    if anim_truncated:
        warnings = [
            *warnings,
            {
                "code": "animation_cap_reached",
                "message": cap_message("animations", caps.animations, "max_animations"),
                "detail": {"kept": len(animations), "cap": caps.animations},
            },
        ]
    keyframes_truncated = any(
        as_text(w.get("code")) == "keyframe_cap_reached"
        for a in animations
        for w in as_rows(a.get("warnings"))
    )
    payload: Record = {
        "schema_version": 2,
        "selector": facts.get("selector"),
        "url": facts.get("url"),
        "captured_at_ms": facts.get("captured_at_ms"),
        "has_motion": False,
        "overview": "",
        "animations": [_ordered(a) for a in animations],
        "transitions": build_transitions(facts),
        "pending_animations": build_pending(facts),
        "interactions": build_interactions(facts, animations),
        "transforms": as_obj(facts.get("transforms")),
        "sources": [_with_author_text(facts, s) for s in as_rows(facts.get("sources"))],
        "warnings": warnings,
        "edit_protocol": EDIT_PROTOCOL,
        "detail_note": (
            "Records with detail_level: 'summary' are animations on DESCENDANTS "
            "of the selected element. They carry identity, timing, semantics and "
            "trigger; keyframes, checkpoints and edit recipes are not computed "
            "for them. To get the full record for one, call this tool again with "
            "that record's target.selector."
        ),
        "caps": {
            "animations": caps.animations,
            "keyframes_per_animation": caps.keyframes,
            "truncated": {
                "animations": anim_truncated,
                "keyframes": keyframes_truncated,
            },
        },
        "options": options,
    }
    payload["has_motion"] = bool(payload["animations"] or payload["transitions"])
    payload["overview"] = build_overview(payload)
    return payload
