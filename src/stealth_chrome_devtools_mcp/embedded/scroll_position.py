"""THE one home for "where is this page scrolled, and has it stopped" (F-875).

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
viewport tall, ``scrollY`` ``0`` before and after). Telling them apart needs two
things this module owns and the tool composes:

**A read.** :func:`read` asks the page for its scroll offsets and its extent in
ONE ``JSON.stringify`` round trip. It is ``JSON.stringify`` for the reason
``page_storage`` (F-869) and ``js_aspect_answer`` (F-872) are: ``Tab.evaluate``
always sends ``SerializationOptions(serialization="deep")`` and hands back
``deep_serialized_value.value`` raw, so an object literal arrives as BiDi
``RemoteValue`` nodes at every depth while a *string* arrives intact. The
offsets are ``Math.round``ed because sub-pixel positions are real (zoom, HiDPI,
the tail of a smooth animation) and a settle that compares floats would never
see two reads agree.

**A settle.** :func:`settle` waits for the page to say the scroll finished,
bounded by :data:`SETTLE_BUDGET_SECONDS` — a settle, not a sleep. A fixed nap
cannot be right for both cases it was serving: an instant scroll is done before
the first frame, and the smooth scroll measured above was still moving three
seconds in.

**Why the page has to say it, and not the reads.** The first version of this
settle stopped when two consecutive reads agreed on the offset. That is not a
stop condition, it is a guess, and CI gate run 35046780659 (macOS/ARM64) caught
it: the record reported ``scroll_y_after: 3498`` while the page read 4898
immediately afterwards — two reads agreed 1400 px from the end, in the FAST part
of an ease-out curve. The mechanism, reproduced here on Chrome 152: a plain
document smooth scroll runs on the COMPOSITOR thread, while ``window.scrollY``
is read on the MAIN thread and only advances when a frame commits to main. Jank
the main thread and the reads go stale while the scroll keeps going. Measured,
with the renderer's main thread blocked for a fixed slice out of every 5 ms:

===============  ==========================
main-thread task  longest run of AGREEING
                  mid-flight reads
===============  ==========================
none             0 ms (6 runs)
120 ms           121 ms
250 ms           250 ms
400 ms           400 ms
===============  ==========================

The false-agreement window is exactly as long as the long task, i.e. unbounded.
No count of agreeing reads and no fixed quiet window can be correct against
that, so neither is used. Instead :data:`SCROLL_JS` arms a one-shot ``scrollend``
listener in the same round trip that performs the scroll, and the read reports
that latch. ``scrollend`` fires when the scroll position has finished changing,
including at the end of a compositor-driven smooth scroll, and it LATCHES — so
jank can only delay our observation of it and can never make a running scroll
look finished. Measured across the same jank levels, ``ended`` was first
observed at the true final position (7023/7023) every time, never early.

**The one case that must not wait for it.** A scroll that moves nothing fires no
``scrollend`` at all — a page already at the requested edge, or with nothing to
scroll. So :data:`SCROLL_JS` also reports ``moves``, computed synchronously from
the clamped target before any frame, and :func:`settle` only waits for the latch
when the page said it would move. That answer is exact, which is why it replaced
the old ``at_edge`` guess, and it is what keeps a one-viewport page and an
instant scroll on the two-read fast path (0.126 s measured).

:data:`START_GRACE_SECONDS` survives for the fallback alone: a browser with no
``onscrollend`` still has to settle by read agreement, and there a smooth scroll
that has not begun yet looks exactly like one that will never move.

**What this module does not decide.** It never says whether a scroll
*succeeded*: only where the page is, what its extent is
(:func:`Position.at_edge`), and whether the window has been spent. Composing
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

#: Offsets AND extent in one round trip, as a JSON *string* (module docstring).
#: ``document.scrollingElement`` is what CSSOM-View names as the element
#: ``window.scrollTo``/``scrollBy`` actually move — ``html`` in standards mode,
#: ``body`` in quirks — so the extent reported here is the extent of the thing
#: that was scrolled. The finding measured it as ``html`` with
#: ``body.scrollHeight == documentElement.scrollHeight`` on every sampled page,
#: which is why reading either by hand happened to work and why asking the
#: browser which one it is costs nothing.
#:
#: It also carries ``ended`` — the latch :data:`SCROLL_JS` armed — because "has
#: the scroll finished" and "where is it" must come from the SAME round trip: two
#: separate reads could straddle the end of the animation and pair a finished
#: flag with a stale offset, which is the very shape this finding retires.
READ_JS = (
    "JSON.stringify((function(){"
    "var e=document.scrollingElement||document.documentElement||document.body;"
    "var s=window.__stealthMcpScroll;"
    "var ended=!!(s&&s.ended);"
    "if(!e){return {x:0,y:0,max_x:0,max_y:0,ended:ended};}"
    "return {"
    "x:Math.round(window.scrollX||0),"
    "y:Math.round(window.scrollY||0),"
    "max_x:Math.max(0,Math.round(e.scrollWidth-e.clientWidth)),"
    "max_y:Math.max(0,Math.round(e.scrollHeight-e.clientHeight)),"
    "ended:ended"
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
    """What one direction name means: which way, and the JS that goes there.

    ``target_x``/``target_y`` are JS expressions for the offsets the scroll is
    AIMED at, in terms of the current ``x0``/``y0``, evaluated in the same task
    as the scroll itself. Clamped against the extent they give
    :data:`SCROLL_JS` its ``moves`` answer — "will anything actually change" —
    without waiting for a frame, which is what lets a no-op scroll answer at
    once while a real one waits for the page to say it finished. ``top`` and
    ``bottom`` pass ``left: 0``, so their ``target_x`` is ``0`` and not ``x0``:
    they really do move a horizontally-scrolled page back to the left edge.
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
#: error. ``bottom`` targets ``document.scrollingElement`` — the element CSSOM
#: View says ``scrollTo`` moves and the one :data:`READ_JS` measures the extent
#: of, so the target and the reported ``max_y`` cannot disagree. (F-875 measured
#: ``body.scrollHeight == documentElement.scrollHeight`` on every sampled page:
#: a consistency fix, not a behaviour change on them.)
_DIRECTIONS: dict[str, _Direction] = {
    "down": _Direction(
        "y",
        True,
        "window.scrollBy({{top: {amount}, left: 0, behavior: {behavior}}})",
        "x0",
        "y0+{amount}",
    ),
    "up": _Direction(
        "y",
        False,
        "window.scrollBy({{top: {negative}, left: 0, behavior: {behavior}}})",
        "x0",
        "y0+{negative}",
    ),
    "right": _Direction(
        "x",
        True,
        "window.scrollBy({{top: 0, left: {amount}, behavior: {behavior}}})",
        "x0+{amount}",
        "y0",
    ),
    "left": _Direction(
        "x",
        False,
        "window.scrollBy({{top: 0, left: {negative}, behavior: {behavior}}})",
        "x0+{negative}",
        "y0",
    ),
    "top": _Direction(
        "y",
        False,
        "window.scrollTo({{top: 0, left: 0, behavior: {behavior}}})",
        "0",
        "0",
    ),
    "bottom": _Direction(
        "y",
        True,
        "window.scrollTo({{top: E.scrollHeight, left: 0, behavior: {behavior}}})",
        "0",
        "E.scrollHeight",
    ),
}

