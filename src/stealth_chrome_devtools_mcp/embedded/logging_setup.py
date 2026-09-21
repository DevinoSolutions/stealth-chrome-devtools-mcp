"""File-based logging spine for stealth-chrome-devtools-mcp (plan M3).

This module is the ONE place log-WRITING is configured: handlers, formatters,
rotation, correlation-id stamping, log-dir resolution, and old-log pruning.
``observability.py`` (Sentry) is the separate error-SHIPPING home — do not
merge or duplicate either. Named ``logging_setup`` (not ``logging``) so it
never shadows the stdlib on the bare-name ``sys.path`` the embedded package
uses.

Two roles call :func:`configure_logging`: the backend process
(``role="backend"``, from ``embedded/server.py``'s ``__main__``) and the
stdio proxy (``role="proxy"``, from ``singleton.run_stdio_proxy``). Each gets
its own ``stealth.<role>`` logger writing to ``<logdir>/<role>-<pid>.log`` —
per-pid filenames sidestep Windows ``RotatingFileHandler`` rename contention
between two backends briefly coexisting (plan_M3 §2.2, rejected alternative 3).

Owning log-WRITING means owning what the DEPENDENCIES may write too, so this
is also the one place third-party logger LEVELS are set:
:func:`apply_payload_log_floor` (F-906, F-908) holds every family in
:data:`PAYLOAD_LOG_FAMILIES` at WARNING — ``nodriver`` and ``websockets``
because they interpolate a raw CDP message into DEBUG/INFO text, cookie names
and values included, and ``sse_starlette`` and ``mcp.client`` because they do
the same to the two ENDS of one tool answer (the SSE frame the backend sends,
and the message the stdio proxy parses back out of it). One caller-side
``logging.basicConfig(level=DEBUG)`` is enough to route any of them to our
stderr and to a Sentry breadcrumb. It belongs HERE and not at the seam that
patches nodriver
(``cdp_transport``, which owns what nodriver may DO with a reply): a level is
log configuration, and log configuration has one home.

``singleton.py`` also needs this module (the boot-log redirect and the
``configure_logging("proxy")`` call), while :func:`resolve_log_dir` reuses
``singleton.STATE_DIR``. Importing ``singleton`` here at module top level
would therefore create a cycle; the codebase's established fix for exactly
this shape (embedded/runpy/singleton architecture, see pyproject.toml's
PLC0415 rationale) is a deferred, function-local import — used below.
"""

from __future__ import annotations

import contextlib
import faulthandler
import functools
import inspect
import logging
import os
import re
import sys
import threading
import time
import uuid
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING

from stealth_chrome_devtools_mcp.embedded import payload_log_sites
from stealth_chrome_devtools_mcp.settings import get_settings

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import TracebackType

LOG_FORMAT = (
    "%(asctime)s %(levelname)s %(process)d [%(correlation_id)s] %(name)s: %(message)s"
)
_MAX_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 3

# The shared raw-stream file every backend's Popen stdout/stderr is redirected
# into (singleton._start_server_process). One file for ALL boots, by design:
# an import-time crash happens before the process knows its own log name.
BOOT_LOG_NAME = "backend-boot.log"
# F-830: unrotated, this reached 794 MB on the reporting machine. 16 MB still
# holds many boots' worth of tracebacks while staying small enough to open in
# an editor; 2 backups caps the whole boot-log family at ~48 MB.
_BOOT_LOG_MAX_BYTES = 16 * 1024 * 1024
_BOOT_LOG_BACKUPS = 2

# F-840: a dead backend's log set IS the post-mortem. Keep the newest few
# unconditionally (age is exactly what an unattended crash accrues before
# anyone looks), and hold every fault log for a fortnight.
_KEEP_BACKEND_SETS = 3
_FAULT_LOG_KEEP_DAYS = 14
# ``backend-<pid>.log``, its ``.1``/``.2`` rotations, and the matching
# ``backend-<pid>-fault.log``. ``backend-boot.log`` deliberately does not match
# (``boot`` is not a pid): it is shared across backends, not one's post-mortem.
_BACKEND_LOG_RE = re.compile(r"^backend-(\d+)(?:-fault)?\.log")

# F-809: FastMCP hard-codes uvicorn's timeout_graceful_shutdown to 0, and a
# zero-second asyncio timeout always fires — so every clean HTTP stop ERROR-logs
# "timeout graceful shutdown exceeded" (and Sentry ships it). Sized against
# singleton._terminate_backend's 5 s wait; never None, uvicorn's "wait forever".
_GRACEFUL_SHUTDOWN_SECONDS = 2.0

correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="-")

