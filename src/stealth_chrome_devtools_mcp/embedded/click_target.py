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
#: :func:`synthetic_click`, an in-page ``el.click()``: untrusted, no
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

#: THE page-side read of "is this node still in a document" (T-10284), and its
#: ONE vocabulary: every script that asks (:data:`AIM_JS`, :data:`SYNTHETIC_JS`,
#: :data:`CONNECTED_JS`, ``dom_handler``'s visibility script) is built from
#: :data:`CONNECTIVITY_JS`, nothing else in the package reads ``isConnected``,
#: and :func:`connectivity` is the one reader of the answer on this side.
#:
#: The answer is one of three TOKENS, never a boolean. ``Element.apply`` drops
#: ``exceptionDetails`` and hands back ``result[0].value``, so a value a getter
#: THROWS arrives as a plain one: ``throw true`` is indistinguishable from a
#: ``true`` that was read, and a thrown Error is ``None``, the same as "no
#: answer". A token can only come from a script that ran to the line that
#: returns it: the read is inside a ``try`` and a throw becomes
#: :data:`CONNECTED_THREW`, as does any value that is not a boolean. Only
#: ``no`` is a departure.
#:
#: The getter is the NATIVE ``Node.prototype.isConnected`` accessor, never
#: ``elem.isConnected`` (a ``<form>`` with a control named ``isConnected``
#: shadows that member, see :data:`NATIVES_JS`). A page script that REPLACES the
#: accessor is the accepted boundary until T-10299 (an isolated world): one
#: that throws is read as ``threw``, one that answers a boolean is believed.
CONNECTED_YES = "stealth.connected:yes"
CONNECTED_NO = "stealth.connected:no"
CONNECTED_THREW = "stealth.connected:threw"

CONNECTIVITY_JS = (
    """const connectivity = (node) => {
        try {
            const answer = Object.getOwnPropertyDescriptor(
                Node.prototype, 'isConnected').get.call(node);
            if (answer === true) return "__YES__";
            if (answer === false) return "__NO__";
        } catch (e) { return "__THREW__"; }
        return "__THREW__";
    };""".replace("__YES__", CONNECTED_YES)
    .replace("__NO__", CONNECTED_NO)
    .replace("__THREW__", CONNECTED_THREW)
)

#: The ONE read. Answers with a JSON **string** for the same reason
#: ``text_entry.READ_JS`` does. ``getClientRects()[0]`` is the box nodriver
#: clicks. An element with NO client rects has no box at all: the rect comes back
#: zeroed, ``rendered`` is false and there is no point to name — deliberately not
#: ``getBoundingClientRect()``, which answers an all-zero rect there and would
#: invent a click point at the viewport origin.
#:
#: ``disabled`` / ``under_disabled`` ask whether a node is an INERT FORM CONTROL: it
#: matches ``:disabled`` itself, or its nearest ``button, input, select, textarea,
#: optgroup, option`` ancestor does (a child of a disabled ``<button>`` gets no
#: mousedown either). It is NOT ``elem.disabled`` and NOT ``closest(':disabled')``.
#: Not the IDL attribute: measured, Chrome 152, a ``<button>`` inside a
#: ``<fieldset disabled>`` reports ``elem.disabled === false`` while ``:disabled``
#: matches, it hit-tests to ITSELF, and the page receives nothing. Not
#: ``closest(':disabled')`` either: ``fieldset[disabled]`` itself matches
#: ``:disabled``, so every ``<div>`` or ``<span>`` inside one would read as
#: disabled although only the form controls in it are (and not the ones in the
#: fieldset's first ``<legend>``) — a click on that ``<div>`` is delivered, and
#: calling it disabled would also have skipped its press verification. The
#: fieldset is deliberately left out of the ancestor list for that reason: the
#: CONTROL's own ``:disabled`` already applies the fieldset and legend rules.
#:
#: ``connected`` is the connectivity token (:data:`CONNECTIVITY_JS`, T-10034,
#: STEALTH-…-ER). A node the page
#: REMOVED has no client rects either, and the read still succeeds through the
#: script's reference to it, so without this it answers exactly like a
#: ``display:none`` target and earns the synthetic click -- on a node no listener
#: in the page can hear (measured: 14 of 15 such clicks reported success, the
#: page counted none). ``isConnected`` is the test because it is the one that is
#: right in both other places a live element can be: inside a shadow root (true
#: while the host is in the document; ``document.contains`` answers false there)
#: and inside an iframe (connected to its OWN document). A disconnected node is
#: never hit-tested: it has no box, and no point in the page can be over it, so
#: it can never read as "covered". :func:`connectivity` is the one reader.
#: The NATIVE accessor or method for every member the click path reads off a
#: node, each read ONCE at the top of the script that uses it (Codex round 5).
#: Never ``elem.<name>``: an HTML ``<form>`` is ``[LegacyOverrideBuiltIns]``, so
#: a named control shadows ANY member -- ``<input name=isConnected>`` makes
#: ``form.isConnected`` the input, a truthy object, with no page script
#: involved -- and a removed form then read as connected and was clicked
#: synthetically. A page script that REPLACES these prototypes is the accepted
#: boundary until T-10299 (an isolated world); a named control is not.
NATIVES_JS = (
    "const getter = (proto, name) => "
    "Object.getOwnPropertyDescriptor(proto, name).get;\n"
    "    const ownerDocument = getter(Node.prototype, 'ownerDocument');\n"
    "    const nodeName = getter(Node.prototype, 'nodeName');\n"
    "    const getRootNode = Node.prototype.getRootNode;\n"
    "    const contains = Node.prototype.contains;\n"
    "    const tagName = getter(Element.prototype, 'tagName');\n"
    "    const idOf = getter(Element.prototype, 'id');\n"
    "    const classListOf = getter(Element.prototype, 'classList');\n"
    "    const getClientRects = Element.prototype.getClientRects;\n"
    "    const matches = Element.prototype.matches;\n"
    "    const closest = Element.prototype.closest;\n"
    "    const hostOf = getter(ShadowRoot.prototype, 'host');\n"
    "    const elementFromPoint = Document.prototype.elementFromPoint;"
)