#: The directions a caller may ask for.
DIRECTIONS = frozenset(_DIRECTIONS)

#: The property the ``ended`` latch lives on. One namespaced name, overwritten
#: by every scroll, with the previous scroll's listener removed first — so a
#: page never accumulates them and a stale latch can never answer for a newer
#: scroll.
LATCH = "__stealthMcpScroll"

#: The scroll, wrapped so the page answers two questions in the SAME round trip
#: it is asked to scroll in:
#:
#: * ``moves`` — will this scroll change anything? Computed from the clamped
#:   target against the current offset, synchronously, before any frame. A
#:   scroll that moves nothing fires no ``scrollend``, so this is what tells
#:   :func:`settle` not to wait for one.
#: * ``supported`` — does this browser have ``onscrollend``? (Measured on
#:   Chrome 152: ``'onscrollend' in window`` is ``true`` while
#:   ``'scrollend' in window`` is ``false`` — the event is not an own property
#:   of ``window``, the handler is.) When it is absent :func:`settle` falls back
#:   to read agreement, which is weaker but is all there is.
#:
#: and arms the one-shot ``scrollend`` latch when, and only when, the page will
#: move. Clamping repeats :data:`READ_JS`'s own ``max`` expressions because it
#: is the same question — how far can this element go — asked about a target
#: rather than about now.
SCROLL_JS = (
    "JSON.stringify((function(){{"
    "var E=document.scrollingElement||document.documentElement||document.body;"
    "if(!E){{return {{moves:false,supported:false}};}}"
    "var mx=Math.max(0,Math.round(E.scrollWidth-E.clientWidth));"
    "var my=Math.max(0,Math.round(E.scrollHeight-E.clientHeight));"
    "var x0=Math.round(window.scrollX||0),y0=Math.round(window.scrollY||0);"
    "var tx=Math.max(0,Math.min(Math.round({target_x}),mx));"
    "var ty=Math.max(0,Math.min(Math.round({target_y}),my));"
    "var moves=(tx!==x0)||(ty!==y0);"
    "var old=window.{latch};if(old&&old.off){{old.off();}}"
    "var st={{ended:0}};"
    "function onEnd(){{st.ended=1;st.off();}}"
    "st.off=function(){{window.removeEventListener('scrollend',onEnd);}};"
    "window.{latch}=st;"
    "var supported=('onscrollend' in window);"
    "if(moves&&supported){{window.addEventListener('scrollend',onEnd);}}"
    "{scroll};"
    "return {{moves:moves,supported:supported}};"
    "}})())"
)


