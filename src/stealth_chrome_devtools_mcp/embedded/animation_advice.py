"""THE one home for what an animation MEANS FOR THE READER (F-848).

``animation_analysis`` answers *what the motion is*, ``animation_edits``
answers *what bytes to change*. This module answers everything else a weak model
needs and cannot work out for itself:

* **Trigger attribution** (§3.5) — load / hover / focus / class-toggle / scroll
  / view / js / unknown, confidence-gated. ``unknown`` is a real answer.
* **Interaction warnings** (§3.6) — the precomputed conflicts a weak model is
  bad at spotting, each with a remedy, from a closed code set.
* **Prose** (§3.1) — the per-animation ``summary`` and the top-level
  ``overview``, TEMPLATE-generated from fields already in the payload so they
  can never contradict it.

Every claim here obeys M11: mechanically decidable from captured facts, carrying
a confidence, or omitted. A middle leaf of the animations pipeline — it imports
only ``animation_facts`` and ``animation_edits``, and never ``server``.
"""

from __future__ import annotations

import re
from itertools import pairwise

from stealth_chrome_devtools_mcp.embedded.animation_edits import applying_rule
from stealth_chrome_devtools_mcp.embedded.animation_facts import (
    Facts,
    Record,
    as_number,
    as_obj,
    as_rows,
    as_strings,
    as_text,
    claim_value,
    own_compound,
    prefers_reduced_motion,
    split_css_list,
)

# Two animations of the same name on different targets is the smallest thing
# that can be called a stagger.
MIN_STAGGER_MEMBERS = 2

_KNOB_LONGHAND = {
    "name": "animation-name",
    "duration": "animation-duration",
    "delay": "animation-delay",
    "easing": "animation-timing-function",
    "iterations": "animation-iteration-count",
}

_TRANSITION_LONGHAND = {
    "property": "transition-property",
    "duration": "transition-duration",
    "delay": "transition-delay",
    "easing": "transition-timing-function",
}

_INTERACTION_PSEUDO = (
    (":hover", "hover"),
    (":focus-visible", "focus"),
    (":focus-within", "focus"),
    (":focus", "focus"),
    (":active", "active"),
)


# ---------------------------------------------------------------------------
# Trigger attribution (§3.5)
# ---------------------------------------------------------------------------


def trigger_from_rules(facts: Facts, properties: set[str]) -> Record:
    """Which interaction, if any, starts this motion.

    ``unknown`` is a real answer, not a failure — a wrong attribution sends a
    model to edit something that was never the knob.
    """
    for rule in as_rows(facts.get("candidate_rules")):
        selector = as_text(rule.get("selector_text")).lower()
        if not rule.get("matches_base"):
            continue
        for token, kind in _INTERACTION_PSEUDO:
            if token in selector:
                detail: Record = {
                    "rule_selector": rule.get("selector_text"),
                    "source_ref": rule.get("source_ref"),
                }
                changed = sorted(properties & set(as_obj(rule.get("declares"))))
                if changed:
                    detail["changed_properties"] = changed
                return {"kind": kind, "confidence": "high", "detail": detail}
    rule = applying_rule(facts)
    if rule is None:
        return {"kind": "unknown", "confidence": "low"}
    return {
        "kind": "load",
        "confidence": "medium",
        "detail": {
            "rule_selector": rule.get("selector_text"),
            "source_ref": rule.get("source_ref"),
        },
    }


