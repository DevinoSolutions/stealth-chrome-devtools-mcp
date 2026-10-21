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

**Why the HIT-TEST point is ``getClientRects()[0]``.** ``Element.mouse_click`` clicks
``Position(quads[0]).center`` — the centre of the element's FIRST box, from
``DOM.getContentQuads``. In the top document that is byte-equal to
``getClientRects()[0]``'s centre (measured on a plain button, a padded+bordered
button, an inline image and an ``<a>`` wrapped over four line boxes — where
``getBoundingClientRect()``'s centre is 28.5 px off). Inside an iframe the two
differ by the frame's offset: ``getClientRects`` is frame-relative and the quad is
in the top-level viewport, which is the space ``Input.dispatchMouseEvent`` takes.
So the hit-test (``AIM_JS``, in the element's own document) keeps the
frame-relative point, and :func:`click` DISPATCHES at the quad centre (F-1017
review).

**Why the target's own flags are read too.** ``elementFromPoint`` is not enough
on its own for exactly one shape: a disabled control hit-tests to ITSELF and
still receives nothing. So ``:disabled``, computed ``pointer-events`` and computed
``visibility`` come back in the same read, and :func:`reason` decides between them
in one place — the hit-test first, because a ``pointer-events: none`` element
whose child re-enables them DOES get the click (measured), and only then the
target's own flags.

**No text content, ever.** The only thing this module says about any element is
its tag, its id and its classes. An overlay is frequently a consent banner or a
modal dialog, and its words are the page's — echoing them into an MCP payload is
the same discipline failure F-869 names for a page's localStorage. Those are
bounded twice for the same reason: :data:`MAX_CLASSES` caps how MANY classes are
named — a page can carry two hundred utility classes on one element — and
:data:`MAX_TOKEN_CHARS` caps how long any one of them, or the id, may be, because
a count is not a bound when one hashed build-tool class outweighs eight ordinary
ones.

**Where the input LANDS (F-1017).** Everything above is about the CSS point. The
point Chrome's input lands on is a different one under a device-metrics override
that carries a ``scale`` (DevTools' device mode sets one to fit the emulated
screen into the window): ``Input.dispatchMouseEvent`` takes widget coordinates, so
the page sees ``x / scale``, while ``getClientRects``, ``getContentQuads`` and
``elementFromPoint`` all stay in CSS pixels. Measured, Chrome 155, headed, a
360x740 mobile override with ``scale: 1.25``: a click at CSS (100, 290) arrived at
(80, 232) and hit ``<body>``, while ``elementFromPoint(100, 290)`` still named the
button — a silent miss. ``scale: 0.8`` gave (125, 362.5); ``deviceScaleFactor``,
``mobile`` and a window taller or wider than the emulated screen changed nothing.
Nothing in CDP reads the scale back (``Page.getLayoutMetrics``, ``visualViewport``,
``devicePixelRatio`` and ``innerWidth`` all ignore it), so :func:`click` MEASURES
it: a ``mouseMoved`` is observed by a capturing listener in the page, the
point is rescaled by the ratio, and the correction is confirmed by another
observed move before anything is pressed. The first probe is sent at the aimed
point, then at half and a quarter of it, because under ``scale < 1`` an input
beyond the emulated widget is DROPPED, not clamped — measured at ``scale: 0.8``,
a move to x=330 reached the page not at all while x=280 arrived at 350. The press
is observed too, between ``mousePressed`` and ``mouseReleased``, and a
``mousedown`` whose real target is outside the element — when the hit-test
promised it would be inside — raises instead of returning a record that says the
click reached it. So does a press the page never reported while it was still
there to ask. Once ``mousePressed`` has gone out nothing here falls back to the
synthetic ``el.click()``: a second click after a real one is the one outcome
worse than a miss.

A leaf: ``tool_errors`` only, and the element and tab arrive as arguments. It has NO
error policy — the one thing it raises is "the page did not answer with the
promised JSON", the same not-a-policy ``text_entry.entered_text`` raises, and for
the same reason (``Element.apply`` returns ``result[0].value``, so a script that
THREW lands there as ``None`` and must never be mistaken for a blank record).
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

from nodriver import cdp

from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

if TYPE_CHECKING:
    from nodriver import Element, Tab

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

#: How many characters of any ONE token (an id, a single class name) it carries.
#: The count bound above is not enough on its own: a build tool can emit a single
#: hashed class name hundreds of characters long, and an id is unbounded by spec.
#: Same discipline as ``page_storage``'s character bounds — a diagnostic names a
#: shape, it does not carry the page's strings at whatever length the page chose.
MAX_TOKEN_CHARS = 64

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
#: clicks. An element with NO client rects has no box at all: the rect comes back
#: zeroed, ``rendered`` is false and there is no point to name — deliberately not
#: ``getBoundingClientRect()``, which answers an all-zero rect there and would
#: invent a click point at the viewport origin.
#:
#: ``disabled`` is ``matches(':disabled')`` and NOT the ``elem.disabled`` IDL
#: attribute. Measured, Chrome 152: a ``<button>`` inside a ``<fieldset disabled>``
#: reports ``elem.disabled === false`` while ``:disabled`` matches, it hit-tests to
#: ITSELF, and the page receives nothing — so the IDL read produced exactly the
#: false "the click reached it" this module exists to retire. ``:disabled`` also
#: covers ``<option disabled>`` and every other inherited-disabled form.
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
        // ``document.elementFromPoint`` answers the shadow HOST for a point over an
        // element inside any shadow root, so the host stands for the element.
        const hosts = [];
        for (let n = elem, r = n.getRootNode(); r instanceof ShadowRoot;
             r = n.getRootNode()) { hosts.push(r.host); n = r.host; }
        hitIsTarget = !!under && (under === elem || elem.contains(under) ||
            hosts.indexOf(under) !== -1);
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
        disabled: elem.matches(':disabled'),
        pointer_events: String(style.pointerEvents),
        visibility: String(style.visibility)
    });
}"""


#: How far (CSS px) a page-observed event may be from the point it was aimed at
#: and still count as "there". Chrome reports ``clientX/Y`` as integers (a move to
#: 362.5 arrived at 362), so this absorbs that truncation and not a scale.
LANDING_TOLERANCE = 2.0

#: How many observed moves :func:`aim_input` may spend converging on the scale.
MAX_PROBES = 4

#: Fractions of the aimed point the FIRST probe is tried at, in order. Under
#: ``scale < 1`` a probe at the aimed point can land beyond the emulated widget
#: and be dropped, so a smaller one is tried before giving up.
PROBE_FRACTIONS = (1.0, 0.5, 0.25)

#: Longest any one input dispatch or page read may take. A ``mousedown`` handler
#: that calls ``alert()`` holds ``Input.dispatchMouseEvent`` open indefinitely.
INPUT_BUDGET_S = 5.0

#: The probe's one handle on the page: a ``Symbol.for`` key, so it is not in
#: ``Object.getOwnPropertyNames(window)`` and a page that enumerates its globals
#: does not see it. It holds the DISPOSER of the armed listener, which is what
#: lets the next arm remove a handler an earlier click left behind.
PROBE_KEY = "stealth.click.probe"

#: Arms a capturing listener on ``window`` that records what the page REALLY
#: received — the real event target and its ``clientX/Y`` — for the event *types*
#: at ``__KINDS__``. ``inside`` is ``composedPath`` containing the element, or
#: the host of a CLOSED shadow root that holds it (``composedPath`` stops at the
#: host there, so nothing inside can ever be named from outside). Any probe a
#: failed earlier click left armed is disposed first.
ARM_PROBE_JS = """(elem) => {
    const key = Symbol.for('__KEY__');
    if (typeof window[key] === 'function') window[key]();
    const kinds = __KINDS__;
    const closedHosts = [];
    for (let n = elem, r = n.getRootNode(); r instanceof ShadowRoot;
         r = n.getRootNode()) {
        if (r.mode === 'closed') closedHosts.push(r.host);
        n = r.host;
    }
    const log = [];
    const handler = (e) => {
        const t = e.target;
        const path = e.composedPath();
        log.push({
            type: e.type, x: e.clientX, y: e.clientY,
            inside: path.indexOf(elem) !== -1 ||
                closedHosts.some((h) => path.indexOf(h) !== -1),
            tag: String((t && (t.tagName || t.nodeName)) || '').toLowerCase(),
            id: String((t && t.id) || ''),
            classes: Array.prototype.slice.call((t && t.classList) || []).map(String)
        });
    };
    kinds.forEach((k) => window.addEventListener(k, handler, true));
    Object.defineProperty(window, key, {
        configurable: true, enumerable: false,
        value: () => {
            kinds.forEach((k) => window.removeEventListener(k, handler, true));
            delete window[key];
            return log;
        }
    });
    return true;
}""".replace("__KEY__", PROBE_KEY)

#: Disposes the probe and answers with what it saw, as a JSON **string** (same
#: reason as :data:`AIM_JS`). ``[]`` when nothing arrived — a listener ahead of
#: ours that stops propagation, or a point no element of this document receives.
READ_PROBE_JS = """(elem) => {
    const probe = window[Symbol.for('__KEY__')];
    return JSON.stringify(typeof probe === 'function' ? probe() : []);
}""".replace("__KEY__", PROBE_KEY)


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


def _token(raw: object) -> str:
    """One id or class name, bounded to :data:`MAX_TOKEN_CHARS` characters."""
    return str(raw or "")[:MAX_TOKEN_CHARS]


def _shape(raw: object) -> dict[str, object] | None:
    """One element as tag + id + (bounded) classes. Never its text.

    Bounded twice over: :data:`MAX_CLASSES` caps how MANY classes are named and
    :data:`MAX_TOKEN_CHARS` caps how long any one of them (or the id) may be. The
    count alone is not a bound — one hashed class name from a build tool can be
    longer than eight ordinary ones together.
    """
    if not isinstance(raw, dict):
        return None
    classes = raw.get("classes")
    named = [_token(c) for c in classes] if isinstance(classes, list) else []
    return {
        "tag": _token(raw.get("tag")),
        "id": _token(raw.get("id")),
        "classes": named[:MAX_CLASSES],
    }


def reason(  # noqa: PLR0911  PERMANENT(one return per measured shape, in measured order)
    facts: dict[str, object],
) -> str | None:
    """Which measured shape kept the click from reaching the resolved target.

    The shape of the decision is what the measurement requires, not taste. The
    hit-test splits it in two: when the click point is over the target or
    something INSIDE it the click reached the target, and the only remaining
    question is whether the target could act on it (``disabled``). Only when it
    did NOT reach the target is there a cause to name, and there the order runs
    from the most specific to the most general — ``visibility``, then
    ``pointer-events``, then the general "something else was there".

    That split is not cosmetic. Measured, Chrome 152: a ``pointer-events: none``
    span whose child re-enables pointer events hit-tests to the CHILD and DOES
    receive the click (it bubbles — the page logged both). Deciding
    ``pointer-events`` before the hit would have reported
    ``pointer-events-none`` for a click that worked.
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
    if not facts.get("hit_is_target"):
        if facts.get("visibility") != "visible":
            return NOT_VISIBLE
        if facts.get("pointer_events") == "none":
            return POINTER_EVENTS_NONE
        return COVERED
    # The click reached the target. The one thing that can still make it inert is
    # the target being disabled — including by an ancestor ``<fieldset disabled>``,
    # which is why ``AIM_JS`` asks ``:disabled`` and not ``elem.disabled``.
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


