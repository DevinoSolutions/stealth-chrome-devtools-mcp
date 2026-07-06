"""Sentry error-shipping for stealth-chrome-devtools-mcp.

This module is error-SHIPPING only: it forwards already-logged or raised errors
to Sentry when ``SENTRY_DSN`` is configured. It is deliberately NOT the logging
spine — plan M3's ``logging_setup.py`` owns log-WRITING (handlers, formatters,
sinks). One home each: ``observability.py`` decides where errors go; M3 decides
how they are recorded. Do not add file logging or an excepthook framework here.

Off by default: ``sentry_init()`` is a no-op unless ``settings.sentry_dsn`` is
set. This is a local, single-user tool, so error reporting is opt-in — the
universal default is silence, enabled per-install by exporting ``SENTRY_DSN``.
"""

import importlib.metadata
import logging

from stealth_chrome_devtools_mcp.settings import get_settings

_PACKAGE_NAME = "stealth-chrome-devtools-mcp"


def _release() -> str | None:
    """Best-effort package version, used as the Sentry ``release`` tag."""
    try:
        return importlib.metadata.version(_PACKAGE_NAME)
    except importlib.metadata.PackageNotFoundError:
        return None


def sentry_init() -> bool:
    """Initialize Sentry error shipping when ``SENTRY_DSN`` is configured.

    Returns ``True`` when Sentry was initialized, ``False`` when it was a no-op
    (no DSN set — the default for this local tool).

    Raises ``RuntimeError`` when a DSN is set but the optional ``sentry`` extra
    is not installed: opting into error reporting and then getting silence is a
    worse failure than a loud, actionable startup error.
    """
    dsn = get_settings().sentry_dsn
    if not dsn:
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.asyncio import AsyncioIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration
    except ImportError as err:
        raise RuntimeError(
            "SENTRY_DSN is set but the 'sentry' extra is not installed. "
            "Install it with: pip install stealth-chrome-devtools-mcp[sentry]"
        ) from err

    sentry_sdk.init(
        dsn=dsn,
        integrations=[
            LoggingIntegration(event_level=logging.ERROR),
            AsyncioIntegration(),
        ],
        release=_release(),
    )
    return True