AIM_JS = (
    """(elem) => {
    __NATIVES__
    __CONNECTIVITY__
    const inert = (node) => {
        if (!node) return false;
        if (matches.call(node, ':disabled')) return true;
        const owner = closest.call(
            node, 'button,input,select,textarea,optgroup,option');
        return !!owner && matches.call(owner, ':disabled');
    };
    const state = connectivity(elem);
    const rects = state === "__YES__" ? getClientRects.call(elem) : [];
    const box = rects.length ? rects[0] : null;
    const style = window.getComputedStyle(elem);
    const shape = (node) => node === null ? null : {
        tag: String(tagName.call(node) || nodeName.call(node) || '').toLowerCase(),
        id: String(idOf.call(node) || ''),
        classes: Array.prototype.slice.call(classListOf.call(node) || []).map(String)
    };
    let point = null;
    let hit = null;
    let hitIsTarget = false;
    let under = null;
    if (box) {
        point = {x: box.left + box.width / 2, y: box.top + box.height / 2};
        under = elementFromPoint.call(ownerDocument.call(elem), point.x, point.y);
        hit = shape(under);
        // ``elementFromPoint`` answers the shadow HOST for a point over an
        // element inside any shadow root, so the host stands for the element.
        const hosts = [];
        for (let n = elem, r = getRootNode.call(n); r instanceof ShadowRoot;
             r = getRootNode.call(n)) { n = hostOf.call(r); hosts.push(n); }
        hitIsTarget = !!under && (under === elem || contains.call(elem, under) ||
            hosts.indexOf(under) !== -1);
    }
    return JSON.stringify({
        connected: state,
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
        disabled: inert(elem),
        under_disabled: inert(under),
        pointer_events: String(style.pointerEvents),
        visibility: String(style.visibility)
    });
}""".replace("__NATIVES__", NATIVES_JS)
    .replace("__CONNECTIVITY__", CONNECTIVITY_JS)
    .replace("__YES__", CONNECTED_YES)
)


#: How far (CSS px) a page-observed event may be from the point it was aimed at
#: and still count as "there". Chrome reports ``clientX/Y`` as integers (a move to
#: 362.5 arrived at 362), so this absorbs that truncation and not a scale.
LANDING_TOLERANCE = 1.0

#: Added per axis in proportion to the coordinate, so rounding is absorbed far from
#: the origin while a real scale (1.25 turns x=10 into 8) is not accepted near it.
LANDING_TOLERANCE_RELATIVE = 0.01

#: How many observed moves :func:`aim_input` may spend converging on the scale.
MAX_PROBES = 4

