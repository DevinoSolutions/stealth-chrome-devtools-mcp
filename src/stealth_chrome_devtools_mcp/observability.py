"""Sentry error-shipping for stealth-chrome-devtools-mcp.

This module is error-SHIPPING only: it forwards already-logged or raised errors
to the project's Sentry. It is deliberately NOT the logging spine — plan M3's
``logging_setup.py`` owns log-WRITING (handlers, formatters, sinks). One home
each: ``observability.py`` decides where errors go; M3 decides how they are
recorded. Do not add file logging or an excepthook framework here.

ON by default, and configuration-free. The destination is the hardcoded
:data:`_DSN` below and the SDK is a first-class dependency, so a user who sets
no environment variable and writes no ``.env`` still gets a working server that
reports its own crashes. Opting out is one namespaced flag:
``STEALTH_MCP_NO_ERROR_REPORTING=true``.

There is deliberately no ``SENTRY_DSN`` knob (issue #55). The backend is a
shared singleton that MCP clients launch with the host project's cwd, so a
DSN read from the environment meant it silently adopted whatever ``SENTRY_DSN``
that project's own ``.env`` happened to hold and shipped this tool's errors to
someone else's app project. A Sentry DSN is public by design (it is an ingest
address, not a credential), so hardcoding ours costs nothing and removes the
collision entirely.

Every outgoing event goes through :func:`_scrub_event` first. Reporting is on by
default for people who never chose it, so the events must not identify them —
neither the machine nor the pages it drives. Four removals, all universal:

* the machine's ``server_name``;
* the home-directory segment of every path, replaced with ``~``;
* every email address, replaced with ``[redacted-email]`` (F-826);
* every URL's query, fragment and userinfo, replaced with
  ``[redacted-query]`` / ``[redacted-fragment]`` / ``[redacted]@`` (F-826).

Everything a maintainer debugs from — release, environment, correlation ids, the
exception type and mechanism, the module path after the home segment, a URL's
scheme, host and path — is deliberately left intact. The redaction is TARGETED
for that reason: a message replaced wholesale is an issue nobody can act on,
which is a slower way of turning reporting off.

The same hook is also the one place that decides an event is not worth sending:
a failure raised through the project's own error CONVENTION
(``embedded/tool_errors.py``) is the product working as designed, not a crash.
See :func:`_is_expected_tool_failure`.
"""

import importlib.metadata
import logging
import re
from functools import cache
from typing import TYPE_CHECKING, cast

from stealth_chrome_devtools_mcp.settings import get_settings

if TYPE_CHECKING:  # the SDK is imported lazily at runtime; this is a type only
    from sentry_sdk.types import Event

_PACKAGE_NAME = "stealth-chrome-devtools-mcp"

#: THE destination for this tool's error reports. Public by design — a DSN is
#: an ingest endpoint that only accepts events; it grants no read access to the
#: project. Published in README.md since 2.0.x.
_DSN = "https://3206541bdab9246f00d7099e692e2ee2@sentry.devino.ca/34"

_log = logging.getLogger(__name__)


#: A home directory under either path flavor, whatever the host OS is: a
#: Windows maintainer receives Linux users' events and vice versa, so this is a
#: string rule and never ``os.path``/``pathlib`` (which only know the local
#: flavor). Separators repeat because ``str(OSError)`` repr-escapes them and
#: because ``//home//x`` is a legal POSIX path; the leading run of a UNC share
#: may itself be repr-escaped. The look-behind keeps a URL such as
#: ``https://host/Users/x`` from matching a home directory it merely spells
#: like.
#:
#: Every separator run is **possessive** (``++``, Python 3.11+, and this package
#: requires 3.11). Greedy runs made the match quadratic: on a long run of
#: separators each start position re-scanned the whole run looking for a root
#: that was not there, so 8000 contiguous slashes cost about a second and 20000
#: about six — inside ``before_send``, in a process that is already crashing,
#: on a string that can come from arbitrary page content quoted into an error.
#: The SDK would not have saved us: sentry 2.64.0 leaves ``max_value_length``
#: unset. Possessive runs never give characters back, which removes the
#: backtracking without changing what matches.
_HOME_ROOTS = (
    r"[A-Za-z]:[\\/]++Users[\\/]++"  # C:\Users\ , C:/Users/ , C:\\Users\\
    r"|\\{2,4}+[^\\/\s]++[\\/]++Users[\\/]++"  # \\fileserver\Users\
    r"|/++(?:var/++|usr/++|export/++)?home/++"  # /home/ , /var/home/ (Silverblue)
    r"|/++Users/++"  # macOS
)

