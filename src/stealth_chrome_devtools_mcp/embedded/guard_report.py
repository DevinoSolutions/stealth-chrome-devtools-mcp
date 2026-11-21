"""What the DevTools-port guard says when Chrome refuses it (T-20441).

On the 2.1.68 publish gate (run 44876025032, macOS) the guard closed two pages
and its warning named neither the command Chrome refused nor the page, so the
cause could not be read from the log. The wording lives here, with the two
facts a line may carry: where the target is (see ``where``), and what each
failed command said.

Stdlib only; ``debug_port_guard`` is the one caller.
"""

from __future__ import annotations

import urllib.parse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

UNGUARDED = {
    "page": "a page could not be guarded; closing it (%s: %s)",
    "iframe": "a frame could not be guarded; blanking its page (%s: %s)",
}


CHROMES_OWN = frozenset({"chrome", "chrome-untrusted", "devtools"})


def refused(method: str, error: object) -> str:
    """The text of a refused guard command: the method and Chrome's own message
    (its first 200 characters), or its error code when it gave no message."""
    said = error
    if isinstance(error, dict):
        said = error.get("message") or f"code {error.get('code')}"
    return f"{method} refused: {str(said)[:200]}"


def where(url: object) -> str:
    """All of a target's address a log line may carry. One of Chrome's own
    pages is named (``chrome://settings``, ``about:blank``). A site is named by
    its scheme alone: a warning becomes a Sentry breadcrumb, a host says where
    a person was browsing, and a path or a query can hold a token."""
    try:
        parts = urllib.parse.urlsplit(str(url or ""))
    except ValueError:
        return "unreadable url"
    if parts.scheme in CHROMES_OWN:
        return f"{parts.scheme}://{parts.hostname or ''}"[:80]
    if parts.scheme == "about":
        return f"about:{parts.path}"[:80]
    return f"{parts.scheme or 'no scheme'}: a site"[:80]


def _said(answer: BaseException) -> str:
    """One failed command, as its type and its text. A timeout's text is empty,
    so its type names it; an error whose text cannot be read is named by type,
    because a raise here would leave the target neither guarded nor closed."""
    try:
        return f"{type(answer).__name__} {answer}".strip()
    except Exception:  # noqa: BLE001  PERMANENT(any __str__ may raise, and a raise here leaves the target unclosed)
        return type(answer).__name__


def why(
    kind: str, info: Mapping[str, object], answers: Sequence[object]
) -> tuple[str, str, str]:
    """The warning for a *kind* of target that could not be guarded, as the
    arguments of ``Logger.warning``: the line, where the target is, and what each
    failed guard command said."""
    said = "; ".join(
        _said(answer) for answer in answers if isinstance(answer, BaseException)
    )
    return UNGUARDED[kind], where(info.get("url")), said
