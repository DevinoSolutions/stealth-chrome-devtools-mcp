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
three things this module owns and the tool composes.

**A pick.** :func:`scroller` answers "which element IS the page's scroller on
this axis" (F-878). F-875 left this open, and measured it: on twelve ``data:``
fixtures against real Chrome 152, ``document.scrollingElement`` alone is the
right answer **4 times out of 12**, because ``body{overflow:hidden}`` plus a
scrolling ``div`` — the app shell every SPA starter template ships — is a page
``window.scrollTo`` cannot move at all. The rule is document-first, then the
largest viewport-clipped area on the requested axis; :data:`SCROLLER_JS` carries
it and ``audit/stage2/finding_F878_scroll_page_nested_scroller.md`` carries the
matrix that chose it over the alternatives.

**A read.** :func:`read` asks the page for the chosen element's scroll offsets,
its extent, its identity and the end-of-scroll latch in ONE ``JSON.stringify``
round trip. It is ``JSON.stringify`` for the reason ``page_storage`` (F-869) and
``js_aspect_answer`` (F-872) are: ``Tab.evaluate`` always sends
``SerializationOptions(serialization="deep")`` and hands back
``deep_serialized_value.value`` raw, so an object literal arrives as BiDi
``RemoteValue`` nodes at every depth while a *string* arrives intact. The
offsets are ``Math.round``ed because sub-pixel positions are real (zoom, HiDPI,
the tail of a smooth animation).

**A settle.** :func:`settle` waits for the PAGE to say the scroll ended, bounded
by :data:`SETTLE_BUDGET_SECONDS`.

**Never "two consecutive reads agree".** That was F-875's first answer and it is
not a stop condition, it is a guess about timing. A document smooth scroll runs
on Chrome's COMPOSITOR thread while the offset is read on the MAIN thread and
only advances when a frame commits to main, so a blocked main thread holds a
mid-flight value for exactly as long as the long task — measured 120 ms → 121,
250 → 250, 400 → 400, i.e. unbounded — and CI gate run 35046780659 (macOS/ARM64)
reported ``scroll_y_after: 3498`` about a page that read 4898. No count of
agreeing reads and no fixed quiet window can be correct against that.

Instead :data:`SCROLL_JS` arms a one-shot ``scrollend`` listener in the SAME
round trip that performs the scroll, and :data:`READ_JS` reports that latch
alongside the offsets in the same round trip. ``scrollend`` fires when the scroll
position has finished changing, including at the end of a compositor-driven
smooth scroll, and it LATCHES — so jank can only delay our observation of it and
can never make a running scroll look finished.

**Where the listener goes is not obvious, and F-878 measured it** (Chrome 152,
the twelve fixtures). ``scrollend`` for a DOCUMENT scroll is dispatched at the
``document`` and reaches ``window``, and is NOT seen at
``document.scrollingElement``; ``scrollend`` for an ELEMENT scroll is dispatched
at that element and does NOT bubble to ``window`` or ``document``. So there is
one right target per scroller kind and both wrong choices fail silently in the
worst way — a listener on ``window`` for an app shell, or on the element for a
plain page, never fires, and the settle would burn its whole 10 s budget and then
report ``settled: false`` about a scroll that finished in a second. The event
target is therefore the SAME expression that receives the scroll: ``window`` for
the document scroller, the resolved element for a nested one. Both answer
``'onscrollend' in …`` truthfully, so the feature detect needs no branch.

**The one case that must not wait for it.** A scroll that moves nothing fires no
``scrollend`` at all — a page already at the requested edge, or with nothing to
scroll (measured: neither the element nor ``window`` latches for a no-op scroll
on a nested div). So :data:`SCROLL_JS` also reports ``moves``, computed
synchronously from the clamped target before any frame, against the SELECTED
scroller's own extent. That answer is exact, which is why it replaced the old
``at_edge`` guess, and it is what keeps a one-viewport page and an instant scroll
on the fast path.

