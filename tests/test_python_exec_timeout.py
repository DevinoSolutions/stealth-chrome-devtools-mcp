"""F-164 pinning tests: execute_python_in_browser attempts
Runtime.terminateExecution on timeout and returns an honest message.

Validates:
- On timeout, tab.send is called with terminate_execution and the error
  message is updated.
- If tab.send raises, it is swallowed and the error dict is still returned.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from cdp_function_executor import CDPFunctionExecutor


@pytest.fixture
def executor():
    return CDPFunctionExecutor()


def _make_fake_tab():
    """Fake tab with a mock send."""
    tab = MagicMock()
    tab.send = AsyncMock(return_value=None)
    return tab


@pytest.mark.asyncio
async def test_timeout_attempts_terminate_execution(executor, monkeypatch):
    """On timeout, tab.send(terminate_execution()) is called and message updated."""
    tab = _make_fake_tab()

    async def raise_timeout(*_a, **_kw):
        raise TimeoutError

    monkeypatch.setattr("cdp_function_executor.asyncio.wait_for", raise_timeout)

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

    async def raise_timeout(*_a, **_kw):
        raise TimeoutError

    monkeypatch.setattr("cdp_function_executor.asyncio.wait_for", raise_timeout)

    result = await executor.execute_python_in_browser(tab, "x = 1")

    assert result["success"] is False
    assert "10s" in result["error"]
