"""THE one home for the animations schema (F-848, schema v2).

Facts in, schema-v2 payload out. This module owns ``analyze()`` — the ONE
composition site — and everything that answers *what the motion is*: the
per-animation records, resolved keyframes, derived timing, static checkpoints,
timeline typing and the transition inventory. What to DO about it (edit
recipes, triggers, conflicts, prose) is ``animation_advice``; reading a CSS
token or a JSON field is ``animation_facts``.

**This is derivation, not extraction.** The ONE-cloner-engine convention
(CLAUDE.md §3 / DESIGN §5) is preserved because the only call path into this
pipeline is ``cdp_element_cloner.extract_element_animations`` -> ``analyze()``.
DOM extraction still has exactly one home; this is downstream of it, is called
from nowhere else, and touches no tab, socket or file — which is what makes
every derived field unit-testable hermetically with no browser.

The pipeline is three leaves, none of which imports ``server``:

    animation_facts (read)  ->  animation_advice (what to do)  ->  this module

It is three rather than one because the derivation is ~950 lines of code and the
gate in ``tools/check_file_budgets.py`` caps a module at 1000 — the split
follows the seam the spec already draws between fidelity and actionability
rather than a line count.

The governing rule throughout is the spec's **M11 honesty rule**: a derived
field is emitted only when it is mechanically decidable from the captured facts,
and it carries a ``confidence`` or is omitted entirely. Never guessed, never
interpolated by our own code and presented as measured — a weak model quotes a
derived field verbatim and will not sanity-check it, so a field that lies on
exotic input is strictly worse than its absence.
"""

from __future__ import annotations

import re

from stealth_chrome_devtools_mcp.embedded.animation_advice import (
    apply_stagger_groups,
    applying_rule,
    build_edits,
    build_interactions,
    build_overview,
    build_pending,
    source_by_id,
    summarize,
    trigger_from_rules,
)
from stealth_chrome_devtools_mcp.embedded.animation_facts import (
    Facts,
    Record,
    as_number,
    as_obj,
    as_rows,
    as_strings,
    as_text,
    cycle_get,
    duration_ms,
    easing_class,
    iteration_count,
    keyframe_offsets,
    motion_kind,
    parse_declarations,
    split_css_list,
)

# Caps are module constants, never ``STEALTH_MCP_*`` env knobs (an unknown env
# key crashes ``get_settings()``, and these are payload shape, not deployment
# config). Overridable per call by tool arguments only.
ANIMATION_CAP = 200
KEYFRAME_CAP = 60
CHECKPOINT_OFFSETS = (0.0, 0.25, 0.5, 0.75, 1.0)

# Properties that appear inside a keyframe block but describe the ANIMATION
# rather than being animated themselves.
_KEYFRAME_META = {"animation-timing-function", "animation-composition"}

_TIMELINE_CLASS = {
    "DocumentTimeline": "time",
    "ScrollTimeline": "scroll",
    "ViewTimeline": "view",
}

# NOTE (D4, deferred): this aspect returns ``{"error": ...}`` dicts rather than
# raising ``ToolError``, matching the other five aspects of the cloner engine.
# Per DESIGN §9 the raised form is the convention, but converting ONE aspect
# would create a second error shape across the six — the "a second way is a
# defect" lens. The conversion is a single deliberate sweep across all six
# aspects at the tool boundary, and is not part of this batch (owner ruling Q4).


# ---------------------------------------------------------------------------
# Derived timing (M9 / §3.3) — the arithmetic weak models get wrong
# ---------------------------------------------------------------------------


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
        "active_start_ms": round(delay, 3),
    }
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


def resolve_keyframes(rule: Record) -> tuple[list[Record], bool]:
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
            record["raw_css_text"] = as_text(frame.get("css_text"))
            records.append(record)
    records.sort(key=lambda r: as_number(r["offset"]) or 0.0)
    return records[:KEYFRAME_CAP], len(records) > KEYFRAME_CAP


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


def build_checkpoints(keyframes: list[Record], duration: float | None) -> list[Record]:
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
    by_offset = {as_number(k["offset"]): k for k in keyframes}
    declared = sorted(o for o in by_offset if o is not None)
    checkpoints: list[Record] = []
    for offset in CHECKPOINT_OFFSETS:
        record: Record = {"offset": offset}
        if duration is not None:
            record["time_ms"] = round(duration * offset, 3)
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


