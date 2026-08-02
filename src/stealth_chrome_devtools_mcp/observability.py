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
#: flavor). Separators repeat because ``str(OSError)`` repr-escapes them, and
#: the look-behind keeps a URL such as ``https://host/Users/x`` from matching a
#: home directory it merely spells like.
_HOME_SEGMENT_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?P<prefix>[A-Za-z]:[\\/]+Users[\\/]+|/home/|/Users/)"
    r"(?P<user>[^\\/\s'\"]+)",
    re.IGNORECASE,
)

#: What the user segment becomes. Keeping the prefix means a reader still sees
#: which OS and which home root the failure came from.
_ANONYMOUS_USER = "~"

#: Depth at which the event walk stops descending. Sentry events are shallow;
#: this only has to make a self-referential one terminate.
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
    up in frame ``abs_path``/``filename``, in exception messages, in log
    messages and their params, in breadcrumbs, and in captured frame locals. A
    field list would have to grow every time the SDK grows one. The transform is
    a no-op on anything that is not a home path, so nothing else can be damaged.
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
    is still worth more than silence, so an internal failure degrades to the
    unscrubbed event minus ``server_name`` rather than to ``None``. The
    ``isinstance`` guards look redundant against the annotation and are not —
    the annotation states what the SDK promises to pass, and this function is
    the last thing that runs before an event leaves the machine, so it defends
    against being handed something else rather than dying at the boundary.
    """
    scrubbed = event
    try:
        anonymized = _anonymize(event)
    except Exception:  # noqa: BLE001  PERMANENT(never-raises contract, #55)
        anonymized = None
    if isinstance(anonymized, dict):
        scrubbed = cast("Event", anonymized)
    if isinstance(scrubbed, dict):
        scrubbed.pop("server_name", None)
    return scrubbed


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
    )
    return True