#: The account name itself. Two alternatives, tried in order:
#:
#: 1. space-tolerant, but only when a separator or a closing quote proves the
#:    path really continues — ``C:\Users\John Doe\app.py`` and the repr form
#:    ``'C:\\Users\\John Doe'``. Deliberately NOT anchored on end-of-string:
#:    prose ends there too, and ``cwd was /home/jdoe and then`` would have had
#:    its sentence eaten. The space run is capped so a stray later separator
#:    cannot swallow an unbounded stretch of text;
#: 2. the plain single-token segment, which is what prose and ordinary
#:    usernames hit.
#:
#: The residual, stated exactly: a space-containing account name that is NOT
#: immediately followed by a separator or a quote keeps its later words — the
#: end of a string, but equally a bare home directory sitting mid-sentence.
#: Only the first word is anonymized there. Closing it means matching to
#: end-of-token-run, which is precisely what would eat the prose above, so the
#: trade is deliberate and it errs toward keeping the sentence readable.
_HOME_USER = (
    r"[^\\/\s'\"]+(?: [^\\/\s'\"]+){0,3}(?=[\\/'\"])"
    r"|[^\\/\s'\"]+"
)

_HOME_SEGMENT_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    rf"(?P<prefix>{_HOME_ROOTS})"
    rf"(?P<user>{_HOME_USER})",
    re.IGNORECASE,
)

#: What the user segment becomes. Keeping the prefix means a reader still sees
#: which OS and which home root the failure came from.
_ANONYMOUS_USER = "~"

#: F-826. This product's exceptions quote the thing that failed, and the thing
#: that failed is a page: a URL an agent navigated to, a value it typed into a
#: form. The project's own Sentry held OAuth authorization URLs with the token
#: still in the query, and email addresses — from third-party machines as well
#: as the maintainer's. Two more string rules, both surgical: they take the
#: secret and leave the sentence, because an unreadable issue is a closed issue.
#:
#: A URL, split where its secrets live. ``scheme://`` then, optionally,
#: ``userinfo@`` (proxy credentials are written exactly like this), then the
#: host and path — which are the diagnostic and are KEPT verbatim — then the
#: query and fragment, which is where tokens ride in both OAuth flows.
#:
#: The look-behind is a performance guard, not a semantic one: without it, a
#: long run of scheme-legal characters containing no ``://`` retried the scan at
#: every offset, which is quadratic on a string that can be arbitrary page
#: content quoted into an error. With it, only a position that actually starts a
#: token is ever tried. Same reasoning as the possessive runs above.
_URL_RE = re.compile(
    r"(?<![A-Za-z0-9+.\-])"
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)"
    r"(?P<userinfo>[^/?#\s'\"\\]*@)?"
    r"(?P<hostpath>[^?#\s'\"<>\\]*)"
    r"(?P<tail>[?#][^\s'\"<>\\]*)?"
)

#: An email address, anchored on a local part that is not preceded by more of
#: itself. The look-behind is what keeps this linear: on ``aaaa…@`` only the
#: first offset is viable, and every other one is rejected without scanning.
#: Whether the trailing token really IS a domain is decided in
#: :func:`_redact_email`, not here — ``python@3.11`` and ``sentry-sdk@2.64.0``
#: match this shape, and redacting them would eat the interpreter path out of
#: every macOS stacktrace.
_EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+\-])"
    r"[A-Za-z0-9._%+\-]++@"
    r"(?P<domain>[A-Za-z0-9.\-]++)"
)

#: The last label of a real domain: alphabetic, at least two characters. This is
#: the whole difference between an address and a version pin.
_EMAIL_TLD_RE = re.compile(r"\A[A-Za-z]{2,}\Z")