def timeline_from_waapi(entry: Record) -> Record:
    """``Animation.timeline`` -> the typed timeline block, with axis and range.

    M12's whole point: a ``view()`` animation reports ``duration: "auto"`` and
    ``iterations: 1``, which reads as a BROKEN time animation. A model seeing
    that will "repair" a correct scroll-driven animation — the class of gap that
    produces actively harmful edits, not merely missing information.
    """
    timeline = as_obj(entry.get("timeline"))
    class_name = as_text(timeline.get("type"), "DocumentTimeline")
    block: Record = {
        "type": _TIMELINE_CLASS.get(class_name, "time"),
        "raw": class_name,
    }
    for key in ("axis", "subject_selector"):
        if timeline.get(key):
            block[key] = timeline[key]
    for key in ("range_start", "range_end"):
        if entry.get(key):
            block[key] = entry[key]
    return block


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


def build_animations(facts: Facts) -> tuple[list[Record], bool]:
    """One record per DECLARED CSS animation on the element itself (M1).

    Today's ``"pulse, spin"`` / ``"2s, 3s"`` parallel strings are zipped HERE,
    where the cycling rule is applied once and correctly, instead of being left
    for a model that cannot know which duration belongs to which name.
    """
    computed = as_obj(facts.get("computed"))
    names = [
        name
        for name in split_css_list(as_text(computed.get("animation_name")))
        if name and name != "none"
    ]
    truncated = len(names) > ANIMATION_CAP
    rule = applying_rule(facts)
    rule_source = source_by_id(facts, rule.get("source_ref") if rule else None)
    records: list[Record] = []
    for index, name in enumerate(names[:ANIMATION_CAP]):
        record: Record = {
            "id": f"anim-{index}",
            "kind": "css-animation",
            "name": name,
            "target": {"relation": "self", "selector": facts.get("selector")},
            "timeline": timeline_from_css(computed),
            "timing": _timing_from_computed(computed, index),
        }
        source_refs = [rule.get("source_ref")] if rule else []
        warnings: list[Record] = []
        keyframe_rule = keyframe_rule_for(facts, name)
        keyframe_source = None
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
            keyframes, kf_truncated = resolve_keyframes(keyframe_rule)
            record["keyframes"] = keyframes
            source_refs.append(keyframe_rule.get("source_ref"))
            keyframe_source = source_by_id(facts, keyframe_rule.get("source_ref"))
            if kf_truncated:
                warnings.append(
                    {
                        "code": "keyframe_cap_reached",
                        "message": f"Keyframes truncated at {KEYFRAME_CAP}",
                        "detail": {"name": name},
                    }
                )
        record["warnings"] = warnings
        record["edits"] = build_edits(record, rule, rule_source, keyframe_source)
        record["source_refs"] = [ref for ref in source_refs if ref]
        records.append(record)
    return records, truncated


# ---------------------------------------------------------------------------
# WAAPI records (M4/S2) — the live truth CSS alone cannot report
# ---------------------------------------------------------------------------


def _dash_case(name: str) -> str:
    """``backgroundColor`` -> ``background-color`` (WAAPI keyframes are camel)."""
    return re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", name).lower()


def _waapi_keyframes(raw: list[Record]) -> list[Record]:
    """``effect.getKeyframes()`` entries -> the keyframe record shape the CSS
    path produces, so one consumer reads both without branching."""
    records: list[Record] = []
    for frame in raw:
        offset = as_number(frame.get("computedOffset"))
        if offset is None:
            offset = as_number(frame.get("offset"))
        if offset is None:
            continue
        record: Record = {"offset": offset}
        if frame.get("easing"):
            record["easing"] = frame["easing"]
        if frame.get("composite"):
            record["composite"] = frame["composite"]
        record["properties"] = {
            _dash_case(key): as_text(value, str(value))
            for key, value in frame.items()
            if key not in {"offset", "computedOffset", "easing", "composite"}
        }
        records.append(record)
    records.sort(key=lambda r: as_number(r["offset"]) or 0.0)
    return records[:KEYFRAME_CAP]