:data:`START_GRACE_SECONDS` survives for the fallback alone: a browser with no
``onscrollend`` still has to settle by read agreement, and there a smooth scroll
that has not begun yet looks exactly like one that will never move.

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

#: The placeholders :func:`scroller`'s axis and the chosen element's index path
#: are substituted into. A plain ``str.replace`` and not ``str.format``, because
#: every script in this module is full of JS object literals and doubling their
#: braces to protect them from a formatter is a transcription error waiting to
#: happen. (:data:`SCROLL_JS` is the one exception — it IS formatted, so its
#: braces ARE doubled, and the resolver is concatenated in rather than embedded.)
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

#: Offsets, extent, identity AND the end-of-scroll latch in one round trip, as a
#: JSON *string* (module docstring), for the element :func:`scroller` chose —
#: ``document.scrollingElement`` when that is the answer, which is what CSSOM-View
#: names as the element ``window.scrollTo``/``scrollBy`` actually move (``html``
#: in standards mode, ``body`` in quirks; F-878 measured both). The offsets are
#: the ELEMENT's ``scrollLeft``/``scrollTop``, which for the document scroller is
#: ``window.scrollX``/``scrollY`` by definition and was measured equal to it in
#: both compatibility modes — one expression, no branch, and the same expression
#: :data:`SCROLL_JS` computes ``moves`` from, so the two cannot disagree.
#:
#: It carries ``ended`` — the latch :data:`SCROLL_JS` armed — because "has the
#: scroll finished" and "where is it" must come from the SAME round trip: two
#: separate reads could straddle the end of the animation and pair a finished
#: flag with a stale offset, which is the very shape this finding retires.
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
    "var s=window." + "__stealthMcpScroll" + ";"
    "var ended=!!(s&&s.ended);"
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
    "document:e===d,"
    "ended:ended"
    "};"
    "})())"
)

#: How long :func:`settle` will wait for the scroll to end. Sized from the
#: finding's own measurement: the worst observed smooth scroll (0 → 7039 px on an
#: 8016 px document) arrived between the 0.5 s sample and the 3 s one, so 10 s is
#: roughly 3x the measured worst case. It is also a THIRD of
#: ``tool_runtime.CDP_OPERATION_TIMEOUT`` (30 s), which is the bound that matters
#: in the other direction: a settle that outran the CDP budget would turn "still
#: moving" into a raised timeout, and the whole point of this finding is that a
#: slow scroll gets REPORTED rather than claimed or raised.
SETTLE_BUDGET_SECONDS = 10.0

#: Gap between polls. Two agreeing reads is the cheapest honest answer the
#: FALLBACK path can give, so this doubled is its fast path (~0.1 s) — the same
#: order as the 0.1 s nap it replaces for ``smooth=False``.
POLL_INTERVAL_SECONDS = 0.05

#: Minimum time a reading EQUAL to the origin must persist before it counts as
#: settled — **the read-agreement FALLBACK only** (module docstring). Six poll
#: intervals: a smooth scroll starts on the next animation frame (~16 ms at
#: 60 Hz), and the finding's 8016 px scroll had already travelled thousands of
#: pixels by 0.5 s, so 0.3 s is far past "has it begun" while staying an order of
#: magnitude under the budget.
START_GRACE_SECONDS = 0.3

#: Slack on the FAR edge in :meth:`Position.at_edge` (see its docstring): the
#: offset and the extent are rounded independently, so one pixel of disagreement
#: is reachable under fractional zoom without the page being able to move.
EDGE_TOLERANCE_PX = 1

#: The four NUMBERS :data:`READ_JS` promises. Named so a malformed answer is
#: reported by SHAPE rather than by echoing whatever the page sent.
_KEYS = ("x", "y", "max_x", "max_y")

#: The property the ``ended`` latch lives on. One namespaced name on ``window``
#: (even when the LISTENER is on a nested element), overwritten by every scroll,
#: with the previous scroll's listener removed from its own target first — so a
#: page never accumulates them and a stale latch can never answer for a newer
#: scroll.
LATCH = "__stealthMcpScroll"