async def _send_mouse(tab: Tab, kind: str, x: float, y: float) -> None:
    """One bounded ``Input.dispatchMouseEvent`` (left button, no modifiers)."""
    pressing = kind != "mouseMoved"
    await asyncio.wait_for(
        tab.send(
            cdp.input_.dispatch_mouse_event(
                kind,
                x=x,
                y=y,
                button=cdp.input_.MouseButton("left" if pressing else "none"),
                buttons=1 if kind == "mousePressed" else 0,
                click_count=1 if pressing else 0,
            )
        ),
        INPUT_BUDGET_S,
    )


async def _arm(element: Element, kinds: list[str]) -> None:
    await asyncio.wait_for(
        element.apply(ARM_PROBE_JS.replace("__KINDS__", json.dumps(kinds))),
        INPUT_BUDGET_S,
    )


async def _read(element: Element) -> list[dict[str, object]]:
    """Dispose the probe and answer what it saw (see :data:`READ_PROBE_JS`)."""
    answer = await asyncio.wait_for(element.apply(READ_PROBE_JS), INPUT_BUDGET_S)
    seen = json.loads(answer) if isinstance(answer, str) else None
    if not isinstance(seen, list):
        raise ToolError(
            "could not read what the page received from the input: "
            f"the page answered with {type(answer).__name__}, "
            "not the promised JSON list"
        )
    return [s for s in seen if isinstance(s, dict)]


