"""THE one home for READING what the animations collector sent (F-848).

Two closely-bound reading concerns, both upstream of any conclusion:

* **JSON fields** of the untrusted fact payload (``as_obj`` / ``as_rows`` /
  ``as_text`` / ``as_number``). The payload crosses a browser boundary, so a
  malformed field must degrade to an empty value, never crash the derivation.
* **CSS value tokens** inside those fields — comma lists and the list-cycling
  rule, ``<time>`` tokens, declaration blocks, keyframe selectors, iteration
  counts, easing-curve classification and the property -> motion-family lookup.

Nothing here knows about the schema-v2 payload; it only knows what a CSS token
means. The animations subsystem is three leaves in a pipeline, none of which
imports ``server`` (CLAUDE.md convention 1):

    animation_facts (read)  ->  animation_advice (what to do)
                            ->  animation_analysis (what it is; owns analyze())

This is the bottom of that pipeline and imports no other ``embedded`` module.
"""

from __future__ import annotations

import re

# JSON-shaped payloads crossing the browser boundary, typed the way the repo's
# other JSON-record modules type them (cf. ``backend_registry.BackendEntry``).
Facts = dict[str, object]
Record = dict[str, object]


# ---------------------------------------------------------------------------
# Reading an untrusted JSON payload
# ---------------------------------------------------------------------------


def as_obj(value: object) -> Record:
    """A mapping field, or an empty mapping when it is anything else.

    Rebuilt with ``str`` keys rather than cast: the payload is JSON, where an
    object's keys are strings by definition, and stating that explicitly is what
    lets the type checker see it.
    """
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def as_rows(value: object) -> list[Record]:
    """A list-of-objects field, keeping only the entries that really are objects."""
    if not isinstance(value, list):
        return []
    return [as_obj(item) for item in value if isinstance(item, dict)]


def as_strings(value: object) -> list[str]:
    """A list-of-strings field, keeping only the entries that really are strings."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def as_text(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default


def as_number(value: object) -> float | None:
    """A numeric field as a float, or ``None``.

    ``bool`` is excluded deliberately: it is an ``int`` in Python, and a stray
    ``True`` silently reading as ``1.0`` ms would be a derived value that lies.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


# ---------------------------------------------------------------------------
# CSS value tokens
# ---------------------------------------------------------------------------


def split_css_list(value: str) -> list[str]:
    """Split a comma-separated CSS value at TOP level only.

    ``cubic-bezier(0.34, 1.56, 0.64, 1), linear`` is two items, not five — the
    commas inside the function's parentheses are not separators.
    """
    items: list[str] = []
    depth = 0
    current = ""
    for ch in value or "":
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            items.append(current.strip())
            current = ""
        else:
            current += ch
    if current.strip():
        items.append(current.strip())
    return items


def cycle_get(items: list[str], index: int, default: str = "") -> str:
    """CSS list cycling: a shorter list repeats independently of the others.

    This is the rule the v1 code never applied (F-847). ``animation: a, b`` with
    a single ``animation-delay`` gives BOTH animations that delay.
    """
    if not items:
        return default
    return items[index % len(items)]


def duration_ms(token: object) -> float | None:
    """A CSS ``<time>`` token as milliseconds, or ``None`` when not a time.

    ``"auto"`` (what ``getComputedTiming().duration`` reports for a scroll/view
    timeline) returns ``None`` — never coerced to 0, which would read as an
    instant animation rather than as "this timeline has no duration".
    """
    if not isinstance(token, str):
        return None
    text = token.strip().lower()
    match = re.fullmatch(r"([+-]?(?:\d+\.?\d*|\.\d+))(ms|s)?", text)
    if not match:
        return None
    value = float(match.group(1))
    return round(value if match.group(2) == "ms" else value * 1000.0, 3)


def parse_declarations(css_text: str) -> dict[str, str]:
    """A declaration block (``a: 1; b: 2``) as an ordered property->value map."""
    out: dict[str, str] = {}
    depth = 0
    current = ""
    parts: list[str] = []
    for ch in css_text or "":
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == ";" and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += ch
    parts.append(current)
    for part in parts:
        if ":" not in part:
            continue
        name, _, value = part.partition(":")
        name = name.strip().lower()
        value = value.strip()
        if name and value:
            out[name] = value
    return out


def keyframe_offsets(key_text: str) -> list[float]:
    """``"0%, 50%"`` / ``"from"`` / ``"to"`` -> numeric offsets in 0..1.

    One keyframe RECORD per offset (M2): the model needs to be told a value
    applies at 0 *and* at 0.5, not to re-parse the string ``"0%, 50%"`` itself.
    """
    offsets: list[float] = []
    for token in split_css_list(key_text or ""):
        text = token.strip().lower()
        if text == "from":
            offsets.append(0.0)
        elif text == "to":
            offsets.append(1.0)
        elif text.endswith("%"):
            try:
                offsets.append(round(float(text[:-1]) / 100.0, 6))
            except ValueError:
                continue
    return offsets