#: A domain has a name and a TLD. ``postgres@localhost`` has neither, and a host
#: with no dot is not an address a person can be reached at.
_EMAIL_MIN_DOMAIN_LABELS = 2

_REDACTED_EMAIL = "[redacted-email]"
_REDACTED_USERINFO = "[redacted]@"
_REDACTED_QUERY = "?[redacted-query]"
_REDACTED_FRAGMENT = "#[redacted-fragment]"

#: What free text becomes when the walk itself failed — see
#: :func:`_without_unscrubbable_text`.
_REDACTED_UNSCRUBBABLE = "[redacted-unscrubbable]"

#: The free-text fields, by name. Only the degraded floor uses this list: the
#: normal path walks everything and needs no list at all. Enumerated here
#: because "drop what we could not prove clean" has to name what it drops.
_FREE_TEXT_FIELDS = ("logentry", "breadcrumbs", "message", "extra")

#: Cycle terminator for the event walk, not a size limit. A real event cannot
#: reach it — by the time ``before_send`` runs the SDK has already serialized
#: the event (``client.py`` calls ``serialize()`` at :880, ``before_send`` at
#: :896, sdk 2.64.0), so what arrives is plain JSON-shaped data: no cycles, no
#: shared subtrees, and nothing but ``str``/``dict``/``list``/``tuple`` and
#: scalars — which is exactly why covering those four types covers everything.
#: This bound exists only so a hand-built or hostile event still terminates.
_MAX_EVENT_DEPTH = 24


def _release() -> str | None:
    """Best-effort package version, used as the Sentry ``release`` tag."""
    try:
        return importlib.metadata.version(_PACKAGE_NAME)
    except importlib.metadata.PackageNotFoundError:
        return None


def _redact_url(match: "re.Match[str]") -> str:
    """Keep a URL's scheme, host and path; drop its userinfo, query and fragment.

    Positional, deliberately: an implicit-flow ``#access_token=…`` and a docs
    ``#readme`` anchor occupy the same slot and nothing can tell them apart, so
    both go. What survives still names the provider, the endpoint and the page,
    which is what a maintainer reads the issue for.
    """
    prefix = match.group("scheme")
    if match.group("userinfo"):
        prefix += _REDACTED_USERINFO
    tail = match.group("tail") or ""
    marks = ""
    if "?" in tail:
        marks += _REDACTED_QUERY
    if "#" in tail:
        marks += _REDACTED_FRAGMENT
    return f"{prefix}{match.group('hostpath')}{marks}"


def _redact_email(match: "re.Match[str]") -> str:
    """Replace a real address; leave every other use of ``@`` exactly as it was.

    "Real" means the domain's last label is alphabetic. A trailing dot or hyphen
    belongs to the prose around the address, not to it, so it is handed back.
    Returning ``match.group(0)`` unchanged is how this rule declines: over-
    redaction costs diagnostics that nobody notices are missing.
    """
    domain = match.group("domain")
    core = domain.rstrip(".-")
    labels = core.split(".")
    if len(labels) < _EMAIL_MIN_DOMAIN_LABELS or not _EMAIL_TLD_RE.match(labels[-1]):
        return match.group(0)
    return f"{_REDACTED_EMAIL}{domain[len(core) :]}"


def _redact_text(text: str) -> str:
    """THE per-string rule: every redaction this module performs, in one place.

    Order matters exactly once. The URL rule runs first so that a proxy URL's
    ``user:pass@host`` is already gone before the email rule looks for an ``@``;
    afterwards the two cannot see each other's output. The home rule is last and
    is independent of both.
    """
    text = _URL_RE.sub(_redact_url, text)
    text = _EMAIL_RE.sub(_redact_email, text)
    return _HOME_SEGMENT_RE.sub(lambda m: f"{m.group('prefix')}{_ANONYMOUS_USER}", text)


