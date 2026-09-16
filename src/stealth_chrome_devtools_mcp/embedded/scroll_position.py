"""THE one home for "which element is the page's scroller, where is it
scrolled, and has it stopped" (F-875, F-878).

``scroll_page`` used to end like this::

    await tab.evaluate(script)
    await asyncio.sleep(0.5 if smooth else 0.1)
    return True

``True`` reported that *the evaluate did not throw*, and the docstring promised
that *the page scrolled*. Measured on 2.1.6 against real Chrome 152 those are
three different states and the tool answered ``True`` in all of them: the scroll
arrived; the scroll was still in flight (4910 of 7039 px on an 8016 px document
when the 0.5 s nap ended, and 1747 of 1815 on a real stackoverflow page); and
there was nothing to scroll at all (a Cloudflare interstitial exactly one
viewport tall, ``scrollY`` ``0`` before and after). Telling them apart needs
three things this module owns and the tool composes:

**A pick.** :func:`scroller` answers "which element IS the page's scroller on
this axis" (F-878). F-875 left this open, and measured it: on twelve ``data:``
fixtures against real Chrome 152, ``document.scrollingElement`` alone is the
right answer **4 times out of 12**, because ``body{overflow:hidden}`` plus a
scrolling ``div`` — the app shell every SPA starter template ships — is a page
``window.scrollTo`` cannot move at all. The rule is document-first, then the
largest viewport-clipped area on the requested axis; :data:`SCROLLER_JS` carries
it and ``audit/stage2/finding_F878_scroll_page_nested_scroller.md`` carries the
matrix that chose it over the alternatives.

**A read.** :func:`read` asks the page for its scroll offsets and its extent in
ONE ``JSON.stringify`` round trip. It is ``JSON.stringify`` for the reason
``page_storage`` (F-869) and ``js_aspect_answer`` (F-872) are: ``Tab.evaluate``
always sends ``SerializationOptions(serialization="deep")`` and hands back
``deep_serialized_value.value`` raw, so an object literal arrives as BiDi
``RemoteValue`` nodes at every depth while a *string* arrives intact. The
offsets are ``Math.round``ed because sub-pixel positions are real (zoom, HiDPI,
the tail of a smooth animation) and a settle that compares floats would never
see two reads agree.

**A settle.** :func:`settle` polls that read until two consecutive answers agree,
bounded by :data:`SETTLE_BUDGET_SECONDS` — a settle, not a sleep. A fixed nap
cannot be right for both cases it was serving: an instant scroll is done before
the first frame, and the smooth scroll measured above was still moving three
seconds in. Polling costs the instant case ~0.1 s (two reads one interval apart)
and gives the smooth case as long as it actually needs.

:data:`START_GRACE_SECONDS` is the one subtlety. A smooth scroll begins on the
next animation frame, so for the first few milliseconds "has not started" and
"will never move" look identical, and two agreeing reads would settle on the
BEFORE position and report ``scrolled: False`` — a new lie in place of the old
one. While the agreed position still equals the origin, the grace has to pass
before that counts as settled. The caller skips the grace (``start_grace=0``)
when it already knows the page is at the requested edge, because then there is
nothing to wait for: that is what keeps "one viewport tall" on the fast path.

**What this module does not decide.** It never says whether a scroll
*succeeded*: only which element it is about, where that element is, what its
extent is (:func:`Position.at_edge`), and whether the window has been spent.
It also never *judges* its own pick — which is why the record names the element
it drove, so a caller who disagrees can see what to disagree with. Composing
that into the tool's record — and choosing the budget to bound it with — is
``dom_handler.scroll_page``'s, because only the caller knows what was asked for.

A leaf: it imports no embedded module but ``tool_errors``, takes the tab as an
argument, and holds no state. The timing seam is the two module functions
:func:`_now` and :func:`_sleep`, in the pattern of ``scheduling_lag``.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, NamedTuple

from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps this module a leaf
    from nodriver import Tab

#: The placeholder :func:`scroller`'s axis and the chosen element's index path
#: are substituted into. A plain ``str.replace`` and not ``str.format``, because
#: every script in this module is full of JS object literals and doubling their
#: braces to protect them from a formatter is a transcription error waiting to
#: happen.
_AXIS_SLOT = "__AXIS__"
_PATH_SLOT = "__PATH__"

#: Slack in the area comparison :data:`SCROLLER_JS` ranks candidates by, and the
#: reason it exists (F-878 §3.3, fixture k): a scroll container is narrower than
#: its parent by exactly the scrollbar's width, so a plain ``area >`` let a
#: ``body`` that could move 20 px outrank a shell that could move 7023. The
#: measured artefact is **0.8 %** (1873 of 1888 px), so this band is six times
#: it, while the smallest REAL difference in the matrix — a reading pane against
#: a message list — is 29 %, an order of magnitude outside it. Within the band
#: the larger scrollable extent wins; an exact tie falls to document order,
#: which prefers the OUTER of two nested candidates, agreeing with the rule's
#: own preference for the bigger box.
AREA_SLACK = 0.05

#: Bounds on the identity :data:`READ_JS` reports for the element it read. A tag
#: name is the browser's; an ``id`` and a ``class`` are the PAGE's, and a
#: CSS-in-JS class name has no natural length — so they are clamped. Nothing
#: else about the element is ever read: no text, no attributes, no content.
SCROLLER_ID_CHARS = 64
SCROLLER_CLASS_CHARS = 32
SCROLLER_CLASSES = 4

#: THE one way a chosen scroller is addressed in a LATER round trip (F-878): an
#: index path of ``children`` offsets from ``document.documentElement``, or
#: ``null`` for the document scroller. A path and not a stashed reference,
#: because ``tab.evaluate`` is stateless and ``window.__something = el`` would
#: leave this tool's bookkeeping on the page. If the path no longer names an
#: element — the page re-rendered mid-scroll — this falls back to the document
#: scroller, and :data:`READ_JS` reports the element it ACTUALLY read, so the
#: record cannot claim a div it did not drive.
_RESOLVE_JS = (
    "function _el(p){"
    "var d=document.scrollingElement||document.documentElement||document.body;"
    "if(p===null){return d;}"
    "var e=document.documentElement;"
    "for(var i=0;i<p.length&&e;i++){e=e.children[p[i]];}"
    "return e||d;"
    "}"
)

#: THE one home for "which element IS the page's scroller on this axis"
#: (F-878). One ``JSON.stringify`` round trip, one pick per call — measured at
#: 2.59 ms on a 6007-element page, which is cheap once and half a second of the
#: page's main thread if it were re-asked on every settle poll.
#:
#: The rule, and the twelve-fixture matrix behind it, is
#: ``audit/stage2/finding_F878_scroll_page_nested_scroller.md``:
#:
#: 1. **if the document scroller can move on this axis, it IS the scroller.** A
#:    precedence, not a tie-break: that is what the window scrolls and what the
#:    wheel scrolls at rest, and it is why a 400 px scrollable box inside a
#:    6023 px scrolling document loses (fixture d) without the rule having to be
#:    argued out of choosing it;
#: 2. else the candidate with the largest VIEWPORT-CLIPPED area, among elements
#:    that can move on this axis and whose computed overflow on this axis is
#:    ``auto``/``scroll``, with :data:`AREA_SLACK` and the larger extent
#:    breaking near-ties. Area and not the element under the viewport centre:
#:    the centre walks INWARD to a page's own scrollable widget (fixture g) and
#:    is blind behind a ``position:fixed`` scrim (fixture h), and it depends on
#:    a single point that a layout shift can move;
#: 3. else the document scroller anyway — "nothing scrolls" is an answer.
#:
#: ``overflow: hidden`` is deliberately NOT a candidate even though ``scrollTop``
#: would move it: a page that hid its scrollbar meant that element not to be
#: scrolled, and ``html,body{overflow:hidden}`` is the app-shell marker itself.
SCROLLER_JS = (
    "JSON.stringify((function(axis){"
    "var vertical=axis==='y';"
    "var d=document.scrollingElement||document.documentElement||document.body;"
    "function ext(e){"
    "return vertical?e.scrollHeight-e.clientHeight:e.scrollWidth-e.clientWidth;"
    "}"
    "if(d&&ext(d)>0){return {path:null,document:true};}"
    "var vw=window.innerWidth,vh=window.innerHeight;"
    "var best=null,bestArea=0,bestExtent=0;"
    "var all=document.querySelectorAll('*');"
    "for(var i=0;i<all.length;i++){"
    "var e=all[i];"
    "if(e===d){continue;}"
    "var x=ext(e);"
    "if(x<=0){continue;}"
    "var cs=window.getComputedStyle(e);"
    "var ov=vertical?cs.overflowY:cs.overflowX;"
    "if(ov!=='auto'&&ov!=='scroll'){continue;}"
    "var r=e.getBoundingClientRect();"
    "var w=Math.max(0,Math.min(r.right,vw)-Math.max(r.left,0));"
    "var h=Math.max(0,Math.min(r.bottom,vh)-Math.max(r.top,0));"
    "var a=w*h;"
    "if(a<=0){continue;}"
    f"if(best===null||a>bestArea*{1 + AREA_SLACK}||"
    f"(a>=bestArea*{1 - AREA_SLACK}&&x>bestExtent)){{"
    "bestArea=Math.max(bestArea,a);bestExtent=x;best=e;"
    "}"
    "}"
    "if(best===null){return {path:null,document:true};}"
    "var p=[],e2=best;"
    "while(e2&&e2!==document.documentElement){"
    "var kids=e2.parentElement?e2.parentElement.children:null;"
    "if(!kids){return {path:null,document:true};}"
    "var k=-1;"
    "for(var j=0;j<kids.length;j++){if(kids[j]===e2){k=j;break;}}"
    "if(k<0){return {path:null,document:true};}"
    "p.unshift(k);e2=e2.parentElement;"
    "}"
    "if(e2!==document.documentElement){return {path:null,document:true};}"
    "return {path:p,document:false};"
    "})(" + _AXIS_SLOT + "))"
)

#: Offsets AND extent in one round trip, as a JSON *string* (module docstring),
#: for the element :func:`scroller` chose — ``document.scrollingElement`` when
#: that is the answer, which is what CSSOM-View names as the element
#: ``window.scrollTo``/``scrollBy`` actually move (``html`` in standards mode,
#: ``body`` in quirks; F-878 measured both). The offsets are the ELEMENT's
#: ``scrollLeft``/``scrollTop``, which for the document scroller is
#: ``window.scrollX``/``scrollY`` by definition and was measured equal to it in
#: both compatibility modes — one expression, no branch.
#:
#: It also reports WHAT it read (tag, id, bounded classes, and whether that is
#: the document scroller). The identity belongs to the READ and not to the pick
#: precisely so the record cannot lie: a path that went stale falls back to the
#: document, and the record then names the document.
READ_JS = (
    "JSON.stringify((function(){"
    + _RESOLVE_JS
    + "var d=document.scrollingElement||document.documentElement||document.body;"
    "var e=_el(" + _PATH_SLOT + ");"
    "var c=e.className;"
    "if(c&&c.baseVal!==undefined){c=c.baseVal;}"
    "var names=String(c||'').trim().split(/\\s+/);"
    "var out=[];"
    f"for(var i=0;i<names.length&&out.length<{SCROLLER_CLASSES};i++){{"
    f"if(names[i]){{out.push(names[i].slice(0,{SCROLLER_CLASS_CHARS}));}}"
    "}"
    "return {"
    "x:Math.round(e.scrollLeft||0),"
    "y:Math.round(e.scrollTop||0),"
    "max_x:Math.max(0,Math.round(e.scrollWidth-e.clientWidth)),"
    "max_y:Math.max(0,Math.round(e.scrollHeight-e.clientHeight)),"
    "tag:String(e.tagName||'').toLowerCase(),"
    f"id:String(e.id||'').slice(0,{SCROLLER_ID_CHARS}),"
    "classes:out,"
    "document:e===d"
    "};"
    "})())"
)

#: How long :func:`settle` will wait for the position to stop changing. Sized
#: from the finding's own measurement: the worst observed smooth scroll (0 →
#: 7039 px on an 8016 px document) arrived between the 0.5 s sample and the 3 s
#: one, so 10 s is roughly 3x the measured worst case. It is also a THIRD of
#: ``tool_runtime.CDP_OPERATION_TIMEOUT`` (30 s), which is the bound that
#: matters in the other direction: a settle that outran the CDP budget would
#: turn "still moving" into a raised timeout, and the whole point of this
#: finding is that a slow scroll gets REPORTED rather than claimed or raised.
SETTLE_BUDGET_SECONDS = 10.0

#: Gap between polls. Two agreeing reads is the cheapest honest answer an
#: instant scroll can get, so this doubled is the fast path (~0.1 s) — the same
#: order as the 0.1 s nap it replaces for ``smooth=False``.
POLL_INTERVAL_SECONDS = 0.05

#: Minimum time a reading EQUAL to the origin must persist before it counts as
#: settled (module docstring). Six poll intervals: a smooth scroll starts on the
#: next animation frame (~16 ms at 60 Hz), and the finding's 8016 px scroll had
#: already travelled thousands of pixels by 0.5 s, so 0.3 s is far past "has it
#: begun" while staying an order of magnitude under the budget.
START_GRACE_SECONDS = 0.3

#: Slack on the FAR edge in :meth:`Position.at_edge` (see its docstring): the
#: offset and the extent are rounded independently, so one pixel of disagreement
#: is reachable under fractional zoom without the page being able to move.
EDGE_TOLERANCE_PX = 1

#: The four keys :data:`READ_JS` promises. Named so a malformed answer is
#: reported by SHAPE rather than by echoing whatever the page sent.
_KEYS = ("x", "y", "max_x", "max_y")


class _Direction(NamedTuple):
    """What one direction name means: which way, and the JS that goes there."""

    axis: str
    towards_max: bool
    script: str


#: THE one reading of a direction — its axis, the edge it heads for, and the JS
#: that moves the page that way. One table and not two (a script table beside an
#: edge table) so a seventh direction cannot arrive in one of them only.
#:
#: ``{negative}`` is the pre-negated amount because a literal ``-{amount}``
#: turned a negative amount into JS's decrement operator (``--500``), a syntax
#: error. ``{target}`` is whatever :func:`scroller` chose and ``{extent}`` is
#: the element whose ``scrollHeight`` ``bottom`` heads for — the SAME element
#: :data:`READ_JS` measures ``max_y`` off, so the target and the reported extent
#: cannot disagree. For a document scroller ``{target}`` is literally ``window``
#: (F-878): the generated JS is then byte-identical to F-875's, which is the
#: mechanical form of "rule 1 changes nothing about a plain page".
_DIRECTIONS: dict[str, _Direction] = {
    "down": _Direction(
        "y", True, "{target}.scrollBy({{top: {amount}, left: 0, behavior: {behavior}}})"
    ),
    "up": _Direction(
        "y",
        False,
        "{target}.scrollBy({{top: {negative}, left: 0, behavior: {behavior}}})",
    ),
    "right": _Direction(
        "x", True, "{target}.scrollBy({{top: 0, left: {amount}, behavior: {behavior}}})"
    ),
    "left": _Direction(
        "x",
        False,
        "{target}.scrollBy({{top: 0, left: {negative}, behavior: {behavior}}})",
    ),
    "top": _Direction(
        "y", False, "{target}.scrollTo({{top: 0, left: 0, behavior: {behavior}}})"
    ),
    "bottom": _Direction(
        "y",
        True,
        "{target}.scrollTo({{top: {extent}.scrollHeight, left: 0, "
        "behavior: {behavior}}})",
    ),
}

#: The directions a caller may ask for.
DIRECTIONS = frozenset(_DIRECTIONS)

#: What ``{target}``/``{extent}`` become for the DOCUMENT scroller. ``window``
#: keeps ``window.scrollTo``/``scrollBy`` as the literal path a plain page is
#: driven by; the extent is read off the element CSSOM View says that moves.
_WINDOW_TARGET = "window"
_WINDOW_EXTENT = "(document.scrollingElement||document.documentElement)"


class Scroller(NamedTuple):
    """WHICH element this call will scroll and read — addressing only.

    It carries no identity on purpose: what the record says about the element
    comes from the READ (:data:`READ_JS`), so a stale path that fell back to the
    document cannot be reported as the div it hoped for.
    """

    #: ``children`` offsets from ``document.documentElement``, or ``None`` for
    #: the document scroller.
    path: tuple[int, ...] | None
    #: Is the chosen element ``document.scrollingElement``?
    is_document: bool

    @property
    def js(self) -> str:
        """This scroller as a JS expression, for ``{target}``/``{extent}``."""
        if self.path is None:
            return _WINDOW_TARGET
        return f"_el({list(self.path)})"


#: The document scroller, for the one caller that has not asked yet.
DOCUMENT = Scroller(None, True)


def _validate(direction: str, amount: int) -> _Direction:
    """The whole request, checked from the request alone — no round trip.

    *amount* is a distance, and *direction* is the only thing that carries a
    sign. A negative *amount* is therefore REJECTED rather than interpreted:
    there is exactly one way to scroll up and it is ``direction="up"``. Before
    F-875 the two readings of a negative amount were both wrong and differently
    wrong — ``down`` with ``-500`` silently scrolled UP, while ``up`` with
    ``-500`` interpolated ``top: --500`` and died as a JS syntax error. Making
    it a magnitude instead would keep the silence and only move it (the record
    would say ``direction: "up"`` about a page that went down), which is the
    class of untruth this whole finding is about.

    This is its own function because since F-878 the FIRST round trip the tool
    makes is the scroller pick, not the scroll: both it and :func:`script` have
    to refuse a bad request, and "costs no round trip" has to stay true of the
    call that now happens first.

    Raises:
        ToolError: *direction* is not one of :data:`DIRECTIONS`, or *amount* is
            negative.
    """
    known = _DIRECTIONS.get(direction)
    if known is None:
        raise ToolError(f"Invalid scroll direction: {direction}")
    if amount < 0:
        raise ToolError(
            f"Invalid scroll amount: {amount}. It is a distance in pixels and "
            "cannot be negative — the direction carries the sign, so scroll the "
            "other way with direction='up' / 'left' instead."
        )
    return known


def script(direction: str, amount: int, smooth: bool, on: Scroller = DOCUMENT) -> str:
    """The JS that scrolls *on* by *amount* pixels *direction*, smoothly or not.

    Args:
        direction: one of :data:`DIRECTIONS`.
        amount: pixels, never negative (see :func:`_validate`); ignored by
            ``top``/``bottom``.
        smooth: ``behavior: 'smooth'`` rather than ``'instant'``.
        on: the element :func:`scroller` chose. The default is the document
            scroller, which produces exactly the JS F-875 produced.

    Raises:
        ToolError: see :func:`_validate`.
    """
    known = _validate(direction, amount)
    body = known.script.format(
        target=on.js,
        extent=_WINDOW_EXTENT if on.path is None else on.js,
        amount=amount,
        negative=-amount,
        behavior="'smooth'" if smooth else "'instant'",
    )
    if on.path is None:
        return body
    return "(function(){" + _RESOLVE_JS + "return " + body + ";})()"


async def scroller(tab: Tab, direction: str, amount: int) -> Scroller:
    """Which element IS the page's scroller on *direction*'s axis (F-878)?

    One :data:`SCROLLER_JS` round trip, once per ``scroll_page`` call. It
    validates the whole request first, because this is the FIRST thing the tool
    asks the page and a bad request must still cost nothing.

    Raises:
        ToolError: the request is invalid (:func:`_validate`), or the evaluate
            did not answer with the JSON :data:`SCROLLER_JS` promises — shape
            and count only, never the page's own text.
    """
    known = _validate(direction, amount)
    data = _json_answer(
        await tab.evaluate(SCROLLER_JS.replace(_AXIS_SLOT, f"'{known.axis}'")),
        "scroller",
    )
    path = data.get("path")
    if path is None:
        return DOCUMENT
    if not isinstance(path, list) or not all(
        isinstance(step, int) and not isinstance(step, bool) and step >= 0
        for step in path
    ):
        raise ToolError(
            "Could not read the page's scroller: the answer's path was "
            f"{type(path).__name__} with {len(path) if isinstance(path, list) else 0} "
            "steps, not the list of child offsets the pick asks for."
        )
    return Scroller(tuple(path), bool(data.get("document")))


def _now() -> float:
    """The one clock (monotonic — a wall-clock step must not end a settle)."""
    return time.monotonic()


async def _sleep(seconds: float) -> None:
    """The one wait."""
    await asyncio.sleep(seconds)


class Position(NamedTuple):
    """Where the page is scrolled, how far it could be, and WHAT was read.

    ``max_x``/``max_y`` are ``0`` for a scroller that fits its viewport — the
    honest description of a page with nothing to scroll, not an error.

    The two numeric halves mean different things and must never be compared
    together. :attr:`offset` is WHERE THE PAGE IS; ``max_x``/``max_y`` describe
    the CONTENT, which a lazy-loading page grows while standing perfectly still.
    Comparing whole ``Position`` values conflates them, and both ways round are
    lies this module exists to prevent: content appended below a stationary
    viewport would read as "it scrolled", and a page that had stopped moving but
    was still loading would never settle.

    The identity (``tag``/``element_id``/``classes``/``is_document``) is here and
    not on :class:`Scroller` because a scroll position is meaningless without
    saying what was measured, and because the READ is the only place that knows
    what was measured: a path that went stale falls back to the document, and
    this then names the document (F-878).
    """

    x: int
    y: int
    max_x: int
    max_y: int
    tag: str = ""
    element_id: str = ""
    classes: tuple[str, ...] = ()
    is_document: bool = True

    @property
    def offset(self) -> tuple[int, int]:
        """Where the page is scrolled — the ONLY part that means "moved"."""
        return (self.x, self.y)

    def moved_from(self, before: Position) -> bool:
        """Did the page actually MOVE between *before* and this reading?

        THE one home for that comparison, so no caller can reach for the halves
        itself. Two conditions, and both are about a lie the record could
        otherwise tell:

        * the offsets differ — never the extent, because a lazy-loading page
          grows its content while standing perfectly still (see the class
          docstring);
        * the two readings are about the SAME element. A path that went stale
          mid-scroll makes *before* the div's offset and this one the
          document's, and the difference between two different elements'
          offsets is not a distance anything travelled (F-878).
        """
        return self.is_document == before.is_document and self.offset != before.offset

    @property
    def descriptor(self) -> dict[str, object]:
        """The element that was read, as the record carries it — SHAPE ONLY.

        A tag name, an ``id`` and the classes, each bounded by
        :data:`SCROLLER_ID_CHARS` / :data:`SCROLLER_CLASS_CHARS` /
        :data:`SCROLLER_CLASSES` in the page itself. Never the element's text,
        its attributes or its content: this exists so a caller can SEE which
        element a judgement picked, not to describe the page.
        """
        return {"tag": self.tag, "id": self.element_id, "classes": list(self.classes)}

    def at_edge(self, direction: str) -> bool:
        """Is the page already as far as *direction* can take it?

        The one place a direction is turned into an edge, so the tool's record
        and its settle's fast path cannot come to disagree about it. A page with
        nothing to scroll (``max_y == 0``) is at BOTH vertical edges, which is
        the literal truth about it.

        The far edge carries :data:`EDGE_TOLERANCE_PX` of slack. The offset and
        the extent are rounded independently (``Math.round(window.scrollY)``
        against an already-integer ``scrollHeight - clientHeight``), so under
        fractional zoom or a non-integer device pixel ratio a page resting at
        its true bottom can read one pixel short of it. One pixel of tolerance
        is cheaper than reporting ``at_edge: false`` for a page that cannot go
        further; the near edge needs none, because ``scrollY`` is never below 0.
        """
        known = _DIRECTIONS.get(direction)
        if known is None:
            raise ToolError(f"Invalid scroll direction: {direction}")
        offset, limit = (
            (self.y, self.max_y) if known.axis == "y" else (self.x, self.max_x)
        )
        if known.towards_max:
            return offset >= limit - EDGE_TOLERANCE_PX
        return offset <= 0


class Settled(NamedTuple):
    """The outcome of one settle window."""

    #: Where the page was on the last read.
    position: Position
    #: Did two consecutive reads agree before the budget ran out?
    settled: bool
    #: How long the window actually cost, in seconds.
    seconds: float


def _json_answer(raw: object, what: str) -> dict[str, object]:
    """The one ladder for "did the evaluate answer with the JSON we asked for".

    Shared by :func:`read` and :func:`scroller` because both ask the page one
    ``JSON.stringify`` question and both have the same answer to a non-answer:
    it is operational failure (DESIGN §9), never a zero. Reporting ``scroll_y:
    0`` for a read that did not happen — or "the document" for a pick that did
    not happen — is the same class of untruth F-875 and F-878 exist to remove.

    Every message reports SHAPE and COUNT only: a type name, a character count,
    a field count. Never the page's own text.
    """
    if not isinstance(raw, str):
        raise ToolError(
            f"Could not read the page's {what}: the evaluate answered with "
            f"{type(raw).__name__}, not the JSON string it asks for."
        )
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ToolError(
            f"Could not read the page's {what}: the evaluate answered with "
            f"{len(raw)} characters that are not JSON."
        ) from exc
    if not isinstance(data, dict):
        raise ToolError(
            f"Could not read the page's {what}: the answer was "
            f"{type(data).__name__}, not the object it asks for."
        )
    return data


async def read(tab: Tab, on: Scroller = DOCUMENT) -> Position:
    """*on*'s scroll offsets, extent and identity, in one round trip.

    Args:
        tab: the tab to read.
        on: the scroller :func:`scroller` chose; the document by default.

    Raises:
        ToolError: the evaluate did not answer with the JSON :data:`READ_JS`
            promises (see :func:`_json_answer`).
    """
    data = _json_answer(
        await tab.evaluate(READ_JS.replace(_PATH_SLOT, _path_js(on))),
        "scroll position",
    )
    if not all(isinstance(data.get(key), (int, float)) for key in _KEYS):
        raise ToolError(
            "Could not read the page's scroll position: the answer carried "
            f"{len(data)} fields but not the {len(_KEYS)} numbers it asks for."
        )
    classes = data.get("classes")
    return Position(
        *(int(data[key]) for key in _KEYS),
        tag=str(data.get("tag") or ""),
        element_id=str(data.get("id") or ""),
        classes=tuple(str(name) for name in classes)
        if isinstance(classes, list)
        else (),
        is_document=bool(data.get("document")),
    )


def _path_js(on: Scroller) -> str:
    """*on*'s index path as the JS literal :data:`READ_JS` resolves."""
    return "null" if on.path is None else str(list(on.path))