def _timing_from_waapi(entry: Record) -> Record:
    """``getComputedTiming()`` -> the ``timing`` block the CSS path also builds.

    Durations arrive already in ms. ``duration`` is the string ``"auto"`` for a
    scroll/view timeline — kept as ``duration_raw`` with ``duration_ms``
    OMITTED, never coerced to 0 (which would read as an instant animation).
    """
    ct = as_obj(entry.get("computed_timing"))
    timing: Record = {}
    duration = as_number(ct.get("duration"))
    if duration is not None:
        timing["duration_ms"] = round(duration, 3)
        timing["duration_raw"] = f"{round(duration / 1000.0, 6)}s"
    elif ct.get("duration") is not None:
        timing["duration_raw"] = as_text(ct.get("duration"), "auto")
    delay = as_number(ct.get("delay"))
    if delay is not None:
        timing["delay_ms"] = round(delay, 3)
        timing["delay_raw"] = f"{round(delay / 1000.0, 6)}s"
    end_delay = as_number(ct.get("end_delay"))
    if end_delay:
        timing["end_delay_ms"] = round(end_delay, 3)
    if ct.get("iterations") is not None:
        timing["iterations"] = ct["iterations"]
    if ct.get("iteration_start"):
        timing["iteration_start"] = ct["iteration_start"]
    timing["direction"] = as_text(ct.get("direction"), "normal")
    timing["fill"] = as_text(ct.get("fill"), "none")
    timing["easing"] = as_text(ct.get("easing"), "linear")
    timing["play_state"] = as_text(entry.get("play_state"), "running")
    if entry.get("composite"):
        timing["composition"] = entry["composite"]
    return timing


def build_waapi(facts: Facts, css_names: set[str], start_index: int) -> list[Record]:
    """Records for live animations the declared CSS did not already describe.

    A ``CSSAnimation`` on the element itself whose name is already a declared
    CSS animation is skipped: it is the SAME animation, and two records for one
    animation is the duplication this schema exists to remove. Everything else —
    ``element.animate()``, transitions, descendants, pseudo-elements — is here
    and nowhere else. v1 reported NOTHING for a running ``element.animate()``,
    telling the model an element was static while it was visibly moving.
    """
    records: list[Record] = []
    for entry in as_rows(facts.get("waapi")):
        target = as_obj(entry.get("target"))
        name = entry.get("animation_name")
        if (
            entry.get("kind") == "CSSAnimation"
            and target.get("relation") == "self"
            and not target.get("pseudo")
            and name in css_names
        ):
            continue
        kind = {
            "CSSAnimation": "css-animation",
            "CSSTransition": "css-transition",
        }.get(as_text(entry.get("kind")), "waapi")
        record: Record = {
            "id": f"anim-{start_index + len(records)}",
            "kind": kind,
            "name": as_text(name) or as_text(entry.get("author_id")) or f"({kind})",
        }
        if entry.get("author_id"):
            record["author_id"] = entry["author_id"]
        target_block: Record = {
            "relation": target.get("relation", "self"),
            "selector": target.get("selector"),
        }
        if target.get("pseudo"):
            target_block["relation"] = "pseudo"
            target_block["pseudo_element"] = target["pseudo"]
        record["target"] = target_block
        record["timeline"] = timeline_from_waapi(entry)
        record["timing"] = _timing_from_waapi(entry)
        record["keyframes"] = _waapi_keyframes(as_rows(entry.get("keyframes")))
        record["warnings"] = []
        records.append(record)
    return records


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
        klass = easing_class(easing)
        if klass:
            record["easing_class"] = klass
        behavior = cycle_get(behaviors, index, "")
        if behavior:
            record["behavior"] = behavior
        record["trigger"] = trigger_from_rules(facts, {prop})
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


