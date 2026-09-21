"""THE one home for "which third-party SITES render a payload into their own
log text, and what a record from one is allowed to say" (F-911).

It is the SITE half of a pair, and the pair is the point. ``logging_setup``
holds third-party log lines down three ways, and each answers a different
question about the same record:

* by **LEVEL** — ``PAYLOAD_LOG_FAMILIES`` / ``apply_payload_log_floor``
  (F-906, F-908). Works when the library names its own logger, because an
  explicit level on the family ROOT is upstream of every sink.
* by **ARGUMENT** — ``PAYLOAD_ARG_PACKAGE`` / ``install_payload_arg_redaction``
  (F-907). Works when the payload rides in ``record.args`` as an object whose
  ``__repr__`` renders it.
* by **SITE** — this module. Works when neither of the others can, which is
  exactly the case ``mcp/shared/session.py`` presents.

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

It is a separate module rather than three more screens of ``logging_setup``
because this is the one of the three rules that is a **registry**: F-906's is a
tuple of names and F-907's is a single package string, while this one is a
table plus the matching logic that reads it, and any future third-party module
that root-logs a payload joins the table here. The timing was the 1000-LOC
budget, which F-911's addition crossed and which **ratchets down only** —
``cli_call`` → ``cli_render``'s precedent, and the cut is this question's
surface rather than a raised cap. If ``logging_setup`` grows again, the honest
next cut is the other two rules joining this module, which would make it THE
one home for "what a third party's log line may carry" entire.
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
#: lookup against a value ``LogRecord.__init__`` has already computed — and two
#: spellings of one fact is how a rule comes to cover nothing.
_SITE_STEMS = frozenset(
    site.rpartition("/")[2].removesuffix(".py") for site in PAYLOAD_LOG_SITES
)

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

    Two reads and no third. The stem first, because this runs for every record
    in the process and ``record.module`` is a string the stdlib has already
    computed — one frozenset lookup, False for everything. Only then the path,
    which is the actual answer: several packages in a normal tree ship a
    ``session.py``, and withholding a stranger's records because their file
    happens to share a name would be silencing rather than redacting.

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
    ``ContraDict``); this function reads only attributes ``LogRecord.__init__``
    computed itself. ``record.module`` is always a ``str`` — that constructor
    sets ``"Unknown module"`` from its own handler when the split fails — so
    the lookup cannot raise, and the one value a caller controls, ``pathname``,
    is tested for ``str`` rather than coerced. A non-string pathname is nobody's
    module path, so ``None`` is the right answer and not a swallowed failure.
    """
    if record.module not in _SITE_STEMS:
        return None
    pathname = record.pathname
    if not isinstance(pathname, str):
        return None
    path = pathname.replace("\\", "/")
    for site in PAYLOAD_LOG_SITES:
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
