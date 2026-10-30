"""F-164 pinning tests: execute_python_in_browser attempts
Runtime.terminateExecution on timeout and returns an honest message.

Validates:
- On timeout, tab.send is called with terminate_execution and the error
  message is updated.
- If tab.send raises, it is swallowed and the error dict is still returned.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from stealth_chrome_devtools_mcp.embedded.cdp_function_executor import (
    CDPFunctionExecutor,
)


@pytest.fixture
def executor():
    return CDPFunctionExecutor()


def _make_fake_tab():
    """Fake tab with a mock send."""
    tab = MagicMock()
    tab.send = AsyncMock(return_value=None)
    return tab


def _hang_until_the_bound_fires(executor, monkeypatch, seconds=0.05):
    """The script never answers and the executor's own 10 s bound is cut to
    *seconds*; the bound itself stays the real ``asyncio.timeout``, so what fires
    is the production one (T-10690 moved it off ``wait_for``)."""
    real_timeout = asyncio.timeout

    def shortened(delay):
        return real_timeout(seconds if delay == 10.0 else delay)

    async def hangs(*_args, **_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(
        "stealth_chrome_devtools_mcp.embedded.cdp_function_executor.asyncio.timeout",
        shortened,
    )
    monkeypatch.setattr(executor, "inject_and_execute_script", hangs)


@pytest.mark.asyncio
async def test_timeout_attempts_terminate_execution(executor, monkeypatch):
    """On timeout, tab.send(terminate_execution()) is called and message updated."""
    tab = _make_fake_tab()

    _hang_until_the_bound_fires(executor, monkeypatch)

    result = await executor.execute_python_in_browser(tab, "while True: pass")

    assert result["success"] is False
    assert "terminateExecution" in result["error"]
    assert "close_instance" in result["error"]
    tab.send.assert_called_once()


@pytest.mark.asyncio
async def test_timeout_terminate_failure_swallowed(executor, monkeypatch):
    """If tab.send raises during terminate_execution, error is swallowed."""
    tab = _make_fake_tab()
    tab.send = AsyncMock(side_effect=ConnectionError("websocket dead"))

    _hang_until_the_bound_fires(executor, monkeypatch)

    result = await executor.execute_python_in_browser(tab, "x = 1")

    assert result["success"] is False
    assert "10s" in result["error"]