def _enrich(animation: Record) -> None:
    """Attach the derived blocks every animation record carries, in place."""
    keyframes = as_rows(animation.get("keyframes"))
    properties = sorted(
        {prop for frame in keyframes for prop in as_obj(frame.get("properties"))}
    )
    animation["animated_properties"] = properties
    semantics: Record = {}
    kind = motion_kind(keyframes)
    if kind:
        semantics["motion_kind"] = kind
        semantics["motion_properties"] = properties
    klass = easing_class(as_text(as_obj(animation.get("timing")).get("easing")))
    if klass:
        semantics["easing_class"] = klass
        semantics["easing_confidence"] = "high"
    if semantics:
        animation["semantics"] = semantics
    timing = as_obj(animation.get("timing"))
    animation["derived"] = derive_timing(timing)
    animation["checkpoints"] = build_checkpoints(
        keyframes, as_number(timing.get("duration_ms"))
    )


def adopt_live_timelines(facts: Facts, animations: list[Record]) -> None:
    """Reconcile each CSS animation with its live twin's timeline, in place.

    Two things a computed style alone gets wrong about a scroll-driven animation:

    1. ``animation-timeline: view()`` computes to a bare token, while the running
       ``Animation`` knows the axis and the resolved ``animation-range``. When
       both describe the same animation we keep the CSS record (it is the one
       with an editable source) and adopt the richer live timeline onto it.
    2. The declared ``animation-duration`` is still reported — ``1s``, say — but
       a scroll/view timeline has no millisecond duration at all; progress comes
       from the scroller. So ``duration_ms`` is REMOVED while ``duration_raw``
       keeps what the author actually wrote. Leaving that 1s sitting in a
       numeric field is the exact reading M12 exists to prevent: it is what
       makes a model "fix" a working animation by retiming it.
    """
    live_by_name: dict[str, Record] = {}
    for entry in as_rows(facts.get("waapi")):
        target = as_obj(entry.get("target"))
        if (
            entry.get("kind") == "CSSAnimation"
            and target.get("relation") == "self"
            and not target.get("pseudo")
        ):
            live_by_name[as_text(entry.get("animation_name"))] = entry
    for animation in animations:
        target = as_obj(animation.get("target"))
        entry = live_by_name.get(as_text(animation.get("name")))
        if entry is not None and target.get("relation") == "self":
            animation["timeline"] = timeline_from_waapi(entry)
        timeline_type = as_text(as_obj(animation.get("timeline")).get("type"), "time")
        timing = animation.get("timing")
        if timeline_type in {"scroll", "view"} and isinstance(timing, dict):
            timing.pop("duration_ms", None)


def _attach_trigger(facts: Facts, animation: Record) -> None:
    timeline_type = as_text(as_obj(animation.get("timeline")).get("type"), "time")
    if timeline_type in {"scroll", "view"}:
        animation["trigger"] = {"kind": timeline_type, "confidence": "high"}
    elif animation.get("kind") == "waapi":
        animation["trigger"] = {"kind": "js", "confidence": "high"}
    else:
        properties = set(as_strings(animation.get("animated_properties")))
        animation["trigger"] = trigger_from_rules(facts, properties)


def analyze(facts: Facts, options: Record | None = None) -> Record:
    """Collected facts -> the schema-v2 payload. THE one composition site."""
    options = options or {}
    animations, anim_truncated = build_animations(facts)
    css_names = {as_text(a.get("name")) for a in animations}
    for record in build_waapi(facts, css_names, len(animations)):
        if record["kind"] == "waapi":
            # A live animation with no CSS declaration has no CSS to edit, and
            # saying so IS the point (M10's negative case): a weak model handed a
            # stylesheet pointer will edit CSS that cannot affect what is running.
            record["editable"] = False
            record["not_editable_reason"] = (
                "set by element.animate() in JS; no CSS declaration to edit"
            )
        record["edits"] = []
        record["source_refs"] = []
        animations.append(record)

    adopt_live_timelines(facts, animations)
    for animation in animations:
        _enrich(animation)
    apply_stagger_groups(animations)
    for animation in animations:
        _attach_trigger(facts, animation)
        animation["summary"] = summarize(animation)

    warnings = as_rows(facts.get("warnings"))
    if anim_truncated:
        warnings = [
            *warnings,
            {
                "code": "animation_cap_reached",
                "message": f"Animations truncated at {ANIMATION_CAP}",
                "detail": {},
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
        "sources": as_rows(facts.get("sources")),
        "warnings": warnings,
        "caps": {
            "animations": ANIMATION_CAP,
            "keyframes_per_animation": KEYFRAME_CAP,
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
