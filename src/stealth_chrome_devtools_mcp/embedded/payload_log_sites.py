"""THE one home for "which third-party SITES render a payload, and what a
record from one is allowed to say" (F-911, F-913).

It is the SITE half of a set, and the set is the point. ``logging_setup``
holds third-party log lines down four ways, and each answers a different
question about the same record:

* by **LEVEL** — ``PAYLOAD_LOG_FAMILIES`` / ``apply_payload_log_floor``
  (F-906, F-908). Works when the library names its own logger, because an
  explicit level on the family ROOT is upstream of every sink.
* by **ARGUMENT** — ``PAYLOAD_ARG_PACKAGE`` / ``install_payload_arg_redaction``
  (F-907). Works when the payload rides in ``record.args`` as an object whose
  ``__repr__`` renders it.
* by **SITE, in the MESSAGE** — :data:`PAYLOAD_LOG_SITES` below. Works when
  neither of the others can, which is exactly the case
  ``mcp/shared/session.py`` presents.
* by **SITE, in the EXCEPTION** — :data:`PAYLOAD_EXCEPTION_SITES` below
  (F-913). Works when the message is SAFE and the payload is in ``exc_info``,
  which is what ``mcp/client/streamable_http.py`` presents and what none of
  the other three can reach.

The last two share this module and share their matching, and they are two
TABLES rather than one because they replace different halves of a record: the
message rule withholds ``record.msg`` and clears ``record.args``; the
exception rule replaces what ``record.exc_info`` renders as and leaves the
message alone. A site in both would get both, which is coherent — the rules
are applied independently in the one factory for that reason.

``mcp/shared/session.py`` logs with the module-level ``logging.warning`` /
``logging.debug`` / ``logging.exception`` functions rather than through a
logger of its own, so ``record.name`` is ``root``. That is not a difference of
degree from F-906/F-908, it is a different door:

* there is no family to name, so ``PAYLOAD_LOG_FAMILIES`` is inert here however
  that tuple grows — and capping ROOT is not the missing entry, because root's
  level is the CALLER's and lowering it silences the whole process;
* the payload is interpolated by an **f-string** before ``logging`` is called,
  so ``record.args`` is empty and F-907's argument rule sees nothing. That is
  the same shape which made a filter impossible at ``connection.py``:451 in
  F-906, arriving at the one mechanism F-906 chose instead.

MEASURED against the installed mcp 1.27.1, by driving the real
``BaseSession._receive_loop`` over memory streams — no socket, no Chrome:

* ``:383`` ``f"Failed to validate request: {e}"`` at **WARNING** — pydantic's
  middle-truncated ``input_value=`` echo of the caller's own arguments;
* ``:384`` ``f"Message that failed validation: {message.message.root}"`` at
  DEBUG — the whole request, arguments and all;
* ``:430`` ``f"Failed to validate notification: {e}. Message was:
  {message.message.root}"`` at **WARNING** — the whole notification, and NOT
  truncated, because the f-string renders the model rather than pydantic's
  error formatter.

The two at WARNING need no ``basicConfig`` from anybody, and they arrange their
own sink: the module-level ``logging.warning`` calls ``logging.basicConfig()``
when root has no handlers (measured — root goes from ``[]`` to one
``StreamHandler``), so the SDK's first such line installs a stderr handler on
OUR root, and for the backend stderr IS ``backend-boot.log``, a durable file.
At WARNING they are Sentry breadcrumbs on the next event too,
``LoggingIntegration``'s breadcrumb handler sitting at INFO.

**A leaf, and it decides nothing about when it is asked.** The record arrives
as an argument, there is no install and no state; ``logging_setup``'s ONE
record factory calls :func:`site_of` and :func:`withheld` and owns the
rewrite, exactly as ``backend_liveness`` takes its probes as arguments and
``backend_eviction`` takes its verdicts. Its own imports are stdlib only, which
is what lets the stdio proxy pay nothing for it.

It is a separate module rather than more screens of ``logging_setup`` because
these are the rules that are a **registry**: F-906's is a tuple of names and
F-907's is a single package string, while these are tables plus the matching
logic that reads them, and any future third-party module that renders a
payload joins a table here. The timing was the 1000-LOC budget, which F-911's
addition crossed and which **ratchets down only** — ``cli_call`` →
``cli_render``'s precedent, and the cut is this question's surface rather than
a raised cap. F-913 landed its rule here for the same reason: it is a third
table-and-matching pair, and ``logging_setup`` is at its budget. If
``logging_setup`` grows again, the honest next cut is the other two rules
joining this module, which would make it THE one home for "what a third
party's log line may carry" entire.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import logging

#: The third-party MODULES whose records land somewhere the LEVEL and ARGUMENT
#: rules cannot reach — today, on the root logger.
#:
#: **The unit is the MODULE, and the key is the record's own ``pathname``.**
#: Not the message text, which the library may reword at will (F-906's rule
#: about pattern-matching a dependency's strings). Not the line number, which
#: is the least stable thing in a dependency — one edit above moves every
#: number below it, and a rule keyed on 383/384/430 would go silently inert on
#: the next release. Not ``record.name``, which is ``root`` and says nothing.
#:
#: A file path is what F-906's family root is, one level finer: the library's
#: own unit of organisation, and a fact it cannot change without the module
#: ceasing to exist — at which point the entry is inert and VISIBLE, because
#: ``tests/test_root_logger_payload.py`` reads the installed SDK's source by
#: AST and goes RED.
#:
#: Written with forward slashes and matched against a ``pathname`` normalised
#: the same way, so one spelling serves both platforms.
#:
#: Deliberately NOT the whole ``mcp`` package, and each exclusion is measured
#: (an AST census of all 206 logging calls in mcp 1.27.1, 11 of them on root):
#:
#: * ``mcp/client/session_group.py``:383/:393/:404 are root-logger WARNINGs
#:   rendering an exception from ``list_tools``/``list_prompts``/
#:   ``list_resources``. Nothing in this tree constructs a
#:   ``ClientSessionGroup``, so naming it would be a claim about a door this
#:   product does not have — F-906's "no ``uc`` entry" reasoning exactly;
#: * ``mcp/server/sse.py``:193 is a root-logger DEBUG carrying a session ID,
#:   which is not payload;
#: * every other module in ``mcp`` logs through a NAMED logger, so it is
#:   ``PAYLOAD_LOG_FAMILIES``' question and not this one.
PAYLOAD_LOG_SITES = ("mcp/shared/session.py",)

#: The cheap first gate, DERIVED from the table rather than typed beside it.
#: Every record in the process is asked this question, so it must be one set
#: lookup — and two spellings of one fact is how a rule comes to cover nothing.
#: It is the site's FILENAME, read off the normalised ``pathname`` in
#: :func:`site_of`, and deliberately NOT ``record.module``: the stdlib computes
#: that with the HOST's ``os.path.basename``, which on POSIX does not split on
#: a backslash, so a Windows-shaped pathname there yields the whole string as
#: its "module" and a gate on it refused the record before the normalisation
#: ever ran — every Linux and macOS cell of gate run 35624857318 went red on
#: exactly that while the Windows pre-push lane was green.
_SITE_FILENAMES = frozenset(site.rpartition("/")[2] for site in PAYLOAD_LOG_SITES)

#: What a withheld record says instead of its text. It keeps the MODULE and the
#: LINE, which is the whole of what makes such a record actionable — an
#: operator reading ``<mcp/shared/session.py:430 …>`` knows exactly which
#: condition fired and can read the SDK's own source for the wording — plus the
#: character count, which says how much was dropped without saying any of it
#: (``logging_setup._shape``'s ``children=`` reasoning).
#:
#: Nothing else survives, and the asymmetry with F-907 is deliberate: an
#: ``Element`` has a safe half (its tag and attribute NAMES) and an unsafe half
#: (the values and the text), while a **pre-rendered string** has no half we
#: have measured to be safe.
WITHHELD_TEMPLATE = "<{site}:{line} message withheld: {measured}{extra}>"


def site_of(record: logging.LogRecord) -> str | None:
    """Which entry in :data:`PAYLOAD_LOG_SITES` MADE this record, if any.

    Two reads and no third. The filename first, because this runs for every
    record in the process — one ``replace``, one ``rpartition`` and one
    frozenset lookup, False for everything. Only then the path, which is the
    actual answer: several packages in a normal tree ship a ``session.py``, and
    withholding a stranger's records because their file happens to share a
    name would be silencing rather than redacting. Both reads are off the
    SAME normalised string, never off ``record.module`` — see
    :data:`_SITE_FILENAMES` for the host-dependence that gate carried.

    **The path match is on a SEPARATOR BOUNDARY, and the boundary is the whole
    point of the second read** (F-911 review S1). A bare ``endswith(site)``
    answers True for ``/opt/fakemcp/shared/session.py`` — measured — so every
    distribution whose name merely ENDS IN ``mcp`` and ships
    ``shared/session.py`` would have its records silently withheld, which is
    this function's own stated failure mode arriving by the other door. The
    repo has already ruled on exactly this shape: ``expected_events.Kind.modules``
    matches "exactly or on a DOTTED boundary … the boundary is what keeps
    ``pydanticfoo`` out". Same rule, one separator instead of a dot. The
    ``path == site`` arm is for a ``pathname`` that IS the relative module path
    with nothing in front of it, which has no separator to anchor on.

    ``record.pathname`` is normalised first because it arrives in the host's
    own flavour — measured on this machine, ``logging`` records the native
    Windows path with backslashes — while :data:`PAYLOAD_LOG_SITES` is written
    one way. Both shapes are pinned.

    There is deliberately **no** ``except`` here, and that is a claim rather
    than an oversight. ``logging_setup._shape`` needs a total one because it
    calls into library code (``tag`` is a property, ``attrs`` answers a
    ``ContraDict``); this function reads ONE attribute, and the one value a
    caller controls, ``pathname``, is tested for ``str`` rather than coerced.
    A non-string pathname is nobody's module path, so ``None`` is the right
    answer and not a swallowed failure; on a ``str`` the three string
    operations below cannot raise.
    """
    return _site_in(record, PAYLOAD_LOG_SITES, _SITE_FILENAMES)


def _site_in(
    record: logging.LogRecord,
    sites: tuple[str, ...],
    filenames: frozenset[str],
) -> str | None:
    """Which entry in ``sites`` made this record — THE one matching rule.

    Parameterised over the table rather than written once per table, because
    what must not be duplicated is the separator-boundary decision documented
    in :func:`site_of`: two copies of it are two things that can drift, and the
    one that drifts is the one nobody re-measures. Both callers hand it a
    ``filenames`` set DERIVED from their own ``sites``, so the cheap gate and
    the answer can never disagree about which table is being asked.
    """
    pathname = record.pathname
    if not isinstance(pathname, str):
        return None
    path = pathname.replace("\\", "/")
    if path.rpartition("/")[2] not in filenames:
        return None
    for site in sites:
        if path == site or path.endswith("/" + site):
            return site
    return None


def withheld(site: str, record: logging.LogRecord) -> str:
    """The shape that replaces a payload-rendering site's rendered text.

    The LENGTH is measured off ``record.msg`` alone and the ``%`` is never run:
    interpolating here would mean executing a third party's format pair inside
    ``Logger.makeRecord``, which for a mismatched pair raises and would break
    every log call in the process — the failure ``logging_setup._shape``'s
    handler exists for, avoided by not doing the thing instead of by catching
    it. For the three measured lines ``record.msg`` IS the whole rendered text
    (they are f-strings), so the count is exact; where a site logs with
    ``%``-args the count is the template's and the arguments are reported as a
    COUNT, which is why ``args=`` appears at all.

    ``str(record.msg)`` is guarded because ``msg`` is the one thing here a
    third party supplies as an object, and ``logging`` documents that it may be
    any object with a ``__str__``. Its TYPE is named and never ``str(exc)``,
    for ``_shape``'s reason: on a payload-derived object that text is the
    subject.

    The caller clears ``record.args`` alongside this, and must: ``getMessage()``
    runs ``msg % args``, so a ``%s``-carrying tuple left beside a message that
    no longer has a ``%s`` raises ``TypeError`` in every handler that formats,
    turning a redaction into an outage.
    """
    try:
        measured = f"{len(str(record.msg))} chars"
    except Exception as exc:  # noqa: BLE001  PERMANENT(F-911 — inside makeRecord)
        measured = f"unmeasurable={type(exc).__name__}"
    extra = ""
    if isinstance(record.args, tuple) and record.args:
        extra = f" args={len(record.args)}"
    return WITHHELD_TEMPLATE.format(
        site=site, line=record.lineno, measured=measured, extra=extra
    )


# ---------------------------------------------------------------------------
# F-913 — the SECOND table: what a record's EXCEPTION may say about its INPUT
# ---------------------------------------------------------------------------
#: The third-party MODULES whose records carry an exception that quotes the
#: PAYLOAD it failed to parse. A second table beside :data:`PAYLOAD_LOG_SITES`
#: and deliberately not an entry in it, because the two answer different halves
#: of one record and no rule covers both:
#:
#: * F-911's table withholds ``record.msg``. At these sites ``record.msg`` is
#:   the static literal ``"Error parsing SSE message"`` — the one part that is
#:   SAFE — and the payload is in ``exc_info``, which that rule leaves alone.
#:   Adding this file there would delete the safe half and close nothing, which
#:   is worse than an absent entry because the next reader believes it is
#:   handled (F-913 §2, and pinned in ``test_validation_input_echo``);
#: * this table replaces what the record's ``exc_info`` RENDERS AS, and leaves
#:   ``record.msg`` exactly where it was.
#:
#: They are disjoint today and nothing requires them to be: a site in both
#: would have its message withheld AND its exception restated, which is
#: coherent. The rules are applied independently in ``logging_setup``'s one
#: factory for that reason.
#:
#: MEASURED against mcp 1.27.1. Three sites in this file, all
#: ``logger.exception(<static literal>)`` at **ERROR**, on the legs that read
#: the backend's answer back:
#:
#: * ``:240`` ``"Error parsing SSE message"`` — the SSE leg, where the data IS
#:   the serialised answer to a ``tools/call``;
#: * ``:394`` ``"Error parsing JSON response"`` — the non-SSE leg;
#: * ``:574`` ``"Error in post_writer"`` — the catch-all around the writer.
#:
#: The unit is the MODULE for :data:`PAYLOAD_LOG_SITES`' reasons exactly — one
#: entry covers all three lines and any the SDK adds, and a line-keyed rule
#: would go silently inert on the next release.
#:
#: **Deliberately NOT keyed on the exception ALONE**, and this is the one
#: decision a reader is most likely to want to undo. A type-only rule would
#: also fire for ``fastmcp/tools/tool_manager.py``'s
#: ``logger.exception(f"Error calling tool {key!r}")``, whose ``exc_info`` is a
#: pydantic ``ValidationError`` too — and ``expected_events.CALLER_VALIDATION``
#: recognises that event by the exception's type NAME and MODULE. Substituting
#: it would stop ``caller-input`` classifying and re-open the class that was
#: 466 events a week when F-887 measured it. The site gate is what keeps this
#: rule away from it; ``tests/test_validation_input_echo.py`` pins both halves.
PAYLOAD_EXCEPTION_SITES = ("mcp/client/streamable_http.py",)

#: :data:`PAYLOAD_EXCEPTION_SITES`' cheap first gate, derived from it for
#: :data:`_SITE_FILENAMES`' reason and read off the same normalised path.
_EXCEPTION_SITE_FILENAMES = frozenset(
    site.rpartition("/")[2] for site in PAYLOAD_EXCEPTION_SITES
)

#: An exception whose ``str()`` quotes the input it was given, spelled as a
#: NAME plus a module ROOT rather than resolved with ``isinstance``. Same
#: reasoning as :data:`logging_setup.PAYLOAD_ARG_PACKAGE`: resolving the class
#: needs ``import pydantic``, and this module is loaded in the stdio proxy,
#: whose whole cost argument is that it imports nothing.
#:
#: MEASURED, pydantic 2.11.7: ``str(ValidationError)`` renders
#: ``input_value=`` for every failing arm, capped at **50 characters of the
#: input** (24 + ``...`` + 23) — so a whole JSON-RPC frame leaks its last 23
#: characters, and **any value shorter than the cap leaks WHOLE**, once per
#: union arm that tripped over it (9 errors for one frame, measured).
#:
#: ``expected_events.CALLER_VALIDATION`` names the same class for a different
#: question. The two spellings are pinned against each other rather than shared,
#: because importing that module here would cost this leaf its stdlib-only
#: imports — two spellings of one fact is how a rule comes to cover nothing, so
#: the pin is the thing that keeps them honest.
INPUT_QUOTING_NAMES = frozenset({"ValidationError"})
INPUT_QUOTING_MODULE_ROOTS = frozenset({"pydantic", "pydantic_core"})

#: How many of an error list's entries the restatement names. A union reports
#: one error per arm per field — ``ClientRequest`` reported 31 in F-911's
#: measurement and ``JSONRPCMessage`` 9 in this one — and an unbounded join
#: would put a page-sized string where a diagnostic belongs.
#:
#: **The cap is what bounds that list, and** :data:`RESTATED_OVERFLOW` **is how
#: a reader learns it bound something.** Deduplication is a SEPARATE mechanism
#: that collapses a genuinely repeated ``type``/``loc`` pair; it is not what
#: makes the cap enough, and on the measured frame it collapses nothing at all
#: — those 9 errors are 9 DISTINCT pairs, so the restatement really does
#: overflow and end ``…+1`` (measured; pinned by
#: ``test_the_restatement_overflows_its_cap_and_says_so``). An earlier version
#: of this comment claimed 3 distinct pairs and offered that as the
#: justification; the number was wrong and so was the argument.
MAX_RESTATED_ERRORS = 8

#: One ``loc`` path, bounded. A ``loc`` is normally schema-derived (a field
#: name the model declares), but for an ``extra_forbidden`` error its last
#: segment is a KEY from the input — a NAME, which is F-907's measured safe
#: half, and never a VALUE. Bounded anyway because a name the input supplied is
#: a length the input chose. Its own number rather than a shared one, on
#: ``tool_errors.JS_ERROR_CHARS``' three-homes precedent.
MAX_LOC_CHARS = 48

#: What an unnamed ``loc`` is called. The empty tuple means "the whole input",
#: which is what a ``json_invalid`` reports.
ROOT_LOC = "<root>"

#: The overflow mark, this module's own — see :data:`MAX_LOC_CHARS`.
RESTATED_OVERFLOW = "…"

#: What a payload-quoting exception is allowed to say about itself. It keeps
#: the exception's TYPE and defining MODULE, the MODEL it was validating, the
#: error COUNT, the length of the rendering it replaced, and each error's
#: ``type`` slug and ``loc`` path — which is the whole of what an operator acts
#: on, and is what F-911 kept the module and line for. It loses every
#: ``input``, every ``ctx`` and every ``msg``.
RESTATED_TEMPLATE = (
    "<{kind} for {title}: {count} error(s), text withheld: {measured}{detail}>"
)

#: ``sys.exc_info()``'s shape — ``(type, value, traceback)``.
_EXC_INFO_LENGTH = 3

#: Cycle terminator for the exception-chain walk, not a size limit. Mirrors
#: ``observability._MAX_EVENT_DEPTH``'s role; it is spelled again rather than
#: imported because that module is not a leaf and this one must stay one.
_MAX_CHAIN = 24


class WithheldInputError(Exception):
    """What a record carries instead of an exception that quotes its input.

    Carries SHAPE only, on ``cdp_transport.CdpReplyError``'s precedent (F-902),
    which reports the CDP method, the exception type and the reply's field
    COUNT rather than nodriver's interpolation of the whole reply. Same answer,
    one layer out: this one reports the validation error's type, its model, its
    error count and each error's ``type``/``loc`` — and never the input.

    It is never RAISED. It is only ever placed in ``record.exc_info``, so the
    original object is untouched and the SDK's own control flow is unchanged —
    ``streamable_http.py``:241 still sends the exception it caught downstream.
    That is also why it carries no ``__cause__`` and no ``__context__``:
    constructing an exception while another is being handled does not set
    either (only raising does), and a chain back to the original would make
    every formatting sink render the text this just withheld.
    """


def exception_site_of(record: logging.LogRecord) -> str | None:
    """Which entry in :data:`PAYLOAD_EXCEPTION_SITES` MADE this record, if any.

    :func:`site_of`'s twin over the other table, and it shares the matching so
    the separator-boundary rule has one home — see :func:`_site_in`.
    """
    return _site_in(record, PAYLOAD_EXCEPTION_SITES, _EXCEPTION_SITE_FILENAMES)


def restated_exc_info(
    record: logging.LogRecord,
) -> tuple[type[BaseException], BaseException, object] | None:
    """The ``exc_info`` this record should carry instead, or ``None``.

    THE one entry point ``logging_setup``'s factory calls, so the factory keeps
    its three rules to three lines each and every decision lives here.

    ``None`` means "leave it alone", and it is the answer for everything except
    the narrow case both gates agree on: a record MADE BY a site in
    :data:`PAYLOAD_EXCEPTION_SITES` whose exception chain contains a link that
    quotes its own input. An ordinary ``ConnectionError`` out of ``:574`` keeps
    its text, because its text is the diagnostic and it quotes nobody — F-907's
    exception clause, which still stands for every exception but this one shape.

    **The TRACEBACK is handed through unchanged**, so every frame survives:
    measured, the serialized Sentry FRAME LIST is byte-identical before and
    after. That is the whole of what was measured, and it is stated that way
    deliberately — the exception's TYPE and VALUE both change here by
    construction, so any Sentry grouping strategy keyed on those rather than on
    the stack sees a different fingerprint, and §6.1 residual 5 is where that
    cost is named. What changes is only what the exception RENDERS AS.

    The chain is WALKED rather than only its head, and :func:`_chain` walks
    three edges: ``__cause__``, ``__context__``, and a group's own
    ``exceptions``. At the three measured sites the quoting error IS the
    outermost exception, but a wrapper that re-raised one would put it a link
    down and an anyio task group would put it a leaf across, and a rule reading
    only ``exc_info[1]`` would answer "nothing to do" while every formatting
    sink — and Sentry's own group-aware serialiser — rendered it. When any link
    quotes its input the WHOLE chain is replaced, and the non-quoting links
    contribute their TYPE only: a wrapper's own text commonly interpolates the
    exception it wrapped (``f"...: {e}"``), and a pre-rendered string has no
    half we have measured to be safe (F-911).
    """
    if exception_site_of(record) is None:
        return None
    info = record.exc_info
    if not isinstance(info, tuple) or len(info) != _EXC_INFO_LENGTH:
        return None
    exception = info[1]
    if not isinstance(exception, BaseException):
        return None
    chain = _chain(exception)
    if not any(_quotes_input(link) for link in chain):
        return None
    return (WithheldInputError, WithheldInputError(_restated(chain)), info[2])


def _chain(exc: BaseException) -> list[BaseException]:
    """``exc``, everything it was raised from, and everything it GROUPS.

    Outermost first, breadth-first, bounded and cycle-safe for
    :data:`_MAX_CHAIN`'s reason. For a plain ``__cause__``/``__context__``
    spine the membership AND the order are exactly what the previous walk
    produced — the queue holds one element at a time — so the group arm adds a
    case rather than changing one.

    **The group arm is why this no longer merely MIRRORS**
    ``observability._exception_chain``. That function follows ``__cause__``,
    else ``__context__`` unless ``raise ... from None`` suppressed it, and
    stops; it has the identical blind spot this arm closes, and the two walks
    agreeing used to be the argument for re-spelling one here rather than
    importing it (that module imports ``settings`` and this one may import
    nothing, which is still why it is re-spelled). They now DIFFER,
    deliberately and in one direction: this one is strictly wider. Widening
    the other is NOT an implied follow-up of this change — it decides which
    Sentry events ``expected_events.classify`` DROPS, a different question with
    a different blast radius — so it is named here and left alone.

    Why the arm exists when no site in :data:`PAYLOAD_EXCEPTION_SITES` reaches
    it today: an ``ExceptionGroup``'s own ``str()`` carries NONE of its leaves
    (measured), so a head-only walk answers "nothing quotes its input" and this
    rule does not fire — while ``sentry_sdk.utils.exceptions_from_error_tuple``
    branches on ``isinstance(exc_value, BaseExceptionGroup)`` and serialises
    every leaf as its own ``exception.values`` entry, carrying its whole
    ``input_value=`` echo (measured). A blind spot named and left open in a
    rule whose entire job is that no payload escapes is the shape F-908 was;
    ``element_box`` is the precedent for closing one over sites measured
    unreachable, and the SDK does run under anyio task groups.

    ``isinstance(BaseExceptionGroup)`` and not ``getattr(exc, "exceptions",
    ())``, which reads as the cheaper spelling and is not: a duck-typed read
    admits any object whose ``exceptions`` attribute is not a tuple of
    exceptions, and iterating one raises ``TypeError`` inside
    ``Logger.makeRecord`` — the failure :func:`_detail`'s total ``except``
    exists for, in the one function in this module that deliberately has none.
    It is also the one place an ``isinstance`` is right here:
    :func:`_quotes_input` avoids one because matching pydantic by CLASS would
    cost the stdio proxy an ``import pydantic``, while ``BaseExceptionGroup``
    is a builtin costing nothing and the thing asked about genuinely IS a class
    (``expected_events._is_ours``' reasoning).
    """
    chain: list[BaseException] = []
    seen: set[int] = set()
    pending: list[BaseException] = [exc]
    cursor = 0
    while cursor < len(pending) and len(chain) < _MAX_CHAIN:
        current = pending[cursor]
        cursor += 1
        if id(current) in seen:
            continue
        seen.add(id(current))
        chain.append(current)
        following = current.__cause__
        if following is None and not current.__suppress_context__:
            following = current.__context__
        if following is not None:
            pending.append(following)
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
    return chain


def _quotes_input(exc: BaseException) -> bool:
    """Does THIS exception's ``str()`` quote the input it was given?

    Matched on the type's NAME and module ROOT, never with ``isinstance`` and
    never on the message text. The text key is what F-906 §3 and F-911 §3 both
    reject and it fails OPEN — a library is free to reword its sentences, and a
    rule that stopped matching would leak silently rather than go red.

    The module test is a ROOT with a dotted BOUNDARY, exactly
    ``expected_events.Kind.matches``' rule, because pydantic's class arrives as
    ``pydantic_core._pydantic_core`` and the boundary is what keeps a package
    merely SPELLED like it (``pydanticfoo``) out.
    """
    kind = type(exc)
    if getattr(kind, "__qualname__", None) not in INPUT_QUOTING_NAMES:
        return False
    module = getattr(kind, "__module__", None)
    if not isinstance(module, str):
        return False
    return any(
        module == root or module.startswith(f"{root}.")
        for root in INPUT_QUOTING_MODULE_ROOTS
    )


def _restated(chain: list[BaseException]) -> str:
    """The whole chain, said in a way that quotes nothing it was given."""
    return " <- ".join(_restated_link(link) for link in chain)


def _restated_link(exc: BaseException) -> str:
    """One link. A quoting one is restated; any other keeps its TYPE only."""
    kind = f"{type(exc).__module__}.{type(exc).__qualname__}"
    if not _quotes_input(exc):
        return f"<{kind}>"
    try:
        measured = f"{len(str(exc))} chars"
    except Exception as exc_text:  # noqa: BLE001  PERMANENT(F-913 — inside makeRecord)
        measured = f"unmeasurable={type(exc_text).__name__}"
    return RESTATED_TEMPLATE.format(
        kind=kind,
        title=_title(exc),
        count=_count(exc),
        measured=measured,
        detail=_detail(exc),
    )


def _title(exc: BaseException) -> str:
    """The MODEL being validated — schema-derived, never input-derived."""
    title = getattr(exc, "title", None)
    return title if isinstance(title, str) else "<unknown>"


def _count(exc: BaseException) -> str:
    """How many errors the validation reported.

    Reached through ``getattr`` rather than an attribute access, because this
    module DUCK-TYPES the exception: :func:`_quotes_input` matched a type NAME
    and a module ROOT, never a class, precisely so the stdio proxy never has to
    ``import pydantic``. Writing ``exc.error_count()`` would claim a static
    guarantee the match does not give.

    The failure is reported in the one channel available here -- the line
    itself -- by TYPE only and never by text, exactly as :func:`_detail` and
    :func:`_restated_link` do. A log call is not an option: this runs inside
    ``Logger.makeRecord``, so logging the failure would re-enter the factory
    that is reporting it. That is also why the answer carries the type rather
    than staying a bare ``?`` -- a truly silent handler in ``embedded/`` is
    what ``tests/test_no_silent_excepts.py`` forbids, and this one was one.
    """
    counter = getattr(exc, "error_count", None)
    if not callable(counter):
        return "?"
    try:
        return str(counter())
    except Exception as failure:  # noqa: BLE001  PERMANENT(F-913 — inside makeRecord)
        return f"?({type(failure).__name__})"


def _detail(exc: BaseException) -> str:
    """Each error's ``type`` slug and ``loc`` path, and nothing else.

    Built from the library's OWN structured accessor with the input left out
    (``errors(include_input=False, …)``) rather than by cutting pydantic's
    rendered sentence, so there is no pattern over a third party's text
    anywhere in this rule. ``include_context`` goes too: a ``ctx`` carries
    per-error context which for a ``value_error`` is the validator's own
    exception, i.e. text composed from the input. ``msg`` is dropped for the
    same reason — for ``value_error``/``assertion_error`` it is
    ``"Value error, " + str(that exception)`` — and the ``type`` slug names the
    same condition in a stable token, which is the trade §6 records.

    TOTAL ``except`` for ``logging_setup._shape``'s reason and not a narrow
    one: ``errors()`` is library code and this runs inside
    ``Logger.makeRecord``, so anything escaping breaks every log call in the
    process, including the one reporting it. The failure is reported in the one
    channel available here — the line itself — by TYPE only, never by text.
    ``getattr`` for :func:`_count`'s reason: the type was matched by NAME, so
    the accessor is a duck-typing assumption and is written as one.
    """
    reader = getattr(exc, "errors", None)
    if not callable(reader):
        return "; unreadable=no-errors-accessor"
    try:
        errors = reader(include_input=False, include_url=False, include_context=False)
    except Exception as failure:  # noqa: BLE001  PERMANENT(F-913 — inside makeRecord)
        return f"; unreadable={type(failure).__name__}"
    seen: list[str] = []
    for error in errors:
        pair = _pair(error)
        if pair not in seen:
            seen.append(pair)
    if not seen:
        return ""
    shown = seen[:MAX_RESTATED_ERRORS]
    if len(seen) > MAX_RESTATED_ERRORS:
        shown.append(f"{RESTATED_OVERFLOW}+{len(seen) - MAX_RESTATED_ERRORS}")
    return "; " + ", ".join(shown)


def _pair(error: object) -> str:
    """One error's ``type`` at its ``loc``, both bounded and neither the input."""
    if not isinstance(error, dict):
        return f"<{type(error).__name__}>"
    kind = error.get("type")
    location = error.get("loc")
    path = ROOT_LOC
    if isinstance(location, (tuple, list)) and location:
        path = ".".join(str(part) for part in location)
        if len(path) > MAX_LOC_CHARS:
            path = path[:MAX_LOC_CHARS] + RESTATED_OVERFLOW
    return f"{kind if isinstance(kind, str) else '<unknown>'} at {path}"
