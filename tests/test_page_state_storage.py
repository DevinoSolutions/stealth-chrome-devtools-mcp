"""F-869 — ``get_instance_state`` reported EMPTY storage with ``partial: false``
while a ``TypeError`` from our own code was logged at INFO and shipped nowhere.

Live evidence (2.1.5, real stdio transport, Windows 11, headless Chrome 152,
backend pid 53836). ``spawn_browser(headless=True)`` → ``navigate(
"https://www.google.com/")`` (success, title "Google") → ``get_instance_state``.
The tool returned 28 cookies, ``"local_storage": {}``, ``"session_storage": {}``
and ``"partial": false``. The backend log for that same call says::

    2026-09-14 23:34:56,804 INFO 53836 [c5e09043b5d9] stealth.backend:
    browser_manager.get_page_state: Storage access unavailable for
    886a408c-4a8f-41ec-a096-8d06a1c1fee3: unhashable type: 'dict'

``www.google.com`` has localStorage entries, so ``{}`` was untrue, and
``unhashable type: 'dict'`` is a Python bug in this package — not the
"page blocks storage" condition the INFO line claims.

The shape, MEASURED (not assumed) against Chrome 152 over a real http origin
with the pinned nodriver 0.47::

    >>> await tab.evaluate("Object.keys(localStorage)")
    [{'type': 'string', 'value': 'alpha'}, {'type': 'string', 'value': 'beta'}]
    >>> await tab.evaluate("localStorage.getItem('alpha')")
    '1'

``Tab.evaluate`` always sends ``SerializationOptions(serialization="deep")`` and
returns ``remote_object.deep_serialized_value.value`` raw;
``DeepSerializedValue.from_json`` keeps ``json["value"]`` verbatim and never
walks into it. A string primitive therefore arrives plain (which is why the
``getItem`` half looked fine) but an ARRAY arrives as a list of BiDi nodes. The
old loop then did ``local_storage[key] = value`` with a ``dict`` for ``key``.

House rule (memory: *mocked fakes can encode the bug*, and *fixtures from the
same serializer cannot fail*): :data:`DEEP_KEYS` below is the literal ``repr``
printed by that probe, not a hand-shaped ``["ls-key"]``. The pre-existing
``tests/test_instance_state_cookies.py`` fixture used exactly that hand-shaped
list of plain strings — which is precisely why this defect was green there
through F-844's own live-driven fix.

The two pins:

* :func:`test_storage_comes_back_from_the_shape_chrome_really_sends` drives the
  real shape through the reader and asserts the VALUES arrive. Under the old
  per-key loop this is the ``TypeError``.
* :func:`test_an_unexpected_storage_failure_is_reported_not_swallowed` asserts
  the error policy: an unexpected exception makes the record say so
  (``partial: True`` + ``detail_error`` carrying the exception text) and is
  logged at WARNING **with a traceback**, instead of INFO + ``{}`` +
  ``partial: false``.
"""

from __future__ import annotations

import json
import logging

import pytest
from nodriver.cdp.network import Cookie

from fakes import FakeBrowser, FakeTab, fake_instance
from stealth_chrome_devtools_mcp.embedded import page_storage
from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager

INSTANCE_ID = "i1"
PAGE_URL = "https://fixture.test/index.html"

#: Verbatim from the Chrome 152 probe — the answer ``Object.keys(localStorage)``
#: really produces once nodriver's deep ``SerializationOptions`` are applied.
DEEP_KEYS = [{"type": "string", "value": "alpha"}, {"type": "string", "value": "beta"}]

#: The one round trip the product now makes. ``ok`` false is Chrome's own
#: SecurityError, caught by the page inside the snippet.
STORAGE_OK = json.dumps(
    {
        "local": {"ok": True, "entries": [["alpha", "1"], ["beta", "2"]]},
        "session": {"ok": True, "entries": [["s", "9"]]},
    }
)
STORAGE_BLOCKED = json.dumps(
    {
        "local": {
            "ok": False,
            "reason": (
                "Failed to read the 'localStorage' property from 'Window': "
                "Storage is disabled inside 'data:' URLs."
            ),
        },
        "session": {"ok": False, "reason": "Storage is disabled inside 'data:' URLs."},
    }
)

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

VIEWPORT_JS = '{"width":1280,"height":720,"devicePixelRatio":1}'


def _page_js(storage_answer: str) -> dict[str, str]:
    """The JS answers ``get_page_state`` reads, keyed by a substring of each
    expression. Both the OLD per-key expressions and the NEW one-shot read are
    answered, so this fixture is honest against either implementation — the pin
    fails on the product's behaviour, never on the harness not knowing the JS.
    """
    return {
        "window.location.href": PAGE_URL,
        "document.title": "fixture-index-page",
        "document.readyState": "complete",
        # OLD path: the deep-serialized array, exactly as Chrome answers it.
        "Object.keys(localStorage)": DEEP_KEYS,
        "localStorage.getItem": "1",
        "Object.keys(sessionStorage)": DEEP_KEYS,
        "sessionStorage.getItem": "9",
        # NEW path (checked BEFORE the viewport read, which also starts with
        # "JSON.stringify" — dict order is insertion order, so this entry wins).
        "read('localStorage')": storage_answer,
        "JSON.stringify": VIEWPORT_JS,
    }


