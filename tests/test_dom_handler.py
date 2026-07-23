"""RED-first pins for C1 (finding A1): query_elements must route select_all
through element_resolution recovery and stop swallowing the CDP -32000
stale-node race into a lying empty list.

nodriver resolves a selector in two non-atomic CDP calls; a DOM.documentUpdated
between them raises ProtocolException "Could not find node with given id
[code: -32000]". query_elements' broad ``except Exception: return []`` turned
that transient race -- and a genuinely-unresolvable selector -- into a
success-shaped ``[]`` ("no matches"). The confirmed contract
(plan_RELEASE_FIX_TierA, C1): a transient -32000 retries-then-recovers; a
persistent -32000 raises ``ToolError`` (never a silent ``[]``); a genuine
zero-match still returns ``[]``. Hermetic (a fake Tab), so the fast unit lane.
"""

import pytest
from nodriver.core.connection import ProtocolException

from stealth_chrome_devtools_mcp.embedded import element_resolution
from stealth_chrome_devtools_mcp.embedded.dom_handler import DOMHandler
from stealth_chrome_devtools_mcp.embedded.element_resolution import _MAX_RESOLVES
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError


@pytest.fixture(autouse=True)
def _instant_backoff(monkeypatch):
    # Zero the recovery backoff so the unit lane stays fast while the real
    # sleep(0) code path still runs. Mirrors tests/test_element_resolution.py.
    monkeypatch.setattr(element_resolution, "_SETTLE_SECONDS", 0.0)


def _stale():
    # Mirrors the real CDP error: str() contains the -32000 marker text.
    return ProtocolException(
        {"message": "Could not find node with given id", "code": -32000}
    )


class _FakeElement:
    """Minimal nodriver-Element stand-in for the per-element loop."""

    def __init__(self, tag="div", text="hello", attrs=None):
        self.tag_name = tag
        self.text_all = text
        self.attrs = attrs or {}

    async def update(self):
        return None

    async def get_position(self):
        return None


class _FakeTab:
    """Fake tab whose ``select_all`` replays an effects list (raise or return),
    matching the recovery-fake idiom in tests/test_element_resolution.py."""

    def __init__(self, select_all_effects):
        self._effects = list(select_all_effects)
        self.select_all_calls = 0

    async def select_all(self, selector, *args, **kwargs):
        self.select_all_calls += 1
        effect = self._effects.pop(0)
        if isinstance(effect, Exception):
            raise effect
        return effect


@pytest.mark.asyncio
async def test_query_elements_recovers_from_transient_stale_node():
    # First select_all hits the -32000 race, the re-resolve on a fresh document
    # succeeds -> the elements surface (recovery), never a swallowed [].
    tab = _FakeTab([_stale(), [_FakeElement()]])
    results = await DOMHandler.query_elements(tab, "div.foo", visible_only=False)
    assert len(results) == 1
    assert tab.select_all_calls == 2


@pytest.mark.asyncio
async def test_query_elements_raises_on_persistent_stale_node():
    # -32000 on every attempt -> after _MAX_RESOLVES the failure surfaces as a
    # ToolError, NOT a lying empty list.
    tab = _FakeTab([_stale() for _ in range(_MAX_RESOLVES)])
    with pytest.raises(ToolError):
        await DOMHandler.query_elements(tab, "div.foo", visible_only=False)


@pytest.mark.asyncio
async def test_query_elements_genuine_zero_match_returns_empty():
    # A successful resolve that legitimately matches nothing must stay a normal
    # empty list -- guards against over-correcting the persistent-failure arm.
    tab = _FakeTab([[]])
    results = await DOMHandler.query_elements(tab, "div.none", visible_only=False)
    assert results == []
    assert tab.select_all_calls == 1