async def _disarm(element: Element) -> str | None:
    """Best-effort dispose after a failure; the failure's type name if it fails.

    A page that is gone has nothing left to dispose, and the next arm disposes
    whatever a failed disarm left, so this never raises over the error already
    in flight.
    """
    try:
        await _read(element)
    except Exception as exc:  # noqa: BLE001  PERMANENT(cleanup must not mask the failure being raised)
        return type(exc).__name__
    return None


async def _moved(
    tab: Tab, element: Element, x: float, y: float
) -> dict[str, object] | None:
    """Move to (x, y) and answer the ``mousemove`` the page received, if any."""
    await _arm(element, ["mousemove"])
    try:
        await _send_mouse(tab, "mouseMoved", x, y)
        seen = await _read(element)
    except BaseException:
        await _disarm(element)
        raise
    return seen[-1] if seen else None


def _landed(
    seen: dict[str, object], offset: tuple[float, float]
) -> tuple[float, float]:
    """Where the page saw a move, in the top-level viewport's CSS px."""
    return (_number(seen.get("x")) + offset[0], _number(seen.get("y")) + offset[1])


async def aim_input(
    tab: Tab,
    element: Element,
    selector: str,
    aimed: tuple[float, float],
    offset: tuple[float, float],
) -> float | None:
    """Measure the factor *k* that makes input at ``aimed * k`` land on ``aimed``.

    *aimed* is in the top-level viewport's CSS px and *offset* is what to add to
    a coordinate the page reports to bring it there (zero in the top document;
    the frame's origin inside an iframe, whose events are frame-relative). Each
    observed move gives ``k = sent / seen`` on the axis with the larger
    coordinate — an emulation scale is uniform, and that axis suffers the
    integer truncation least — and the move at ``aimed * k`` is itself observed
    until it lands, at most :data:`MAX_PROBES` moves. ``None`` when no probe
    reached the page at all.
    """
    axis = 0 if aimed[0] >= aimed[1] else 1
    sent = aimed
    seen: dict[str, object] | None = None
    for fraction in PROBE_FRACTIONS:
        sent = (aimed[0] * fraction, aimed[1] * fraction)
        seen = await _moved(tab, element, *sent)
        if seen is not None:
            break
    if seen is None:
        return None
    for _ in range(MAX_PROBES - 1):
        landed = _landed(seen, offset)
        if all(abs(landed[i] - aimed[i]) <= LANDING_TOLERANCE for i in (0, 1)):
            return sent[axis] / aimed[axis]
        if landed[axis] <= 0 or sent[axis] <= 0:
            raise ToolError(
                f"could not aim the click on '{selector}': input at "
                f"({sent[0]:g}, {sent[1]:g}) reached the page at "
                f"({landed[0]:g}, {landed[1]:g}), which names no scale"
            )
        k = sent[axis] / landed[axis]
        sent = (aimed[0] * k, aimed[1] * k)
        seen = await _moved(tab, element, *sent)
        if seen is None:
            raise ToolError(
                f"could not aim the click on '{selector}': input rescaled to "
                f"({sent[0]:g}, {sent[1]:g}) did not reach the page"
            )
    landed = _landed(seen, offset)
    if any(abs(landed[i] - aimed[i]) > LANDING_TOLERANCE for i in (0, 1)):
        raise ToolError(
            f"could not aim the click on '{selector}': input at CSS "
            f"({aimed[0]:g}, {aimed[1]:g}) still reaches the page at "
            f"({landed[0]:g}, {landed[1]:g}) after {MAX_PROBES} probes"
        )
    return sent[axis] / aimed[axis]


