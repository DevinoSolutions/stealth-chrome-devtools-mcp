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
    At least one link in the chain is our ``ToolError`` (subclasses included),
    and every OTHER link is a *budget* link — ``TimeoutError`` or
    ``asyncio.CancelledError``. Nothing else in the chain is tolerated. Those
    two are what a bounded operation is made of: ``tool_runtime`` and
    ``browser_manager`` both bound work with ``asyncio.wait_for``, which raises
    ``TimeoutError`` *from* the ``CancelledError`` it used to stop the coroutine,
    and both convert it to a ``ToolError`` the caller receives. A budget link
    never stands alone: a ``TimeoutError`` nobody converted is a place the error
    convention is MISSING, which is a finding and not noise.

``client-disconnect``
    Either the chain is entirely ``starlette.requests.ClientDisconnect``, or the
    event is the message-only form the session loop logs:
    ``mcp/server/lowlevel/server.py``:707 (mcp 1.27.1) is
    ``logger.error(f"Received exception from stream: {message}")`` and
    ``str(ClientDisconnect())`` is ``""`` (measured), so the tail is empty. The
    match on that message is EQUALITY, never a prefix: the same line with a real
    tail — "Received response with an unknown request ID: … Method not found" —
    is a protocol fault and must keep shipping.

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
    ``ValidationError`` (module under ``pydantic``). FastMCP raises it from
    ``tool.py``'s ``type_adapter.validate_python`` and answers the caller with
    its message, so the caller already knows. Our own code validating its own
    model is not a caller's typo, which is why the logger is part of the rule.

One rule, two paths
-------------------
``before_send`` may or may not be handed the live exception: the logging
integration fills ``hint["exc_info"]`` today, but a replayed or hand-built event
carries only the serialized payload. Both paths must judge the same set, so
every link is reduced to a :class:`Link` first — a type NAME plus a MODULE,
spelled the way the SDK spells them — and every class is a :class:`Kind` test
over those two fields.

That means builtins modules are normalized: ``sentry_sdk.utils.get_type_module``
omits ``builtins``, so a serialized ``TimeoutError`` carries ``module: None``
while the live class says ``"builtins"`` (both measured, sdk 2.64.0).
:data:`_UNWRITTEN_MODULES` is that normalization and it is the only reason one
rule can read both shapes.

There is exactly ONE exception to name-and-module matching, and it is the one
that was already there: our own error base is matched with ``isinstance`` when
the live exception is available, because CLAUDE.md convention 2 is about a CLASS
and a subclass declared anywhere must be covered. Every other kind matches by
name and module on both paths deliberately — ``isinstance`` on the live path
would accept subclasses that the serialized path, which sees only the subclass's
own name, could never accept, and the two paths would quietly disagree.

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
# Reported, never branched on — there is one drop.
# ---------------------------------------------------------------------------
ERROR_CONVENTION = "error-convention"
CLIENT_DISCONNECT = "client-disconnect"
PROACTOR_TEARDOWN = "proactor-teardown"
NODRIVER_DEAD_BROWSER = "nodriver-dead-browser"
CALLER_INPUT = "caller-input"

#: What the SDK writes for a class that lives in ``builtins``: nothing at all.
#: Mirrors ``sentry_sdk.utils.get_type_module``, which is what makes a live
#: ``TimeoutError`` (``__module__ == "builtins"``) and a serialized one
#: (``module: None``) the same :class:`Link`.
_UNWRITTEN_MODULES = frozenset({None, "builtins", "__builtins__", "__main__"})


@dataclass(frozen=True)
class Link:
    """One exception in an event's chain, as BOTH of the paths can describe it.

    ``live`` is the exception object when ``before_send`` was handed one, and it
    is read by exactly one rule — our own error base, the single ``isinstance``
    match. Everything else reads ``type_name`` and ``module``, which the
    serialized payload has too.
    """

    type_name: str
    module: "str | None"
    live: "BaseException | None" = None


#: A serialized exception value this module could not read. It matches no
#: :class:`Kind`, because no kind's name set contains the empty string — which
#: is how an unreadable value keeps a whole chain from being recognised.
_UNREADABLE = Link("", None)


@dataclass(frozen=True)
class Kind:
    """One exception class, spelled the way an event carries it.

    ``module`` is a PREFIX when it is set, because the SDK reports the defining
    submodule: pydantic's ``ValidationError`` arrives as
    ``pydantic_core._pydantic_core`` and our own errors as
    ``stealth_chrome_devtools_mcp.embedded.tool_errors``. ``None`` means
    "builtins", where the SDK writes no module at all, and it is matched exactly
    so that a third-party class sharing a builtin's NAME cannot pass.
    """

    names: "frozenset[str]"
    module: "str | None"

    def matches(self, link: Link) -> bool:
        if link.type_name not in self.names:
            return False
        if self.module is None:
            return link.module is None
        return link.module is not None and link.module.startswith(self.module)


#: Our error convention. The one kind that also matches SUBCLASSES — see
#: :func:`_is_ours`. The names are an explicit allowlist rather than "anything
#: from our package" because this decides what is never seen again, and the
#: module is checked because ``fastmcp.exceptions.ToolError`` is a DIFFERENT
#: class with the same name and is what wraps a genuine crash on its way out of
#: ``tool_manager``.
OURS = Kind(
    frozenset({"ToolError", "InstanceNotFoundError"}), "stealth_chrome_devtools_mcp"
)

#: What a bounded operation is made of: ``asyncio.wait_for`` cancels the
#: coroutine it gave up on and raises ``TimeoutError`` from that
#: ``CancelledError``. Tolerated only BESIDE :data:`OURS`, never alone.
BUDGET_KINDS = (
    Kind(frozenset({"TimeoutError"}), None),
    Kind(frozenset({"CancelledError"}), "asyncio.exceptions"),
)

