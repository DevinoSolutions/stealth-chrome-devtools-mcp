"""THE one home for "is this Sentry event the product working as designed?" (F-887).

``observability.py`` decides where errors go and scrubs what leaves the machine.
This module answers a different question, and it is the only thing in the tree
that answers it: of the events the logging integration hands ``before_send``,
WHICH ones are shapes this product is known to produce on purpose? It is
consumed from exactly one place — ``observability._expected_event_class``, step
0 of the one ``before_send`` — and it never decides anything else: it does not
scrub, does not log, does not import the SDK, and does not send.

Why it exists
-------------
Step 0 shipped with one class: "every exception in the chain is our
``tool_errors.ToolError``, so this is CLAUDE.md convention 2 doing its job".
That rule is right in spirit and missed the chains the product itself produces.
Triaged live on 2026-09-18 against release 2.1.8, the last seven days of the
project's Sentry were, in order of volume:

* **6 500** events of the bare message ``Received exception from stream: ``
  (logger ``mcp.server.lowlevel.server``, no exception values at all);
* **6 200** ``starlette.requests.ClientDisconnect``, raised inside
  ``request.body()`` under ``streamable_http._handle_post_request``;
* **466** pydantic ``ValidationError`` for ``call[spawn_browser]`` — callers
  sending ``window_width=`` at a tool whose parameters are
  ``viewport_width``/``viewport_height``;
* **231** ``ConnectionResetError`` ``[WinError 10054]`` from CPython's own
  ``_ProactorBasePipeTransport._call_connection_lost``;
* **151** ``ConnectionRefusedError`` out of nodriver's ``Browser.update_targets``
  background task, after its Chrome had already gone;
* **140+**, spread over ~20 separate issues (one per instance uuid in the
  message), of ``ToolError: CDP operation timed out`` and
  ``ToolError: Navigation to … timed out``.

Every one of those had already been ANSWERED before it was logged: the client
that disconnected is gone, the budget's ``ToolError`` reached the caller, FastMCP
replied to the caller with the validation message, and the two teardown races
belong to CPython and to nodriver. What they cost is the only thing Sentry is
for — an issue list a maintainer can read.

The five classes
----------------
Each is a NAME and ONE rule, and the rule is applied to both of the shapes an
event can arrive in (see "one rule, two paths" below):

``error-convention``
    The OUTERMOST link is our ``ToolError`` (subclasses included), and every
    other link is either ours or a *budget* link — ``TimeoutError`` or
    ``asyncio.CancelledError``. Nothing else in the chain is tolerated. Those
    two are what a bounded operation is made of: ``tool_runtime`` and
    ``browser_manager`` both bound work with ``asyncio.wait_for``, which raises
    ``TimeoutError`` *from* the ``CancelledError`` it used to stop the coroutine,
    and both convert it to a ``ToolError`` the caller receives — the conversion
    is always the LAST raise, which is why "outermost" is the right test and
    mere set membership is not. A body whose cleanup timed out UNCONVERTED while
    handling a ``ToolError`` has that ``ToolError`` in its chain and is still
    the missing-convention case: Sentry titles that issue with the
    ``TimeoutError``, and it must keep shipping.

``client-disconnect``
    Either the chain is entirely ``starlette.requests.ClientDisconnect``, or the
    event is the message-only form the session loop logs. **That second arm
    matches "an exception with NO TEXT at ``mcp.server.lowlevel.server``", not
    "a ``ClientDisconnect``"** — and it cannot be narrower.
    ``mcp/server/lowlevel/server.py``:707 (mcp 1.27.1) is a ``case Exception():``
    catch-all doing ``logger.error(f"Received exception from stream: {message}")``
    with no ``exc_info``, so the event carries no exception values, no frames and
    no extra: the empty tail is ``str(exc) == ""`` and there is genuinely nothing
    else in the event to read. A ``ClientDisconnect`` is what fills it 6 500
    times a week (its ``str()`` is empty, measured), but a bare
    ``RuntimeError()`` from our own session handling produces a byte-identical
    event and is dropped with it. That is the trade, taken deliberately and
    named again in the finding's §6. The match is EQUALITY, never a prefix: the
    same line with a real tail — "Received response with an unknown request ID:
    … Method not found" — is a protocol fault and keeps shipping.

``proactor-teardown``
    Logger ``asyncio``, message beginning ``Exception in callback
    _ProactorBasePipeTransport._call_connection_lost``, chain entirely
    ``ConnectionResetError``. CPython 3.13's ``asyncio/proactor_events.py``:154
    closes a pipe transport by calling ``self._sock.shutdown(socket.SHUT_RDWR)``
    in a ``finally`` — a line its own ``XXX`` comment describes as a cure for
    ``ERROR_NETNAME_DELETED``. If the peer has already reset, that shutdown
    raises out of a ``call_soon`` callback and the loop's default exception
    handler logs it. The callback is the whole claim: a reset our own code saw is
    a fact about this product's sockets and still ships.

``nodriver-dead-browser``
    Logger ``asyncio``, message beginning ``Task exception was never retrieved``
    AND containing ``nodriver``, chain entirely ``ConnectionRefusedError``.
    nodriver fires ``Browser.update_targets()`` without awaiting it; when its
    Chrome is gone the websocket connect is refused and the loop complains about
    the orphaned task. Requiring ``nodriver`` in the message is what keeps an
    unawaited task of OURS — same logger, same complaint, same exception type —
    shipping.

``caller-input``
    Logger ``FastMCP.fastmcp.tools.tool_manager``, chain entirely a pydantic
    ``ValidationError``, **and** the outermost link's frames carry
    :data:`ARG_VALIDATION_FRAMES` adjacently. That third condition is not
    decoration and it is the whole rule: the logger does NOT tell a caller's
    typo from a ``ValidationError`` our own code raised.
    ``fastmcp/tools/tool_manager.py``:220-229 wraps ``await tool.run(arguments)``
    in ONE ``try`` and logs everything out of it with the same
    ``logger.exception(f"Error calling tool {key!r}")`` — and
    ``fastmcp/tools/tool.py``:295's ``type_adapter.validate_python(arguments)``
    is INSIDE that ``run``. So logger, message and chain are byte-identical for
    the two, and dropping on them dropped our own bugs: measured, an unknown
    ``STEALTH_MCP_*`` key (which makes ``Settings()`` raise and fails EVERY
    spawn, since ``get_settings()`` is on the spawn path) and
    ``browser_manager.py``:429's ``BrowserInstance(...)`` with a wrong field type
    both vanished under the label "the caller already knows". The FRAMES do tell
    them apart, identically on both paths (measured): FastMCP's own validation
    always has ``fastmcp.tools.tool`` ``run`` immediately above
    ``pydantic.type_adapter`` ``validate_python``, and ours never does — a body
    of ours sits between them.

One rule, two paths
-------------------
``before_send`` may or may not be handed the live exception: the logging
integration fills ``hint["exc_info"]`` today, but a replayed or hand-built event
carries only the serialized payload. Both paths must judge the same set, so
every link is reduced to a :class:`Link` first — a type NAME, a MODULE and the
FRAMES, each spelled the way the SDK spells them — and every class is a
:class:`Kind` test over the first two plus, for ``caller-input``, an adjacency
test over the third.

"The way the SDK spells them" is a rule read out of ``sentry_sdk/utils.py`` and
not guessed, because all three differ from the obvious Python answer:

* ``get_type_name`` (:426) is ``__qualname__ or __name__``, so a class declared
  inside a function serializes as ``outer.<locals>.Name``. :func:`_type_name_of`
  mirrors it; reading ``__name__`` on the live path made a nested class match a
  :class:`Kind` that the serialized path could never match.
* ``get_type_module`` (:430) drops ``None``, ``builtins`` and ``__builtins__``
  — and **not** ``__main__``. So a serialized ``TimeoutError`` carries
  ``module: None`` while the live class says ``"builtins"``, which
  :data:`_UNWRITTEN_MODULES` normalizes; but a class from ``__main__`` keeps its
  module on BOTH paths, and ``embedded/server.py`` really does run as
  ``__main__`` under runpy.
* ``serialize_frame`` writes ``module`` as ``frame.f_globals["__name__"]``, which
  is what :func:`_live_frames` reads, and it lists frames oldest-first — caller
  first, the raising frame last — which is also the traceback's own order
  (measured identical, both paths).

The two paths do NOT agree on ORDER, and that is normalized here rather than in
each rule: the live chain walks outermost-first (``exc``, then its cause), while
Sentry's ``values`` list the ROOT cause first and the reported exception LAST.
:func:`_links` hands every rule one order — **outermost first** — so
``links[0]`` is the exception Sentry titles the issue with on either path.

There is exactly ONE exception to name-and-module matching, and it is the one
that was already there: our own error base is matched with ``isinstance`` when
the live exception is available, because CLAUDE.md convention 2 is about a CLASS
and a subclass declared anywhere must be covered. Every other kind matches by
name and module on both paths deliberately — ``isinstance`` on the live path
would accept subclasses that the serialized path, which sees only the subclass's
own qualified name, could never accept, and the two paths would quietly
disagree.

Never decides "expected" from nothing. An event with no readable links is
recognised by exactly one arm — ``client-disconnect``'s message-only form, which
names its logger and its whole message — and an exception value this module
could not read is not a link it will ever match. "Could not be read" is not
"recognised", because the default is to send.

A leaf: stdlib only, and the caller supplies both the chain and the error base,
so the lazy ``embedded.tool_errors`` import and the ``hint`` shapes stay
single-homed in ``observability.py`` (which is also what keeps
``observability._expected_error_base`` the one patchable name the suite targets).
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # the SDK is imported lazily by our caller; these are types
    from collections.abc import Callable

    from sentry_sdk.types import Event

# ---------------------------------------------------------------------------
# The class names. Returned rather than a bare bool so a drop is attributable:
# a surprise in the Sentry volume traces to ONE rule instead of to "the filter".
# ``observability._is_expected_tool_failure`` logs the name it got at DEBUG, so
# that is true of a running process and not only of the suite. Reported, never
# branched on — there is one drop.
# ---------------------------------------------------------------------------
ERROR_CONVENTION = "error-convention"
CLIENT_DISCONNECT = "client-disconnect"
PROACTOR_TEARDOWN = "proactor-teardown"
NODRIVER_DEAD_BROWSER = "nodriver-dead-browser"
CALLER_INPUT = "caller-input"

#: What the SDK writes for a class that lives in ``builtins``: nothing at all.
#: Mirrors ``sentry_sdk.utils.get_type_module`` EXACTLY — three names, and
#: ``"__main__"`` is deliberately NOT among them, because the SDK does not drop
#: it either. Dropping it here made a ``__main__``-defined class read as a
#: builtin on the live path and as itself on the serialized one, which is the
#: one thing this reduction exists to prevent.
_UNWRITTEN_MODULES = frozenset({None, "builtins", "__builtins__"})

#: How far into a traceback the frame read goes. A traceback cannot cycle, so
#: this is a cost bound and not a terminator. Truncating a caller-first list can
#: only REMOVE an adjacency, never invent one, so the cap can only cost a drop —
#: i.e. it resolves toward sending, like every other uncertainty here.
_MAX_FRAMES = 128


@dataclass(frozen=True)
class Frame:
    """One stack frame, as both paths describe it: a module and a function name.

    ``module`` is ``frame.f_globals["__name__"]`` — what
    ``sentry_sdk.utils.serialize_frame`` writes — so the live walk and the
    serialized list are directly comparable.
    """

    module: "str | None"
    function: "str | None"


@dataclass(frozen=True)
class Link:
    """One exception in an event's chain, as BOTH of the paths can describe it.

    ``live`` is the exception object when ``before_send`` was handed one, and it
    is read by exactly one rule — our own error base, the single ``isinstance``
    match. Everything else reads ``type_name``, ``module`` and ``frames``, all
    of which the serialized payload has too.
    """

    type_name: str
    module: "str | None"
    frames: "tuple[Frame, ...]" = ()
    live: "BaseException | None" = None


#: A serialized exception value this module could not read. It matches no
#: :class:`Kind`, because no kind's name set contains the empty string — which
#: is how an unreadable value keeps a whole chain from being recognised.
_UNREADABLE = Link("", None)


@dataclass(frozen=True)
class Kind:
    """One exception class, spelled the way an event carries it.

    ``modules`` is a set of module ROOTS, matched exactly or on a dotted
    boundary, because the SDK reports the defining submodule: pydantic's
    ``ValidationError`` arrives as ``pydantic_core._pydantic_core`` and our own
    errors as ``stealth_chrome_devtools_mcp.embedded.tool_errors``. The boundary
    is what keeps a package merely SPELLED like one of ours (``pydanticfoo``,
    ``stealth_chrome_devtools_mcp_extra``) out. ``None`` means "builtins", where
    the SDK writes no module at all, and it is matched exactly so that a
    third-party class sharing a builtin's NAME cannot pass.
    """

    names: "frozenset[str]"
    modules: "frozenset[str] | None"

    def matches(self, link: Link) -> bool:
        if link.type_name not in self.names:
            return False
        if self.modules is None:
            return link.module is None
        if link.module is None:
            return False
        return any(
            link.module == root or link.module.startswith(f"{root}.")
            for root in self.modules
        )


#: Our error convention. The one kind that also matches SUBCLASSES — see
#: :func:`_is_ours`. The names are an explicit allowlist rather than "anything
#: from our package" because this decides what is never seen again, and the
#: module is checked because ``fastmcp.exceptions.ToolError`` is a DIFFERENT
#: class with the same name and is what wraps a genuine crash on its way out of
#: ``tool_manager``.
OURS = Kind(
    frozenset({"ToolError", "InstanceNotFoundError"}),
    frozenset({"stealth_chrome_devtools_mcp"}),
)

#: What a bounded operation is made of: ``asyncio.wait_for`` cancels the
#: coroutine it gave up on and raises ``TimeoutError`` from that
#: ``CancelledError``. Tolerated only BEHIND :data:`OURS` in the chain — see
#: ``error-convention`` above for why the outermost link is the test.
BUDGET_KINDS = (
    Kind(frozenset({"TimeoutError"}), None),
    Kind(frozenset({"CancelledError"}), frozenset({"asyncio.exceptions"})),
)

CLIENT_GONE = Kind(frozenset({"ClientDisconnect"}), frozenset({"starlette.requests"}))
CONNECTION_RESET = Kind(frozenset({"ConnectionResetError"}), None)
CONNECTION_REFUSED = Kind(frozenset({"ConnectionRefusedError"}), None)
CALLER_VALIDATION = Kind(
    frozenset({"ValidationError"}), frozenset({"pydantic", "pydantic_core"})
)

#: The two frames FastMCP's OWN argument validation always puts adjacent, and
#: the only thing that tells a caller's bad kwarg from a ``ValidationError`` our
#: code raised under the same logger with the same message and the same chain.
#: ``tool.run`` calls ``validate_python`` directly (``fastmcp/tools/tool.py``
#: :295); a body of ours always sits between them. Measured on both paths,
#: fastmcp 2.11.2 / pydantic 2.11.7.
ARG_VALIDATION_FRAMES = (
    Frame("fastmcp.tools.tool", "run"),
    Frame("pydantic.type_adapter", "validate_python"),
)

#: The loggers the four logger-gated classes name, verbatim.
ASYNCIO_LOGGER = "asyncio"
MCP_SESSION_LOGGER = "mcp.server.lowlevel.server"
TOOL_MANAGER_LOGGER = "FastMCP.fastmcp.tools.tool_manager"

#: ``mcp/server/lowlevel/server.py``:707's whole message when the exception it
#: formatted had no text. Compared with ``==`` on purpose: a prefix match would
#: also swallow the protocol faults that line reports, which are real.
STREAM_EXCEPTION_MESSAGE = "Received exception from stream: "

#: CPython's Windows pipe-transport teardown, and the loop complaint that names
#: an unawaited task. Prefixes, because both messages continue with the callback
#: arguments / the task repr.
PROACTOR_TEARDOWN_PREFIX = (
    "Exception in callback _ProactorBasePipeTransport._call_connection_lost"
)
UNRETRIEVED_TASK_PREFIX = "Task exception was never retrieved"

#: What tells nodriver's orphaned task from one of ours: the task repr names the
#: coroutine's defining file.
NODRIVER_MARK = "nodriver"


@dataclass(frozen=True)
class EventFacts:
    """Everything a rule may ask about an event, read exactly once.

    Reading the event up front is what keeps the rules total functions of it:
    a rule cannot reach past these four fields, so adding one is a visible
    change to this class rather than a new way to interrogate an event. Named
    ``EventFacts`` and not ``Facts`` because ``animation_facts.Facts`` already
    means "what the collector sent" — one meaning per term.
    """

    links: "tuple[Link, ...]"
    logger: "str | None"
    message: str
    error_base: "type[BaseException] | None"


def classify(
    event: "Event",
    chain: "list[BaseException] | None",
    error_base: "type[BaseException] | None",
) -> "str | None":
    """Which named class of expected noise is this event, or ``None``.

    ``chain`` is the live exception chain when ``before_send`` was handed one
    (``observability._exception_chain``, outermost first), and ``None`` or empty
    when only the serialized payload is available — in which case the links are
    read out of ``event["exception"]["values"]`` and reversed into that same
    order. ``error_base`` is our own ``ToolError``, or ``None`` when it could not
    be imported, which degrades the one ``isinstance`` match to
    name-and-module like every other kind.

    Does not catch: the never-raises contract lives at the one call site, so a
    bug here is visible to the suite rather than silently turning into "ship it".
    """
    facts = EventFacts(
        links=_links(event, chain),
        logger=_logger(event),
        message=_message(event),
        error_base=error_base,
    )
    for name, rule in RULES:
        if rule(facts):
            return name
    return None


# ---------------------------------------------------------------------------
# The five rules
# ---------------------------------------------------------------------------
def _error_convention(facts: EventFacts) -> bool:
    """Ours as the REPORTED exception, over nothing but ours and budget links."""
    if not facts.links or not _is_ours(facts.links[0], facts.error_base):
        return False
    return all(
        _is_ours(link, facts.error_base) or _is_budget(link) for link in facts.links[1:]
    )


def _client_disconnect(facts: EventFacts) -> bool:
    """The client went away mid-request — from the POST, or from the session loop."""
    if _all_are(facts.links, CLIENT_GONE):
        return True
    return (
        not facts.links
        and facts.logger == MCP_SESSION_LOGGER
        and facts.message == STREAM_EXCEPTION_MESSAGE
    )


def _proactor_teardown(facts: EventFacts) -> bool:
    """CPython's own ``shutdown()`` on a socket the peer already reset."""
    return (
        facts.logger == ASYNCIO_LOGGER
        and facts.message.startswith(PROACTOR_TEARDOWN_PREFIX)
        and _all_are(facts.links, CONNECTION_RESET)
    )