#: F-906. The logger families that interpolate a RAW CDP message into their own
#: log text, and so carry whatever the page holds — cookie names and values
#: included. Named by family ROOT, because both libraries build their loggers
#: from ``__name__`` (measured: ``nodriver.core.connection.logger.name`` IS
#: ``nodriver.core.connection``), so one explicit level on the root covers every
#: module under it, present and future.
#:
#: What each one does, measured against nodriver 0.47.0 / websockets 16.0:
#:
#: * ``nodriver`` — ``core/connection.py``:445 DEBUG-logs the whole reply
#:   (``"got answer for (message_id:%d) => %s"``, the parsed ``message`` dict),
#:   :451 INFO-logs the whole EVENT message when a field will not parse, and
#:   ``core/browser.py``:824/:869 DEBUG-log a cookie's name and value outright;
#: * ``websockets`` — ``protocol.py``:609 DEBUG-logs the frame, and a frame
#:   short enough is printed whole (truncation starts past ~75 characters), so
#:   this is the same payload one layer down. Capping ``nodriver`` alone would
#:   have left that door open.
#:
#: * ``sse_starlette`` — F-908. ``sse/sse.py``:362 is
#:   ``logger.debug("chunk: %s", chunk)``, and for THIS backend that chunk is
#:   the answer to a ``tools/call``, whole: ``get_cookies``' jar,
#:   ``get_page_content``'s HTML, ``get_instance_state``'s localStorage. The
#:   SSE frame is what carries every answer because FastMCP leaves
#:   ``json_response`` at its ``False`` default and nothing here asks
#:   otherwise (an inherited ``FASTMCP_JSON_RESPONSE`` cannot either —
#:   ``backend_env.scrub`` drops the prefix, F-890). Measured by driving the
#:   real ``EventSourceResponse``, not read off the call.
#:
#: * ``mcp.client`` — F-908 review M1, and it is the OTHER END of that same
#:   frame. The backend logs the SSE chunk it SENDS; the STDIO PROXY re-parses
#:   the identical bytes and logs the MESSAGE:
#:   ``client/streamable_http.py``:218 is ``logger.debug(f"SSE message:
#:   {message}")`` — the whole ``JSONRPCResponse``, i.e. the same tool result —
#:   and :547 is its argument-side twin on the POST leg. Both processes call
#:   :func:`configure_logging` (the proxy at ``singleton.py``:976), so capping
#:   only ``sse_starlette`` left the payload reaching a root handler in the
#:   proxy, which is what the review reproduced.
#:
#:   The entry is ``mcp.client`` and not ``mcp.client.streamable_http`` because
#:   a full census of the installed ``mcp/client`` package found a SECOND
#:   renderer of the same shape — ``client/sse.py``:114/:137, the legacy SSE
#:   client transport — and that module IS loaded (transitively, by importing
#:   the streamable one: measured), so the logger exists even though nothing
#:   here drives it. ``mcp.client`` is the narrowest name covering the measured
#:   set; it is not the whole ``mcp`` family, and the reason for that line is
#:   in the paragraph below. Its cost is zero on the same measurement as the
#:   others: every call at WARNING and above under ``mcp/client`` still passes.
#:
#: Deliberately NOT ``uc``: that is only the local alias this codebase imports
#: ``nodriver`` under, and no logger is ever named by it — capping a name that
#: does not exist would be a claim the evidence does not support.
#:
#: Deliberately NOT the whole ``mcp`` family, and not ``fastmcp``; the census
#: behind both is pinned rather than summarised
#: (``TestTheFamiliesDeliberatelyLeftOut``).
#:
#: The line between IN and OUT inside ``mcp`` is SERVER versus CLIENT, and it
#: was measured on both sides rather than inferred from one. The SERVER tree
#: renders nothing for a request: ``server/lowlevel/server.py``:676 logs the
#: whole incoming message and IS admitted at DEBUG, but for a REQUEST that
#: object is a ``RequestResponder``, which defines neither ``__repr__`` nor
#: ``__str__``, so ``%s`` yields ``<… object at 0x…>``. (The qualifier is
#: load-bearing: the same line's NOTIFICATION arm renders its pydantic model in
#: full. It is still out, because every notification a client may send is
#: enumerated from the SDK's own union and none carries a tool result or tool
#: arguments — pinned, so an SDK that adds one goes RED.) The CLIENT transport
#: renders the whole message and is capped, above. Capping ``mcp`` entire would
#: therefore silence the server SDK's own INFO diagnostics and close no door
#: that ``mcp.client`` does not already close.
#:
#: ``fastmcp``'s tool-ARGUMENT line (``server/server.py``:672) is real, and the
#: library already shields it: its loggers hang under a ``FastMCP`` root
#: carrying its own level and ``propagate = False``, so a caller's root DEBUG
#: never reaches them — and the bare ``fastmcp`` family, which DOES inherit
#: root, holds no payload line, so capping it would be the ``uc`` mistake
#: spelled differently.
#:
#: Two SITES are RECORDED and not fixed, because no family cap can reach either
#: however this tuple grows: ``mcp/shared/session.py``:383-384 and :430-432 use
#: module-level ``logging.warning`` / ``logging.debug``, i.e. the ROOT logger.
#: Both are on a validation-failure path and both are reachable AS SHIPPED,
#: with no ``basicConfig`` anywhere: :383 carries pydantic's middle-truncated
#: ``input_value=`` echo of a caller's arguments at WARNING (:384 renders the
#: whole message, but only at DEBUG), and :430-432 renders the whole
#: ``message.message.root`` AT WARNING in one line. They are named in the
#: finding (§6) as the F-911 candidate, which needs a different mechanism — a
#: root filter or a ``before_breadcrumb`` — exactly as F-907's does.
#:
#: Deliberately NOT ``starlette``, ``anyio`` (neither logs anything below
#: WARNING at all — an AST census, so "nothing" is measured rather than
#: grepped), ``uvicorn`` (its whole-ASGI-message logger replaces bodies with a
#: ``<N bytes>`` placeholder BY CONSTRUCTION and logs at TRACE, which
#: ``basicConfig(DEBUG)`` does not admit; the access log is already off, F-830)
#: or ``httpcore``/``httpx`` (the body traces set no ``return_value``, so their
#: message is the trace NAME alone — response HEADERS can appear, a tool result
#: cannot).
PAYLOAD_LOG_FAMILIES = ("nodriver", "websockets", "sse_starlette", "mcp.client")

#: The floor those families are held at. WARNING is not a new policy — it is
#: the effective level every shipped configuration of this product already had
#: (measured across backend, proxy, ``--debug`` and
#: ``STEALTH_MCP_LOG_LEVEL=DEBUG``), which is exactly why setting it changes
#: nothing an operator sees and only closes the one door that was open. It is
#: also the level at which those libraries stop quoting payloads and start
#: reporting faults: ``connection.py``:483's callback WARNING names the callback
#: and the event CLASS, never the message. For ``sse_starlette`` the floor
#: costs even less — it has no call at WARNING or above anywhere in the package
#: (measured), so there is nothing there for a floor to stand in front of.
PAYLOAD_LOG_FLOOR = logging.WARNING


