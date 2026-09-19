"""F-834 stage 2 — a Chrome we launched that has not opened its DevTools
endpoint YET is not a Chrome that failed to start.

THE one home for "how long does a freshly launched Chrome get to answer
``/json/version`` before this spawn attempt is called failed" — ``install()``,
``installed()``, ``CONNECT_PATIENCE_SECONDS`` and nothing else.

The deadline being extended is nodriver's and it is FIXED. ``Browser.start``
spawns Chrome and then::

    await asyncio.sleep(0.25)
    for _ in range(5):
        try:
            self.info = ContraDict(await self._http.get("version"), silent=True)
        except (Exception,):
            ...
            await self.sleep(0.5)
        else:
            break
    if not self.info:
        raise Exception("Failed to connect to browser")

0.25 s plus four 0.5 s sleeps is **2.75 s of WAITING**, it is a loop count in
library source with no config field behind it, and Chrome routinely misses it.
The wall-clock window is that plus what five refusals cost, which is the
platform's: near-instant on POSIX (F-870 §1.3 measured the CI hits as
"near-instant refusals, not timeouts"), so ≈2.75 s there — which is the macOS
cell this finding is about — but **2023-2060 ms per refusal on Windows**
(measured locally, six fetches, a never-bound port and nodriver's own
reserved-then-closed idiom alike), i.e. ≈12.9 s, which is why the Windows cell's
4250 ms cold start does not fail. A port that LISTENS but never answers costs
``urlopen``'s own 10 s per attempt on every platform (measured), i.e. ≈52.5 s —
a shape nothing here makes better or worse.

The F-870 cold-start probe runs on every gate cell before the suite, with the
runner otherwise IDLE and launching exactly ONE Chrome, and reports
``ms_to_json_version``:

===============  =============  ==============  =================
cell             gate run       first (cold) ms  second (warm) ms
===============  =============  ==============  =================
macOS/ARM64      35304880367        3943.6            581.6
macOS/ARM64      35316298288        4786.0           1289.7
macOS/ARM64      35454765486        5839.1           2138.7
Windows/X64      35316298288        4250.0            344.0
Linux/X64        35316298288         685.8            232.6
===============  =============  ==============  =================

Two of the three cells have NEVER answered inside 2.75 s on a cold binary, and
the warmest macOS reading (2138.7 ms) already spends 78 % of that budget on one
launch with nothing else running. What that costs in production is one line in
the log and one wasted Chrome: the attempt raises "Failed to connect", the
``Browser`` object is never handed back, ``spawn_leak.reap_launched_browsers``
correctly kills the Chrome that IS coming up (F-860, a WARNING on the backend's
durable channel), and ``_SPAWN_ATTEMPTS`` launches another one from cold. On the
six-way fleet (``tests/test_e2e_fleet.py``, "spawn 26.0s (1 lead + 5 in 3 lanes
on 3 cpus)") a follower loses that race often enough to have failed four of the
last five macOS gate runs.

**The seam is ``HTTPApi.get``, and it is the only place that sits between
"Chrome is spawned" and "the endpoint answers".** Nothing else can be: the
process does not exist until ``Browser.start`` creates it, and by the time
``start`` returns or raises the decision is already made. ``HTTPApi.get`` has
exactly ONE caller in nodriver 0.47 — the ``version`` fetch above — so patching
it reaches that decision and nothing else. It is still keyed on the endpoint
name, so a future nodriver that grows a second ``get`` caller does not inherit
this patience silently.

Deliberately NOT, and each was tried against the source first:

* Re-calling ``Browser.start``. It refuses re-entry while ``_process`` is live
  ("ignored! this call has no effect when already running") and clearing that
  guard makes it spawn a SECOND Chrome.
* Recovering afterwards with ``Browser.create(host=…, port=…)``. That path works
  — nodriver mutates the caller's ``Config`` with the host and the port it
  chose, so both are in hand after the failure — but ``connect_existing`` leaves
  ``_process`` None, and a ``Browser`` that cannot kill its own Chrome is a leak
  with better manners.
* Setting ``config.host``/``config.port`` ourselves so nodriver attaches instead
  of launching. Then the launch is OURS, which is a second home for spawning
  Chrome (convention 4).
* Copying ``Browser.start`` and widening the loop. A double of a library
  function in the one path every browser takes.
* A ``STEALTH_MCP_*`` knob. The number is a property of how long Chrome takes to
  open a socket, which is measured above, not of an operator's taste.

**The ceiling is for LAUNCHES WE OWN, and the witness decides which those are.**
``_launched_process`` resolves the owning browser once per ``HTTPApi`` and reads
nodriver's own ``_process``; ``None`` means nobody here launched it — the
``connect_existing`` branch, i.e. F-810's delegated launch and F-888's re-attach
— and such a call gets nodriver's own window unchanged. That is not a caution,
it is what the measurement says: an ATTACH targets an endpoint that is already
open, and a `/json/version` fetch against a live one takes **0.78 ms median**
(measured locally, ten fetches; min 0.51, max 170.8 on the first). nodriver's
five attempts are already ~1000x the healthy cost there, so patience buys
nothing and would only make a stale recorded port expensive on a user-facing
call. There is no second constant for the attach door, deliberately: the
cheapest honest answer was no change at all.

The patience is charged ONCE per launch, not once per call: the deadline is
stamped on the ``HTTPApi`` instance, which nodriver builds fresh in each
``start``. nodriver's four remaining attempts therefore see a spent deadline,
fail immediately and add only their own 2 s of sleeps to the worst case.

A launcher that has already EXITED short-circuits the wait, and so does one that
exits mid-wait — the ``returncode`` is re-read every pass, not once — so the one
case this could have made slower, Chrome dying at startup, is instead faster
than before: today that still costs the full window of polling a port nobody
will ever open.

A leaf: ``nodriver`` and stdlib only. ``install()`` is called from
``browser_manager._launch_browser``, the one function in the tree that launches
a browser, on ``session_hygiene.install()``'s precedent of installing at the
site that needs it rather than at import time — and it is idempotent anyway.
"""

