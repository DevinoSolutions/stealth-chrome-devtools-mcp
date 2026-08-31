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
from typing import NamedTuple

# JSON-shaped payloads crossing the browser boundary, typed the way the repo's
# other JSON-record modules type them (cf. ``backend_registry.BackendEntry``).
Facts = dict[str, object]
Record = dict[str, object]

# The closed set. An invented level ("certain", "probably") reads as a stronger
# or weaker claim than any of these, so there are exactly three.
CONFIDENCE_LEVELS = ("high", "medium", "low")


# ---------------------------------------------------------------------------
# Derived values and the confidence they were derived WITH (F-850)
# ---------------------------------------------------------------------------


class Derived(NamedTuple):
    """A derived value welded to the confidence its own branch produced.

    The point is that a caller CANNOT supply the confidence. Ten separately
    reported defects shared one root cause: a heuristic returned a bare value
    and the call site stamped ``"high"`` on whatever came back — including a
    fall-through branch that had decided nothing. With this type, a branch that
    reached no conclusion physically cannot inherit a caller's optimism.

    An empty ``confidence`` means "I do not know", and such a field is OMITTED
    rather than emitted hedged: a weak model quotes a present field verbatim and
    reads absence as a reason to be careful (M11, owner ruling).
    """

    value: object = None
    confidence: str = ""
    reason: str = ""

    @property
    def known(self) -> bool:
        return bool(self.confidence) and self.value is not None


UNKNOWN = Derived()


def claim(derived: Derived) -> Record | None:
    """One derived claim as the payload carries it, or ``None`` to omit it.

    Value and confidence travel in ONE object so a reader cannot pick up the
    value without the caveat that belongs to it.
    """
    if not derived.known:
        return None
    body: Record = {"value": derived.value, "confidence": derived.confidence}
    if derived.reason:
        body["reason"] = derived.reason
    return body


def put(record: Record, field: str, derived: Derived) -> None:
    """Attach ``derived`` at ``field``, or leave the field absent entirely."""
    body = claim(derived)
    if body is not None:
        record[field] = body


def claim_value(record: Record, field: str) -> object:
    """The value inside a claim field, for a reader that has already decided the
    claim is good enough to act on (the prose templates, mainly)."""
    return as_obj(record.get(field)).get("value")


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


_REDUCED_MOTION = re.compile(r"prefers-reduced-motion\s*(?::\s*([A-Za-z-]+))?")


def prefers_reduced_motion(context: str) -> bool:
    """Does this at-rule context apply only when the user asked for LESS motion?

    ``@media (prefers-reduced-motion: no-preference)`` applies exactly when
    motion IS allowed, so a substring match on the feature NAME fired a warning
    whose remedy is backwards for that input (R9). A bare feature query is true
    for anything but ``no-preference``, so it counts.
    """
    match = _REDUCED_MOTION.search(context or "")
    if match is None:
        return False
    return (match.group(1) or "reduce").strip().lower() == "reduce"


def split_at_top_level(value: str, separators: str) -> list[str]:
    """Split on ``separators`` that are OUTSIDE any parentheses.

    Selector-shaped text needs this as much as value-shaped text does: the space
    inside ``:is(.a, .b) .card`` is not a descendant combinator.
    """
    parts: list[str] = []
    depth = 0
    current = ""
    for ch in value or "":
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch in separators and depth == 0:
            if current.strip():
                parts.append(current.strip())
            current = ""
        else:
            current += ch
    if current.strip():
        parts.append(current.strip())
    return parts


def own_compound(selector: str) -> str:
    """The RIGHTMOST compound of a selector — the only part that describes this
    element. ``.gallery .card`` is a claim about a ``.card`` inside a
    ``.gallery``; adding ``gallery`` to the element cannot make it match (R8)."""
    parts = split_at_top_level(selector, " \t\n>+~")
    return parts[-1] if parts else ""


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