def apply_payload_log_floor() -> None:
    """Hold the payload-carrying library loggers at :data:`PAYLOAD_LOG_FLOOR`.

    F-906. ``nodriver`` logs every raw CDP reply verbatim and ``websockets``
    logs the frame under it, so a page's cookies are one enabled DEBUG record
    away from our stderr — which for the backend is redirected into
    ``backend-boot.log``, a durable file — and, for the one line that sits at
    INFO, away from a Sentry breadcrumb on the next event.

    F-908 added two more under the identical premise, from the other end of the
    same request: ``sse_starlette`` logs the SSE chunk the BACKEND sends, and
    the chunk is the whole serialised answer to a ``tools/call``; ``mcp.client``
    logs the MESSAGE the STDIO PROXY parses back out of those same bytes, which
    is the same answer again in a second process that also calls
    :func:`configure_logging`. Where F-906's lines quote what CHROME said,
    these quote what WE said back — and one tool result has two ends, so
    capping one of them is half a fix. That is why this stays one list and one
    mechanism rather than a second floor beside the first.

    Until this, the only thing stopping them was that those loggers carry no
    level of their own and INHERIT root's. That is a real protection and it was
    measured to hold for every configuration this product ships — but it is
    root's to give away, and one ``logging.basicConfig(level=DEBUG)`` in a
    caller that embeds this backend, a notebook or a test gives it away for the
    whole process. MEASURED both ways round: every payload line reached a root
    handler and ``connection.py``:451 reached Sentry, whether the
    ``basicConfig`` came before or after our own setup.

    So the level is set EXPLICITLY on the family root. ``getEffectiveLevel``
    stops at the first ancestor carrying a non-``NOTSET`` level, and
    ``basicConfig`` only ever sets ROOT's — so ours wins in either order, and
    keeps winning. That is also why this is the whole fix and there is no
    filter and no ``before_breadcrumb`` rule beside it: Sentry's
    ``LoggingIntegration`` patches ``logging.Logger.callHandlers``, which
    ``Logger.handle`` only reaches for a record ``isEnabledFor`` has already
    admitted, so a level is upstream of every sink at once. A second mechanism
    would be a second home for one decision (convention 4), and a filter keyed
    on a library's message strings would be a pattern-match against text that
    library is free to reword.

    It never SILENCES: WARNING and above pass exactly as they did, because
    those are nodriver's real diagnostics and losing them would be this fix
    costing more than it saves. The residual is named rather than hidden — a
    caller who writes ``logging.getLogger("nodriver").setLevel(DEBUG)`` still
    gets DEBUG, because that is them asking for this library's payloads by
    name, which is a different act from turning DEBUG on globally.

    Called from :func:`configure_logging`, before anything that can fail: a
    process whose log directory could not be created still has stderr and
    Sentry, so it still needs the floor. It honours that caller's never-raises
    contract BY CONSTRUCTION rather than with a handler — these statements are
    a dict lookup and an integer assignment on a stdlib logger, with no I/O and
    nothing to fail — so there is no ``except`` here that could only ever hide
    a bug of ours. Adding a family costs one more of each, which is the other
    reason the list is the extension point and a per-library helper would not
    be.
    """
    for family in PAYLOAD_LOG_FAMILIES:
        logging.getLogger(family).setLevel(PAYLOAD_LOG_FLOOR)


#: F-907. The package whose INSTANCES render page content out of their own
#: ``__repr__``, so a log line that interpolates one carries whatever the page
#: holds. Measured on nodriver 0.47.0:
#:
#: * ``Element.__repr__`` renders the tag, **every attribute as
#:   ``name="value"``** and the element's whole recursive **text content** — so
#:   an ``<input type=password>``'s ``value=``, a ``data-*`` bearing a session
#:   token and a balance in a ``<div>`` all ride in it;
#: * ``Tab.__repr__`` renders ``target.url``, and a URL carries tokens in its
#:   query string.
#:
#: ``element.py``:537/:624/:633 log ``"could not calculate box model for %s"``
#: with a live ``Element`` at **WARNING** — above :data:`PAYLOAD_LOG_FLOOR`, so
#: F-906's level does not reach it, and no handler of anyone's is needed for
#: such a record to land: production root carries none and ``logging.lastResort``
#: (an ``_StderrHandler`` at WARNING) carries it to stderr, which for the backend
#: IS ``backend-boot.log``.
#:
#: Those three sites are NOT reachable in nodriver 0.47 — measured, and it moves
#: this finding's severity DOWN rather than up. ``Position.center`` is a
#: non-empty 2-tuple and therefore always truthy, even for a zero-size box at the
#: origin, so ``if not center:`` cannot open; the other two ways out of
#: ``get_position()`` (``element.py``:499's raised ``Exception`` and the
#: ``except IndexError`` branch's ``None``) both leave ``mouse_click`` before the
#: warning line. This is therefore insurance, plus correctness for any FUTURE
#: nodriver WARNING that renders an object, and the reachability premises are
#: pinned so a bump that makes them live is a RED test rather than a Sentry
#: event.
#:
#: Named as a PACKAGE and matched against ``type(arg).__module__`` rather than
#: with ``isinstance``, because resolving the class needs ``import nodriver``
#: and :func:`configure_logging` runs in the **stdio proxy**, which must never
#: import the browser stack (``desktop_launch``'s measured cold-start
#: paragraph). Deliberately NOT paired with ``websockets`` the way
#: :data:`PAYLOAD_LOG_FAMILIES` is: measured across websockets 16.0, every one
#: of its WARNING-and-above sites logs a static message or a ``str``, so there
#: is nothing there to redact and claiming otherwise would be a claim the
#: evidence does not support (F-906's "no ``uc`` entry" reasoning).
PAYLOAD_ARG_PACKAGE = "nodriver"

#: Bounds on the shape rendered in place of a redacted argument. An element's
#: tag and its attribute NAMES are page-authored, so their count and their
#: length are the page's — the same reasoning, and deliberately its own numbers,
#: as ``click_target.MAX_CLASSES`` and ``scroll_position.SCROLLER_CLASSES``
#: (``tool_errors.JS_ERROR_CHARS``' three-homes precedent: three bounds, three
#: questions, three homes).
SHAPE_MAX_ATTRS = 12
SHAPE_MAX_NAME_CHARS = 32
SHAPE_OVERFLOW = "…"