async def click(
    tab: Tab, element: Element, selector: str, facts: dict[str, object]
) -> None:
    """The ONE coordinate click, correct under any device-metrics override.

    *facts* is :func:`aim`'s answer. An element with no hit-testable point (no
    box, or a box outside the viewport) is handed to ``Element.mouse_click``
    unchanged: no input could reach it, so there is nothing to measure.

    The point dispatched is the centre of the first content quad, in the top-level
    viewport — what ``Element.mouse_click`` has always used, and the only space
    that is right inside an iframe. Whatever is raised BEFORE ``mousePressed`` is
    sent is a plain exception the caller may answer with the synthetic click;
    from then on everything is a ``ToolError``, because a second click after a
    delivered one must never happen.

    Raises ``ToolError`` when the input cannot be aimed, when the page did not
    report the press although it was there to ask, or when its own ``mousedown``
    landed outside an element the hit-test said the point was over.
    """
    point = facts.get("point")
    if not isinstance(point, dict) or facts.get("hit") is None:
        await element.mouse_click()
        return
    position = await element.get_position()
    if position is None:
        await element.mouse_click()
        return
    x, y = position.center
    offset = (x - _number(point.get("x")), y - _number(point.get("y")))
    expected = bool(facts.get("hit_is_target"))
    k = await aim_input(tab, element, selector, (x, y), offset)
    if k is None:
        if expected:
            raise ToolError(
                f"could not aim the click on '{selector}': no probe move reached "
                "the page, so where input lands could not be measured"
            )
        k = 1.0
    at = (x * k, y * k)
    await _arm(element, ["mousedown"])
    try:
        await _send_mouse(tab, "mousePressed", *at)
    except BaseException as failure:
        await _disarm(element)
        raise ToolError(
            f"click on '{selector}' failed while pressing: {type(failure).__name__}"
        ) from failure
    # Read BETWEEN press and release: a page that navigates on mousedown takes the
    # document with it, and a read after the release would then fail for a click
    # that worked.
    gone: str | None = None
    seen: list[dict[str, object]] = []
    try:
        seen = await _read(element)
    except Exception as failure:  # noqa: BLE001  PERMANENT(the page may navigate on mousedown; the press already went out)
        gone = type(failure).__name__
    try:
        await _send_mouse(tab, "mouseReleased", *at)
    except Exception as failure:
        raise ToolError(
            f"click on '{selector}' pressed but could not release: "
            f"{type(failure).__name__}"
        ) from failure
    # A disabled control is sent no mousedown by Chrome at all (measured, plain and
    # inside a ``<fieldset disabled>``), so silence from it is the answer ``reason``
    # already gives, not a press nobody saw.
    if not expected or gone is not None or facts.get("disabled"):
        return
    if not seen:
        raise ToolError(
            f"click on '{selector}' could not be verified: aimed at CSS "
            f"({x:g}, {y:g}) where the hit-test names the element, but the page "
            "reported no mousedown"
        )
    reached = seen[0]
    if not reached.get("inside"):
        raise ToolError(
            f"click on '{selector}' missed: aimed at CSS ({x:g}, {y:g}) where the "
            f"hit-test names the element, but the page's mousedown landed at "
            f"({_number(reached.get('x')):g}, {_number(reached.get('y')):g}) on "
            f"{_shape(reached)}"
        )