def _anonymize(value: object, depth: int = 0) -> object:
    """Return ``value`` with :func:`_redact_text` applied to every string in it.

    Applied to the whole event rather than to a list of known fields: paths, URLs
    and quoted page content turn up in frame ``abs_path``/``filename``, in
    exception messages, in log messages and their params, and in breadcrumbs. A
    field list would have to grow every time the SDK grows one. The transform is
    a no-op on anything that matches no rule, so nothing else can be damaged.
    """
    if depth > _MAX_EVENT_DEPTH:
        return value
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, dict):
        return {key: _anonymize(item, depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return [_anonymize(item, depth + 1) for item in value]
    if isinstance(value, tuple):
        return tuple(_anonymize(item, depth + 1) for item in value)
    return value


#: The tool-surface classes that mean "expected failure", by NAME. Used only by
#: the payload fallback below; the live-object path uses ``isinstance`` instead.
#: Deliberately an explicit allowlist rather than "anything from our package":
#: this decides what is never seen again, so it names what it drops.
_EXPECTED_ERROR_NAMES = frozenset({"ToolError", "InstanceNotFoundError"})

#: A name alone is not enough. ``fastmcp.exceptions.ToolError`` is a DIFFERENT
#: class with the same name, and it is what wraps a genuine crash on its way out
#: of ``tool_manager`` (``raise ToolError(...) from e``) — matching on the bare
#: name would drop exactly the real bugs this filter exists to preserve.
_EXPECTED_ERROR_MODULE_PREFIX = "stealth_chrome_devtools_mcp"

#: ``sys.exc_info()``'s shape — ``(type, value, traceback)``.
_EXC_INFO_LENGTH = 3


@cache
def _expected_error_base() -> "type[BaseException] | None":
    """``tool_errors.ToolError``, imported on first use rather than at module import.

    Lazy on purpose. ``cli.py`` imports this module at startup and imports
    nothing from ``embedded/`` at top level, and importing the ``embedded``
    package runs its sanctioned ``sys.path`` shim (``embedded/__init__.py``),
    which puts module names like ``models`` and ``settings`` ahead of everything
    else on the path. That shim belongs to the backend; nothing is gained by
    firing it in an ops-CLI process that may never ship an event. By the time a
    ``ToolError`` exists to classify, the leaf is loaded anyway.

    ``ToolError`` alone is the base: ``InstanceNotFoundError`` and any future
    subclass are covered by ``isinstance``, which is the point of the convention.
    Returns ``None`` if the import fails, which degrades to the payload fallback
    rather than to an exception.
    """
    try:
        from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError
    except Exception:  # noqa: BLE001  PERMANENT(never-raises contract, #55)
        return None
    return ToolError


def _exception_chain(exc: BaseException) -> "list[BaseException]":
    """``exc`` and everything it was raised from, in the order Sentry reports them.

    Mirrors the SDK's own walk (``__cause__``, else ``__context__`` unless
    ``raise ... from None`` suppressed it) so the live-object path and the
    payload path below judge the SAME set of exceptions. Bounded and cycle-safe
    for the same reason :data:`_MAX_EVENT_DEPTH` exists.
    """
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if len(chain) >= _MAX_EVENT_DEPTH:
            break
        seen.add(id(current))
        chain.append(current)
        following = current.__cause__
        if following is None and not current.__suppress_context__:
            following = current.__context__
        current = following
    return chain


def _hint_exception(hint: "dict[str, object] | None") -> "BaseException | None":
    """The live exception behind an event, if the SDK handed one over.

    Two shapes, because the logging integration produces both: an exception
    event carries ``hint["exc_info"]`` (``event_from_exception`` fills it in even
    when the event came from ``logger.exception``), and the record itself is
    attached as ``hint["log_record"]``. The record is consulted only as a
    fallback — a hand-built hint, or an SDK that stops filling ``exc_info`` in.
    """
    if not isinstance(hint, dict):
        return None
    candidates = (
        hint.get("exc_info"),
        getattr(hint.get("log_record"), "exc_info", None),
    )
    for candidate in candidates:
        if isinstance(candidate, BaseException):
            return candidate
        if (
            isinstance(candidate, tuple)
            and len(candidate) == _EXC_INFO_LENGTH
            and isinstance(candidate[1], BaseException)
        ):
            return candidate[1]
    return None


def _is_expected_tool_failure(
    event: "Event", hint: "dict[str, object] | None" = None
) -> bool:
    """Is this event nothing but the error convention doing its job?

    Tools report an expected failure by RAISING ``tool_errors.ToolError`` (a bad
    script, a missing instance, an unknown selector) — CLAUDE.md convention 2.
    FastMCP logs every raising tool through ``logger.exception``, so the logging
    integration turns each of those into a Sentry ERROR event with
    ``handled: yes``. The top issues in the project's Sentry were all of them:
    205 events of an agent passing an illegal script to ``execute_script``. That
    is the product answering correctly, and it drowns the events that are not.

    Two paths to the same decision, and the SAME rule:

    * ``hint`` carries the live exception → ``isinstance``, which is exact and
      picks up subclasses raised anywhere;
    * only the serialized payload is available → the exception ``type`` name must
      be in :data:`_EXPECTED_ERROR_NAMES` *and* its ``module`` must be ours.

    **Drops only when EVERY exception in the chain is one of ours.** A
    ``ToolError`` raised while handling an ``AttributeError`` keeps the event:
    the real bug is in there, and this filter must never be the reason nobody
    saw it. That is also why a broken logger name is not the test — the
    ``AttributeError`` in ``navigate`` that this project actually shipped
    arrived through ``FastMCP.fastmcp.tools.tool_manager``, the very logger the
    noise arrives on, so ``ignore_logger`` on it would have hidden a real bug.

    Never raises: an event it cannot classify is an event it sends.
    """
    try:
        exception = _hint_exception(hint)
        if exception is not None:
            base = _expected_error_base()
            if base is not None:
                return all(
                    isinstance(link, base) for link in _exception_chain(exception)
                )
        return _payload_is_expected_tool_failure(event)
    except Exception:  # noqa: BLE001  PERMANENT(never-raises contract, #55)
        _log.debug("Sentry expected-failure check failed; shipping", exc_info=True)
        return False


def _payload_is_expected_tool_failure(event: "Event") -> bool:
    """The name-and-module rule, applied to a serialized event's exception values.

    Every value must match, and an event with no exception values at all does
    not match: "nothing to classify" is not "expected", or a message-only event
    would be silently dropped.
    """
    if not isinstance(event, dict):
        return False
    exception = event.get("exception")
    values = exception.get("values") if isinstance(exception, dict) else None
    if not isinstance(values, list) or not values:
        return False
    return all(_value_is_expected_tool_failure(value) for value in values)


def _value_is_expected_tool_failure(value: object) -> bool:
    """One serialized exception: ours by name AND by module, or it is not ours."""
    if not isinstance(value, dict):
        return False
    module = value.get("module")
    return (
        value.get("type") in _EXPECTED_ERROR_NAMES
        and isinstance(module, str)
        and module.startswith(_EXPECTED_ERROR_MODULE_PREFIX)
    )


def _scrub_event(
    event: "Event", hint: "dict[str, object] | None" = None
) -> "Event | None":
    """Sentry's ``before_send``: drop the expected, scrub what is left.

    THE one hook (there is no second ``before_send``; a second way to change an
    outgoing event is a defect). It does two things in order:

    0. an event that is only the error convention working as designed is dropped
       — see :func:`_is_expected_tool_failure`;
    1. everything that survives is scrubbed.

    Four removals, all universal — there is no maintainer-only path:

    * ``server_name``, which is the machine's own hostname;
    * the home-directory segment of every path, which is the account name;
    * every email address (F-826);
    * every URL's userinfo, query and fragment (F-826).

    Step 0 runs FIRST and that ordering is load-bearing in both directions: an
    expected ``ToolError`` is dropped whole, page content and all, so the rules
    below never have to be the last line of defence for it — and equally, they
    only ever run on the events that survived, so nothing is scrubbed twice.

    Never raises. The only event it drops is the one it positively recognised in
    step 0; an event we could not fully scrub, or could not classify, is still
    worth more than silence, so an internal failure degrades to
    :func:`_without_unscrubbable_text` rather than to ``None``. The ``isinstance``
    guards look redundant against the annotation and are not — the annotation
    states what the SDK promises to pass, and this function is the last thing
    that runs before an event leaves the machine, so it defends against being
    handed something else rather than dying at the boundary.
    """
    if _is_expected_tool_failure(event, hint):
        return None
    try:
        scrubbed = _anonymize(event)
        if isinstance(scrubbed, dict):
            scrubbed.pop("server_name", None)
            return cast("Event", scrubbed)
    except Exception:  # noqa: BLE001  PERMANENT(never-raises contract, #55)
        # DEBUG on purpose: the logging integration turns INFO into breadcrumbs
        # and ERROR into events, and an event raised while shipping an event is
        # how a reporting loop starts.
        _log.debug("Sentry event scrubbing failed; degrading", exc_info=True)
    return _without_unscrubbable_text(event)


def _without_unscrubbable_text(event: "Event") -> "Event":
    """The floor the scrubber degrades to: no hostname, and no unproven free text.

    The event is still SENT — losing it entirely is the failure mode the
    never-raises contract exists to prevent, and its type, module, mechanism,
    frames, tags and release are still worth reading. What it no longer carries
    is the free text F-826 found page content in: if the walk that redacts those
    strings is the thing that failed, we cannot claim they are clean, and a
    guess is not a redaction. So they are dropped, not shipped raw.
    (Structurally this path is unreachable for a real event — by the time
    ``before_send`` runs the SDK has serialized it to plain JSON — but it is the
    one place a hostile or hand-built event could smuggle text past the rules.)

    Copies before removing. The event belongs to the SDK and the caller may
    still hold it, so mutating it in place would be a second, invisible way for
    this module to change an event.
    """
    try:
        if isinstance(event, dict):
            remainder = dict(event)
            remainder.pop("server_name", None)
            for field in _FREE_TEXT_FIELDS:
                remainder.pop(field, None)
            exception = remainder.get("exception")
            values = exception.get("values") if isinstance(exception, dict) else None
            if isinstance(values, list):
                remainder["exception"] = {
                    **exception,
                    "values": [_value_without_text(item) for item in values],
                }
            return cast("Event", remainder)
    except Exception:  # noqa: BLE001  PERMANENT(never-raises contract, #55)
        _log.debug("Sentry degraded scrub failed", exc_info=True)  # see above
    return event


def _value_without_text(value: object) -> object:
    """One serialized exception with its message replaced, its identity kept."""
    if isinstance(value, dict) and "value" in value:
        return {**value, "value": _REDACTED_UNSCRUBBABLE}
    return value


def sentry_init() -> bool:
    """Initialize Sentry error shipping unless the operator opted out.

    Returns ``True`` when Sentry was initialized, ``False`` when it was a no-op
    — either ``STEALTH_MCP_NO_ERROR_REPORTING`` is set, or ``sentry_sdk`` could
    not be imported.

    Never raises. The previous contract raised ``RuntimeError`` when a DSN was
    set without the optional ``sentry`` extra installed, on the rationale that
    "opting into error reporting and then getting silence is worse than a loud,
    actionable startup error". That rationale is now obsolete: reporting is on
    by default with a bundled SDK, so there is no per-user opt-in left to
    betray — only a server that would refuse to boot over its own telemetry.
    A missing SDK degrades to a warning and unreported errors.
    """
    if get_settings().no_error_reporting:
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.asyncio import AsyncioIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration
    except ImportError:
        _log.warning(
            "sentry_sdk is not importable; error reporting is disabled for this "
            "process. Reinstall stealth-chrome-devtools-mcp to restore it, or "
            "set STEALTH_MCP_NO_ERROR_REPORTING=true to silence this warning."
        )
        return False

    sentry_sdk.init(
        dsn=_DSN,
        integrations=[
            LoggingIntegration(event_level=logging.ERROR),
            AsyncioIntegration(),
        ],
        release=_release(),
        before_send=_scrub_event,
        # The SDK captures every frame's local variables by default. In THIS
        # product those locals hold proxy credentials (`proxy_utils`,
        # `proxy_forwarder`), Authorization and Cookie headers
        # (`network_interceptor`), and user script bodies — the exact classes
        # the canary suite treats as release blockers on every other surface.
        # Path scrubbing does not help there: the secret is the value itself.
        # We now know third parties run this, so the locals are theirs.
        include_local_variables=False,
    )
    return True