#: Set on our own factory so :func:`install_payload_arg_redaction` can see that
#: it is already the one installed. An attribute on the function, not a module
#: global, because what must be idempotent is the FACTORY CHAIN and that lives
#: in ``logging``, not here — a module flag would read "installed" in a process
#: whose factory a caller had since replaced.
_REDACTION_MARK = "_stealth_payload_arg_redaction"


def _clamped(name: object) -> str:
    """One page-authored name, bounded."""
    text = str(name)
    if len(text) <= SHAPE_MAX_NAME_CHARS:
        return text
    return text[:SHAPE_MAX_NAME_CHARS] + SHAPE_OVERFLOW


def _shape(value: object) -> str:
    """What a payload-carrying argument is allowed to say about itself.

    An ELEMENT keeps its tag, its attribute NAMES and its child COUNT, and
    loses every attribute VALUE and all of its descendant TEXT: redacting is
    not silencing, and "which control is this" is the whole diagnostic value of
    the line it replaces. A name is the page's vocabulary while a value is the
    user's secret — ``value=`` and ``data-session-token=`` are exactly the pair
    that makes the distinction — and a count says how much text was dropped
    without saying any of it.

    It is stricter than ``click_target.Shape``, which reports an id and a class
    list by VALUE, and the asymmetry is deliberate: that module reads a known
    element through our own code with its own bounds, this one is handed an
    arbitrary object on a third-party line we do not control, and "never a
    value" is a rule with no edge cases to get wrong.

    Anything else — a ``Tab``, a ``Connection``, a generated CDP record — keeps
    its TYPE and nothing else, because there is no half of it we have measured
    to be safe.
    """
    kind = f"{type(value).__module__}.{type(value).__qualname__}"
    try:
        # `getattr` with a default, so "this object is not element-shaped" is
        # an ordinary answer rather than an exception to classify. It suppresses
        # `AttributeError` ONLY, so a property that raises anything else still
        # reaches the handler below — which is the case that matters.
        tag = getattr(value, "tag", None)  # nodriver Element: node_name.lower()
        attrs = getattr(value, "attrs", None)  # nodriver Element: a ContraDict
        names = None if attrs is None else list(attrs.keys())
        # The CHILD COUNT and never the children: `__repr__` renders descendant
        # TEXT by recursing `str(child)`, and a text node's own `__repr__`
        # answers its raw `node_value` -- so "$12,345.67" in a <div> is as much
        # page content as an attribute value. A count says how much was dropped
        # without saying any of it.
        children = getattr(value, "child_node_count", None)
    except Exception as exc:  # noqa: BLE001  PERMANENT(F-907 — inside makeRecord)
        # It must be TOTAL, not narrow. Both reads run arbitrary library code
        # -- `tag` is a property and `attrs` answers a `ContraDict` -- and this
        # function runs inside `Logger.makeRecord`, so anything escaping breaks
        # every log call in the process, including the one reporting it. A
        # narrow tuple was written first and a `RuntimeError` from a property
        # walked straight through it (pinned).
        #
        # It is not a SWALLOW: the failure is reported in the one channel
        # available here, the line itself, because logging about it would
        # recurse. The exception's TYPE only -- never `str(exc)`, which on a
        # page-derived object is page-authored, which is the whole subject.
        return f"<{kind} shape-unreadable={type(exc).__name__}>"
    if not isinstance(tag, str) or not isinstance(names, list):
        return f"<{kind}>"
    shown = [_clamped(name) for name in names[:SHAPE_MAX_ATTRS]]
    if len(names) > SHAPE_MAX_ATTRS:
        shown.append(f"{SHAPE_OVERFLOW}+{len(names) - SHAPE_MAX_ATTRS}")
    counted = f" children={children}" if isinstance(children, int) else ""
    return f"<{_clamped(tag)} attrs=[{', '.join(shown)}]{counted}>"


def _carries_payload(value: object) -> bool:
    """Is THIS argument one whose rendering can carry a page's own content?

    Two clauses, and the order is the argument. An **exception is never one**,
    whatever package defined it: its ``str()`` is a diagnostic about a failure,
    and the one site that logs one — ``connection.py``:483, nodriver's only
    genuine WARNING — passes ``exc_info=True`` beside it, so every sink that
    formats a traceback renders that text ANYWAY. Redacting the ``%s`` while
    ``exc_info`` carries it through withholds nothing and costs the Sentry
    breadcrumb its whole diagnostic: measured, a
    ``nodriver.core.connection.ProtocolException`` — the commonest thing a
    CDP-touching event handler raises, and a nodriver TYPE — rendered as
    ``<nodriver.core.connection.ProtocolException>``, losing Chrome's own
    ``Inspected target navigated [code: -32000]``.

    Otherwise it is the argument's package, matched on ``type(value).__module__``
    and never with ``isinstance``, for :data:`PAYLOAD_ARG_PACKAGE`'s reason.

    Read of the ARGUMENT alone and never of ``record.name``: whether a rendering
    carries page content is a property of the object, not of the logger someone
    passed it to. A gate on the logger's package left a ``stealth.*`` record
    carrying an ``Element`` leaking (measured), which is the one shape the rule
    exists for, in exchange for skipping a loop over arguments no record of ours
    has — and it made this module's stated key false in the navigation map,
    which is how the next change to it would go wrong.
    """
    if isinstance(value, BaseException):
        return False
    return type(value).__module__.partition(".")[0] == PAYLOAD_ARG_PACKAGE


def _redacted(args: tuple[object, ...]) -> tuple[object, ...]:
    """Replace every payload-carrying argument with its shape.

    EAGERLY, and with a plain ``str``: a lazy wrapper would keep the element
    alive for the life of the record and would still have to render for any
    sink that formats, while a ``str`` leaves nothing downstream — Sentry, the
    debug ring, a handler's ``getMessage()`` — able to re-derive the payload.
    It costs nothing extra, because a ``LogRecord`` only exists at all once
    ``isEnabledFor`` has admitted it: under F-906's floor nodriver's DEBUG and
    INFO payload lines never reach this function, and the two findings compose
    exactly along that line.
    """
    # The scan is separate from the rewrite so the overwhelmingly common record
    # -- every one in the process that carries no nodriver object -- gets its
    # OWN tuple back rather than an equal copy, which is what lets a pin assert
    # IDENTITY: a stronger statement of "untouched" than equality is.
    if not any(_carries_payload(arg) for arg in args):
        return args
    return tuple(_shape(arg) if _carries_payload(arg) else arg for arg in args)


