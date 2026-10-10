"""THE per-directory gate two spawns naming one profile directory serialize on.

One gate per requested DIRECTORY, because the directory is what two concurrent
spawns collide on: without it both read the same live holder, both attach, and
one Chrome is registered as two instances that then close each other. Keyed on
the normalized path through the record's own ``normalize_path``, so ``C:\\P`` and
``c:\\p\\`` are one gate.

Deliberately NOT ``clone_storage``'s protected-dir set: that is sweep EXEMPTION --
a membership test with no waiting -- and a mutex is not what it offers, so
reusing it would mean building the exclusion beside it anyway. And deliberately
per directory rather than ``browser_reattach``'s module-wide record-pass lock: an
unrelated spawn must not wait out a wedged browser's whole ``ATTACH_BUDGET_SECONDS``.

The gate is in-PROCESS only, and is not what makes adoption safe; that is
``browser_reattach.claim``, which two backends and two tasks alike must pass. This
only keeps the common case cheap by not sending a second task down a path the
claim will refuse.

Two holders (F-961):

* ``browser_reattach.adopt_held_profile`` holds it for the question "does a
  browser hold this directory?".
* ``spawn_browser`` holds it for the WHOLE of a named spawn, question and launch
  together (:func:`hold`). The question alone was not enough: two chats asking for
  a just-closed ``fleet`` at once both read "nothing holds it", both launched, and
  one signed-in profile was driven by two instances. Held across the launch, the
  second reads the first's browser and is handed it.

So the same task takes it twice, and an ``asyncio.Lock`` is not reentrant: the gate
counts how many times the CURRENT context holds it (a ``ContextVar``, so a child
task the holder starts inherits the hold) and only the outermost acquire waits and
the outermost release lets go.
"""

import asyncio
from contextvars import ContextVar
from types import MappingProxyType

from stealth_chrome_devtools_mcp.embedded import browser_pid_registry

_EMPTY: "MappingProxyType[str, int]" = MappingProxyType({})
#: gate key -> how many times THIS context holds it. Replaced, never mutated:
#: a context's copy is shared with every task it spawns.
_holds: ContextVar["MappingProxyType[str, int]"] = ContextVar(
    "directory_gate_holds", default=_EMPTY
)


class Gate:
    """A reentrant (per context) lock on one profile directory."""

    def __init__(self, key: str) -> None:
        self.key = key
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        holds = _holds.get()
        depth = holds.get(self.key, 0)
        if depth == 0:
            await self._lock.acquire()
        _holds.set(MappingProxyType({**holds, self.key: depth + 1}))

    def release(self) -> None:
        holds = dict(_holds.get())
        depth = holds.pop(self.key, 0) - 1
        if depth > 0:
            holds[self.key] = depth
        else:
            self._lock.release()
        _holds.set(MappingProxyType(holds))

    async def __aenter__(self) -> "Gate":
        await self.acquire()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self.release()


class _NoGate(Gate):
    """The gate of a spawn that names no directory: it lands on a disposable copy
    of its own and has nothing to share, so it neither waits nor makes others wait."""

    def __init__(self) -> None:
        super().__init__("")

    async def acquire(self) -> None:
        return None

    def release(self) -> None:
        return None


_gates: dict[str, Gate] = {}


def gate_for(user_data_dir: str) -> Gate:
    """THE gate for *user_data_dir*, whatever way it is spelled."""
    key = browser_pid_registry.normalize_path(user_data_dir) or user_data_dir
    gate = _gates.get(key)
    if gate is None:
        # No await between the miss and the insert, so this is atomic for the
        # loop; two tasks cannot both create one.
        gate = _gates.setdefault(key, Gate(key))
    return gate


async def hold(user_data_dir: str | None) -> Gate:
    """Take the gate of *user_data_dir* and return it; the caller ``release``s it.
    A spawn that names no directory gets a gate that holds nothing."""
    gate = gate_for(user_data_dir) if user_data_dir else _NoGate()
    await gate.acquire()
    return gate
