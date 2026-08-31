"""THE one home for the ACTIONABLE half of the animations schema (F-848).

``animation_analysis`` answers *what the motion is*. This module answers *what
to do about it*, which is the half the spec's weak-model lens cares about:

* **Edit recipes** (M10) — per knob, the source pointer PLUS the exact literal
  to find, verified unique in its rule at extraction time. This turns the edit
  from CSS comprehension into find/replace. Includes the negative case.
* **Trigger attribution** (§3.5) — load / hover / focus / class-toggle / scroll
  / view / js / unknown, confidence-gated. ``unknown`` is a real answer.
* **Interaction warnings** (§3.6) — the precomputed conflicts a weak model is
  bad at spotting, each with a remedy, from a closed code set.
* **Prose** (§3.1) — the per-animation ``summary`` and the top-level
  ``overview``, TEMPLATE-generated from fields already in the payload so they
  can never contradict it.

Every claim here obeys M11: mechanically decidable from captured facts, carrying
a confidence, or omitted. A middle leaf of the animations pipeline — it imports
only ``animation_facts`` and never ``server``.
"""

from __future__ import annotations

import re
from itertools import pairwise

from stealth_chrome_devtools_mcp.embedded.animation_facts import (
    Facts,
    Record,
    as_number,
    as_obj,
    as_rows,
    as_strings,
    as_text,
    split_css_list,
)

EDIT_CAP = 40
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

_INTERACTION_PSEUDO = (
    (":hover", "hover"),
    (":focus-visible", "focus"),
    (":focus-within", "focus"),
    (":focus", "focus"),
    (":active", "active"),
)


# ---------------------------------------------------------------------------
# Source pointers (M3)
# ---------------------------------------------------------------------------


def applying_rule(facts: Facts) -> Record | None:
    """The currently-matching rule that declares this element's animation.

    The LAST such rule wins, mirroring the cascade — the rule a model must edit
    is the one that actually applies, not the first one found.
    """
    found = None
    for rule in as_rows(facts.get("matched_rules")):
        if any(prop.startswith("animation") for prop in as_obj(rule.get("declares"))):
            found = rule
    return found


def source_by_id(facts: Facts, source_id: object) -> Record | None:
    if not source_id:
        return None
    for source in as_rows(facts.get("sources")):
        if source.get("id") == source_id:
            return source
    return None


def stylesheet_file(source: Record | None) -> str | None:
    """A human-usable file name for a source: the href, else the sheet index."""
    if not source:
        return None
    sheet = as_obj(source.get("stylesheet"))
    href = sheet.get("href")
    if isinstance(href, str) and href:
        return href
    return f"<style> #{sheet.get('index')}"


# ---------------------------------------------------------------------------
# Edit recipes (M10 / §3.4)
# ---------------------------------------------------------------------------


def find_literal(css_text: str, candidates: list[str]) -> tuple[str | None, bool]:
    """The first candidate that ACTUALLY OCCURS in ``css_text``, and whether it
    occurs exactly once.

    Never emit a ``find`` we did not verify: a literal that is not in the rule
    turns a find/replace into a silent no-op, and a weak model will report the
    edit as done.
    """
    for candidate in candidates:
        count = (css_text or "").count(candidate)
        if count:
            return candidate, count == 1
    return None, False


def _recipe(
    knob: str,
    current: str,
    source: Record | None,
    css_text: str,
    candidates: list[str],
) -> Record:
    """One edit recipe. ``find`` is omitted (not guessed) when unverifiable."""
    literal, unique = find_literal(css_text, candidates)
    recipe: Record = {"knob": knob, "current": current}
    if source:
        recipe["source_ref"] = source.get("id")
        recipe["file"] = stylesheet_file(source)
    if literal is None:
        recipe["confidence"] = "low"
        recipe["note"] = "no verifiable literal; edit the rule at source_ref by hand"
        return recipe
    recipe["find"] = literal
    recipe["find_unique_in_rule"] = unique
    recipe["confidence"] = "high" if unique else "medium"
    if not unique:
        recipe["note"] = "this literal repeats in the rule; match by position"
    return recipe