def install_payload_arg_redaction() -> None:
    """Stop a third party's log line carrying content we never shaped.

    **THE one record-factory install, now carrying TWO rules** — F-907's, which
    shapes a payload-carrying ARGUMENT, and F-911's, which withholds the
    rendered text of a record made by a payload-rendering MODULE. The name is
    F-907's and is kept deliberately: what must never be duplicated is the
    INSTALL, because a factory chain is ordered by install time and two of them
    make the outcome depend on call order. A rule is added by adding a rule.

    F-907. F-906 held ``nodriver`` and ``websockets`` at WARNING because
    everything below it quoted raw CDP. This is the half ABOVE that floor:
    ``element.py``'s three box-model WARNINGs interpolate a live ``Element``,
    whose ``__repr__`` renders every attribute VALUE and all of its text.
    MEASURED, in the **shipped backend configuration** and not merely under a
    caller's ``basicConfig``: a password field's ``value=``, a ``data-*``
    session token, the element's text and a tab's URL all reached a root
    handler — hence stderr, hence ``backend-boot.log``, a durable file — and all
    of them reached Sentry as breadcrumbs on the next event, because
    ``LoggingIntegration``'s breadcrumb handler sits at INFO and these are
    WARNINGs. And with **no handler anywhere**, which is the shipped shape,
    ``logging.lastResort`` carries it to stderr regardless. Whether those three
    nodriver lines can FIRE is a separate question with a separate answer —
    see :data:`PAYLOAD_ARG_PACKAGE`.

    **Why a record FACTORY and not a ``logging.Filter``.** Both were measured.
    ``Logger.handle`` consults only the filters of the logger the call was made
    ON, and ``callHandlers`` then walks ancestors for HANDLERS, never for their
    filters — so a filter on the family root ``nodriver`` never fires for a
    ``nodriver.core.element`` record (pinned, because a fix written that way
    passes a test that emits on the family root and leaks every real line).
    Filtering each descendant instead cannot work either: at
    :func:`configure_logging` time not one ``nodriver.*`` logger exists, since
    the proxy never imports nodriver and the backend imports it later, so an
    enumeration would cover nothing and would need a SECOND install site after
    the import — two homes for one decision. And a filter on a HANDLER is no
    use in the configuration this finding is about, because we do not own the
    handler: production root carries none (``logging.lastResort``) and under a
    caller's ``basicConfig`` it is theirs.

    A factory runs inside ``Logger.makeRecord``, which is upstream of filters,
    of handlers, of ``lastResort`` and of Sentry's ``callHandlers`` patch —
    MEASURED against all four — and it covers a logger created after it is
    installed, including a module a future nodriver adds. It also survives
    ``logging.config.dictConfig(disable_existing_loggers=True)``, which is not
    true of anything attached to a logger.

    **Why no ``before_breadcrumb`` beside it.** For F-906's reason exactly: one
    mechanism upstream of every sink closes all four at once, and a rule in
    ``observability`` would be a second home for one decision (convention 4)
    that could only ever matter if this one were removed.

    Keyed on the ARGUMENT's type — never on the message text, so a nodriver
    release is free to reword these lines, and never on ``record.name``, because
    whether a rendering carries page content is a property of the object rather
    than of the logger it was passed to (see :func:`_carries_payload`).

    **Cost, measured** (min of 7 x 200 000, against ~1.7 us to build a record):
    ~350 ns for the chained factory CALL itself, paid by every record in the
    process, plus ~80 ns per argument for the scan. Reading the argument rather
    than the logger name is that per-argument half only — the 350 ns is the
    price of installing any factory at all.

    Called from :func:`configure_logging` beside
    :func:`apply_payload_log_floor`, ahead of the idempotency guard and of
    everything that can raise ``OSError``, for that function's reason: a
    process whose log directory could not be created still has stderr and
    Sentry. Idempotent, on ``session_hygiene.install()``'s precedent, and it
    CHAINS rather than replaces, so a caller's own factory keeps running.

    **F-911, the second rule.** Its table and its two helpers are
    ``payload_log_sites``' — see that module for the measurement and for why
    the unit is a MODULE — and what lives here is the one thing a leaf cannot
    own: the rewrite, inside the one factory. Keyed on the same species of fact
    as F-907's — the identity of the CODE, never the wording of the line — but
    read off the record's own ``pathname`` instead of an argument's type,
    because ``mcp/shared/session.py`` interpolates with an f-string and so
    hands ``logging`` a finished string with no arguments at all.

    It is a rule here and not a ``logging.Filter`` for a reason F-907
    half-stated and F-911 completes: a filter on ROOT *would* fire for these
    (``Logger.handle`` consults the filters of the logger the call was made ON,
    and that logger is root), so the objection is no longer that it cannot work
    — it is that it would be a SECOND mechanism answering "what may a third
    party's log line carry", one import away from this one, covering strictly
    less (only records made on root) and needing its own idempotency, its own
    install site and its own restore in ``tests/logging_state.py``. Convention
    4 read literally.

    A ``before_breadcrumb`` in ``observability`` is declined for F-906's reason
    unchanged, and it is MEASURED rather than argued: ``makeRecord`` runs
    upstream of ``Logger.callHandlers``, the one method ``LoggingIntegration``
    patches, so the breadcrumb is built from an already-withheld record. Both
    halves of that premise are pinned, so an SDK that moved its hook upstream
    makes this RED instead of quietly re-opening the door.

    **F-913, the third rule.** The first one here that is not about the log
    RECORD at all: ``mcp/client/streamable_http.py`` logs a STATIC message with
    no arguments at ERROR, so F-907's rule sees nothing to shape, F-911's would
    withhold the one part that is safe, and F-908's WARNING floor on
    ``mcp.client`` sits below it — while its ``exc_info`` carries a pydantic
    ``ValidationError`` whose ``str()`` quotes the SSE data, which on the proxy
    leg is the answer to a ``tools/call``. Its table, its structural test and
    the restatement are ``payload_log_sites``' too; what lives here is the same
    thing as for F-911 — the rewrite, inside the one factory.

    Keyed on the same species of fact as the other two: the identity of the
    CODE (the record's ``pathname``) and the identity of the exception's TYPE,
    never the wording of either. It is gated on the SITE and not on the type
    alone deliberately — see :data:`payload_log_sites.PAYLOAD_EXCEPTION_SITES`
    for the measured reason (``expected_events``' ``caller-input`` class reads
    that same exception type off FastMCP's own records).

    It does NOT reopen F-907's exception clause. That clause says an exception
    is never SHAPED where a traceback renders it anyway, which is still true of
    every exception that quotes nobody; this rule is about the one measured
    shape whose rendering IS the payload, and it replaces the rendering rather
    than suppressing the diagnostic — the pydantic error type, the model, the
    error count and every field path survive.

    The residual is F-906's, named rather than hidden: a caller who installs
    their own record factory AFTER this one replaces it.
    """
    previous = logging.getLogRecordFactory()
    if getattr(previous, _REDACTION_MARK, False):
        return

    def factory(*args: object, **kwargs: object) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        # THREE rules, ONE factory, and the grouping is the argument. F-911's
        # asks about the SITE and answers for the whole MESSAGE; F-907's asks
        # about an ARGUMENT. A record whose site is withholding has no
        # arguments left to shape -- they are cleared one line down -- so the
        # second rule has nothing to do and the `elif` is the cheaper spelling
        # of that, not a precedence anyone has to reason about.
        #
        # F-913's is a separate `if` below and NOT part of that chain, because
        # it replaces a different half of the record: the two site tables are
        # disjoint today, but a module that both root-logs a payload AND
        # carries a payload-quoting exception would need both rules, and an
        # `elif` would silently give it one.
        #
        # A SECOND factory install would be the defect here, not a third rule
        # inside this one: the chain is ordered by install time, so two
        # factories make "which rewrite saw the record first" depend on the
        # order `configure_logging` happens to call them in.
        site = payload_log_sites.site_of(record)
        if site is not None:
            # F-911. `record.args` must go WITH the message and not after it:
            # `getMessage()` runs `msg % args`, so leaving a `%s`-carrying
            # tuple beside a message that no longer has a `%s` raises
            # `TypeError` in every handler that formats -- turning a redaction
            # into an outage. `exc_info` is F-913's rule below, never this
            # one's (see `payload_log_sites`, and F-907's exception clause).
            record.msg = payload_log_sites.withheld(site, record)
            record.args = ()
        # `record.args` is a TUPLE unless the caller passed a single mapping,
        # logging's own `%(name)s` special case -- which no measured nodriver
        # line uses, and which we leave alone rather than guess a rewrite for.
        #
        # Deliberately NOT gated on `record.name`: `_carries_payload` asks about
        # the ARGUMENT, so one of our own records carrying a nodriver object is
        # redacted exactly as nodriver's own is. See that function.
        elif isinstance(record.args, tuple) and record.args:
            record.args = _redacted(record.args)
        # F-913. The only rule here that touches `exc_info`, and it does so by
        # REPLACING what the record carries rather than by mutating anything:
        # the SDK sends the very exception it just logged downstream
        # (`streamable_http.py`:241), so the live object has to survive intact.
        # The original TRACEBACK is handed through, so every frame — and
        # therefore Sentry's grouping — is unchanged.
        restated = payload_log_sites.restated_exc_info(record)
        if restated is not None:
            record.exc_info = restated
        return record

    # Through the CONSTANT, never a literal: the mark is read one function up
    # by the same name, and two spellings of it would make a rename silently
    # turn this install non-idempotent.
    setattr(factory, _REDACTION_MARK, True)
    logging.setLogRecordFactory(factory)