def _manager(storage_answer: str) -> tuple[BrowserManager, FakeTab]:
    tab = FakeTab(
        url=PAGE_URL,
        evaluate_map=_page_js(storage_answer),
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


# ---------------------------------------------------------------------------
# (a) the TypeError — the shape Chrome really sends must read correctly
# ---------------------------------------------------------------------------


async def test_storage_comes_back_from_the_shape_chrome_really_sends():
    """THE pin. Asserting the VALUES, not merely "no exception": a fix that
    swallowed the TypeError and kept answering ``{}`` would pass a no-raise pin
    while still being the defect this finding is about.
    """
    manager, _tab = _manager(STORAGE_OK)

    state = await manager.get_page_state(INSTANCE_ID)

    assert state is not None
    assert state.local_storage == {"alpha": "1", "beta": "2"}
    assert state.session_storage == {"s": "9"}


async def test_the_deep_serialized_key_array_is_never_hashed():
    """The mechanism, isolated: the reader must not use a BiDi node as a key.

    Kept as its own pin because it names the trap rather than the symptom — the
    same trap F-844 closed for the viewport object in the same function.
    """
    tab = FakeTab(evaluate_map={"read('localStorage')": STORAGE_OK})

    local, session = await page_storage.read(tab)

    assert local == {"alpha": "1", "beta": "2"}
    assert session == {"s": "9"}
    assert all(isinstance(k, str) for k in local)


async def test_a_key_with_a_quote_in_it_survives():
    """The old loop interpolated the key into ``localStorage.getItem('{key}')``.

    A key containing ``'`` produced a JS syntax error; a key containing
    ``');...`` was script injection from page-controlled data. The one-shot read
    interpolates nothing, so this is structurally impossible now — pinned so it
    stays that way.
    """
    key = "it's\n');alert(1)//"
    hostile = json.dumps(
        {
            "local": {"ok": True, "entries": [[key, "kept"]]},
            "session": {"ok": True, "entries": []},
        }
    )
    tab = FakeTab(evaluate_map={"read('localStorage')": hostile})

    local, _session = await page_storage.read(tab)

    assert local == {key: "kept"}


async def test_a_page_that_blocks_storage_is_still_not_partial(
    call_tool, patched_server
):
    """The NAMED tolerance, unchanged and now reached only by its own condition.

    An opaque origin / ``data:`` URL really has no readable storage, so empty
    dicts are the truth there and the call is not degraded. This is the branch
    the INFO line was always meant for.
    """
    manager, _tab = _manager(STORAGE_BLOCKED)
    srv = patched_server(browser_manager=manager)

    state = await call_tool(srv, "get_instance_state", instance_id=INSTANCE_ID)

    assert state["local_storage"] == {}
    assert state["session_storage"] == {}
    assert state["partial"] is False


# ---------------------------------------------------------------------------
# (b) the error policy — an unexpected failure must be visible
# ---------------------------------------------------------------------------


async def test_an_unexpected_storage_failure_is_reported_not_swallowed(
    call_tool, patched_server, caplog
):
    """The second half of the finding, and the one that makes the record honest.

    On 2.1.5 this call returned ``local_storage: {}`` with ``partial: false``
    and one INFO line. INFO is not error-reported, so the ``TypeError`` reached
    neither the caller nor Sentry: the defect was invisible from both ends.
    """
    manager, tab = _manager(STORAGE_OK)

    async def boom(_expression, *args, **kwargs):
        if "read('localStorage')" in _expression:
            raise TypeError("unhashable type: 'dict'")
        return tab._answer_for_js(_expression)

    tab.evaluate = boom
    srv = patched_server(browser_manager=manager)

    with caplog.at_level(logging.WARNING, logger="stealth.backend"):
        state = await call_tool(srv, "get_instance_state", instance_id=INSTANCE_ID)

    assert state["partial"] is True
    assert "unhashable type: 'dict'" in state["detail_error"]

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "an unexpected failure must not be logged at INFO"
    assert any("unhashable type: 'dict'" in r.getMessage() for r in warnings)
    assert any(r.exc_info for r in warnings), (
        "a defect in our own code must carry its traceback"
    )


async def test_a_blocked_page_is_logged_at_info_and_carries_no_traceback(caplog):
    """The two conditions must not collapse into one log level.

    If the blocked branch were also WARNING+exc_info, every opaque-origin page
    would read as a defect and the level would stop meaning anything.
    """
    manager, _tab = _manager(STORAGE_BLOCKED)

    with caplog.at_level(logging.INFO, logger="stealth.backend"):
        await manager.get_page_state(INSTANCE_ID)

    blocked = [
        r for r in caplog.records if "Storage access unavailable" in r.getMessage()
    ]
    assert blocked, "the page-refused branch still says so"
    assert all(r.levelno == logging.INFO for r in blocked)
    assert all(r.exc_info is None for r in blocked)


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(object(), id="a-husk-instead-of-a-string"),
        pytest.param('{"local": null, "session": null}', id="a-record-that-is-not-one"),
        pytest.param(
            '{"local":{"ok":true,"entries":[["only-a-key"]]},'
            '"session":{"ok":true,"entries":[]}}',
            id="an-entry-that-is-not-a-pair",
        ),
        pytest.param(
            '{"local":{"ok":true,"entries":"nope"},"session":{"ok":true,"entries":[]}}',
            id="entries-that-are-not-a-list",
        ),
    ],
)
async def test_an_answer_that_is_not_the_promised_json_is_an_error(answer):
    """Not-the-JSON-we-asked-for is a read failure, never "no storage".

    ``tab.evaluate`` answers a JS throw with an ``ExceptionDetails`` husk rather
    than raising, so "it returned something" is not evidence that it worked.
    """
    tab = FakeTab(evaluate_map={"read('localStorage')": answer})

    with pytest.raises(page_storage.StorageReadError):
        await page_storage.read(tab)