def iteration_count(token: str) -> float | int | str | None:
    """``"infinite"`` stays the documented string; a number stays a number."""
    text = (token or "").strip().lower()
    if text == "infinite":
        return "infinite"
    try:
        return float(text) if "." in text else int(text)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Easing curves (§3.2)
# ---------------------------------------------------------------------------

_EASING_KEYWORDS = {
    "linear": "linear",
    "ease": "ease-in-out",
    "ease-in": "ease-in",
    "ease-out": "ease-out",
    "ease-in-out": "ease-in-out",
    "step-start": "stepped",
    "step-end": "stepped",
}


def _bezier_points(text: str) -> tuple[float, float, float, float] | None:
    match = re.fullmatch(r"cubic-bezier\(([^)]*)\)", text)
    if not match:
        return None
    try:
        x1, y1, x2, y2 = (float(part) for part in match.group(1).split(","))
    except ValueError:
        return None
    return x1, y1, x2, y2


def _bezier_class(x1: float, y1: float, x2: float, y2: float) -> str:
    """Classify a bezier by its control points.

    ``overshoot`` is any curve whose control points leave the 0..1 band, in
    EITHER direction and at either end — the family a designer means by
    "springy". The spec's shorthand for this was ``y1 < 0 || y2 > 1``, which
    misses ``cubic-bezier(0.34, 1.56, 0.64, 1)``: the classic "back-out" curve
    and the most common overshoot in the wild, whose excursion is ``y1 > 1``.
    The band check is the honest form of the same rule.
    """
    if not (0.0 <= y1 <= 1.0) or not (0.0 <= y2 <= 1.0):
        return "overshoot"
    slow_start = y1 < x1
    slow_end = y2 > x2
    if slow_start and slow_end:
        return "ease-in-out"
    if slow_start:
        return "ease-in"
    if slow_end:
        return "ease-out"
    return "linear"


def easing_class(easing: str) -> str | None:
    """The curve's shape as a class, or ``None`` when it is not decidable.

    There is deliberately NO ``spring-like`` class: real springs are not
    beziers, and the label invites a model to reach for spring physics the CSS
    cannot express — such curves fold into ``overshoot``. An easing function we
    do not recognise is OMITTED rather than guessed at (M11).
    """
    text = (easing or "").strip().lower()
    if text in _EASING_KEYWORDS:
        return _EASING_KEYWORDS[text]
    if text.startswith("steps("):
        return "stepped"
    if text.startswith("linear("):
        return "custom"
    points = _bezier_points(text)
    return None if points is None else _bezier_class(*points)


# ---------------------------------------------------------------------------
# Motion families (§3.2)
# ---------------------------------------------------------------------------

_PROPERTY_FAMILY = {
    "opacity": "fade",
    "color": "color",
    "background-color": "color",
    "border-color": "color",
    "fill": "color",
    "stroke": "color",
    "width": "size",
    "height": "size",
    "top": "size",
    "left": "size",
    "right": "size",
    "bottom": "size",
    "inset": "size",
    "margin": "size",
    "padding": "size",
    "filter": "filter",
    "backdrop-filter": "filter",
    "scale": "scale",
    "rotate": "rotate",
    "translate": "translate",
}

_TRANSFORM_FAMILY = (
    ("scale", "scale"),
    ("rotate", "rotate"),
    ("translate", "translate"),
    ("matrix", "translate"),
)


def _transform_family(value: str) -> str:
    text = (value or "").lower()
    for token, family in _TRANSFORM_FAMILY:
        if token in text:
            return family
    return "other"


def motion_kind(keyframes: list[Record]) -> str | None:
    """The kind of motion, from the property/value pairs the keyframes touch.

    A name lookup, not a judgement. Omitted (not guessed) when no keyframe was
    readable — an element whose ``@keyframes`` sits in a cross-origin sheet must
    not be described as animating "other". Two or more families is ``mixed``.
    """
    families: set[str] = set()
    for frame in keyframes:
        for prop, value in as_obj(frame.get("properties")).items():
            if prop == "transform":
                families.add(_transform_family(as_text(value)))
            else:
                families.add(_PROPERTY_FAMILY.get(prop, "other"))
    if len(families) > 1:
        # "other" is only informative when it is the ONLY thing we saw.
        families.discard("other")
    if not families:
        return None
    return "mixed" if len(families) > 1 else families.pop()