def _bezier_class(x1: float, y1: float, x2: float, y2: float) -> Derived:
    """Classify a bezier by its control points.

    ``overshoot`` is any curve whose control points leave the 0..1 band, in
    EITHER direction and at either end — the family a designer means by
    "springy". The spec's shorthand for this was ``y1 < 0 || y2 > 1``, which
    misses ``cubic-bezier(0.34, 1.56, 0.64, 1)``: the classic "back-out" curve
    and the most common overshoot in the wild, whose excursion is ``y1 > 1``.
    The band check is the honest form of the same rule.

    The final branch is the one that mattered (R4). A curve that is FAST at both
    ends — ``cubic-bezier(0.1, 0.9, 0.9, 0.1)`` — is none of these families, and
    the old code fell through to ``"linear"``, which the caller then stamped
    ``"high"``. It has no name in this vocabulary, so it gets none.
    """
    if not (0.0 <= y1 <= 1.0) or not (0.0 <= y2 <= 1.0):
        return Derived("overshoot", "high")
    slow_start = y1 < x1
    slow_end = y2 > x2
    if slow_start and slow_end:
        return Derived("ease-in-out", "high")
    if slow_start:
        return Derived("ease-in", "high")
    if slow_end:
        return Derived("ease-out", "high")
    if (y1, y2) == (x1, x2):
        return Derived("linear", "high")
    return UNKNOWN


def easing_class(easing: str) -> Derived:
    """The curve's shape as a class, with the confidence that branch produced.

    There is deliberately NO ``spring-like`` class: real springs are not
    beziers, and the label invites a model to reach for spring physics the CSS
    cannot express — such curves fold into ``overshoot``. An easing function we
    do not recognise is OMITTED rather than guessed at (M11).
    """
    text = (easing or "").strip().lower()
    if text in _EASING_KEYWORDS:
        return Derived(_EASING_KEYWORDS[text], "high")
    if text.startswith("steps("):
        return Derived("stepped", "high")
    if text.startswith("linear("):
        return Derived(
            "custom",
            "medium",
            "a linear() easing is a polyline; 'custom' names the family, not the "
            "shape of this particular curve",
        )
    points = _bezier_points(text)
    return UNKNOWN if points is None else _bezier_class(*points)


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

_TRANSFORM_FUNCTION_FAMILY = {
    "translate": "translate",
    "translatex": "translate",
    "translatey": "translate",
    "translatez": "translate",
    "translate3d": "translate",
    "scale": "scale",
    "scalex": "scale",
    "scaley": "scale",
    "scalez": "scale",
    "scale3d": "scale",
    "rotate": "rotate",
    "rotatex": "rotate",
    "rotatey": "rotate",
    "rotatez": "rotate",
    "rotate3d": "rotate",
    "skew": "skew",
    "skewx": "skew",
    "skewy": "skew",
    "perspective": "perspective",
}

# A matrix encodes translate, scale, rotate AND skew at once, so naming any one
# of them is a guess. The retired table mapped the SUBSTRING "matrix" to
# "translate" and asserted it (R10).
_OPAQUE_TRANSFORMS = {"matrix", "matrix3d"}


def transform_families(value: str) -> tuple[set[str], bool]:
    """Every family a ``transform`` value touches, plus "was any part opaque".

    Function names are read as functions, not as substrings: the retired scan
    was first-match-wins over ``"scale" in text``, so ``translateX(10px)
    scale(1.2)`` came back as "scale" alone and a compound transform could never
    be reported as ``mixed`` (R10).
    """
    families: set[str] = set()
    opaque = False
    for name in re.findall(r"([A-Za-z0-9]+)\s*\(", value or ""):
        lowered = name.lower()
        if lowered in _OPAQUE_TRANSFORMS:
            opaque = True
        elif lowered in _TRANSFORM_FUNCTION_FAMILY:
            families.add(_TRANSFORM_FUNCTION_FAMILY[lowered])
    return families, opaque


def motion_kind(keyframes: list[Record]) -> Derived:
    """The kind of motion, from the property/value pairs the keyframes touch.

    A name lookup, not a judgement. Omitted (not guessed) when no keyframe was
    readable — an element whose ``@keyframes`` sits in a cross-origin sheet must
    not be described as animating "other". Two or more families is ``mixed``.
    """
    families: set[str] = set()
    opaque = False
    for frame in keyframes:
        for prop, value in as_obj(frame.get("properties")).items():
            if prop == "transform":
                found, was_opaque = transform_families(as_text(value))
                families |= found
                opaque = opaque or was_opaque
            else:
                families.add(_PROPERTY_FAMILY.get(prop, "other"))
    if len(families) > 1:
        # "other" is only informative when it is the ONLY thing we saw.
        families.discard("other")
    if not families:
        return UNKNOWN
    kind = "mixed" if len(families) > 1 else families.pop()
    if opaque:
        return Derived(
            kind,
            "medium",
            "a matrix() transform was not decoded, so more families may be in play",
        )
    return Derived(kind, "high")