def build_pending(facts: Facts) -> list[Record]:
    """Animations declared on a rule the element does NOT currently match.

    The class-toggle case: ``.card.is-open`` declares the animation and the
    element lacks ``is-open``, so the actionable answer is "toggle .is-open",
    not "edit the keyframes". Gated at ``medium`` confidence because a scoped
    selector scan can mis-attribute, and reported separately from ``animations``
    so nothing here is ever mistaken for motion that is running now.
    """
    element = as_obj(facts.get("element"))
    classes = set(as_strings(element.get("classes")))
    pending: list[Record] = []
    for rule in as_rows(facts.get("candidate_rules")):
        declares = as_obj(rule.get("declares"))
        declared = declares.get("animation-name") or declares.get("animation")
        if not isinstance(declared, str) or not declared or rule.get("matches_now"):
            continue
        selector = as_text(rule.get("selector_text"))
        own = own_compound(selector)
        own_classes = re.findall(r"\.([A-Za-z0-9_-]+)", own)
        addable = [token for token in own_classes if token not in classes]
        context = [
            token
            for token in re.findall(r"\.([A-Za-z0-9_-]+)", selector)
            if token not in own_classes
        ]
        name = split_css_list(declared)[0]
        detail: Record = {
            "rule_selector": selector,
            "source_ref": rule.get("source_ref"),
        }
        if addable:
            detail["class"] = addable[0]
            trigger: Record = {
                "kind": "class-toggle",
                "confidence": "medium",
                "detail": detail,
            }
            summary = (
                f"'{name}' is declared on {selector}, which does not match yet; "
                f"add the '{addable[0]}' class to run it"
            )
        elif context:
            # R8: the missing class is on an ANCESTOR or SIBLING compound.
            # Telling a model to add it to the element is advice that cannot
            # work, and it will follow the advice rather than check.
            detail["required_context_classes"] = context
            trigger = {
                "kind": "context-required",
                "confidence": "medium",
                "detail": detail,
            }
            summary = (
                f"'{name}' is declared on {selector}, which does not match yet; "
                f"it needs '{context[0]}' on an ancestor or sibling, so adding a "
                f"class to this element cannot make it run"
            )
        else:
            # It does not match for a reason the selector text does not name
            # (an attribute, a state pseudo-class we did not model). Silence is
            # the honest answer.
            continue
        pending.append(
            {
                "name": name,
                "summary": summary,
                "trigger": trigger,
                "source_refs": [rule.get("source_ref")],
            }
        )
    return pending


# ---------------------------------------------------------------------------
# Stagger grouping (§3.3)
# ---------------------------------------------------------------------------


def stagger_group(
    group_id: str, name: str, delays: list[float], deltas: list[float], position: int
) -> Record:
    """One member's view of a stagger group, with the confidence it was derived
    with. Uniformity is a property of the delays, so it is decided here rather
    than asserted by the caller."""
    uniform = bool(deltas) and len(set(deltas)) == 1
    group: Record = {
        "group_id": group_id,
        "name": name,
        "members": len(delays),
        "position": position,
        "uniform": uniform,
        "delays_ms": delays,
        "confidence": "high",
    }
    if uniform:
        group["delta_ms"] = deltas[0]
    return group


def apply_stagger_groups(animations: list[Record], complete: bool = True) -> None:
    """Group same-named animations across sibling targets, in place.

    ``complete`` is False when the animation list hit its cap. A group computed
    from a truncated list reports the members it can see as if they were all of
    them, so `members` and `delays_ms` would both be wrong -- and an off-by-one
    stagger is precisely what these groups exist to prevent (R10/R12).

    An off-by-one stagger is a visible bug and list arithmetic is exactly what
    weak models get wrong, so the full ``delays_ms`` list is always present.
    When the deltas are NOT equal we emit ``uniform: false`` and the list, and
    NO ``delta_ms`` — a single averaged delta would be a number we invented.
    """
    if not complete:
        return
    groups: dict[str, list[Record]] = {}
    for animation in animations:
        groups.setdefault(as_text(animation.get("name")), []).append(animation)
    index = 0
    for name, members in groups.items():
        if len(members) < MIN_STAGGER_MEMBERS or not name:
            continue
        readable = [as_number(as_obj(a.get("timing")).get("delay_ms")) for a in members]
        if any(value is None for value in readable):
            # An unreadable delay used to be coerced to 0.0 before differencing,
            # so a member we could not measure produced a confident, invented
            # spacing (R10). Say nothing instead.
            continue
        ordered = sorted(
            members,
            key=lambda a: as_number(as_obj(a.get("timing")).get("delay_ms")) or 0.0,
        )
        delays = [
            round(as_number(as_obj(a.get("timing")).get("delay_ms")) or 0.0, 3)
            for a in ordered
        ]
        deltas = [round(b - a, 3) for a, b in pairwise(delays)]
        if not any(deltas):
            # Same delay on every member is a chorus, not a stagger. Reporting
            # ``{uniform: true, delta_ms: 0.0}`` invites a model to "fix" the
            # spacing of something that was never staggered (R10).
            continue
        for position, animation in enumerate(ordered, start=1):
            group = stagger_group(f"stagger-{index}", name, delays, deltas, position)
            derived = as_obj(animation.get("derived"))
            derived["stagger_group"] = group
            animation["derived"] = derived
        index += 1


