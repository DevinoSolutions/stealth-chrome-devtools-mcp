"""THE one home for reading the live ``Animation`` objects (WAAPI, F-848).

Everything that is running right now rather than merely declared: the animations
``element.animate()`` created, CSS animations and transitions as the engine
actually resolved them, and the timeline each one is driven by. The declared-CSS
half of the same question is ``animation_analysis``; this module exists beside
it because the two read different sources and answer differently when they
disagree — and because ``analyze()``'s module was at its 1000-line budget, which
is the gate telling us the seam was already there.

The reconciliation between the two lives here too (``adopt_live_timelines``):
which of the two readings wins for a scroll-driven animation is a fact about the
LIVE object, so it belongs on this side of the seam.

A leaf: it imports only ``animation_facts`` and is called only from
``animation_analysis.analyze()``. It touches no tab, socket or file.
"""

from __future__ import annotations

import re

from stealth_chrome_devtools_mcp.embedded.animation_facts import (
    Caps,
    Facts,
    Record,
    as_number,
    as_obj,
    as_rows,
    as_text,
    cap_message,
    warn,
)

_TIMELINE_CLASS = {
    "DocumentTimeline": "time",
    "ScrollTimeline": "scroll",
    "ViewTimeline": "view",
}


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


def _dash_case(name: str) -> str:
    """``backgroundColor`` -> ``background-color`` (WAAPI keyframes are camel)."""
    return re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", name).lower()


def _waapi_keyframes(raw: list[Record], caps: Caps) -> tuple[list[Record], bool]:
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
    return records[: caps.keyframes], len(records) > caps.keyframes


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
    facts: Facts, css_names: set[str], start_index: int, caps: Caps
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
    room = max(caps.animations - start_index, 0)
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
        keyframes, kf_truncated = _waapi_keyframes(
            as_rows(entry.get("keyframes")), caps
        )
        record["keyframes"] = keyframes
        record["warnings"] = []
        if kf_truncated:
            warn(
                record,
                "keyframe_cap_reached",
                cap_message("keyframes", caps.keyframes, "max_keyframes"),
                {"name": record["name"]},
            )
        records.append(record)
    return records, truncated


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