CLIENT_GONE = Kind(frozenset({"ClientDisconnect"}), "starlette.requests")
CONNECTION_RESET = Kind(frozenset({"ConnectionResetError"}), None)
CONNECTION_REFUSED = Kind(frozenset({"ConnectionRefusedError"}), None)
CALLER_VALIDATION = Kind(frozenset({"ValidationError"}), "pydantic")

#: The loggers the four logger-gated classes name, verbatim.
ASYNCIO_LOGGER = "asyncio"
MCP_SESSION_LOGGER = "mcp.server.lowlevel.server"
TOOL_MANAGER_LOGGER = "FastMCP.fastmcp.tools.tool_manager"

#: ``mcp/server/lowlevel/server.py``:707's whole message when the thing it
#: formatted was a ``ClientDisconnect``, whose ``str()`` is empty. Compared with
#: ``==`` on purpose: a prefix match would also swallow the protocol faults that
#: line reports, which are real.
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
class Facts:
    """Everything a rule may ask about an event, read exactly once.

    Reading the event up front is what keeps the rules total functions of it:
    a rule cannot reach past these four fields, so adding one is a visible
    change to this class rather than a new way to interrogate an event.
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
    (``observability._exception_chain``), and ``None`` or empty when only the
    serialized payload is available — in which case the links are read out of
    ``event["exception"]["values"]`` instead. ``error_base`` is our own
    ``ToolError``, or ``None`` when it could not be imported, which degrades the
    one ``isinstance`` match to name-and-module like every other kind.

    Does not catch: the never-raises contract lives at the one call site, so a
    bug here is visible to the suite rather than silently turning into "ship it".
    """
    facts = Facts(
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
def _error_convention(facts: Facts) -> bool:
    """Ours, over nothing but the budget links it was raised on."""
    if not facts.links:
        return False
    seen_ours = False
    for link in facts.links:
        if _is_ours(link, facts.error_base):
            seen_ours = True
        elif not any(kind.matches(link) for kind in BUDGET_KINDS):
            return False
    return seen_ours


def _client_disconnect(facts: Facts) -> bool:
    """The client went away mid-request — from the POST, or from the session loop."""
    if _all_are(facts.links, CLIENT_GONE):
        return True
    return (
        not facts.links
        and facts.logger == MCP_SESSION_LOGGER
        and facts.message == STREAM_EXCEPTION_MESSAGE
    )


def _proactor_teardown(facts: Facts) -> bool:
    """CPython's own ``shutdown()`` on a socket the peer already reset."""
    return (
        facts.logger == ASYNCIO_LOGGER
        and facts.message.startswith(PROACTOR_TEARDOWN_PREFIX)
        and _all_are(facts.links, CONNECTION_RESET)
    )


def _nodriver_dead_browser(facts: Facts) -> bool:
    """nodriver's unawaited target refresh, after its Chrome had gone."""
    return (
        facts.logger == ASYNCIO_LOGGER
        and facts.message.startswith(UNRETRIEVED_TASK_PREFIX)
        and NODRIVER_MARK in facts.message
        and _all_are(facts.links, CONNECTION_REFUSED)
    )


def _caller_input(facts: Facts) -> bool:
    """A caller sent a parameter the tool does not have; FastMCP already said so."""
    return facts.logger == TOOL_MANAGER_LOGGER and _all_are(
        facts.links, CALLER_VALIDATION
    )


#: The taxonomy, in the order it is asked. Order is not load-bearing — the five
#: rules are disjoint by construction (each names either a different exception
#: kind or a different logger) — but a table is what makes them ENUMERABLE, so
#: a sixth class is one row and cannot be a sixth branch somewhere else.
RULES: "tuple[tuple[str, Callable[[Facts], bool]], ...]" = (
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


def _all_are(links: "tuple[Link, ...]", kind: Kind) -> bool:
    """Every link is that kind, and there is at least one link to say it about."""
    return bool(links) and all(kind.matches(link) for link in links)


def _links(event: "Event", chain: "list[BaseException] | None") -> "tuple[Link, ...]":
    """The chain to judge: the live objects when we have them, the payload else."""
    if chain:
        return tuple(
            Link(type(exc).__name__, _module_of(type(exc)), exc) for exc in chain
        )
    return _payload_links(event)


def _payload_links(event: "Event") -> "tuple[Link, ...]":
    """``event["exception"]["values"]``, in the order Sentry reports them."""
    if not isinstance(event, dict):
        return ()
    exception = event.get("exception")
    values = exception.get("values") if isinstance(exception, dict) else None
    if not isinstance(values, list):
        return ()
    return tuple(_payload_link(value) for value in values)


def _payload_link(value: object) -> Link:
    """One serialized exception value, or :data:`_UNREADABLE` if it is not one."""
    if not isinstance(value, dict):
        return _UNREADABLE
    name = value.get("type")
    module = value.get("module")
    if not isinstance(name, str):
        return _UNREADABLE
    return Link(name, module if isinstance(module, str) else None)


def _module_of(cls: "type[BaseException]") -> "str | None":
    """A class's module, spelled the way the SDK spells it in a payload."""
    module = cls.__module__
    return None if module in _UNWRITTEN_MODULES else module


def _logger(event: "Event") -> "str | None":
    """``event["logger"]`` — what ``LoggingIntegration`` puts the record name in."""
    if not isinstance(event, dict):
        return None
    name = event.get("logger")
    return name if isinstance(name, str) else None


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
            text = entry.get(key)
            if isinstance(text, str):
                return text
    text = event.get("message")
    return text if isinstance(text, str) else ""
