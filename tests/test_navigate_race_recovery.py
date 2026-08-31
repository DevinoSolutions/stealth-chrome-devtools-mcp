"""F-824 — the F-817 nodriver-race classifier must reach the navigate path.

`STEALTH-CHROME-DEVTOOLS-MCP-3N` is `Error calling tool 'navigate'` →
`browser_manager.navigate` → `tab.get(url)` → nodriver's `Tab.wait` →
`Connection.remove_handler` → `KeyError(<nodriver.cdp.page.FrameStoppedLoading>)`:
the exact handler-cleanup race `element_resolution` has recovered from since
F-817, escaping raw because `navigate` decided recoverability from its OWN
substring list — and a `KeyError` whose arg is a CDP event class matches none of
those markers, so the very first attempt re-raised it at the caller.

The fix is reach, not a second classifier: `_is_recoverable_navigation_error`
asks `element_resolution.recoverable_race` — the one home for "is this a known
nodriver race" — and navigate's own budget (2 attempts) is untouched. These pins
therefore assert both halves: the races are recovered, and the verdict comes
from that one function (patch it out and navigate stops recovering).

Hermetic: a fake tab whose `get()` raises the real nodriver exception objects,
built the way the library builds them.
"""

from __future__ import annotations

from typing import Any

import pytest
from nodriver import cdp
from nodriver.core.connection import ProtocolException

from fakes import FakeTab
from stealth_chrome_devtools_mcp.embedded import browser_manager, element_resolution
from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager

URL = "https://fake.test/target"

# ``navigate`` reads both back off the tab after a successful get().
NAV_EVALUATES = {"location.href": URL, "document.title": "t"}


def _handler_race() -> KeyError:
    """nodriver's bookkeeping race: ``Tab.wait``'s finally-block does a bare
    ``del self.handlers[evt_dom]``, so an overlapping wait raises a ``KeyError``
    whose arg is the CDP event CLASS (3N's exact value)."""
    return KeyError(cdp.page.FrameStoppedLoading)


def _stale_node() -> ProtocolException:
    return ProtocolException(
        {"message": "Could not find node with given id", "code": -32000}
    )


class _RacingTab(FakeTab):
    """A tab whose FIRST ``get()`` raises ``error``; the next one navigates."""

    def __init__(self, error: BaseException) -> None:
        super().__init__(url=URL, evaluate_map=NAV_EVALUATES)
        self._error: BaseException | None = error

    async def get(self, url: str, *args: Any, **kwargs: Any) -> Any:
        if self._error is not None:
            error, self._error = self._error, None
            self.get_calls.append(url)
            raise error
        return await super().get(url, *args, **kwargs)


@pytest.fixture
def navigating_manager(monkeypatch):
    """A ``BrowserManager`` whose instance bookkeeping and post-nav waits are
    stubbed, leaving ``tab.get`` and the retry decision as the live behaviour."""

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(BrowserManager, "touch_instance", noop)
    monkeypatch.setattr(BrowserManager, "update_instance_state", noop)
    monkeypatch.setattr(BrowserManager, "_wait_for_navigation_condition", noop)
    return BrowserManager()


def _with_tab(monkeypatch, tab):
    async def get_navigation_tab(self, instance_id):
        return tab

    async def replace_main_tab(self, instance_id, reason, close_existing=True):
        # Production hands back a fresh about:blank tab; what is pinned here is
        # that a second attempt HAPPENS, not which object it lands on.
        return tab

    monkeypatch.setattr(BrowserManager, "get_navigation_tab", get_navigation_tab)
    monkeypatch.setattr(BrowserManager, "_replace_main_tab", replace_main_tab)


# ---------------------------------------------------------------------------
# The races reach navigate
# ---------------------------------------------------------------------------


async def test_navigate_recovers_from_the_nodriver_handler_race(
    monkeypatch, navigating_manager
):
    """THE 3N pin: the KeyError escaped `navigate` on attempt 1 of 2."""
    tab = _RacingTab(_handler_race())
    _with_tab(monkeypatch, tab)

    result = await navigating_manager.navigate(instance_id="iid-1", url=URL)

    assert result["success"] is True
    assert tab.get_calls == [URL, URL]  # raced once, recovered on the retry


async def test_navigate_recovers_from_the_stale_document_race(
    monkeypatch, navigating_manager
):
    """The -32000 half of the same classifier, on the same path."""
    tab = _RacingTab(_stale_node())
    _with_tab(monkeypatch, tab)

    result = await navigating_manager.navigate(instance_id="iid-1", url=URL)

    assert result["success"] is True
    assert tab.get_calls == [URL, URL]


async def test_navigate_still_refuses_to_retry_an_unrelated_failure(
    monkeypatch, navigating_manager
):
    """Reach, not blanket retrying: a genuinely fatal error surfaces at once."""
    tab = _RacingTab(RuntimeError("boom"))
    _with_tab(monkeypatch, tab)

    with pytest.raises(RuntimeError, match="boom"):
        await navigating_manager.navigate(instance_id="iid-1", url=URL)

    assert tab.get_calls == [URL]


# ---------------------------------------------------------------------------
# One classifier, no copy
# ---------------------------------------------------------------------------


def test_navigate_uses_element_resolutions_classifier_object():
    assert browser_manager.recoverable_race is element_resolution.recoverable_race


def test_the_navigation_classifier_answers_for_both_known_races():
    classify = BrowserManager._is_recoverable_navigation_error

    assert classify(_handler_race()) is True
    assert classify(_stale_node()) is True
    assert classify(KeyError("some-missing-dict-key")) is False
    assert classify(RuntimeError("boom")) is False


async def test_navigate_stops_recovering_when_the_one_classifier_says_no(
    monkeypatch, navigating_manager
):
    """Delegation is live, not a re-listed copy: neutralise the shared
    classifier and the same race stops being retried on this path."""
    monkeypatch.setattr(browser_manager, "recoverable_race", lambda exc: None)
    tab = _RacingTab(_handler_race())
    _with_tab(monkeypatch, tab)

    with pytest.raises(KeyError):
        await navigating_manager.navigate(instance_id="iid-1", url=URL)

    assert tab.get_calls == [URL]