class _Direction(NamedTuple):
    """What one direction name means: which way, and the JS that goes there.

    ``target_x``/``target_y`` are JS expressions for the offsets the scroll is
    AIMED at, in terms of the current ``x0``/``y0``, evaluated in the same task
    as the scroll itself. Clamped against the extent they give :data:`SCROLL_JS`
    its ``moves`` answer — "will anything actually change" — without waiting for
    a frame, which is what lets a no-op scroll answer at once while a real one
    waits for the page to say it finished. ``top`` and ``bottom`` pass
    ``left: 0``, so their ``target_x`` is ``0`` and not ``x0``: they really do
    move a horizontally-scrolled page back to the left edge.
    """

    axis: str
    towards_max: bool
    script: str
    target_x: str
    target_y: str


#: THE one reading of a direction — its axis, the edge it heads for, and the JS
#: that moves the page that way. One table and not two (a script table beside an
#: edge table) so a seventh direction cannot arrive in one of them only.
#:
#: ``{negative}`` is the pre-negated amount because a literal ``-{amount}``
#: turned a negative amount into JS's decrement operator (``--500``), a syntax
#: error. ``{target}`` is whatever :func:`scroller` chose — literally ``window``
#: for a document scroller, so the generated scroll expression is byte-identical
#: to F-875's — and ``E`` is that same scroller bound once in
#: :data:`SCROLL_JS`'s prologue, so ``bottom``'s destination and the reported
#: ``max_y`` are read off ONE object and cannot disagree.
_DIRECTIONS: dict[str, _Direction] = {
    "down": _Direction(
        "y",
        True,
        "{target}.scrollBy({{top: {amount}, left: 0, behavior: {behavior}}})",
        "x0",
        "y0+{amount}",
    ),
    "up": _Direction(
        "y",
        False,
        "{target}.scrollBy({{top: {negative}, left: 0, behavior: {behavior}}})",
        "x0",
        "y0+{negative}",
    ),
    "right": _Direction(
        "x",
        True,
        "{target}.scrollBy({{top: 0, left: {amount}, behavior: {behavior}}})",
        "x0+{amount}",
        "y0",
    ),
    "left": _Direction(
        "x",
        False,
        "{target}.scrollBy({{top: 0, left: {negative}, behavior: {behavior}}})",
        "x0+{negative}",
        "y0",
    ),
    "top": _Direction(
        "y",
        False,
        "{target}.scrollTo({{top: 0, left: 0, behavior: {behavior}}})",
        "0",
        "0",
    ),
    "bottom": _Direction(
        "y",
        True,
        "{target}.scrollTo({{top: E.scrollHeight, left: 0, behavior: {behavior}}})",
        "0",
        "E.scrollHeight",
    ),
}

#: The directions a caller may ask for.
DIRECTIONS = frozenset(_DIRECTIONS)

#: What ``{target}`` becomes for the DOCUMENT scroller, and the element whose
#: extent bounds it. ``window`` keeps ``window.scrollTo``/``scrollBy`` as the
#: literal path a plain page is driven by AND is the right ``scrollend`` target
#: for it (measured: a document scroll's ``scrollend`` reaches ``window`` and is
#: never seen at ``document.scrollingElement``).
_WINDOW_TARGET = "window"
_WINDOW_EXTENT = "(document.scrollingElement||document.documentElement)"