def _nodriver_dead_browser(facts: EventFacts) -> bool:
    """nodriver's unawaited target refresh, after its Chrome had gone."""
    return (
        facts.logger == ASYNCIO_LOGGER
        and facts.message.startswith(UNRETRIEVED_TASK_PREFIX)
        and NODRIVER_MARK in facts.message
        and _all_are(facts.links, CONNECTION_REFUSED)
    )


def _caller_input(facts: EventFacts) -> bool:
    """FastMCP validating a CALLER's arguments — never our own model failing.

    The frame pair is the discriminator; the logger alone is not. See
    ``caller-input`` in the module docstring for the measurement.
    """
    return (
        facts.logger == TOOL_MANAGER_LOGGER
        and _all_are(facts.links, CALLER_VALIDATION)
        and _has_adjacent(facts.links[0].frames, ARG_VALIDATION_FRAMES)
    )


#: The taxonomy, in the order it is asked. Order is not load-bearing — the five
#: rules are disjoint by construction (each names either a different exception
#: kind or a different logger) — but a table is what makes them ENUMERABLE, so
#: a sixth class is one row and cannot be a sixth branch somewhere else.
RULES: "tuple[tuple[str, Callable[[EventFacts], bool]], ...]" = (
    (ERROR_CONVENTION, _error_convention),
    (CLIENT_DISCONNECT, _client_disconnect),
    (PROACTOR_TEARDOWN, _proactor_teardown),
    (NODRIVER_DEAD_BROWSER, _nodriver_dead_browser),
    (CALLER_INPUT, _caller_input),
)