#: Fractions of the aimed point the FIRST probe is tried at, in order. Under
#: ``scale < 1`` a probe at the aimed point can land beyond the emulated widget
#: and be dropped, so a smaller one is tried before giving up.
PROBE_FRACTIONS = (1.0, 0.5, 0.25)

#: Longest any one input dispatch or page read may take. A ``mousedown`` handler
#: that calls ``alert()`` holds ``Input.dispatchMouseEvent`` open indefinitely.
INPUT_BUDGET_S = 5.0

#: The probe's one handle on the page: a ``Symbol.for`` key, so it is not a
#: string-named global (``Object.getOwnPropertyNames(window)`` and ``for…in`` do not
#: list it; ``Object.getOwnPropertySymbols`` / ``Reflect.ownKeys`` do, so it is not
#: invisible). It holds the DISPOSER of the armed listener, which is what
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
    const getRootNode = Node.prototype.getRootNode;
    const shadow = (name) =>
        Object.getOwnPropertyDescriptor(ShadowRoot.prototype, name).get;
    const hostOf = shadow('host');
    const modeOf = shadow('mode');
    const closedHosts = [];
    for (let n = elem, r = getRootNode.call(n); r instanceof ShadowRoot;
         r = getRootNode.call(n)) {
        if (modeOf.call(r) === 'closed') closedHosts.push(hostOf.call(r));
        n = hostOf.call(r);
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


#: The connectivity read ON ITS OWN, for a caller that holds an ``Element`` and
#: has no aim to read it off (``element_resolution.refresh_element``). One
#: token, one ``Element.apply``. nodriver's ``update()`` re-resolves by
#: backend node id and that succeeds for a node the page removed but still
#: references, so ``update`` cannot say whether an element has left the page.
CONNECTED_JS = (
    "(elem) => {\n    __CONNECTIVITY__\n    return connectivity(elem);\n}"
).replace("__CONNECTIVITY__", CONNECTIVITY_JS)


def connectivity(answer: object, what: str) -> bool:
    """THE reader of a connectivity answer: ``True`` in a document, ``False`` left.

    Accepts ONLY the script's own tokens (:data:`CONNECTED_YES`,
    :data:`CONNECTED_NO`). ``False`` is the page SAYING the node left: the one
    answer that is a departure. Everything else -- :data:`CONNECTED_THREW`, a
    value that is no token (a ``true`` a getter threw, ``None`` from a thrown
    Error or from a script that stopped returning its token, a shadowed
    object) -- is a read that did not succeed, which says nothing about the
    node, so it raises ``ToolError`` naming *what* could not be read. It is
    never a departure (the caller would drop or refuse a node that may be
    there) and never a connected node (the caller would act on one that may be
    gone).
    """
    if isinstance(answer, str):
        if answer == CONNECTED_YES:
            return True
        if answer == CONNECTED_NO:
            return False
    found = (
        "the page's isConnected read threw or answered something that is not a boolean"
        if answer == CONNECTED_THREW
        else f"the page answered with {type(answer).__name__}, not one of the "
        "connectivity read's own answers"
    )
    raise ToolError(f"could not read whether {what} is still in its document: {found}")


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
    # the target being a disabled form control — including one disabled by an
    # ancestor ``<fieldset disabled>``, which is why ``AIM_JS`` asks ``:disabled``
    # and not ``elem.disabled``.
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


class InputSent:
    """Whether any input of ONE click may have reached the page (T-10034).

    ``sent`` turns true immediately BEFORE the click's first
    ``Input.dispatchMouseEvent`` goes out, in :func:`_send_mouse` -- the one
    function here that sends pointer input -- and never turns back. A send
    that fails still counts: a timeout does not prove the event was not
    delivered. :func:`synthetic_click`, the one synthetic sender, sets it for
    every outcome but the page's definite "the node had left, nothing ran".

    It is how the caller tells "nothing reached the page" (a re-run or the
    synthetic ``el.click()`` is safe) from "something may have" (neither is:
    a second click after a delivered one is the outcome worse than a miss).
    """

    __slots__ = ("sent",)

    def __init__(self) -> None:
        self.sent = False


async def _send_mouse(
    tab: Tab, kind: str, x: float, y: float, ledger: InputSent
) -> None:
    """One bounded ``Input.dispatchMouseEvent`` (left button, no modifiers).

    THE one place a click sends input, so it is where *ledger* is set.
    """
    pressing = kind != "mouseMoved"
    ledger.sent = True
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
    try:
        await asyncio.wait_for(
            element.apply(ARM_PROBE_JS.replace("__KINDS__", json.dumps(kinds))),
            INPUT_BUDGET_S,
        )
    except BaseException:
        # An arm that timed out may still have run; dispose now, not at the next arm.
        await _disarm(element)
        raise


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


async def _let_go(tab: Tab, at: tuple[float, float], ledger: InputSent) -> str | None:
    """Best-effort, bounded ``mouseReleased``: never leave the button held.

    Errors are swallowed because the failure already in flight is the one worth
    reporting. Call it through :func:`_wind_down`, which shields it from a
    cancellation arriving DURING the release.
    """
    try:
        await _send_mouse(tab, "mouseReleased", *at, ledger)
    except Exception as exc:  # noqa: BLE001  PERMANENT(cleanup must not mask the failure being raised)
        return type(exc).__name__
    return None


async def _wind_down(
    tab: Tab,
    element: Element,
    at: tuple[float, float],
    ledger: InputSent,
    *,
    disarm: bool,
) -> None:
    """Release the button (and dispose the probe if *disarm*) EVEN IF cancelled.

    The cleanup runs as its own task behind :func:`asyncio.shield`, so a second
    cancellation (or an outer timeout) landing while it runs cannot skip the
    release or the disarm. That cancellation is then waited out for at most the
    cleanup's own two :data:`INPUT_BUDGET_S` bounds and RE-RAISED, so a
    cancellation always stays a cancellation and the whole cleanup stays bounded.
    """

    async def cleanup() -> None:
        await _let_go(tab, at, ledger)
        if disarm:
            await _disarm(element)

    work = asyncio.ensure_future(cleanup())
    try:
        await asyncio.shield(work)
    except asyncio.CancelledError:
        await asyncio.wait({work}, timeout=2 * INPUT_BUDGET_S)
        raise


async def _moved(
    tab: Tab, element: Element, x: float, y: float, ledger: InputSent
) -> dict[str, object] | None:
    """Move to (x, y) and answer the ``mousemove`` the page received, if any."""
    await _arm(element, ["mousemove"])
    try:
        await _send_mouse(tab, "mouseMoved", x, y, ledger)
        seen = await _read(element)
    except BaseException:
        await _disarm(element)
        raise
    return seen[-1] if seen else None


def _lands(landed: tuple[float, float], aimed: tuple[float, float]) -> bool:
    """Whether a page-observed point is the aimed one, within rounding."""
    return all(
        abs(landed[i] - aimed[i])
        <= LANDING_TOLERANCE + LANDING_TOLERANCE_RELATIVE * abs(aimed[i])
        for i in (0, 1)
    )


def _landed(
    seen: dict[str, object], offset: tuple[float, float]
) -> tuple[float, float]:
    """Where the page saw a move, in the top-level viewport's CSS px."""
    return (_number(seen.get("x")) + offset[0], _number(seen.get("y")) + offset[1])


async def aim_input(  # noqa: PLR0913  PERMANENT(the click's input ledger rides with every send)
    tab: Tab,
    element: Element,
    selector: str,
    aimed: tuple[float, float],
    offset: tuple[float, float],
    ledger: InputSent,
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
        seen = await _moved(tab, element, *sent, ledger)
        if seen is not None:
            break
    if seen is None:
        return None
    for _ in range(MAX_PROBES - 1):
        landed = _landed(seen, offset)
        if _lands(landed, aimed):
            return sent[axis] / aimed[axis]
        if landed[axis] <= 0 or sent[axis] <= 0:
            raise ToolError(
                f"could not aim the click on '{selector}': input at "
                f"({sent[0]:g}, {sent[1]:g}) reached the page at "
                f"({landed[0]:g}, {landed[1]:g}), which names no scale"
            )
        k = sent[axis] / landed[axis]
        sent = (aimed[0] * k, aimed[1] * k)
        seen = await _moved(tab, element, *sent, ledger)
        if seen is None:
            raise ToolError(
                f"could not aim the click on '{selector}': input rescaled to "
                f"({sent[0]:g}, {sent[1]:g}) did not reach the page"
            )
    landed = _landed(seen, offset)
    if not _lands(landed, aimed):
        raise ToolError(
            f"could not aim the click on '{selector}': input at CSS "
            f"({aimed[0]:g}, {aimed[1]:g}) still reaches the page at "
            f"({landed[0]:g}, {landed[1]:g}) after {MAX_PROBES} probes"
        )
    return sent[axis] / aimed[axis]


def _verify(
    selector: str,
    aimed: tuple[float, float],
    facts: dict[str, object],
    seen: list[dict[str, object]],
) -> None:
    """Raise unless the page's own ``mousedown`` landed inside the element.

    Nothing is claimed for an element the hit-test did not name, and a disabled
    control is exempt: Chrome sends it no ``mousedown`` at all (measured, plain
    and inside a ``<fieldset disabled>``), so silence from it is the answer
    ``reason`` already gives. That holds for the target or anything inside a
    disabled ancestor AND for a disabled control under the point when the target
    is an enabled wrapper around it.
    """
    if not facts.get("hit_is_target") or facts.get("disabled"):
        return
    if facts.get("under_disabled"):
        return
    x, y = aimed
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


async def _plain_click(tab: Tab, element: Element, ledger: InputSent) -> None:
    """nodriver's ``Element.mouse_click``, its press and release sent from here.

    The same point -- the first content quad's centre -- and the same silence
    for a node with no quads (nodriver returns without input there). What is
    not kept is its trailing ``Tab.flash_point``: a marker injected into the
    page AFTER the release, whose failure reached the caller as a plain error
    and was answered with a SECOND, synthetic click (T-10034 review). Owning
    the two sends is also what lets *ledger* see them.
    """
    position = await element.get_position()
    if position is None:
        return
    at = position.center
    try:
        await _send_mouse(tab, "mousePressed", *at, ledger)
    except BaseException:
        await _wind_down(tab, element, at, ledger, disarm=False)
        raise
    await _send_mouse(tab, "mouseReleased", *at, ledger)


#: The synthetic script's three answers. Ours, not the page's: every page
#: call inside the script is wrapped, so nothing the page throws or returns can
#: become the script's answer, and ``Element.apply`` -- which hands back
#: ``result[0].value`` and drops ``exceptionDetails``, so a THROWN value lands
#: there as a plain one -- can only carry one of these from a script that ran.
SYNTHETIC_DETACHED = "stealth.click.synthetic:detached"
SYNTHETIC_CLICKED = "stealth.click.synthetic:clicked"
SYNTHETIC_THREW = "stealth.click.synthetic:threw"

#: THE one synthetic sender's script (T-10034). The connectivity check and the
#: ``el.click()`` run in ONE page-side task, so the page cannot remove the node
#: between them. nodriver's ``Element.click`` is not used: it awaits a
#: ``resolveNode`` and a ``flash`` (a marker injected into the page) before its
#: unguarded ``(el) => el.click()``, and a node removed in that window is
#: clicked where no listener in the page can hear it.
#:
#: The answer is a token, never a boolean (Codex re-review HIGH): a page whose
#: ``click()`` calls the native one and then ``throw false`` would otherwise
#: hand back the very ``false`` that means "nothing ran", and the click would be
#: re-run on a page that had already taken it. A page that makes
#: ``isConnected`` read false is taken at its word: nothing is clicked here.
#: That includes a page whose own ``isConnected`` getter clicks and then reads
#: false: this script runs in the page's world, so it cannot tell (Codex round
#: 4, accepted until T-10299 moves these reads into an isolated world).
SYNTHETIC_JS = (
    """(elem) => {
    __CONNECTIVITY__
    const click = HTMLElement.prototype.click;
    const state = connectivity(elem);
    if (state === "__NO__") return "__DETACHED__";
    if (state !== "__YES__") return "__THREW__";
    try { click.call(elem); } catch (e) { return "__THREW__"; }
    return "__CLICKED__";
}""".replace("__CONNECTIVITY__", CONNECTIVITY_JS)
    .replace("__NO__", CONNECTED_NO)
    .replace("__YES__", CONNECTED_YES)
    .replace("__DETACHED__", SYNTHETIC_DETACHED)
    .replace("__CLICKED__", SYNTHETIC_CLICKED)
    .replace("__THREW__", SYNTHETIC_THREW)
)


async def synthetic_click(element: Element, selector: str, ledger: InputSent) -> bool:
    """The ONE synthetic ``el.click()``: ``False`` when the page said it left.

    Sent through ``Element.apply`` (``Runtime.callFunctionOn`` with
    ``userGesture``, as ``Element.click`` sends it), bounded by
    :data:`INPUT_BUDGET_S`. ONLY the exact :data:`SYNTHETIC_DETACHED` answer
    proves ``el.click()`` never ran, so only it leaves *ledger* clear and
    answers ``False`` for the caller to re-resolve. :data:`SYNTHETIC_CLICKED` is
    the click. Everything else -- the page's ``click()`` throwing, any other
    value, a failure, a timeout or a cancellation -- proves nothing about
    whether the page took the click, so *ledger* is set and it is never re-run.

    Raises ``ToolError`` for :data:`SYNTHETIC_THREW` and for any answer that is
    not one of the script's own, saying the click may have reached the page.
    """
    try:
        answer = await asyncio.wait_for(element.apply(SYNTHETIC_JS), INPUT_BUDGET_S)
    except BaseException:
        ledger.sent = True
        raise
    token = answer if isinstance(answer, str) else None
    if token == SYNTHETIC_DETACHED:
        return False
    ledger.sent = True
    if token == SYNTHETIC_CLICKED:
        return True
    if token == SYNTHETIC_THREW:
        raise ToolError(
            f"the page's click handler for '{selector}' threw during the "
            "synthetic click; it may have reached the page, so it was not repeated"
        )
    raise ToolError(
        f"the synthetic click on '{selector}' answered with "
        f"{type(answer).__name__}, not one of its own answers; it may have "
        "reached the page, so it was not repeated"
    )


async def click(
    tab: Tab,
    element: Element,
    selector: str,
    facts: dict[str, object],
    ledger: InputSent,
) -> None:
    """The ONE coordinate click, correct under any device-metrics override.

    *facts* is :func:`aim`'s answer. An element with no hit-testable point (no
    box, or a box outside the viewport) is clicked at its quad centre unchanged
    (:func:`_plain_click`): no input could reach it, so there is nothing to
    measure. *ledger* records whether any input went out (:class:`InputSent`).

    The point dispatched is the centre of the first content quad, in the top-level
    viewport — what ``Element.mouse_click`` has always used, and the only space
    that is right inside an iframe. Once ``mousePressed`` is sent everything is
    a ``ToolError``, because a second click after a delivered one must never
    happen. A plain exception may come out before that, and the caller may
    answer it with the synthetic click or a re-run ONLY while ``ledger.sent``
    is false: a probe ``mouseMoved``, or the plain path's press, may already
    have reached the page.

    Raises ``ToolError`` when the input cannot be aimed, when the page did not
    report the press although it was there to ask, or when its own ``mousedown``
    landed outside an element the hit-test said the point was over.
    """
    point = facts.get("point")
    if not isinstance(point, dict) or facts.get("hit") is None:
        await _plain_click(tab, element, ledger)
        return
    position = await element.get_position()
    if position is None:
        # ``Element.mouse_click`` asks the same question and returns there.
        return
    x, y = position.center
    offset = (x - _number(point.get("x")), y - _number(point.get("y")))
    expected = bool(facts.get("hit_is_target"))
    k = await aim_input(tab, element, selector, (x, y), offset, ledger)
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
        await _send_mouse(tab, "mousePressed", *at, ledger)
    except BaseException as failure:
        # A timeout or a cancellation does not prove the press was not delivered,
        # so the button is let go whatever happened.
        await _wind_down(tab, element, at, ledger, disarm=True)
        if not isinstance(failure, Exception):
            raise
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
    except BaseException:
        await _wind_down(tab, element, at, ledger, disarm=False)
        raise
    try:
        await _send_mouse(tab, "mouseReleased", *at, ledger)
    except BaseException as failure:
        # N2, accepted: a release whose reply was lost may HAVE been delivered, and
        # this sends a second one, so the page can see a duplicate ``mouseup``. The
        # alternative is to send none and risk a button left held, which is the
        # worse failure; the two cases cannot be told apart from here (a timeout
        # proves nothing), and a stray ``mouseup`` with no press pending does
        # nothing in Chrome's click machinery.
        await _wind_down(tab, element, at, ledger, disarm=False)
        if not isinstance(failure, Exception):
            raise
        raise ToolError(
            f"click on '{selector}' pressed but could not release: "
            f"{type(failure).__name__}"
        ) from failure
    if gone is None:
        _verify(selector, (x, y), facts, seen)
