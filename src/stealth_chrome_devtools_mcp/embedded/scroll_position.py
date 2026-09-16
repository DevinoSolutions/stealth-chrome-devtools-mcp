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
READ_JS = (
    "JSON.stringify((function(){"
    "var e=document.scrollingElement||document.documentElement||document.body;"
    "if(!e){return {x:0,y:0,max_x:0,max_y:0};}"
    "return {"
    "x:Math.round(window.scrollX||0),"
    "y:Math.round(window.scrollY||0),"
    "max_x:Math.max(0,Math.round(e.scrollWidth-e.clientWidth)),"
    "max_y:Math.max(0,Math.round(e.scrollHeight-e.clientHeight))"
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
#: error. ``bottom`` targets ``document.scrollingElement`` — the element CSSOM
#: View says ``scrollTo`` moves and the one :data:`READ_JS` measures the extent
#: of, so the target and the reported ``max_y`` cannot disagree. (F-875 measured
#: ``body.scrollHeight == documentElement.scrollHeight`` on every sampled page:
#: a consistency fix, not a behaviour change on them.)
_DIRECTIONS: dict[str, _Direction] = {
    "down": _Direction(
        "y", True, "window.scrollBy({{top: {amount}, left: 0, behavior: {behavior}}})"
    ),
    "up": _Direction(
        "y",
        False,
        "window.scrollBy({{top: {negative}, left: 0, behavior: {behavior}}})",
    ),
    "right": _Direction(
        "x", True, "window.scrollBy({{top: 0, left: {amount}, behavior: {behavior}}})"
    ),
    "left": _Direction(
        "x",
        False,
        "window.scrollBy({{top: 0, left: {negative}, behavior: {behavior}}})",
    ),
    "top": _Direction(
        "y", False, "window.scrollTo({{top: 0, left: 0, behavior: {behavior}}})"
    ),
    "bottom": _Direction(
        "y",
        True,
        "window.scrollTo({{top: (document.scrollingElement||"
        "document.documentElement).scrollHeight, left: 0, behavior: {behavior}}})",
    ),
}

#: The directions a caller may ask for.
DIRECTIONS = frozenset(_DIRECTIONS)


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
    return known.script.format(
        amount=amount,
        negative=-amount,
        behavior="'smooth'" if smooth else "'instant'",
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
    #: Did two consecutive reads agree before the budget ran out?
    settled: bool
    #: How long the window actually cost, in seconds.
    seconds: float


async def read(tab: Tab) -> Position:
    """The page's scroll offsets and extent, in one round trip.

    Raises:
        ToolError: the evaluate did not answer with the JSON :data:`READ_JS`
            promises. Reporting ``0`` for a read that did not happen would be
            the same class of untruth this module exists to remove, so an
            unreadable answer is operational failure (DESIGN §9). The message
            reports SHAPE only — a type name and a key count — never the page's
            own text.
    """
    raw = await tab.evaluate(READ_JS)
    if not isinstance(raw, str):
        raise ToolError(
            "Could not read the page's scroll position: the evaluate answered "
            f"with {type(raw).__name__}, not the JSON string the read asks for."
        )
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ToolError(
            "Could not read the page's scroll position: the evaluate answered "
            f"with {len(raw)} characters that are not JSON."
        ) from exc
    if not isinstance(data, dict) or not all(
        isinstance(data.get(key), (int, float)) for key in _KEYS
    ):
        raise ToolError(
            "Could not read the page's scroll position: the answer carried "
            f"{len(data) if isinstance(data, dict) else 0} of the "
            f"{len(_KEYS)} fields the read asks for."
        )
    return Position(*(int(data[key]) for key in _KEYS))


async def settle(
    tab: Tab,
    origin: Position,
    budget: float | None = None,
    start_grace: float | None = None,
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
        current = await read(tab)
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
