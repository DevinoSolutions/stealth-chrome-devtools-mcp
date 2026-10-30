"""T-10690: a click cancelled as its inner call answers stays cancelled.

``click_element`` bounds each of its awaits (the probe's arm and read, the
synthetic click, every mouse event). On CPython 3.11 ``asyncio.wait_for`` hands
back the inner result when the outer task is cancelled after the inner future
finished but before the waiter resumed, so a ``click_element`` cancelled at that
instant (a shutdown) went on to arm the probe or send input as if nothing had
happened. 3.12+ builds ``wait_for`` on ``asyncio.timeout`` and re-raises.

Each node holds the caller inside the bounded await, then in ONE loop turn
releases the reply and cancels the caller, which is the order that loses the
cancellation on 3.11: the reply is in before the cancellation is seen. The
assertion is the same on every version (the caller ends cancelled), so it is
meaningful everywhere and RED only where ``wait_for`` is the 3.11 one.

Every wait is bounded by ``asyncio.wait`` (never ``wait_for``, which is the thing
under test), and the doubles are the repo's own: ``FakeTab`` for the tab, a real
``cdp`` command for each send, and ``page_natives.run`` replaced at its one seam.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from fakes import FakeTab
from stealth_chrome_devtools_mcp.embedded import click_target, page_natives

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

pytestmark = pytest.mark.asyncio

#: Far longer than any of these nodes takes; only a hang reaches it.
BOUND = 5.0


class Reply:
    """A reply the test holds back: the double blocks in :meth:`hold` until the
    test calls :meth:`arrive`, the way a CDP round trip blocks until Chrome
    answers. Only the first hold waits; later ones (the click's own clean-up
    reads) answer at once."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.arrived = asyncio.Event()
        self.holds = 0

    async def hold(self) -> None:
        self.holds += 1
        if self.holds > 1:
            return
        self.entered.set()
        await self.arrived.wait()

    def arrive(self) -> None:
        self.arrived.set()


async def cancelled_as_the_reply_arrives(
    reply: Reply, call: Callable[[], Awaitable[object]]
) -> asyncio.Future[object]:
    """Run *call* as the caller, wait until it is inside its bounded await, then
    in one loop turn let the reply in and cancel the caller. Returns the caller,
    finished (a caller still running after :data:`BOUND` fails the node)."""
    caller = asyncio.ensure_future(call())
    await asyncio.wait({asyncio.ensure_future(reply.entered.wait())}, timeout=BOUND)
    assert reply.entered.is_set(), "the caller never reached the bounded await"
    reply.arrive()
    caller.cancel()
    await asyncio.wait({caller}, timeout=BOUND)
    assert caller.done(), "the caller did not finish within the bound"
    return caller


def answering_natives(monkeypatch, reply: Reply, answer: object) -> None:
    async def run(element, js_function, *, user_gesture=False):
        await reply.hold()
        return answer

    monkeypatch.setattr(page_natives, "run", run)


class GatedTab(FakeTab):
    """A ``FakeTab`` whose first ``send`` is held until the reply arrives."""

    def __init__(self, reply: Reply, **kwargs) -> None:
        super().__init__(**kwargs)
        self.reply = reply

    async def send(self, cdp_obj, *args, **kwargs):
        await self.reply.hold()
        return await super().send(cdp_obj, *args, **kwargs)


ELEMENT = SimpleNamespace()


async def _position(center: tuple[float, float]) -> SimpleNamespace:
    return SimpleNamespace(center=center)


class TestACancellationLandingAfterTheAnswerIsNotLost:
    async def test_the_arm_of_the_probe(self, monkeypatch):
        reply = Reply()
        answering_natives(monkeypatch, reply, None)
        caller = await cancelled_as_the_reply_arrives(
            reply, lambda: click_target._arm(ELEMENT, ["mousedown"])
        )
        assert caller.cancelled(), "an arm cancelled as it answered went on"

    async def test_the_read_of_the_probe(self, monkeypatch):
        reply = Reply()
        answering_natives(monkeypatch, reply, "[]")
        caller = await cancelled_as_the_reply_arrives(
            reply, lambda: click_target._read(ELEMENT)
        )
        assert caller.cancelled(), "a read cancelled as it answered went on"

    async def test_the_synthetic_click(self, monkeypatch):
        reply = Reply()
        ledger = click_target.InputSent()
        answering_natives(monkeypatch, reply, click_target.SYNTHETIC_CLICKED)
        caller = await cancelled_as_the_reply_arrives(
            reply, lambda: click_target.synthetic_click(ELEMENT, "#go", ledger)
        )
        assert caller.cancelled(), "a synthetic click cancelled as it answered went on"
        assert ledger.sent, "a click that may have reached the page must say so"

    async def test_one_mouse_event(self):
        reply = Reply()
        tab = GatedTab(reply)
        ledger = click_target.InputSent()
        caller = await cancelled_as_the_reply_arrives(
            reply,
            lambda: click_target._send_mouse(tab, "mousePressed", 5.0, 5.0, ledger),
        )
        assert caller.cancelled(), "a mouse event cancelled as it answered went on"
        assert ledger.sent

    async def test_the_click_sends_no_input_after_its_arm_was_cancelled(
        self, monkeypatch
    ):
        """The whole click: the probe's arm answers as the caller is cancelled, so
        the press is never sent. On 3.11's ``wait_for`` the arm returned, the
        click went on and pressed."""
        reply = Reply()
        answering_natives(monkeypatch, reply, "[]")

        async def aimed(*_args, **_kwargs):
            return 1.0

        monkeypatch.setattr(click_target, "aim_input", aimed)
        tab = FakeTab()
        element = SimpleNamespace(
            get_position=lambda: _position((10.0, 20.0)),
        )
        facts = {"point": {"x": 10, "y": 20}, "hit": {"tag": "BUTTON"}}
        ledger = click_target.InputSent()
        caller = await cancelled_as_the_reply_arrives(
            reply,
            lambda: click_target.click(tab, element, "#go", facts, ledger),
        )
        assert caller.cancelled(), "a click cancelled as its arm answered went on"
        assert "dispatch_mouse_event" not in tab.send_calls, tab.send_calls
        assert not ledger.sent
