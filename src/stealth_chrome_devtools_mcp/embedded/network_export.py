"""What ``export_network_data`` writes for one captured request or response (F-1025).

The export is a file a caller points anywhere, and a captured request carries
the live ``Cookie`` and ``Authorization`` it was sent with, so a log exported to
a shared or synced folder was a credential leak. By default every credential
header's VALUE (and every request cookie's) is replaced by :data:`REDACTED`; the
NAMES stay, so the export still shows that a request carried a login.
``include_credentials=True`` is the one switch that keeps them, passed down from
the tool as ``hide=False``.

THE one place a captured request or response is turned into an export record,
which is why it is a module of its own: ``network_interceptor`` is at its line
budget, and the redaction rule belongs next to the record shape it protects.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from stealth_chrome_devtools_mcp.embedded.models import (
        NetworkRequest,
        NetworkResponse,
    )

#: What an exported credential becomes.
REDACTED = "REDACTED"

#: Header names whose VALUE is a credential, lower-cased for comparison.
CREDENTIAL_HEADERS = frozenset(
    {
        "cookie",
        "set-cookie",
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "x-auth-token",
        "x-csrf-token",
        "x-xsrf-token",
        "x-amz-security-token",
    }
)

#: A header whose NAME contains one of these is a credential too (``x-session-
#: token``, ``x-client-secret``, ``x-goog-api-key``): the named set cannot list
#: every vendor's spelling.
CREDENTIAL_HEADER_MARKERS = ("token", "secret", "api-key", "apikey")


def is_credential_header(name: str) -> bool:
    """Whether the header called *name* carries a credential, case-blind."""
    lowered = name.lower()
    return lowered in CREDENTIAL_HEADERS or any(
        marker in lowered for marker in CREDENTIAL_HEADER_MARKERS
    )


def redacted_headers(headers: dict[str, str], hide: bool) -> dict[str, str]:
    """*headers* with every credential header's value replaced, case-blind.

    ``hide=False`` returns *headers* as is.
    """
    if not hide:
        return headers
    return {
        name: REDACTED if is_credential_header(name) else value
        for name, value in headers.items()
    }


def request_record(req: NetworkRequest, hide: bool) -> dict[str, object]:
    return {
        "request_id": req.request_id,
        "url": req.url,
        "method": req.method,
        "headers": redacted_headers(req.headers, hide),
        "cookies": dict.fromkeys(req.cookies, REDACTED) if hide else req.cookies,
        "post_data": req.post_data,
        "resource_type": req.resource_type,
        "timestamp": req.timestamp.isoformat(),
    }


def response_record(resp: NetworkResponse, hide: bool) -> dict[str, object]:
    return {
        "request_id": resp.request_id,
        "status": resp.status,
        "headers": redacted_headers(resp.headers, hide),
        "content_type": resp.content_type,
        "body": base64.b64encode(resp.body).decode("utf-8") if resp.body else None,
        "timestamp": resp.timestamp.isoformat(),
    }
