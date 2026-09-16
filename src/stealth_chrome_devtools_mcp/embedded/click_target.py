"""THE one home for "where was this click aimed, and what was under that point".

F-876. ``click_element`` dispatched a coordinate click and answered ``True``,
which is the success of its own dispatch and not the success of the interaction
— F-873's sentence, one interaction over. Measured on Chrome 152.0.7977.83,
product code path, throwaway profile, six shapes that each answered ``True``:

============================ ==========================================
shape                        what the target received
============================ ==========================================
covered by an overlay        nothing — the OVERLAY got the click
``disabled``                 nothing — Chrome suppresses the activation
``pointer-events: none``     nothing — the click went to ``<body>``
zero-size (0x0, laid out)    nothing — same
``visibility: hidden``       nothing — same
positioned off the viewport  nothing — the point is negative
``display: none``            an UNTRUSTED synthetic ``el.click()``
============================ ==========================================

**Why a record and not a verdict.** There is no bounded oracle for "did the page
react": a navigation, a fetch, a re-render or nothing at all are all legitimate
answers to a correct click, and the absence of any of them within any deadline is
not evidence. What IS bounded is two facts, and the table above shows they
explain every measured failure between them: which KIND of click was dispatched,
and what ``document.elementFromPoint`` saw at the point it was dispatched to.

**Why the point is ``getClientRects()[0]``.** ``Element.mouse_click`` clicks
``Position(quads[0]).center`` — the centre of the element's FIRST box, from
``DOM.getContentQuads``. Measured byte-equal to ``getClientRects()[0]``'s centre
on a plain button, a padded+bordered button, an inline image and an ``<a>``
wrapped over four line boxes — where ``getBoundingClientRect()``'s centre is
28.5 px off. A record built on the bounding box would name a point the click
never used.

**Why the target's own flags are read too.** ``elementFromPoint`` is not enough
on its own for exactly one shape: a ``disabled`` control hit-tests to ITSELF and
still receives nothing. So ``disabled``, computed ``pointer-events`` and computed
``visibility`` come back in the same read, and :func:`reason` decides between them
in one place.

**No text content, ever.** The only thing this module says about any element is
its tag, its id and its classes. An overlay is frequently a consent banner or a
modal dialog, and its words are the page's — echoing them into an MCP payload is
the same discipline failure F-869 names for a page's localStorage. The class list
is bounded by :data:`MAX_CLASSES` for the same reason: a page can carry two
hundred utility classes on one element and a record that copied them all would be
a payload, not a diagnostic.

A leaf: ``tool_errors`` only, and the element arrives as an argument. It has NO
error policy — the one thing it raises is "the page did not answer with the
promised JSON", the same not-a-policy ``text_entry.entered_text`` raises, and for
the same reason (``Element.apply`` returns ``result[0].value``, so a script that
THREW lands there as ``None`` and must never be mistaken for a blank record).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

if TYPE_CHECKING:
    from nodriver import Element

#: The two kinds of click ``click_element`` can dispatch. ``coordinate`` is
#: ``Element.mouse_click`` — a real, trusted, hit-tested pointer input at
#: :func:`aim`'s point. ``synthetic`` is the error-only fallback
#: ``Element.click``, an in-page ``(el) => el.click()``: untrusted, no
#: coordinate, no hit-testing. It is kept because for a ``display:none`` target
#: it is the only thing that reaches the element at all — what F-876 changes is
#: that the caller is told which one happened.
COORDINATE = "coordinate"
SYNTHETIC = "synthetic"

#: How many of an element's classes the shape descriptor carries.
MAX_CLASSES = 8

#: The closed set of reasons the resolved target could not have taken the click.
#: ``None`` — the hit element IS the target or sits inside it — is the seventh
#: member and is not listed here because it is the absence of a reason.
NOT_RENDERED = "not-rendered"
OFF_VIEWPORT = "off-viewport"
ZERO_SIZE = "zero-size"
NOT_VISIBLE = "not-visible"
POINTER_EVENTS_NONE = "pointer-events-none"
COVERED = "covered"
DISABLED = "disabled"

#: The ONE read. Answers with a JSON **string** for the same reason
#: ``text_entry.READ_JS`` does. ``getClientRects()[0]`` is the box nodriver
#: clicks; ``getBoundingClientRect()`` is the fallback for an element that has no
#: client rects at all, where it is all zeros and ``rendered`` is already false.
AIM_JS = """(elem) => {
    const rects = elem.getClientRects();
    const box = rects.length ? rects[0] : null;
    const style = window.getComputedStyle(elem);
    const shape = (node) => node === null ? null : {
        tag: String(node.tagName || node.nodeName || '').toLowerCase(),
        id: String(node.id || ''),
        classes: Array.prototype.slice.call(node.classList || []).map(String)
    };
    let point = null;
    let hit = null;
    let hitIsTarget = false;
    if (box) {
        point = {x: box.left + box.width / 2, y: box.top + box.height / 2};
        const under = document.elementFromPoint(point.x, point.y);
        hit = shape(under);
        hitIsTarget = !!under && (under === elem || elem.contains(under));
    }
    return JSON.stringify({
        rendered: !!box,
        rect: {
            left: box ? box.left : 0,
            top: box ? box.top : 0,
            width: box ? box.width : 0,
            height: box ? box.height : 0
        },
        point: point,
        target: shape(elem),
        hit: hit,
        hit_is_target: hitIsTarget,
        disabled: !!elem.disabled,
        pointer_events: String(style.pointerEvents),
        visibility: String(style.visibility)
    });
}"""


async def aim(element: Element, selector: str) -> dict[str, object]:
    """Read where a click on *element* would go, and what is under that point.

    Read BEFORE the click, deliberately: afterwards would describe a page the
    click may already have changed, and for a ``display:none`` target there is no
    box left to ask about at all.

    Raises ``ToolError`` when the answer is not the promised JSON object:
    "I could not read it" and "nothing was under the point" are different facts.
    """
    answer = await element.apply(AIM_JS)
    if not isinstance(answer, str):
        raise ToolError(
            f"could not read where a click on '{selector}' would land: "
            f"the page answered with {type(answer).__name__}, "
            "not the promised JSON string"
        )
    try:
        record = json.loads(answer)
    except ValueError:
        raise ToolError(
            f"could not read where a click on '{selector}' would land: "
            f"the {len(answer)}-character answer is not valid JSON"
        ) from None
    if not isinstance(record, dict) or not isinstance(record.get("rect"), dict):
        raise ToolError(
            f"could not read where a click on '{selector}' would land: "
            f"the answer carries no rect field ({type(record).__name__})"
        )
    return record


def _number(raw: object) -> float:
    """One JSON number as a float; anything else is ``0.0``.

    The read is typed ``object`` because ``json.loads`` promises nothing, and a
    bare ``float(...)`` over that is a cast rather than a check. A page that
    answered with a string where a coordinate belongs would otherwise raise from
    inside the composition, after the click had already been dispatched.
    """
    return float(raw) if isinstance(raw, (int, float)) else 0.0


def _shape(raw: object) -> dict[str, object] | None:
    """One element as tag + id + (bounded) classes. Never its text."""
    if not isinstance(raw, dict):
        return None
    classes = raw.get("classes")
    named = [str(c) for c in classes] if isinstance(classes, list) else []
    return {
        "tag": str(raw.get("tag") or ""),
        "id": str(raw.get("id") or ""),
        "classes": named[:MAX_CLASSES],
    }


def reason(  # noqa: PLR0911  PERMANENT(one return per measured shape, in measured order)
    facts: dict[str, object],
) -> str | None:
    """Which measured shape kept the click from reaching the resolved target.

    Order is what the measurement requires, not taste.
    ``pointer-events: none`` is decided before ``covered`` because both are true
    for that shape and only one of them is the cause; ``disabled`` is decided
    last because it is the one shape where the hit-test names the target and the
    click is still inert.
    """
    if not facts.get("rendered"):
        return NOT_RENDERED
    if facts.get("hit") is None:
        # A box exists but ``elementFromPoint`` answered null, which it does for
        # a point outside the viewport. Measured: an ``absolute; left:-500px``
        # button keeps a 33.5x21 box at a NEGATIVE point, ``scroll_into_view``
        # does not bring it back, and the coordinate click reaches nobody.
        return OFF_VIEWPORT
    rect = facts.get("rect")
    if isinstance(rect, dict) and not (
        _number(rect.get("width")) and _number(rect.get("height"))
    ):
        return ZERO_SIZE
    if facts.get("visibility") != "visible":
        return NOT_VISIBLE
    if facts.get("pointer_events") == "none":
        return POINTER_EVENTS_NONE
    if not facts.get("hit_is_target"):
        return COVERED
    if facts.get("disabled"):
        return DISABLED
    return None


def record(selector: str, facts: dict[str, object], dispatch: str) -> dict[str, object]:
    """Compose what ``click_element`` answers with.

    ``point``, ``size`` and ``hit`` are ``None`` together for an element with no
    box: it has no click point to name.
    """
    rect = facts.get("rect")
    rendered = bool(facts.get("rendered")) and isinstance(rect, dict)
    point = facts.get("point") if rendered else None
    return {
        "selector": selector,
        "dispatch": dispatch,
        "point": (
            {"x": _number(point.get("x")), "y": _number(point.get("y"))}
            if isinstance(point, dict)
            else None
        ),
        "size": (
            {
                "width": _number(rect.get("width")),
                "height": _number(rect.get("height")),
            }
            if rendered and isinstance(rect, dict)
            else None
        ),
        "target": _shape(facts.get("target")),
        "hit": _shape(facts.get("hit")),
        "hit_is_target": bool(facts.get("hit_is_target")),
        "reason": reason(facts),
    }