#: The scroll, wrapped so the page answers two questions in the SAME round trip
#: it is asked to scroll in:
#:
#: * ``moves`` — will this scroll change anything? Computed from the clamped
#:   target against the current offset OF THE SELECTED SCROLLER, synchronously,
#:   before any frame. A scroll that moves nothing fires no ``scrollend``, so
#:   this is what tells :func:`settle` not to wait for one.
#: * ``supported`` — does this browser have ``onscrollend`` ON THIS TARGET?
#:   (Measured on Chrome 152: ``'onscrollend' in window`` is ``true`` while
#:   ``'scrollend' in window`` is ``false`` — the event is not an own property of
#:   ``window``, the handler is; and ``'onscrollend' in someDiv`` is ``true``
#:   too, so one expression serves both kinds.) When it is absent :func:`settle`
#:   falls back to read agreement, which is weaker but is all there is.
#:
#: and arms the one-shot ``scrollend`` latch **on ``T``, the same object that
#: receives the scroll**, when and only when the page will move. That is not a
#: convenience: an element's ``scrollend`` does not bubble to ``window`` and a
#: document's is never dispatched at ``document.scrollingElement``, so either
#: mismatch would arm a listener that can never fire (module docstring).
#: Clamping repeats :data:`READ_JS`'s own ``max`` expressions because it is the
#: same question — how far can this element go — asked about a target rather
#: than about now.
#:
#: Braces are DOUBLED here because this template is ``str.format``ed; the
#: resolver is concatenated in by :func:`script` rather than embedded, so it does
#: not have to be written twice.
_SCROLL_BODY = (
    "var T={target};"
    "var E={extent};"
    "if(!E||!T){{return {{moves:false,supported:false}};}}"
    "var mx=Math.max(0,Math.round(E.scrollWidth-E.clientWidth));"
    "var my=Math.max(0,Math.round(E.scrollHeight-E.clientHeight));"
    "var x0=Math.round(E.scrollLeft||0),y0=Math.round(E.scrollTop||0);"
    "var tx=Math.max(0,Math.min(Math.round({target_x}),mx));"
    "var ty=Math.max(0,Math.min(Math.round({target_y}),my));"
    "var moves=(tx!==x0)||(ty!==y0);"
    "var old=window.{latch};if(old&&old.off){{old.off();}}"
    "var st={{ended:0}};"
    "function onEnd(){{st.ended=1;st.off();}}"
    "st.off=function(){{T.removeEventListener('scrollend',onEnd);}};"
    "window.{latch}=st;"
    "var supported=('onscrollend' in T);"
    "if(moves&&supported){{T.addEventListener('scrollend',onEnd);}}"
    "{scroll};"
    "return {{moves:moves,supported:supported}};"
)