# ---------------------------------------------------------------------------
# Interaction / conflict detection (§3.6) — closed code set
# ---------------------------------------------------------------------------


def _conflict(
    code: str, severity: str, involves: list[str], message: str, remedy: str
) -> Record:
    return {
        "code": code,
        "severity": severity,
        "involves": involves,
        "message": message,
        "remedy": remedy,
        "confidence": "high",
    }


def _per_animation_conflicts(
    animation: Record, inline: set[str], properties: set[str]
) -> list[Record]:
    anim_id = as_text(animation.get("id"))
    timing = as_obj(animation.get("timing"))
    records: list[Record] = []
    timeline_type = as_text(as_obj(animation.get("timeline")).get("type"), "time")
    if timeline_type in {"scroll", "view"}:
        records.append(
            _conflict(
                "scroll_timeline_duration_noop",
                "high",
                [anim_id],
                f"{anim_id} is driven by a {timeline_type}() timeline; "
                f"animation-duration is ignored.",
                "Edit animation-range (its start/end), not the duration.",
            )
        )
    if as_text(timing.get("play_state")) == "paused":
        records.append(
            _conflict(
                "paused_play_state",
                "medium",
                [anim_id],
                f"{anim_id} is paused, so nothing moves regardless of its timing.",
                "Set animation-play-state: running (or call .play()).",
            )
        )
    if as_number(timing.get("duration_ms")) == 0.0:
        records.append(
            _conflict(
                "zero_duration_animation",
                "medium",
                [anim_id],
                f"{anim_id} has a 0s duration, so its keyframes never play.",
                "Give animation-duration a non-zero time.",
            )
        )
    delay = as_number(timing.get("delay_ms")) or 0.0
    if delay > 0 and as_text(timing.get("fill")) == "none":
        records.append(
            _conflict(
                "fill_none_before_delay",
                "low",
                [anim_id],
                f"{anim_id} has animation-fill-mode: none with a {round(delay)}ms "
                f"delay, so nothing renders for the first {round(delay)}ms.",
                "Set animation-fill-mode: backwards (or both).",
            )
        )
    overridden = sorted(inline & properties)
    if overridden:
        records.append(
            _conflict(
                "inline_style_overrides",
                "high",
                [anim_id],
                f"element.style declares {', '.join(overridden)}, which {anim_id} "
                f"also animates; the stylesheet is not what renders.",
                "Remove the inline declaration, or edit it instead of the rule.",
            )
        )
    return records


def build_interactions(facts: Facts, animations: list[Record]) -> list[Record]:
    """Precomputed conflicts, each mechanically decidable from captured facts.

    Every check reads only fields that are PRESENT. An interaction that fires on
    absent data is worse than one that never fires, because a weak model will
    act on it without checking.
    """
    element = as_obj(facts.get("element"))
    inline = set(as_strings(element.get("inline_properties")))
    records: list[Record] = []
    writers: dict[str, list[str]] = {}
    for animation in animations:
        properties = {
            prop
            for frame in as_rows(animation.get("keyframes"))
            for prop in as_obj(frame.get("properties"))
        }
        for prop in properties:
            writers.setdefault(prop, []).append(as_text(animation.get("id")))
        records.extend(_per_animation_conflicts(animation, inline, properties))
    for prop, ids in sorted(writers.items()):
        unique_ids = sorted(set(ids))
        if len(unique_ids) > 1:
            records.append(
                _conflict(
                    "same_property_multi_writer",
                    "medium",
                    unique_ids,
                    f"{' and '.join(unique_ids)} both write '{prop}'.",
                    "The later name in animation-name wins; combine them or set "
                    "animation-composition: add.",
                )
            )
    rules = as_rows(facts.get("matched_rules")) + as_rows(facts.get("candidate_rules"))
    for rule in rules:
        context = " ".join(as_strings(rule.get("at_rule_context")))
        if prefers_reduced_motion(context):
            records.append(
                _conflict(
                    "reduced_motion_override",
                    "medium",
                    [],
                    f"A prefers-reduced-motion block ({context}) also declares this "
                    f"element's motion.",
                    "Edit both blocks, or reduced-motion users keep the old timing.",
                )
            )
            break
    return records


