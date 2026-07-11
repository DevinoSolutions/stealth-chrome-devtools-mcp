"""Unit tests for the asyncio close-noise filter.

When Chrome dies, nodriver's background websocket listener raises a
ConnectionClosed* error into the event loop. The handler installed by
_install_asyncio_close_noise_filter swallows those — both the clean
ConnectionClosedOK and the abnormal ConnectionClosedError from the
`websockets` package — while letting every other exception reach the default
handler. No browser required.
"""

import asyncio

import pytest

from stealth_chrome_devtools_mcp.embedded import server


def _make_exc(name, module):
    """Build an exception whose class name/module match what the filter checks."""
    cls = type(name, (Exception,), {})
    cls.__module__ = module
    return cls("boom")


async def _install_with_previous(previous_handler):
    """Install the noise filter on the running loop, return (loop, installed_handler)."""
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(previous_handler)
    server._install_asyncio_close_noise_filter()
    return loop, loop.get_exception_handler()


class TestCloseNoiseFilter:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exc_name",
        [
            "ConnectionClosedOK",  # clean close (already handled before the fix)
            "ConnectionClosedError",  # abnormal close (added by the fix)
            "ConnectionClosed",  # base class
        ],
    )
    async def test_websocket_closes_are_swallowed(self, exc_name):
        delegated = []
        loop, handler = await _install_with_previous(
            lambda l, ctx: delegated.append(ctx)
        )
        handler(
            loop,
            {
                "exception": _make_exc(exc_name, "websockets.exceptions"),
                "message": "task exception was never retrieved",
            },
        )
        assert delegated == [], f"{exc_name} from websockets should be swallowed"

    @pytest.mark.asyncio
    async def test_other_exceptions_are_delegated(self):
        delegated = []
        loop, handler = await _install_with_previous(
            lambda l, ctx: delegated.append(ctx)
        )
        handler(loop, {"exception": ValueError("a real bug"), "message": "boom"})
        assert len(delegated) == 1, (
            "non-websocket errors must reach the default handler"
        )

    @pytest.mark.asyncio
    async def test_non_websockets_connection_closed_is_delegated(self):
        """A same-named error from a different module must NOT be swallowed."""
        delegated = []
        loop, handler = await _install_with_previous(
            lambda l, ctx: delegated.append(ctx)
        )
        handler(
            loop,
            {
                "exception": _make_exc("ConnectionClosedError", "some.other.module"),
                "message": "boom",
            },
        )
        assert len(delegated) == 1