def backend_uvicorn_config() -> dict[str, object]:
    """The ``uvicorn_config`` the backend's ``mcp.run(transport="http", …)``
    passes — the one home for how the backend's HTTP server logs and stops.

    ``access_log=False`` is F-830's first half. Uvicorn writes one INFO line
    per request to stdout, which ``singleton._start_server_process`` redirects
    into the shared boot log, and the client watchdog probes every live stdio
    proxy every ~2 s: ~13M lines / 794 MB of ``"POST /mcp/ 200"`` on the
    reporting machine, with zero diagnostic value. Only the HTTP access spam
    goes — the calls that matter are logged by :func:`with_correlation_id`
    against ``stealth.backend``, which this does not touch.
    """
    return {
        "timeout_graceful_shutdown": _GRACEFUL_SHUTDOWN_SECONDS,
        "access_log": False,
    }


def new_correlation_id() -> str:
    """A short id for one tool call, stamped on every log line emitted during
    it by :class:`CorrelationIdFilter`."""
    return uuid.uuid4().hex[:12]


class CorrelationIdFilter(logging.Filter):
    """Stamps ``record.correlation_id`` from the current context var."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = correlation_id_var.get()
        return True


_tool_call_logger = logging.getLogger("stealth.backend")


def _record_tool_failure(tool_name: str, error: Exception) -> None:
    """Put a failed tool call into the in-memory debug ring (F-835).

    Called from the ONE wrapper every registered tool passes through, so a
    failure is visible in the product's own debug surface no matter which tool
    it came from — before this, a total ``spawn_browser`` outage (24 consecutive
    failures) left ``get_debug_view`` reporting ``total_errors: 0``.

    Two properties this helper exists to guarantee:

    * it **never raises**. It sits on the failure path of all 94 tools, and a
      debug-ring problem must not replace (or mask) the error the client is
      owed. The recording is the only thing that can be lost here.
    * it **never touches** ``error``. The exception continues to the client
      byte-identical — same type, same args, no attributes added (a pinned
      contract: ``test_observability`` asserts ``vars(exc) == {}``).

    Note what it does NOT do: write the failure to the backend log. That is
    ``log_tool_failure``'s deliberate split (F-782's redaction condition), not
    an oversight — the ring is process-local, the log file is durable and
    Sentry-bridged, and a failure message echoes the caller's arguments.

    ``debug_logger`` is imported here rather than at module scope because it
    imports ``correlation_id_var`` from THIS module; a top-level import would
    close the cycle. Deferred, function-local imports are the established fix
    for exactly this shape here (see the module docstring and pyproject's
    PLC0415 rationale) — and on the failure path the cost is a dict lookup.
    """
    with contextlib.suppress(Exception):
        from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger

        debug_logger.log_tool_failure(tool_name, error)


def with_correlation_id(func: Callable[..., object]) -> Callable[..., object]:
    """Wrap a registered tool function (the ``section_tool`` chokepoint, F-308)
    so every call gets a fresh correlation id — stamped by
    :class:`CorrelationIdFilter` onto every log line emitted during the call,
    backend file and ``debug_logger`` entries alike — and one INFO start/end
    pair. ``functools.wraps`` preserves the schema FastMCP introspects
    (name/signature/docstring); a ``tools/list`` schema-snapshot test pins
    that this holds for a representative tool per section.

    It is also where a FAILED call is recorded (F-835,
    :func:`_record_tool_failure`): the same chokepoint argument that makes this
    the home of the correlation id makes it the home of "this call failed" —
    one place, all 94 tools, instead of a per-tool ``except`` nobody adds. The
    exception is recorded and re-raised unchanged.

    91 of the 96 registered tools are ``async def`` and 5 are plain ``def``;
    Python has no single syntax that both ``await``s and doesn't, so this
    branches once on ``iscoroutinefunction`` to produce a matching wrapper.
    """
    # Not every Callable is guaranteed a __name__ (e.g. a callable class
    # instance); all 96 real registrations are plain def/async def, but this
    # keeps the wrapper honest for its declared, more general parameter type.
    tool_name = getattr(func, "__name__", repr(func))

    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_wrapper(*args: object, **kwargs: object) -> object:
            token = correlation_id_var.set(new_correlation_id())
            start = time.monotonic()
            _tool_call_logger.info("tool %s start", tool_name)
            try:
                return await func(*args, **kwargs)
            except Exception as error:
                # Record and re-raise, unchanged (F-835). Only Exception:
                # CancelledError is a shutdown signal, not a tool failure.
                _record_tool_failure(tool_name, error)
                raise
            finally:
                elapsed_ms = (time.monotonic() - start) * 1000
                _tool_call_logger.info("tool %s end (%.1fms)", tool_name, elapsed_ms)
                correlation_id_var.reset(token)

        return async_wrapper

    @functools.wraps(func)
    def sync_wrapper(*args: object, **kwargs: object) -> object:
        token = correlation_id_var.set(new_correlation_id())
        start = time.monotonic()
        _tool_call_logger.info("tool %s start", tool_name)
        try:
            return func(*args, **kwargs)
        except Exception as error:
            _record_tool_failure(tool_name, error)  # F-835, as above
            raise
        finally:
            elapsed_ms = (time.monotonic() - start) * 1000
            _tool_call_logger.info("tool %s end (%.1fms)", tool_name, elapsed_ms)
            correlation_id_var.reset(token)

    return sync_wrapper


def resolve_log_dir() -> Path:
    """``STEALTH_MCP_LOG_DIR`` override, else the existing per-user state-dir
    convention (``singleton.STATE_DIR / "logs"``). Pure — never creates the
    directory.
    """
    configured = get_settings().log_dir
    if configured and configured.strip():
        return Path(configured).expanduser()

    from stealth_chrome_devtools_mcp.embedded import singleton

    return singleton.STATE_DIR / "logs"


def configure_logging(role: str) -> Path:
    """Idempotent: install one ``RotatingFileHandler`` for ``stealth.<role>``.

    Returns the log file path regardless of whether setup succeeded. Never
    raises — a logging-setup failure must not take down the backend/proxy
    (plan_M3 risk #7); on failure this degrades to a no-op.

    It is also where the payload-carrying library loggers are held down
    (:func:`apply_payload_log_floor`, F-906/F-908 — all four families in
    :data:`PAYLOAD_LOG_FAMILIES`). That call is FIRST — ahead of the
    idempotency guard and ahead of everything that can raise ``OSError`` —
    because the floor is about what may leave the process, and a process that
    failed to open its log file still has stderr and still has Sentry.
    """
    apply_payload_log_floor()
    install_payload_arg_redaction()

    log_dir = resolve_log_dir()
    log_path = log_dir / f"{role}-{os.getpid()}.log"
    logger = logging.getLogger(f"stealth.{role}")

    if logger.handlers:
        return log_path  # already configured in this process

    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            log_path,
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            delay=True,
            encoding="utf-8",
        )
        handler.addFilter(CorrelationIdFilter())
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logger.addHandler(handler)
        logger.propagate = False
        level_name = get_settings().log_level.upper()
        logger.setLevel(getattr(logging, level_name, logging.INFO))
    except OSError:
        return log_path

    prune_old_logs(log_dir)
    return log_path


_bootstrapped_roles: set[str] = set()


def bootstrap_backend_process_logging() -> Path:
    """Backend boot-time wiring — the single call ``embedded/server.py``'s
    ``__main__`` makes before anything else, including ``sentry_init()``
    (F-303's in-process half). Installs the ``stealth.backend`` file handler,
    then a ``sys.excepthook``/``threading.excepthook`` pair that record a
    fatal exception before the process dies, plus ``faulthandler`` for
    hard/C-level faults that never reach Python's exception machinery at
    all. Idempotent (safe if ``embedded/server.py`` loads twice via
    ``runpy``). Returns the ``stealth.backend`` log path.
    """
    log_path = configure_logging("backend")
    logger = logging.getLogger("stealth.backend")

    if "backend" in _bootstrapped_roles:
        return log_path  # already wired in this process

    def _log_excepthook(
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_tb: TracebackType | None,
    ) -> None:
        logger.critical(
            "Fatal unhandled exception", exc_info=(exc_type, exc_value, exc_tb)
        )
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    def _log_thread_excepthook(args: threading.ExceptHookArgs) -> None:
        thread = args.thread
        thread_name = thread.name if thread is not None else "unknown"
        exc_value = args.exc_value
        if exc_value is None:
            threading.__excepthook__(args)
            return
        logger.critical(
            "Fatal unhandled exception in thread %r",
            thread_name,
            exc_info=(args.exc_type, exc_value, args.exc_traceback),
        )
        threading.__excepthook__(args)

    sys.excepthook = _log_excepthook
    threading.excepthook = _log_thread_excepthook
    _bootstrapped_roles.add("backend")

    # A dedicated, never-rotated file: faulthandler writes at the C level on a
    # hard crash, so sharing the RotatingFileHandler's file would add a second
    # open handle across the SAME path it may later os.rename() during
    # rotation (Windows WinError 32 risk, plan_M3 risk #1).
    fault_log_path = log_path.with_name(f"{log_path.stem}-fault.log")
    with contextlib.suppress(OSError):
        fault_log = fault_log_path.open("a", encoding="utf-8")
        faulthandler.enable(file=fault_log)

    # Affirmative proof of boot, independent of any error occurring: without
    # this the structured log stays EMPTY until the first error/warning/info
    # call, so "did the backend boot at all" was answerable only from
    # backend-boot.log (plan_M3 §3 step-2 verify: "a backend-<pid>.log with
    # the ... startup line"). F-840 adds argv: the ONE thing that distinguishes
    # a console-attached `serve --http` birth from the detached
    # _start_server_process spawn, which a post-mortem otherwise cannot tell
    # apart. Local file only — this line is never shipped to Sentry.
    logger.info(
        "backend process starting (pid=%d, log=%s, argv=%r)",
        os.getpid(),
        log_path,
        sys.argv,
    )

    return log_path


def roll_boot_log(log_dir: Path) -> Path:
    """Rotate ``<logdir>/backend-boot.log`` if it has grown past
    :data:`_BOOT_LOG_MAX_BYTES`, then return its (now free) path.

    Called by ``singleton._start_server_process`` immediately before it opens
    the file for a NEW backend. That is the ONLY moment rotation is possible:
    the boot log is a raw ``Popen`` stdout/stderr redirect, so the running
    backend holds its descriptor open for its entire life — an in-process
    ``RotatingFileHandler`` never sees these bytes, and an external rotation
    would either fail (Windows sharing violation) or silently keep writing to
    the renamed inode (POSIX). The launcher is between two backends and holds
    no descriptor, so it is the one safe hand-off point.

    Keeps ``.1`` … ``.<_BOOT_LOG_BACKUPS>``, newest first, and drops the rest —
    a rotation that accumulated would only rename F-830, not fix it. Never
    raises: a boot log that cannot be rolled must not block a spawn (plan_M3
    §7's fail-open discipline, same as the caller's own OSError fallback).
    """
    boot_log = log_dir / BOOT_LOG_NAME
    try:
        if boot_log.stat().st_size <= _BOOT_LOG_MAX_BYTES:
            return boot_log
        (log_dir / f"{BOOT_LOG_NAME}.{_BOOT_LOG_BACKUPS}").unlink(missing_ok=True)
        for index in range(_BOOT_LOG_BACKUPS - 1, 0, -1):
            older = log_dir / f"{BOOT_LOG_NAME}.{index}"
            if older.exists():
                older.replace(log_dir / f"{BOOT_LOG_NAME}.{index + 1}")
        boot_log.replace(log_dir / f"{BOOT_LOG_NAME}.1")
    except OSError:
        pass
    return boot_log


def _post_mortem_exempt(files: list[Path]) -> set[Path]:
    """F-840: the subset of ``files`` (newest first) that age must not reach.

    A dead backend's ``backend-<pid>.log`` + ``backend-<pid>-fault.log`` pair
    is the whole post-mortem for that process, and the age at which it becomes
    interesting is exactly the age at which an unattended crash gets noticed —
    so a plain mtime sweep deletes evidence precisely when it is needed (the
    2026-08-30 OOM investigation started blind for this reason). Two rules,
    both narrow: keep the newest :data:`_KEEP_BACKEND_SETS` pid-sets whatever
    their age, and keep every fault log younger than
    :data:`_FAULT_LOG_KEEP_DAYS` (they are near-empty unless a hard crash
    actually wrote one, so this costs bytes, not megabytes).
    """
    fault_cutoff = time.time() - _FAULT_LOG_KEEP_DAYS * 86400
    exempt = {
        path
        for path in files
        if path.name.endswith("-fault.log") and path.stat().st_mtime >= fault_cutoff
    }
    recent_pids: list[str] = []
    for path in files:
        match = _BACKEND_LOG_RE.match(path.name)
        if match is not None and match.group(1) not in recent_pids:
            recent_pids.append(match.group(1))
    keep_pids = set(recent_pids[:_KEEP_BACKEND_SETS])
    for path in files:
        match = _BACKEND_LOG_RE.match(path.name)
        if match is not None and match.group(1) in keep_pids:
            exempt.add(path)
    return exempt


def prune_old_logs(
    log_dir: Path | None = None, keep_days: int = 7, keep_files: int = 50
) -> None:
    """Best-effort sweep of ``<logdir>`` so per-pid log files (one per proxy
    session) don't accumulate forever. Never raises.

    Dead-backend post-mortems are exempt — see :func:`_post_mortem_exempt`.
    """
    try:
        target_dir = log_dir if log_dir is not None else resolve_log_dir()
        if not target_dir.is_dir():
            return
        files = sorted(
            (p for p in target_dir.glob("*.log*") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        exempt = _post_mortem_exempt(files)
        cutoff = time.time() - keep_days * 86400
        for index, path in enumerate(files):
            if path in exempt:
                continue
            if index >= keep_files or path.stat().st_mtime < cutoff:
                with contextlib.suppress(OSError):
                    path.unlink()
    except OSError:
        pass
