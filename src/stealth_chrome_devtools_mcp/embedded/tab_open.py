"""THE one home for opening a new tab (F-940).

Three places open one — the ``new_tab`` tool, ``BrowserManager._replace_main_tab``
(which ``navigate`` takes on every 25th navigation and whenever its tracked tab
went stale) and ``login_persistence``'s settings tab — and all three called
nodriver's ``Browser.get(url, new_tab=True)``. That call sends
``Target.createTarget`` and then finds the new ``Tab`` with a bare
``next(filter(...))`` over ``browser.targets``: a list only nodriver's
``Target.targetCreated`` handler fills.

Chrome sends that event only on a session that asked for it with
``Target.setDiscoverTargets``, and nodriver asks exactly once, in
``Browser.start``. When the BROWSER-level websocket drops, the next ``send``
reconnects it silently and ``Connection._register_handlers`` skips the Target
domain as "enabled by default" — so the new session never asks again, no
``targetCreated`` ever arrives, and every later ``get(new_tab=True)`` dies with
``RuntimeError: coroutine raised StopIteration`` (PEP 479). Measured on Chrome
154: 0 of 40 opens failed on a fresh browser, 3 of 3 after one forced drop. In
the field it took out ``new_tab`` and, through the 25-navigation recycle, every
``navigate`` on the instance (Sentry -B5, -B4 and -B0, two backends on 2.1.1 and
2.1.18; -2K on 2.0.5 is the same StopIteration before F-818 reworded it).

Re-asking for discovery is not the fix: on a fresh session Chrome answers it with
a ``targetCreated`` for EVERY existing target, and nodriver's handler appends
each one again, so ``browser.targets`` would list every open tab twice. Instead
the tab is opened here without depending on the event at all: the ``Tab`` the
handler registered is used when there is one — the normal case, since Chrome
sends the event ahead of the reply — and otherwise it is built from Chrome's own
``Target.getTargetInfo`` answer exactly the way the handler builds it.

**A ``Tab``, never a bare ``Connection``.** ``update_targets`` registers targets
it discovers as bare ``Connection`` objects (F-771/F-775); every caller here goes
on to await the result or ``close()`` it, which only a ``Tab`` can do. Such an
entry for the new target — one a concurrent ``update_targets`` registered while
``getTargetInfo`` was in flight — is replaced in place, so the target is never
listed twice.
"""

from __future__ import annotations

from nodriver import Browser, Connection, Tab, cdp


async def open_tab(browser: Browser, url: str) -> Tab:
    """Open *url* in a new tab of *browser* and return its ``Tab``.

    The arguments to ``createTarget`` are nodriver's own, so the tab is the one
    ``Browser.get(url, new_tab=True)`` would have opened; the closing
    ``update_targets`` is the ``await self`` that call ends with.
    """
    target_id = await browser.connection.send(
        cdp.target.create_target(url, new_window=False, enable_begin_frame_control=True)
    )
    tab = _registered_tab(browser, target_id)
    if tab is None:
        info = await browser.connection.send(cdp.target.get_target_info(target_id))
        tab = _register(
            browser, Tab(_websocket_url(browser, info), target=info, browser=browser)
        )
    await browser.update_targets()
    return tab


def find(browser: Browser, tab_id: str) -> Tab | None:
    """The open tab whose target id is *tab_id*, as a ``Tab``; ``None`` if none.

    Moved from ``BrowserManager._find_tab`` (F-962), which handed back the bare
    ``Connection`` ``update_targets`` registers for a target it discovered
    (F-771/F-775): a caller's page tools now act on any tab it picks, so it gets
    a ``Tab`` built the way :func:`open_tab` builds one, registered in place.
    """
    entry = next(
        (t for t in browser.tabs if str(t.target.target_id) == str(tab_id)), None
    )
    if entry is None or not _is_bare_connection(entry):
        return entry
    return _register(
        browser,
        Tab(
            _websocket_url(browser, entry.target), target=entry.target, browser=browser
        ),
    )


def _is_bare_connection(entry: Connection) -> bool:
    # The exact type, not ``isinstance``: ``Tab`` IS a ``Connection``, and the
    # bare base class is precisely what ``update_targets`` registers.
    return type(entry) is Connection


def _registered_tab(browser: Browser, target_id: cdp.target.TargetID) -> Tab | None:
    """The ``Tab`` nodriver's ``targetCreated`` handler registered, if it did."""
    return next(
        (
            entry
            for entry in browser.targets
            if entry.target.target_id == target_id and not _is_bare_connection(entry)
        ),
        None,
    )


def _register(browser: Browser, tab: Tab) -> Tab:
    """Put *tab* in ``browser.targets`` in place of a bare ``Connection`` for the
    same target, and never beside a ``Tab`` that got there first."""
    for index, entry in enumerate(browser.targets):
        if entry.target.target_id != tab.target.target_id:
            continue
        if not _is_bare_connection(entry):
            return entry
        browser.targets[index] = tab
        return tab
    browser.targets.append(tab)
    return tab


def _websocket_url(browser: Browser, info: cdp.target.TargetInfo) -> str:
    """The URL nodriver's ``targetCreated`` handler gives the tabs it builds."""
    return (
        f"ws://{browser.config.host}:{browser.config.port}"
        f"/devtools/{info.type_ or 'page'}/{info.target_id}"
    )
