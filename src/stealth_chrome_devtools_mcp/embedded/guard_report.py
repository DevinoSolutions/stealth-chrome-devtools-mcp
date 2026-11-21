"""What the DevTools-port guard says when Chrome refuses it (T-20441).

On the 2.1.68 publish gate (run 44876025032, macOS) the guard closed two pages
and its warning named neither the command Chrome refused nor the page, so the
cause could not be read from the log. The wording lives here, with the two
facts a line may carry: the page's scheme and host, and what each failed
command said.

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


def refused(method: str, error: object) -> str:
    """The text of a refused guard command: the method and Chrome's own message
    (its first 200 characters)."""
    said = error.get("message", "") if isinstance(error, dict) else error
    return f"{method} refused: {str(said)[:200]}"


def where(url: object) -> str:
    """The scheme and host of *url*: all of a page's address a log line may
    carry, because a path or a query can hold a token."""
    try:
        parts = urllib.parse.urlsplit(str(url or ""))
        return f"{parts.scheme}://{parts.hostname or ''}"[:80]
    except ValueError:
        return "unreadable url"


def why(
    kind: str, info: Mapping[str, object], answers: Sequence[object]
) -> tuple[str, str, str]:
    """The warning for a *kind* of target that could not be guarded, as the
    arguments of ``Logger.warning``: the line, where the target is, and what each
    failed guard command said. A timeout's text is empty, so its type names it."""
    said = "; ".join(
        f"{type(answer).__name__} {answer}".strip()
        for answer in answers
        if isinstance(answer, BaseException)
    )
    return UNGUARDED[kind], where(info.get("url")), said
