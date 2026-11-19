"""The evidence ``logged_in`` needs after the password was typed (T-19366).

A new document without a login form is not a signed-in session: an "account locked",
a captcha page or an "approve on your phone" page is one too, and telling the agent it
is signed in would be a false claim. ``logged_in`` therefore needs POSITIVE evidence:

* the browser left the hosts the entry names (a single sign-on hand-off), or
* the cookie jar for those hosts GAINED or CHANGED a cookie since just before Enter
  (:func:`session_marks` / :func:`gained`).

A page with neither is ``needs_user``, and nothing is frozen. A single-page app that
signs in with a token it keeps in ``localStorage`` and sets no cookie therefore reads
``needs_user``; that is the safe side of the trade, and the person can still use the
browser the agent was given.

**Cookies are read raw.** ``Network.getCookies`` goes through nodriver's own
``Cookie`` parser, and Chrome 153's reply has fields that parser does not know; a
parse failure there kills the connection's listener (see F-902). The command below
asks the same question and returns the reply's own dictionaries.

**No cookie value is kept.** A mark is ``(name, domain, path, digest)`` where the
digest is a truncated SHA-256 of the value, so a comparison needs no secret at rest.
"""

from __future__ import annotations

import hashlib
import urllib.parse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Generator

    from nodriver import Tab

Mark = tuple[str, str, str, str]
_DIGEST_CHARS = 16


def _get_cookies(
    urls: list[str],
) -> Generator[dict[str, object], dict[str, object], list[dict[str, object]]]:
    """``Network.getCookies`` for ``urls``, returned as the browser sent it."""
    reply = yield {"method": "Network.getCookies", "params": {"urls": urls}}
    cookies = reply.get("cookies") if isinstance(reply, dict) else None
    return (
        [c for c in cookies if isinstance(c, dict)] if isinstance(cookies, list) else []
    )


def urls_for(hosts: list[str], current: str) -> list[str]:
    """The URLs whose cookies count: each login host (``https``), and the origin of
    ``current`` when it is a web page on one of them (the loopback test site)."""
    urls = [f"https://{host}/" for host in hosts]
    parts = urllib.parse.urlsplit(current)
    if parts.scheme in ("http", "https") and parts.hostname in hosts:
        urls.append(f"{parts.scheme}://{parts.netloc}/")
    return sorted(set(urls))


async def session_marks(tab: Tab, urls: list[str]) -> frozenset[Mark]:
    """The cookies the browser would send to ``urls``, as value-free marks."""
    cookies = await tab.send(_get_cookies(urls))
    marks: set[Mark] = set()
    for cookie in cookies:
        value = str(cookie.get("value", ""))
        digest = hashlib.sha256(value.encode()).hexdigest()[:_DIGEST_CHARS]
        marks.add(
            (
                str(cookie.get("name", "")),
                str(cookie.get("domain", "")),
                str(cookie.get("path", "")),
                digest,
            )
        )
    return frozenset(marks)


def gained(before: frozenset[Mark], after: frozenset[Mark]) -> bool:
    """Whether ``after`` holds a cookie ``before`` lacks (new, or a changed value)."""
    return bool(after - before)
