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
default for people who never chose it, so the events must not identify them:
the machine's ``server_name`` is dropped and the home-directory segment of every
path is replaced with ``~``. Everything a maintainer debugs from — release,
environment, correlation ids, the exception type and mechanism, the module path
after the home segment — is deliberately left intact.
"""

import importlib.metadata
import logging
import re
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


def _anonymize(value: object, depth: int = 0) -> object:
    """Return ``value`` with every home-directory segment in it replaced.

    Applied to the whole event rather than to a list of known fields: paths turn
    up in frame ``abs_path``/``filename``, in exception messages, in log messages
    and their params, and in breadcrumbs. A field list would have to grow every
    time the SDK grows one. The transform is a no-op on anything that is not a
    home path, so nothing else can be damaged.
    """
    if depth > _MAX_EVENT_DEPTH:
        return value
    if isinstance(value, str):
        return _HOME_SEGMENT_RE.sub(
            lambda m: f"{m.group('prefix')}{_ANONYMOUS_USER}", value
        )
    if isinstance(value, dict):
        return {key: _anonymize(item, depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return [_anonymize(item, depth + 1) for item in value]
    if isinstance(value, tuple):
        return tuple(_anonymize(item, depth + 1) for item in value)
    return value


def _scrub_event(event: "Event", _hint: "dict[str, object] | None" = None) -> "Event":
    """Sentry's ``before_send``: strip what identifies the reporter, keep the rest.

    Two removals, both universal — there is no maintainer-only path:

    * ``server_name``, which is the machine's own hostname;
    * the home-directory segment of every path, which is the account name.

    Never raises, and never drops the event: an event we could not fully scrub
    is still worth more than silence, so an internal failure degrades to
    :func:`_without_server_name` rather than to ``None``. The ``isinstance``
    guards look redundant against the annotation and are not — the annotation
    states what the SDK promises to pass, and this function is the last thing
    that runs before an event leaves the machine, so it defends against being
    handed something else rather than dying at the boundary.
    """
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
    return _without_server_name(event)


def _without_server_name(event: "Event") -> "Event":
    """The floor the scrubber degrades to: no hostname, nothing else promised.

    Copies before removing. The event belongs to the SDK and the caller may
    still hold it, so mutating it in place would be a second, invisible way for
    this module to change an event.
    """
    try:
        if isinstance(event, dict):
            remainder = dict(event)
            remainder.pop("server_name", None)
            return cast("Event", remainder)
    except Exception:  # noqa: BLE001  PERMANENT(never-raises contract, #55)
        _log.debug("Sentry server_name removal failed", exc_info=True)  # see above
    return event


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
