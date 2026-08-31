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

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

import re

from stealth_chrome_devtools_mcp.embedded.animation_advice import (
    EditTarget,
    apply_stagger_groups,
    applying_rule,
    build_edits,
    build_interactions,
    build_overview,
    build_pending,
    build_transition_edits,
    rule_declaring,
    source_by_id,
    source_span,
    summarize,
    trigger_from_rules,
)
from stealth_chrome_devtools_mcp.embedded.animation_facts import (
    Derived,
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
    put,
    split_css_list,
)

# Caps are module constants, never ``STEALTH_MCP_*`` env knobs (an unknown env
# key crashes ``get_settings()``, and these are payload shape, not deployment
# config). Overridable per call by tool arguments only.
ANIMATION_CAP = 200
KEYFRAME_CAP = 60
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


def warn(record: Record, code: str, message: str, detail: Record | None = None) -> None:
    """Append one warning to a record's ``warnings``, creating it if needed.

    THE one way an animation record grows a warning, so no site can decide to
    hardcode ``warnings: []`` and drop what it had to say (R3).
    """
    existing = record.get("warnings")
    warnings: list[Record] = as_rows(existing) if isinstance(existing, list) else []
    warnings.append({"code": code, "message": message, "detail": detail or {}})
    record["warnings"] = warnings


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
            # Chrome's re-serialization, NOT the author's bytes (it expands
            # `.7` to `0.7` and respaces): named so nothing treats it as a
            # find/replace target. Author text lives on the edit recipes.
            record["computed_css_text"] = as_text(frame.get("css_text"))
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
        record["edits"] = build_edits(
            record,
            rule,
            EditTarget(rule_source, source_span(facts, rule_source)),
            EditTarget(keyframe_source, source_span(facts, keyframe_source)),
            index,
        )
        record["source_refs"] = [ref for ref in source_refs if ref]
        records.append(record)
    return records, truncated


def attach_css_edits(facts: Facts, record: Record) -> None:
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
    rule_source = source_by_id(facts, rule.get("source_ref") if rule else None)
    keyframe_rule = keyframe_rule_for(facts, name)
    keyframe_source = source_by_id(
        facts, keyframe_rule.get("source_ref") if keyframe_rule else None
    )
    if keyframe_rule is not None and not as_rows(record.get("keyframes")):
        record["keyframes"], _ = resolve_keyframes(keyframe_rule)
    record["edits"] = build_edits(
        record,
        rule,
        EditTarget(rule_source, source_span(facts, rule_source)),
        EditTarget(keyframe_source, source_span(facts, keyframe_source)),
    )
    record["source_refs"] = [
        source.get("id") for source in (rule_source, keyframe_source) if source
    ]


# ---------------------------------------------------------------------------
# WAAPI records (M4/S2) — the live truth CSS alone cannot report
# ---------------------------------------------------------------------------


def _dash_case(name: str) -> str:
    """``backgroundColor`` -> ``background-color`` (WAAPI keyframes are camel)."""
    return re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", name).lower()


def _waapi_keyframes(raw: list[Record]) -> tuple[list[Record], bool]:
    """``effect.getKeyframes()`` entries -> the keyframe record shape the CSS
    path produces, so one consumer reads both without branching.

    Returns ``(keyframes, truncated)``: this used to slice to the cap and say
    nothing, so 240 of 300 keyframes vanished while the payload reported
    completeness (R3). The CSS path already reported its truncation; both now do.
    """
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
    return records[:KEYFRAME_CAP], len(records) > KEYFRAME_CAP


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


def build_waapi(
    facts: Facts, css_names: set[str], start_index: int
) -> tuple[list[Record], bool]:
    """Records for live animations the declared CSS did not already describe.

    A ``CSSAnimation`` on the element itself whose name is already a declared
    CSS animation is skipped: it is the SAME animation, and two records for one
    animation is the duplication this schema exists to remove. Everything else —
    ``element.animate()``, transitions, descendants, pseudo-elements — is here
    and nowhere else. v1 reported NOTHING for a running ``element.animate()``,
    telling the model an element was static while it was visibly moving.

    Returns ``(records, truncated)``. ``include_subtree`` defaults ON, so one
    call on a page section can pull in hundreds of live animations; this loop
    had NO cap at all while ``caps.truncated`` reported ``false`` (R3).
    """
    records: list[Record] = []
    room = max(ANIMATION_CAP - start_index, 0)
    truncated = False
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
        if len(records) >= room:
            truncated = True
            break
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
        keyframes, kf_truncated = _waapi_keyframes(as_rows(entry.get("keyframes")))
        record["keyframes"] = keyframes
        record["warnings"] = []
        if kf_truncated:
            warn(
                record,
                "keyframe_cap_reached",
                f"Keyframes truncated at {KEYFRAME_CAP}",
                {"name": record["name"]},
            )
        records.append(record)
    return records, truncated


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
    rule_source = source_by_id(facts, rule.get("source_ref") if rule else None)
    target = EditTarget(rule_source, source_span(facts, rule_source))
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
        record["edits"] = build_transition_edits(record, rule, target, index)
        if rule is not None:
            record["source_refs"] = [rule.get("source_ref")]
        if not as_rows(record.get("edits")):
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
    animations, anim_truncated = build_animations(facts)
    css_names = {as_text(a.get("name")) for a in animations}
    live, live_truncated = build_waapi(facts, css_names, len(animations))
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
            attach_css_edits(facts, record)
        animations.append(record)

    adopt_live_timelines(facts, animations)
    for animation in animations:
        _enrich(animation)
    apply_stagger_groups(animations)
    for animation in animations:
        _attach_trigger(facts, animation)
        animation["summary"] = summarize(animation)
        if not as_rows(animation.get("edits")) and "editable" not in animation:
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
        "sources": [_with_author_text(facts, s) for s in as_rows(facts.get("sources"))],
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