async def settle(
    tab: Tab,
    origin: Position,
    budget: float | None = None,
    start_grace: float | None = None,
    on: Scroller = DOCUMENT,
) -> Settled:
    """Poll :func:`read` until two consecutive answers agree, or *budget* ends.

    Args:
        tab: the tab to read.
        origin: where the page was before the scroll was asked for — the reading
            that :data:`START_GRACE_SECONDS` refuses to settle on early.
        budget: seconds to spend; :data:`SETTLE_BUDGET_SECONDS` when ``None``
            (read at call time, so the module global is the one knob).
        start_grace: seconds before a reading equal to *origin* may settle;
            :data:`START_GRACE_SECONDS` when ``None``. Pass ``0`` when the page
            is already at the edge the caller asked for — nothing will move, so
            there is nothing to wait for.
        on: the scroller :func:`scroller` chose. It is polled, not re-picked:
            the pick costs 2.59 ms of the page's main thread (measured on a
            6007-element page) and a full budget is ~200 polls, so re-asking
            would spend half a second answering a question whose answer does not
            change.

    Returns:
        Settled: the last reading, whether it settled, and what it cost.
    """
    limit = SETTLE_BUDGET_SECONDS if budget is None else budget
    grace = START_GRACE_SECONDS if start_grace is None else start_grace
    started = _now()
    previous: tuple[int, int] | None = None
    while True:
        # Read FIRST and sleep between reads, not before the first one: an
        # instant scroll is already applied when its evaluate returns, so the
        # fast path costs two reads and ONE interval. The case that first sleep
        # used to guard — a smooth scroll that has not begun — is the grace's,
        # and the grace still holds it.
        current = await read(tab, on)
        elapsed = _now() - started
        # OFFSETS only. What "has it stopped" asks about is the viewport, not
        # the document: an infinite-scroll page appends content for as long as
        # you let it, so a whole-``Position`` comparison would spend the entire
        # budget on a page that stopped moving in the first 100 ms and then
        # report ``settled: false`` about a stationary viewport. The EXTENT the
        # caller gets is the final read's, which is the freshest one there is.
        if current.offset == previous and (
            current.offset != origin.offset or elapsed >= grace
        ):
            return Settled(current, True, elapsed)
        if elapsed >= limit:
            return Settled(current, False, elapsed)
        previous = current.offset
        await _sleep(POLL_INTERVAL_SECONDS)
