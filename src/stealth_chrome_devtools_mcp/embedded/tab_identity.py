"""THE one home for "what page is this tab showing RIGHT NOW" (F-874).

Three tools answer that question — ``list_tabs``, ``get_active_tab`` and
``list_instances`` — and before this module two of them wrote the same four
expressions out by hand while the third did not ask at all::

    {
        "tab_id": str(tab.target.target_id),
        "url": getattr(tab, "url", "") or "",
        "title": getattr(tab.target, "title", "") or "Untitled",
        "type": getattr(tab.target, "type_", "page"),
    }

``list_instances`` instead reported ``BrowserInstance.current_url`` /
``.title``, a pair written once at spawn and once per ``navigate`` tool call and
never again — so a page that navigated itself, a tab the caller ``switch_tab``'d
away from and a title the page set after load were all invisible to it. Measured
on 2.1.6 with ten headed browsers (Chrome 152, Windows 11): ``list_instances``
said ``https://www.youtube.com/`` / "YouTube" while ``get_active_tab`` on the
same instance said ``…/results?search_query=lofi+hip+hop+radio`` / "lofi hip hop
radio - YouTube". One home is what stops the two disagreeing again.

**Why the reads are ``getattr`` and not attribute access.** ``Browser.tabs``
yields a raw ``Connection`` — not a ``Tab`` — for every target nodriver
rediscovered rather than opened (``core/browser.py``'s ``update_targets``), and
``Connection.__getattr__`` delegates to ``self.target``, so ``.url`` resolves on
both shapes but neither is guaranteed to carry it.

**Why the refresh is a ``Target.getTargets`` and not an event.** ``tab.target``
is kept current by nodriver's ``TargetInfoChanged`` handler
(``Browser._handle_target_update`` assigns ``current_tab._target``), which is
only as fresh as whatever the browser websocket has already delivered — a title
Chrome set a millisecond ago may not have arrived. ``Browser.update_targets()``
asks Chrome and rewrites every known target's metadata in place, so
:func:`refreshed` answers from Chrome's own state rather than from a bet that an
event landed first. It costs ONE round trip for the whole browser, which is also
why a listing pays it once per browser instead of awaiting each tab (``Tab.wait``
has a 0.5 s floor and a rediscovered ``Connection`` cannot be awaited at all —
F-771).

A leaf: it imports no other embedded module, takes the tab as an argument, and
does no error handling of its own — a refresh that never returns is the CALLER's
budget to bound (``rt._with_cdp_timeout``) and the caller's degradation to
report, because only the caller knows whether one wedged browser should cost the
whole answer or just its own row.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps this module a leaf
    from nodriver import Browser, Tab

#: What a tab with no title of its own is called. Chrome reports ``""`` for a
#: document that never set one (a bare ``data:text/html`` page, a blank tab); an
#: empty string reads as a missing field to a caller, so the placeholder is
#: stated once here rather than at each of the three tools.
UNTITLED = "Untitled"


def record(tab: Tab) -> dict[str, str]:
    """The ``{tab_id, url, title, type}`` record for *tab*, as it stands now.

    A pure read of metadata nodriver already holds — no CDP round trip, no
    ``await``. Call :func:`refreshed` instead when that metadata must be known
    to be current rather than merely recent.
    """
    return {
        "tab_id": str(tab.target.target_id),
        "url": getattr(tab, "url", "") or "",
        "title": getattr(tab.target, "title", "") or UNTITLED,
        "type": getattr(tab.target, "type_", "page"),
    }


async def refreshed(browser: Browser | None, tab: Tab) -> dict[str, str]:
    """:func:`record` for *tab*, after refreshing *browser*'s targets from Chrome.

    *browser* is optional because the one thing worse than an unrefreshed answer
    is no answer: an instance whose browser handle has already gone still gets
    the metadata nodriver last saw, under the same key names.
    """
    if browser is not None:
        await browser.update_targets()
    return record(tab)