import asyncio
import http.client
import json
import time
import urllib.error
from collections.abc import Awaitable, Callable
from typing import NamedTuple, Protocol

from nodriver.core.browser import HTTPApi
from nodriver.core.util import get_registered_instances

__all__ = ["CONNECT_PATIENCE_SECONDS", "install", "installed"]

#: How long a freshly launched Chrome gets to open its DevTools endpoint before
#: the attempt is called failed. ~5x the worst MEASURED cold launch (5839.1 ms,
#: macOS/ARM64, gate run 35454765486) and ~14x the worst warm one (2138.7 ms) —
#: the margin is for the load the fleet applies, six Chromes launching in three
#: lanes on three cpus, which no single-launch probe measures. It is a ceiling
#: and not a wait: an endpoint that opens in 300 ms costs 300 ms.
CONNECT_PATIENCE_SECONDS = 30.0

#: How often the endpoint is asked again once a refusal has come back. This is
#: NOT the cadence — what a refusal itself costs dominates it and is the
#: platform's, not ours: near-instant on POSIX (F-870 §1.3 measured the CI
#: refusals that way) and 2023-2060 ms on Windows, measured locally over six
#: refused fetches against both a never-bound port and nodriver's own
#: reserved-then-closed idiom. So this only sets how promptly a POSIX wait
#: notices an endpoint that has just opened.
POLL_SECONDS = 0.1

#: The one endpoint this patience applies to — nodriver's single ``get`` caller.
VERSION_ENDPOINT = "version"

# What "the endpoint is not open yet" looks like out of ``urllib``: a refused or
# reset connection and a read timeout are ``OSError`` (``URLError`` included), a
# half-written answer is an ``HTTPException``, and a truncated body fails to
# parse. ``HTTPError`` is deliberately EXCLUDED even though it is a ``URLError``
# is an ``OSError``: a server that answered — a squatter on the port, a 500 — is
# not a socket that is not open yet, and waiting out the ceiling on one would
# turn a 2.75 s failure into a 30 s one. Anything else propagates on the first
# try, exactly as it does today.
_NOT_OPEN_YET = (OSError, http.client.HTTPException, json.JSONDecodeError)
_ANSWERED = urllib.error.HTTPError

# The marker lives on the wrapper, so "is the patience in place" is a question
# about the function Python will actually call, and the original stays reachable
# for the sensitivity control in the pins.
_MARKER = "__stealth_browser_connect__"

# Per-launch, because nodriver builds one ``HTTPApi`` per ``Browser.start``.
_WAIT = "__stealth_connect_wait__"


class _Launched(Protocol):
    """The one thing this module asks of nodriver's process handle."""

    returncode: int | None


class _Wait(NamedTuple):
    """This endpoint's standing answer, decided once per ``HTTPApi``.

    ``process is None`` is "no launch of ours is behind this endpoint", which is
    the ``connect_existing`` door and takes nodriver's own window.
    """

    process: _Launched | None
    deadline: float


def _now() -> float:
    """The single timing seam, with ``_sleep`` (the ``scroll_position`` pattern)."""
    return time.monotonic()


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _launched_process(api: HTTPApi) -> _Launched | None:
    """The process WE launched behind this endpoint, or ``None``.

    ``Browser.start`` sets ``_process`` before it builds the ``HTTPApi`` and
    registers itself before it starts polling, so a launch is always findable
    from here. ``None`` means the browser was ATTACHED to rather than launched —
    ``connect_existing``, i.e. F-810's delegated launch and F-888's re-attach —
    and that is the whole reason this lookup exists rather than a bare deadline.
    """
    for browser in tuple(get_registered_instances()):
        if getattr(browser, "_http", None) is api:
            return getattr(browser, "_process", None)
    return None


_Get = Callable[[HTTPApi, str], Awaitable[object]]


def _patient(original: _Get) -> _Get:
    async def get(self: HTTPApi, endpoint: str) -> object:
        """Ask the DevTools endpoint, waiting out a Chrome still opening it."""
        if endpoint != VERSION_ENDPOINT:
            return await original(self, endpoint)
        wait: _Wait | None = getattr(self, _WAIT, None)
        if wait is None:
            wait = _Wait(_launched_process(self), _now() + CONNECT_PATIENCE_SECONDS)
            setattr(self, _WAIT, wait)
        if wait.process is None:
            return await original(self, endpoint)
        while True:
            try:
                return await original(self, endpoint)
            except _ANSWERED:
                raise
            except _NOT_OPEN_YET:
                if _now() >= wait.deadline or wait.process.returncode is not None:
                    raise
            await _sleep(POLL_SECONDS)

    setattr(get, _MARKER, original)
    return get


def installed() -> bool:
    """Is the patience in place on the class nodriver will construct?"""
    return hasattr(HTTPApi.get, _MARKER)


def install() -> None:
    """Give every launched Chrome our connect budget, not nodriver's. Idempotent."""
    if installed():
        return
    HTTPApi.get = _patient(HTTPApi.get)
