"""F-844 — ``get_instance_state`` answered ``partial: true`` on EVERY call.

Live evidence (2.0.8, real stdio transport): 4/4 calls came back
``partial: true`` with
``detail_error: "Failed to collect full page state: AttributeError: 'list'
object has no attribute 'get'"``. The whole page-state collection is one
``try`` block (``browser_manager.get_page_state``), so the localStorage,
sessionStorage and viewport it had already gathered were discarded too — the
tool never once returned real state.

The mechanism is entirely Python-side, which is why this pin is hermetic:
nodriver's generated CDP wrapper for ``Network.getCookies`` ends in
``return [Cookie.from_json(i) for i in json['cookies']]`` — it hands back the
DESERIALIZED ``list[Cookie]``, never the raw ``{"cookies": [...]}`` envelope.
``get_page_state`` called ``cookies.get("cookies", [])`` on that list.

The cookie line was only the FIRST raising statement. Behind it sat a second:
``tab.evaluate("({width: innerWidth, …})")`` never returned that object.
nodriver always sends deep ``SerializationOptions``, so Chrome answers an object
literal as ``[[key, {type, value}], …]`` — and nodriver's ``return_by_value``
flag cannot override the options it always sends. ``PageState.viewport`` then
failed validation, and the tool was still ``partial: true``. The product now
asks the page for ``JSON.stringify(...)``, whose answer is a plain string.
``viewport`` is also widened from ``dict[str, int]`` to ``dict[str, int | float]``
so a fractional ``devicePixelRatio`` (Windows at 125%, any Retina panel) is not
a third way to be partial.

House rule (memory: *mocked fakes can encode the bug*): the canned CDP answer
here is built with ``nodriver.cdp.network.Cookie.from_json`` from a payload
Chrome really sends, NOT a hand-written ``{"cookies": [...]}`` dict modelling
the assumption the product got wrong. A fake of that second shape is precisely
what would have kept this defect green. The viewport answer is a JSON *string*
for the same reason — and the deep-serialization half was found only because
the fix was driven against real Chrome over the real transport, not because
any fake predicted it.
"""

from __future__ import annotations

import pytest
from nodriver.cdp.network import Cookie

from fakes import FakeBrowser, FakeTab, fake_instance
from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager

INSTANCE_ID = "i1"

#: One cookie exactly as Chrome sends it in a ``Network.getCookies`` response.
CHROME_COOKIE_JSON = {
    "name": "sid",
    "value": "abc123",
    "domain": "fixture.test",
    "path": "/",
    "expires": -1,
    "size": 9,
    "httpOnly": True,
    "secure": False,
    "session": True,
    "sameSite": "Lax",
    "priority": "Medium",
    "sameParty": False,
    "sourceScheme": "NonSecure",
    "sourcePort": 80,
}

PAGE_URL = "https://fixture.test/index.html"

#: The JS answers ``get_page_state`` reads, keyed by an unambiguous substring of
#: each expression it evaluates.
PAGE_JS = {
    "window.location.href": PAGE_URL,
    "document.title": "fixture-index-page",
    "document.readyState": "complete",
    "Object.keys(localStorage)": ["ls-key"],
    "localStorage.getItem": "ls-value",
    "Object.keys(sessionStorage)": ["ss-key"],
    "sessionStorage.getItem": "ss-value",
    # A JSON *string*, because that is what the product now asks the page for
    # and what nodriver hands back for one. A dict here would model an
    # `evaluate` that returns plain objects — which it does not (see below).
    "JSON.stringify": '{"width":1280,"height":720,"devicePixelRatio":1}',
}


@pytest.fixture()
def manager_and_tab():
    """A real ``BrowserManager`` over a tab that answers like a live page.

    ``cdp_responses`` returns what nodriver's ``get_cookies`` wrapper really
    returns — a ``list[Cookie]`` — so the product meets the shape it meets in
    production.
    """
    tab = FakeTab(
        url=PAGE_URL,
        evaluate_map=PAGE_JS,
        cdp_responses={"get_cookies": [Cookie.from_json(CHROME_COOKIE_JSON)]},
    )
    manager = BrowserManager()
    manager._instances[INSTANCE_ID] = {
        "browser": FakeBrowser(tabs=[tab]),
        "tab": tab,
        "instance": fake_instance(INSTANCE_ID),
        "navigation_count": 0,
    }
    return manager, tab


async def test_page_state_survives_the_cookie_list(manager_and_tab):
    """THE pin: a full ``PageState``, with the cookie really carried through.

    Asserting the cookie's own fields — not merely "no exception" — is what
    makes this evidence: a fix that swallowed the AttributeError and returned
    ``cookies=[]`` would pass a no-raise pin while still losing every cookie.
    """
    manager, _tab = manager_and_tab

    state = await manager.get_page_state(INSTANCE_ID)

    assert state is not None
    assert [c["name"] for c in state.cookies] == ["sid"]
    assert state.cookies[0]["value"] == "abc123"
    assert state.cookies[0]["domain"] == "fixture.test"


async def test_page_state_keeps_the_storage_it_already_collected(manager_and_tab):
    """The collateral half: one raising line discarded four working reads.

    localStorage, sessionStorage, the URL/title/readyState and the viewport were
    all gathered before the cookie line and thrown away with it.
    """
    manager, _tab = manager_and_tab

    state = await manager.get_page_state(INSTANCE_ID)

    assert state.url == PAGE_URL
    assert state.local_storage == {"ls-key": "ls-value"}
    assert state.session_storage == {"ss-key": "ss-value"}
    assert state.viewport == {"width": 1280, "height": 720, "devicePixelRatio": 1}


async def test_get_instance_state_is_not_partial(
    manager_and_tab, call_tool, patched_server
):
    """The user-facing half, through the real tool body.

    ``partial: false`` is the whole finding: on 2.0.8 this key was ``true`` on
    every call, with the ``'list' object has no attribute 'get'`` detail_error.
    """
    manager, _tab = manager_and_tab
    srv = patched_server(browser_manager=manager)

    state = await call_tool(srv, "get_instance_state", instance_id=INSTANCE_ID)

    assert state["partial"] is False
    assert "detail_error" not in state
    assert state["cookies"][0]["name"] == "sid"


async def test_page_state_reports_a_cookieless_page_as_an_empty_list(manager_and_tab):
    """A page with no cookies is not a failure — and ``tab.send`` may answer
    ``None`` when Chrome sends nothing at all."""
    manager, tab = manager_and_tab
    tab._cdp_responses["get_cookies"] = None

    state = await manager.get_page_state(INSTANCE_ID)

    assert state.cookies == []


async def test_page_state_accepts_a_fractional_device_pixel_ratio(manager_and_tab):
    """The second half of F-844, found by the live run and invisible here before.

    ``window.devicePixelRatio`` is ``1.25`` at Windows' 125% display scaling and
    ``2.0`` on a Retina panel. ``PageState.viewport`` was declared
    ``dict[str, int]``, so every such machine would have kept answering
    ``partial: true`` — with a pydantic validation error in place of the
    ``AttributeError`` — after the cookie fix alone. Integral values must stay
    ``int``: nobody wants ``"width": 1280.0``.
    """
    manager, tab = manager_and_tab
    tab._evaluate_map["JSON.stringify"] = (
        '{"width":1280,"height":720,"devicePixelRatio":1.25}'
    )

    state = await manager.get_page_state(INSTANCE_ID)

    assert state.viewport == {"width": 1280, "height": 720, "devicePixelRatio": 1.25}
    assert isinstance(state.viewport["width"], int)