# ---------------------------------------------------------------------------
# Prose (§3.1) — templates over the payload, so they cannot contradict it
# ---------------------------------------------------------------------------

_MOTION_VERB = {
    "fade": "fades",
    "scale": "scales",
    "rotate": "rotates",
    "translate": "moves",
    "color": "changes color",
    "size": "resizes",
    "filter": "changes filter",
}


def summarize(animation: Record) -> str:
    """One quotable line per animation, built only from fields already present.

    Weak models anchor hard on prose, so this is high-leverage — and precisely
    because of that it is a template over the payload, never free text. A clause
    whose field is unknown is DROPPED rather than filled, and there are no
    adjectives that are not derived ("snappy", "subtle").
    """
    timing = as_obj(animation.get("timing"))
    semantics = as_obj(animation.get("semantics"))
    kind = as_text(claim_value(semantics, "motion_kind"))
    properties = animation.get("animated_properties")
    if kind in _MOTION_VERB:
        clause = _MOTION_VERB[kind]
    elif isinstance(properties, list) and properties:
        clause = "animates " + ", ".join(str(p) for p in properties)
    else:
        clause = "animates"
    parts = [f"{as_text(animation.get('name'))} {clause}"]
    timeline_type = as_text(as_obj(animation.get("timeline")).get("type"), "time")
    if timeline_type in {"scroll", "view"}:
        parts.append(f"driven by a {timeline_type}() timeline (duration is ignored)")
    elif timing.get("duration_raw"):
        parts.append(f"over {timing['duration_raw']}")
    iterations = timing.get("iterations")
    if iterations == "infinite":
        parts.append("infinite")
    elif isinstance(iterations, (int, float)) and iterations != 1:
        parts.append(f"{iterations}x")
    direction = as_text(timing.get("direction"))
    if direction and direction != "normal":
        parts.append(direction)
    klass = as_text(claim_value(semantics, "easing_class"))
    if klass:
        parts.append(f"with {klass} easing")
    delay = as_number(timing.get("delay_ms")) or 0.0
    if delay > 0:
        parts.append(f"after a {timing.get('delay_raw')} delay")
    elif delay < 0:
        # A negative delay is not a wait. The old truthiness check narrated it
        # as "after a -0.5s delay"; it starts immediately, already 500ms in (R7).
        parts.append(f"starting {round(-delay)}ms into the animation")
    if as_text(timing.get("play_state")) == "paused":
        parts.append("currently PAUSED")
    return ", ".join(parts)


def _count(noun: str, total: int) -> str:
    return f"{total} {noun}" if total == 1 else f"{total} {noun}s"


def build_overview(payload: Record) -> str:
    """The two-to-four sentences a model reads before any raw CSS."""
    animations = as_rows(payload.get("animations"))
    transitions = as_rows(payload.get("transitions"))
    pending = as_rows(payload.get("pending_animations"))
    if not animations and not transitions:
        message = "No animations or transitions on this element."
        if not pending:
            return message
        names = ", ".join(f"'{p.get('name')}'" for p in pending)
        return f"{message} {names} would run if the declaring rule matched."
    sentences = [
        f"{_count('animation', len(animations))} and "
        f"{_count('transition', len(transitions))}."
    ]
    named = [
        f"'{a.get('name')}' on {as_obj(a.get('target')).get('selector')}"
        for a in animations[:3]
    ]
    if named:
        sentences.append(f"Running: {'; '.join(named)}.")
    scroll_driven = [
        as_text(a.get("name"))
        for a in animations
        if as_text(as_obj(a.get("timeline")).get("type")) in {"scroll", "view"}
    ]
    if scroll_driven:
        sentences.append(
            f"Duration edits do not affect {', '.join(scroll_driven)} — change "
            f"animation-range instead."
        )
    codes = sorted(
        {as_text(c.get("code")) for c in as_rows(payload.get("interactions"))}
    )
    if codes:
        sentences.append(f"Interaction warnings: {', '.join(codes)}.")
    return " ".join(sentences)