class Scroller(NamedTuple):
    """WHICH element this call will scroll, read and listen on — addressing only.

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
        """This scroller as a JS expression — the scroll AND listener target."""
        if self.path is None:
            return _WINDOW_TARGET
        return f"_el({list(self.path)})"

    @property
    def extent_js(self) -> str:
        """The element whose extent bounds it.

        Differs from :attr:`js` for the document alone, and for one measured
        reason: ``window`` receives the scroll and the ``scrollend``, but it has
        no ``scrollHeight`` — the extent of a document scroll is
        ``document.scrollingElement``'s.
        """
        if self.path is None:
            return _WINDOW_EXTENT
        return self.js


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

    The answer is the whole :data:`_SCROLL_BODY` wrapper, not just the scroll: it
    arms the ``scrollend`` latch on the same object it scrolls and reports
    ``moves``/``supported`` in the same round trip.

    Args:
        direction: one of :data:`DIRECTIONS`.
        amount: pixels, never negative (see :func:`_validate`); ignored by
            ``top``/``bottom``.
        smooth: ``behavior: 'smooth'`` rather than ``'instant'``.
        on: the element :func:`scroller` chose. The default is the document
            scroller, whose inner scroll expression is exactly F-875's.

    Raises:
        ToolError: see :func:`_validate`.
    """
    known = _validate(direction, amount)
    fill = {
        "amount": amount,
        "negative": -amount,
        "behavior": "'smooth'" if smooth else "'instant'",
        "target": on.js,
    }
    body = _SCROLL_BODY.format(
        latch=LATCH,
        target=on.js,
        extent=on.extent_js,
        scroll=known.script.format(**fill),
        target_x=known.target_x.format(**fill),
        target_y=known.target_y.format(**fill),
    )
    prologue = "" if on.path is None else _RESOLVE_JS
    return "JSON.stringify((function(){" + prologue + body + "})())"


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
    data = _answer(
        await tab.evaluate(SCROLLER_JS.replace(_AXIS_SLOT, f"'{known.axis}'")),
        "read the page's scroller",
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

        ``is_document`` is a COARSE identity and that second condition is
        therefore not exhaustive: a re-render that puts a DIFFERENT div at the
        same index path leaves both readings ``is_document=False``, and this
        compares two elements again. Documented rather than coded around — it is
        strictly less likely than the case it does catch (which needs only the
        path to stop resolving, not to resolve to something else), and the
        record still names the element the FINAL read found, so the answer is
        visible even when this flag is not.
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
        """Is the scroller already as far as *direction* can take it?

        The one place a direction is turned into an edge. A scroller with
        nothing to scroll (``max_y == 0``) is at BOTH vertical edges, which is
        the literal truth about it.

        The far edge carries :data:`EDGE_TOLERANCE_PX` of slack. The offset and
        the extent are rounded independently (``Math.round(scrollTop)`` against
        an already-integer ``scrollHeight - clientHeight``), so under fractional
        zoom or a non-integer device pixel ratio a page resting at its true
        bottom can read one pixel short of it. One pixel of tolerance is cheaper
        than reporting ``at_edge: false`` for a page that cannot go further; the
        near edge needs none, because a scroll offset is never below 0.
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
    #: Did the scroll finish before the budget ran out?
    settled: bool
    #: How long the window actually cost, in seconds.
    seconds: float


class Reading(NamedTuple):
    """One round trip's answer: where the page is, and whether it has stopped."""

    position: Position
    #: The ``scrollend`` latch :data:`SCROLL_JS` armed. Latched, so jank can only
    #: DELAY this becoming visible — never make a running scroll look finished.
    ended: bool


class Scrolled(NamedTuple):
    """What the scroll round trip itself reported."""

    #: Will this scroll change the offset at all? ``False`` for a page already at
    #: the requested edge, or with nothing to scroll — neither fires
    #: ``scrollend``, so neither may be waited on.
    moves: bool
    #: Does this browser have ``onscrollend`` on the target that was armed?
    supported: bool


def _answer(raw: object, what: str) -> dict[str, object]:
    """One JSON answer from the page, validated by SHAPE only.

    THE one ladder for "did the evaluate answer with the JSON we asked for",
    shared by the pick, the scroll and the read because all three ask the page
    one ``JSON.stringify`` question and all three have the same answer to a
    non-answer: it is operational failure (DESIGN §9), never a zero. Reporting
    ``scroll_y: 0`` for a read that did not happen — or "the document" for a pick
    that did not happen — is the same class of untruth F-875 and F-878 exist to
    remove.

    The message never repeats the page's own text — a type name, a character
    count and a field count say everything a diagnosis needs.
    """
    if not isinstance(raw, str):
        raise ToolError(
            f"Could not {what}: the evaluate answered with "
            f"{type(raw).__name__}, not the JSON string it asks for."
        )
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ToolError(
            f"Could not {what}: the evaluate answered with {len(raw)} "
            "characters that are not JSON."
        ) from exc
    if not isinstance(data, dict):
        raise ToolError(
            f"Could not {what}: the answer was a "
            f"{type(data).__name__}, not the object it asks for."
        )
    return data


async def start(tab: Tab, scroll_js: str) -> Scrolled:
    """Run a :func:`script` answer, and report what the page said about it.

    ONE round trip: the wrapper arms the ``scrollend`` latch on the scroller and
    performs the scroll in the same task, so there is no window in which the
    scroll could finish before anything was listening. It takes the BUILT script
    rather than the request, so the caller can reject an invalid direction or a
    negative amount before it spends a round trip on anything at all.

    Raises:
        ToolError: the answer is not the JSON :data:`_SCROLL_BODY` promises.
    """
    data = _answer(await tab.evaluate(scroll_js), "scroll")
    if not isinstance(data.get("moves"), bool) or not isinstance(
        data.get("supported"), bool
    ):
        raise ToolError(
            "Could not scroll: the answer carried "
            f"{len(data)} of the 2 fields the scroll reports."
        )
    return Scrolled(bool(data["moves"]), bool(data["supported"]))


def _path_js(on: Scroller) -> str:
    """*on*'s index path as the JS literal :data:`READ_JS` resolves."""
    return "null" if on.path is None else str(list(on.path))


async def read(tab: Tab, on: Scroller = DOCUMENT) -> Reading:
    """*on*'s scroll offsets, extent, identity and end-latch, in one round trip.

    Args:
        tab: the tab to read.
        on: the scroller :func:`scroller` chose; the document by default.

    Raises:
        ToolError: the evaluate did not answer with the JSON :data:`READ_JS`
            promises (see :func:`_answer`).
    """
    data = _answer(
        await tab.evaluate(READ_JS.replace(_PATH_SLOT, _path_js(on))),
        "read the page's scroll position",
    )
    if not all(isinstance(data.get(key), (int, float)) for key in _KEYS):
        raise ToolError(
            "Could not read the page's scroll position: the answer carried "
            f"{len(data)} fields but not the {len(_KEYS)} numbers it asks for."
        )
    classes = data.get("classes")
    return Reading(
        Position(
            *(int(data[key]) for key in _KEYS),
            tag=str(data.get("tag") or ""),
            element_id=str(data.get("id") or ""),
            classes=(
                tuple(str(name) for name in classes)
                if isinstance(classes, list)
                else ()
            ),
            is_document=bool(data.get("document")),
        ),
        bool(data.get("ended")),
    )


async def settle(  # noqa: PLR0913  PERMANENT(function interface)
    tab: Tab,
    origin: Position,
    awaiting_end: bool,
    budget: float | None = None,
    start_grace: float | None = None,
    on: Scroller = DOCUMENT,
) -> Settled:
    """Poll :func:`read` until the scroll has finished, or *budget* ends.

    Two stop conditions, and which one applies is decided by the page, not by a
    threshold:

    * ``awaiting_end`` — the page said this scroll WILL move it and that the
      target it was armed on has ``onscrollend``. The only thing that ends the
      wait is the latch: the page itself saying the scroll finished. Nothing
      about the reads' timing can end it early, which is the whole point (see
      the module docstring).
    * otherwise — the page said nothing will move (already at the edge, or
      nothing to scroll), or the browser has no ``scrollend``. Then there is no
      end to wait for and the weaker rule applies: two consecutive reads that
      agree on the OFFSET, with :data:`START_GRACE_SECONDS` forbidding an early
      agreement on the origin itself.

    Args:
        tab: the tab to read.
        origin: where the page was before the scroll was asked for — the reading
            that :data:`START_GRACE_SECONDS` refuses to settle on early.
        awaiting_end: wait for the page's own end-of-scroll signal (see above).
        budget: seconds to spend; :data:`SETTLE_BUDGET_SECONDS` when ``None``
            (read at call time, so the module global is the one knob).
        start_grace: seconds before a reading equal to *origin* may settle;
            :data:`START_GRACE_SECONDS` when ``None``. Ignored when
            *awaiting_end*.
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
        # fast path costs two reads and ONE interval.
        current = await read(tab, on)
        elapsed = _now() - started
        if awaiting_end:
            finished = current.ended
        else:
            # OFFSETS only. What "has it stopped" asks about is the viewport,
            # not the content: an infinite-scroll page appends content for as
            # long as you let it, so a whole-``Position`` comparison would spend
            # the entire budget on a page that stopped moving in the first
            # 100 ms and then report ``settled: false`` about a stationary
            # viewport. The EXTENT the caller gets is the final read's.
            finished = current.position.offset == previous and (
                current.position.offset != origin.offset or elapsed >= grace
            )
        if finished:
            return Settled(current.position, True, elapsed)
        if elapsed >= limit:
            return Settled(current.position, False, elapsed)
        previous = current.position.offset
        await _sleep(POLL_INTERVAL_SECONDS)