def script(direction: str, amount: int, smooth: bool) -> str:
    """The JS that scrolls *amount* pixels *direction*, smoothly or not.

    *amount* is a distance, and *direction* is the only thing that carries a
    sign. A negative *amount* is therefore REJECTED rather than interpreted:
    there is exactly one way to scroll up and it is ``direction="up"``. Before
    F-875 the two readings of a negative amount were both wrong and differently
    wrong — ``down`` with ``-500`` silently scrolled UP, while ``up`` with
    ``-500`` interpolated ``top: --500`` and died as a JS syntax error. Making
    it a magnitude instead would keep the silence and only move it (the record
    would say ``direction: "up"`` about a page that went down), which is the
    class of untruth this whole finding is about.

    Raises:
        ToolError: *direction* is not one of :data:`DIRECTIONS`, or *amount* is
            negative. Both are decided before the page is asked anything, so a
            bad request costs no round trip.
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
    fill = {
        "amount": amount,
        "negative": -amount,
        "behavior": "'smooth'" if smooth else "'instant'",
    }
    return SCROLL_JS.format(
        latch=LATCH,
        scroll=known.script.format(**fill),
        target_x=known.target_x.format(**fill),
        target_y=known.target_y.format(**fill),
    )


def _now() -> float:
    """The one clock (monotonic — a wall-clock step must not end a settle)."""
    return time.monotonic()


async def _sleep(seconds: float) -> None:
    """The one wait."""
    await asyncio.sleep(seconds)


class Position(NamedTuple):
    """Where the page is scrolled, and how far it could be scrolled.

    ``max_x``/``max_y`` are ``0`` for a document that fits its viewport — the
    honest description of a page with nothing to scroll, not an error.

    The two halves mean different things and must never be compared together.
    :attr:`offset` is WHERE THE PAGE IS; ``max_x``/``max_y`` describe the
    DOCUMENT, which a lazy-loading page grows while standing perfectly still.
    Comparing whole ``Position`` values conflates them, and both ways round are
    lies this module exists to prevent: content appended below a stationary
    viewport would read as "it scrolled", and a page that had stopped moving but
    was still loading would never settle.
    """

    x: int
    y: int
    max_x: int
    max_y: int

    @property
    def offset(self) -> tuple[int, int]:
        """Where the page is scrolled — the ONLY part that means "moved"."""
        return (self.x, self.y)

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
    #: Does this browser have ``onscrollend``?
    supported: bool


def _answer(raw: object, what: str) -> dict[str, object]:
    """One JSON answer from the page, validated by SHAPE only.

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

    ONE round trip: the wrapper arms the ``scrollend`` latch and performs the
    scroll in the same task, so there is no window in which the scroll could
    finish before anything was listening. It takes the BUILT script rather than
    the request, so the caller can reject an invalid direction or a negative
    amount before it spends a round trip on anything at all.

    Raises:
        ToolError: the answer is not the JSON :data:`SCROLL_JS` promises.
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


async def read(tab: Tab) -> Reading:
    """The page's scroll offsets, extent and end-latch, in one round trip.

    Raises:
        ToolError: the evaluate did not answer with the JSON :data:`READ_JS`
            promises. Reporting ``0`` for a read that did not happen would be
            the same class of untruth this module exists to remove, so an
            unreadable answer is operational failure (DESIGN §9). The message
            reports SHAPE only — a type name and a key count — never the page's
            own text.
    """
    data = _answer(await tab.evaluate(READ_JS), "read the page's scroll position")
    if not all(isinstance(data.get(key), (int, float)) for key in _KEYS):
        raise ToolError(
            "Could not read the page's scroll position: the answer carried "
            f"{len(data)} of the {len(_KEYS)} fields the read asks for."
        )
    return Reading(
        Position(*(int(data[key]) for key in _KEYS)), bool(data.get("ended"))
    )


async def settle(
    tab: Tab,
    origin: Position,
    awaiting_end: bool,
    budget: float | None = None,
    start_grace: float | None = None,
) -> Settled:
    """Poll :func:`read` until the scroll has finished, or *budget* ends.

    Two stop conditions, and which one applies is decided by the page, not by a
    threshold:

    * ``awaiting_end`` — the page said this scroll WILL move it and that it has
      ``onscrollend``. The only thing that ends the wait is the latch: the page
      itself saying the scroll finished. Nothing about the reads' timing can end
      it early, which is the whole point (see the module docstring).
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
        current = await read(tab)
        elapsed = _now() - started
        if awaiting_end:
            finished = current.ended
        else:
            # OFFSETS only. What "has it stopped" asks about is the viewport,
            # not the document: an infinite-scroll page appends content for as
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
