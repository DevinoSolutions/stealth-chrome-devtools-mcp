"""Pins for element_resolution: recovery from the CDP -32000 stale-document race.

nodriver resolves selectors in two non-atomic CDP calls; a DOM.documentUpdated
between them invalidates the document nodeId and query_selector raises
ProtocolException "Could not find node with given id [code: -32000]". The
resolution helpers must: re-resolve on that specific signal (bounded), pass
success straight through, and propagate any other error unchanged. These are
hermetic (a fake Tab) so they run in the fast unit lane, not the browser lane.
"""

import pytest
from nodriver.core.connection import ProtocolException

from stealth_chrome_devtools_mcp.embedded import element_resolution
from stealth_chrome_devtools_mcp.embedded.element_resolution import (
    _MAX_RESOLVES,
    query_selector_all,
    resolve_by_text,
    resolve_element,
)


@pytest.fixture(autouse=True)
def _instant_backoff(monkeypatch):
    # The recovery backoff is real; zero it so the unit lane stays fast while the
    # real asyncio.sleep(0) code path still runs.
    monkeypatch.setattr(element_resolution, "_SETTLE_SECONDS", 0.0)


def _stale():
    # Mirrors the real CDP error: str() contains the -32000 marker text.
    return ProtocolException(
        {"message": "Could not find node with given id", "code": -32000}
    )


def _other():
    return ProtocolException({"message": "Some unrelated CDP failure", "code": -32601})


def _pop(effects):
    effect = effects.pop(0)
    if isinstance(effect, Exception):
        raise effect
    return effect


class _FakeTab:
    def __init__(self, *, select=None, find=None, send=None):
        self._select = list(select or [])
        self._find = list(find or [])
        self._send = list(send or [])
        self.select_calls = 0
        self.find_calls = 0
        self.send_calls = 0

    async def select(self, selector, timeout=None):
        self.select_calls += 1
        return _pop(self._select)

    async def find(self, text, best_match=True, timeout=None):
        self.find_calls += 1
        return _pop(self._find)

    async def send(self, _cmd):
        self.send_calls += 1
        return _pop(self._send)


class _Doc:
    node_id = 1


@pytest.mark.asyncio
async def test_resolve_element_recovers_from_transient_stale_node():
    sentinel = object()
    tab = _FakeTab(select=[_stale(), sentinel])
    assert await resolve_element(tab, "#btn") is sentinel
    assert tab.select_calls == 2  # failed once, recovered on a fresh document


@pytest.mark.asyncio
async def test_resolve_element_passes_success_through_without_retry():
    sentinel = object()
    tab = _FakeTab(select=[sentinel])
    assert await resolve_element(tab, "#btn") is sentinel
    assert tab.select_calls == 1


@pytest.mark.asyncio
async def test_resolve_element_propagates_non_stale_error_immediately():
    tab = _FakeTab(select=[_other()])
    with pytest.raises(ProtocolException, match="unrelated"):
        await resolve_element(tab, "#btn")
    assert tab.select_calls == 1  # not a stale-node error -> no retry


@pytest.mark.asyncio
async def test_resolve_element_is_bounded_when_stale_persists():
    tab = _FakeTab(select=[_stale() for _ in range(_MAX_RESOLVES)])
    with pytest.raises(ProtocolException, match="Could not find node with given id"):
        await resolve_element(tab, "#btn")
    assert tab.select_calls == _MAX_RESOLVES  # bounded; genuine failure surfaces


@pytest.mark.asyncio
async def test_resolve_element_forwards_timeout():
    captured = {}
    tab = _FakeTab(select=[object()])

    async def _select(selector, timeout=None):
        captured["timeout"] = timeout
        tab.select_calls += 1
        return _pop(tab._select)

    tab.select = _select
    await resolve_element(tab, "#btn", timeout=2.5)
    assert captured["timeout"] == 2.5


@pytest.mark.asyncio
async def test_resolve_by_text_recovers_from_stale_node():
    sentinel = object()
    tab = _FakeTab(find=[_stale(), sentinel])
    assert await resolve_by_text(tab, "Submit") is sentinel
    assert tab.find_calls == 2


@pytest.mark.asyncio
async def test_query_selector_all_recovers_from_stale_node():
    # Each attempt sends get_document then query_selector_all. First attempt:
    # get_document ok, query_selector_all stale -> re-resolve; second succeeds.
    nodes = [10, 11]
    tab = _FakeTab(send=[_Doc(), _stale(), _Doc(), nodes])
    assert await query_selector_all(tab, ".x") == nodes
    assert tab.send_calls == 4