# ---------------------------------------------------------------------------
# Reading an event
# ---------------------------------------------------------------------------
def _is_ours(link: Link, error_base: "type[BaseException] | None") -> bool:
    """Our error convention — the ONE kind that must also cover subclasses.

    ``isinstance`` when the live object is there, because the convention is about
    a class and ``InstanceNotFoundError`` (or a future subclass declared
    anywhere) needs no edit here. Name-and-module otherwise, which is also what
    a missing ``tool_errors`` import degrades to.
    """
    if link.live is not None and error_base is not None:
        return isinstance(link.live, error_base)
    return OURS.matches(link)


def _is_budget(link: Link) -> bool:
    """A ``TimeoutError`` or an ``asyncio.CancelledError`` — never alone."""
    return any(kind.matches(link) for kind in BUDGET_KINDS)


def _all_are(links: "tuple[Link, ...]", kind: Kind) -> bool:
    """Every link is that kind, and there is at least one link to say it about."""
    return bool(links) and all(kind.matches(link) for link in links)


def _has_adjacent(frames: "tuple[Frame, ...]", pair: "tuple[Frame, Frame]") -> bool:
    """Do these two frames appear next to each other, in this order?

    Adjacency and not mere presence: ``fastmcp.tools.tool`` ``run`` is in the
    traceback of EVERY tool failure, our own bugs included. It is only directly
    above ``pydantic.type_adapter`` ``validate_python`` when FastMCP itself was
    the thing validating.
    """
    first, second = pair
    return any(
        frames[index] == first and frames[index + 1] == second
        for index in range(len(frames) - 1)
    )