def build_edits(
    record: Record,
    rule: Record | None,
    rule_source: Record | None,
    keyframe_source: Record | None,
) -> list[Record]:
    """Edit recipes for one animation: the timing knobs plus every keyframe
    declaration, each with a verified write-back literal."""
    edits: list[Record] = []
    timing = as_obj(record.get("timing"))
    if rule is not None:
        rule_text = as_text(rule.get("css_text"))
        shorthand = as_obj(rule.get("declares")).get("animation")
        iterations = timing.get("iterations")
        for knob, current in (
            ("duration", timing.get("duration_raw")),
            ("delay", timing.get("delay_raw")),
            ("easing", timing.get("easing")),
            ("iterations", None if iterations is None else str(iterations)),
            ("name", record.get("name")),
        ):
            if not isinstance(current, str) or not current:
                continue
            longhand = _KNOB_LONGHAND[knob]
            candidates = [f"{longhand}: {current}", f"{longhand}:{current}"]
            if isinstance(shorthand, str) and shorthand:
                candidates.append(f"animation: {shorthand}")
            edits.append(_recipe(knob, current, rule_source, rule_text, candidates))
    if keyframe_source is not None:
        kf_text = as_text(keyframe_source.get("css_text"))
        for frame in as_rows(record.get("keyframes")):
            for prop, value in as_obj(frame.get("properties")).items():
                if len(edits) >= EDIT_CAP:
                    return edits
                text = as_text(value)
                edits.append(
                    _recipe(
                        f"keyframe[{frame['offset']}].{prop}",
                        text,
                        keyframe_source,
                        kf_text,
                        [f"{prop}: {text}", f"{prop}:{text}"],
                    )
                )
    return edits


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
        missing = [
            token
            for token in re.findall(r"\.([A-Za-z0-9_-]+)", selector)
            if token not in classes
        ]
        if not missing:
            continue
        name = split_css_list(declared)[0]
        pending.append(
            {
                "name": name,
                "summary": (
                    f"'{name}' is declared on {selector}, which does not match yet; "
                    f"add the '{missing[0]}' class to run it"
                ),
                "trigger": {
                    "kind": "class-toggle",
                    "confidence": "medium",
                    "detail": {
                        "class": missing[0],
                        "rule_selector": selector,
                        "source_ref": rule.get("source_ref"),
                    },
                },
                "source_refs": [rule.get("source_ref")],
            }
        )
    return pending


# ---------------------------------------------------------------------------
# Stagger grouping (§3.3)
# ---------------------------------------------------------------------------


def apply_stagger_groups(animations: list[Record]) -> None:
    """Group same-named animations across sibling targets, in place.

    An off-by-one stagger is a visible bug and list arithmetic is exactly what
    weak models get wrong, so the full ``delays_ms`` list is always present.
    When the deltas are NOT equal we emit ``uniform: false`` and the list, and
    NO ``delta_ms`` — a single averaged delta would be a number we invented.
    """
    groups: dict[str, list[Record]] = {}
    for animation in animations:
        groups.setdefault(as_text(animation.get("name")), []).append(animation)
    index = 0
    for name, members in groups.items():
        if len(members) < MIN_STAGGER_MEMBERS or not name:
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
        uniform = bool(deltas) and len(set(deltas)) == 1
        for position, animation in enumerate(ordered, start=1):
            group: Record = {
                "group_id": f"stagger-{index}",
                "name": name,
                "members": len(ordered),
                "position": position,
                "uniform": uniform,
                "delays_ms": delays,
            }
            if uniform:
                group["delta_ms"] = deltas[0]
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
        if "prefers-reduced-motion" in context:
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
    kind = as_text(semantics.get("motion_kind"))
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
    klass = as_text(semantics.get("easing_class"))
    if klass:
        parts.append(f"with {klass} easing")
    if as_number(timing.get("delay_ms")):
        parts.append(f"after a {timing.get('delay_raw')} delay")
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