def _links(event: "Event", chain: "list[BaseException] | None") -> "tuple[Link, ...]":
    """The chain to judge, OUTERMOST FIRST on both paths.

    The live chain already arrives that way. Sentry's ``values`` are the other
    way round — root cause first — so they are reversed here, once, rather than
    in each rule that reads a position.
    """
    if chain:
        return tuple(_live_link(exc) for exc in chain)
    return _payload_links(event)


def _live_link(exc: BaseException) -> Link:
    """One live exception, spelled the way the SDK would have serialized it."""
    cls = type(exc)
    return Link(
        type_name=_type_name_of(cls),
        module=_module_of(cls),
        frames=_live_frames(exc),
        live=exc,
    )


def _live_frames(exc: BaseException) -> "tuple[Frame, ...]":
    """``exc``'s traceback, caller first — the order the SDK serializes."""
    frames: list[Frame] = []
    traceback = exc.__traceback__
    while traceback is not None and len(frames) < _MAX_FRAMES:
        frame = traceback.tb_frame
        frames.append(Frame(frame.f_globals.get("__name__"), frame.f_code.co_name))
        traceback = traceback.tb_next
    return tuple(frames)


def _payload_links(event: "Event") -> "tuple[Link, ...]":
    """``event["exception"]["values"]``, reversed into outermost-first order."""
    if not isinstance(event, dict):
        return ()
    exception = event.get("exception")
    values = exception.get("values") if isinstance(exception, dict) else None
    if not isinstance(values, list):
        return ()
    return tuple(_payload_link(value) for value in reversed(values))


def _payload_link(value: object) -> Link:
    """One serialized exception value, or :data:`_UNREADABLE` if it is not one."""
    if not isinstance(value, dict):
        return _UNREADABLE
    name = value.get("type")
    if not isinstance(name, str):
        return _UNREADABLE
    module = value.get("module")
    return Link(
        type_name=name,
        module=module if isinstance(module, str) else None,
        frames=_payload_frames(value.get("stacktrace")),
    )


def _payload_frames(stacktrace: object) -> "tuple[Frame, ...]":
    """One serialized value's frames, in the order the SDK wrote them."""
    if not isinstance(stacktrace, dict):
        return ()
    frames = stacktrace.get("frames")
    if not isinstance(frames, list):
        return ()
    return tuple(
        Frame(_text(frame.get("module")), _text(frame.get("function")))
        if isinstance(frame, dict)
        else Frame(None, None)
        for frame in frames[:_MAX_FRAMES]
    )


def _text(value: object) -> "str | None":
    """A payload field that is supposed to be a string, or nothing."""
    return value if isinstance(value, str) else None


def _type_name_of(cls: "type[BaseException]") -> str:
    """A class's name, spelled the way the SDK spells it in a payload.

    ``sentry_sdk.utils.get_type_name`` is ``__qualname__ or __name__``, so a
    class declared inside a function serializes as ``outer.<locals>.Name``.
    Reading ``__name__`` here let a nested class match a :class:`Kind` the
    serialized path could never match.
    """
    return getattr(cls, "__qualname__", None) or cls.__name__


def _module_of(cls: "type[BaseException]") -> "str | None":
    """A class's module, spelled the way the SDK spells it in a payload."""
    module = getattr(cls, "__module__", None)
    return None if module in _UNWRITTEN_MODULES else module


def _logger(event: "Event") -> "str | None":
    """``event["logger"]`` — what ``LoggingIntegration`` puts the record name in."""
    if not isinstance(event, dict):
        return None
    return _text(event.get("logger"))


def _message(event: "Event") -> str:
    """The record's text, whichever of the three keys carries it.

    ``LoggingIntegration`` writes ``logentry`` with both ``formatted`` and
    ``message`` (measured, sdk 2.64.0); ``capture_message`` writes top-level
    ``message`` instead. Empty string when there is none, so every rule can
    ``startswith`` without a guard of its own.
    """
    if not isinstance(event, dict):
        return ""
    entry = event.get("logentry")
    if isinstance(entry, dict):
        for key in ("formatted", "message"):
            text = _text(entry.get(key))
            if text is not None:
                return text
    return _text(event.get("message")) or ""
