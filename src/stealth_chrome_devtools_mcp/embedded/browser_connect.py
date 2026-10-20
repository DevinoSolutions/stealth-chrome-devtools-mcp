"""F-834 stage 2 — a Chrome we launched that has not opened its DevTools
endpoint YET is not a Chrome that failed to start.

THE one home for "how long does a freshly launched Chrome get to answer
``/json/version`` — and then to open its first page — before this spawn attempt
is called failed" — ``install()``, ``installed()``, ``first_page()``,
``CONNECT_PATIENCE_SECONDS`` and nothing else.

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
``connect_existing`` branch, which since F-888 has ONE home, ``cdp_attach``, and
two consumers through it (``desktop_launch``'s delegated launch and
``browser_reattach``'s adoption of a browser a dead backend left running) — and
such a call gets nodriver's own window unchanged. That is not a caution,
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

**F-1003 — the endpoint answering is not the first page existing.** The same
question has a second milestone one step later. ``Browser.start`` reads the
target list exactly ONCE (``update_targets()``) right after ``/json/version``
answers, and ``Browser.main_tab`` is ``sorted(self.targets, key=type_ == "page",
reverse=True)[0]``. On a large real profile (275-871 MB, extensions,
restore-on-startup) Chrome answers ``/json/version`` BEFORE it has created its
first page target, so ``targets`` is empty and ``main_tab`` raised
``IndexError: list index out of range`` — the attempt was torn down, F-860 killed
a Chrome that was coming up fine, and ``_SPAWN_ATTEMPTS`` relaunched from cold
(measured live: two such retries before the third attempt succeeded, each failed
attempt's Chrome log showing it reaching its post-start point as it was killed).
Its latent shape: when the list holds only non-page targets (``background_page``,
``service_worker``, ``browser_ui`` — measured present on those profiles at
startup) ``main_tab`` does not raise, it silently returns a NON-page target as
the instance's tab. ``first_page()`` re-reads the list with nodriver's own
``update_targets()`` until a ``page`` target exists, then answers ``main_tab``;
it polls at ``POLL_SECONDS`` through the same ``_now``/``_sleep`` seam and ends at
once if the launched Chrome has exited. **It is a SECOND, sequential wait, not a
share of the first**: its ``CONNECT_PATIENCE_SECONDS`` starts when the launch
returns, i.e. after the endpoint wait has spent whatever it spent, so one slow
attempt can wait up to twice the ceiling. The two are the same "Chrome is still
coming up" and so use the one constant — no second constant, no knob. It is also
bounded by ``spawn_budget``'s ``POST_LAUNCH_BUDGET_SECONDS`` phase, because
nodriver's ``update_targets`` carries no deadline of its own and a wedged
websocket would otherwise park the spawn for ever (F-969's whole subject).

Who is waited for is decided by the CALLER, not by ``_process``: ``first_page``
is for a launch of ours and ``spawn_browser`` is its one caller. That includes
the F-810 DELEGATED headed launch, which ``cdp_attach`` connects to (so nodriver
leaves ``_process`` ``None`` and only ``_process_pid`` is stamped) but which is
every bit as fresh a Chrome — its liveness is ``process_exit.browser_is_alive``,
the one rule for "is the process behind this browser still running", read off
``_process`` or the pid. ``browser_reattach``'s adoption of a browser a dead
backend left running is an ATTACH to an already-open Chrome, reads ``main_tab``
itself and names its own ``IndexError``; it never calls this.

**F-1009 — and the endpoint that answers is not necessarily OURS.** nodriver
chooses the port by binding and releasing a socket, then launches Chrome on it; a
sibling that takes the port in the gap answers ``/json/version`` in place of the
Chrome we launched, and the spawn registered the sibling's browser (see
``devtools_port``). :class:`LaunchConfig` is the ``uc.Config`` of a launch of
ours: its argv asks Chrome for port ``0``, and the patience above, once the
launch is one of ours, waits for the port Chrome wrote to ``DevToolsActivePort``
in OUR profile, points nodriver's ``HTTPApi`` at THAT port and refuses an answer
whose websocket is not the one the file names. nodriver swallows every exception
out of that fetch, so the named reason is kept on the config and :func:`start`
raises it in place of nodriver's "Failed to connect to browser".

A leaf: ``nodriver``, stdlib, ``process_exit``, ``spawn_budget`` and
``devtools_port`` (leaves of their own). ``install()`` is called from
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

import nodriver
from nodriver.core.browser import Browser, HTTPApi
from nodriver.core.config import Config
from nodriver.core.tab import Tab
from nodriver.core.util import get_registered_instances

from stealth_chrome_devtools_mcp.embedded import (
    devtools_port,
    process_exit,
    spawn_budget,
)
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

__all__ = [
    "CONNECT_PATIENCE_SECONDS",
    "LaunchConfig",
    "NoFirstPageError",
    "first_page",
    "install",
    "installed",
    "start",
]

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


class NoFirstPageError(Exception):
    """A Chrome we launched answered its endpoint but opened no page (F-1003)."""


class LaunchConfig(Config):
    """The ``uc.Config`` of a launch of OURS: Chrome picks its own DevTools port.

    ``failure`` is the named reason the connect gave up, left for :func:`start`
    because nodriver discards the exceptions its fetch raises (F-1009).
    """

    failure: ToolError | None = None

    def __call__(self) -> list[str]:
        return devtools_port.with_port_zero(super().__call__())


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
    #: Set when the launch is ours (a :class:`LaunchConfig`): the port is Chrome's.
    config: LaunchConfig | None = None


def _now() -> float:
    """The single timing seam, with ``_sleep`` (the ``scroll_position`` pattern)."""
    return time.monotonic()


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _launched(api: HTTPApi) -> Browser | None:
    """The ``Browser`` whose ``Browser.start`` built this endpoint, or ``None``.

    ``Browser.start`` sets ``_process`` before it builds the ``HTTPApi`` and
    registers itself before it starts polling, so a launch is always findable
    from here. A ``None`` ``_process`` on what is found means the browser was
    ATTACHED to rather than launched — ``connect_existing``, i.e. every caller of
    ``cdp_attach`` — and that is the whole reason this lookup exists rather than
    a bare deadline.
    """
    for browser in tuple(get_registered_instances()):
        if getattr(browser, "_http", None) is api:
            return browser
    return None


def _standing_wait(api: HTTPApi) -> _Wait:
    browser = _launched(api)
    config = getattr(browser, "config", None)
    return _Wait(
        getattr(browser, "_process", None),
        _now() + CONNECT_PATIENCE_SECONDS,
        config if isinstance(config, LaunchConfig) else None,
    )


_Get = Callable[[HTTPApi, str], Awaitable[object]]


async def _own_endpoint_get(
    original: _Get, api: HTTPApi, endpoint: str, wait: _Wait, config: LaunchConfig
) -> object:
    """``/json/version`` of the Chrome THIS launch started (F-1009).

    The port is whatever that Chrome wrote to ``DevToolsActivePort`` in its own
    profile, not the one nodriver picked, and the answer must carry the websocket
    the file names. Every give-up leaves its reason on *config* before it raises,
    because ``Browser.start`` swallows what this raises and says only "Failed".
    """
    profile = config.user_data_dir
    while True:
        found = devtools_port.own_endpoint(profile)
        if found is not None:
            api.port = config.port = found.port
            api.api = f"http://{api.host}:{found.port}"
            try:
                answer = await original(api, endpoint)
            except _ANSWERED:
                raise
            except _NOT_OPEN_YET:
                pass
            else:
                reported = (
                    answer.get("webSocketDebuggerUrl")
                    if isinstance(answer, dict)
                    else None
                )
                try:
                    devtools_port.verify(str(reported or ""), found)
                except ToolError as refusal:
                    config.failure = refusal
                    raise
                return answer
        if wait.process.returncode is not None:
            config.failure = devtools_port.exited(profile)
            raise config.failure
        if _now() >= wait.deadline:
            if found is None:
                config.failure = devtools_port.missing(
                    profile, CONNECT_PATIENCE_SECONDS
                )
            raise config.failure or TimeoutError(endpoint)
        await _sleep(POLL_SECONDS)


async def start(config: LaunchConfig) -> Browser:
    """``nodriver.start`` for a launch of ours: no stale port file, a named failure.

    The only caller is ``browser_manager._launch_browser``. A leftover
    ``DevToolsActivePort`` is removed first (it is some earlier Chrome's), and a
    failed connect raises the reason :func:`_own_endpoint_get` recorded.
    """
    devtools_port.clear_stale(config.user_data_dir)
    try:
        return await nodriver.start(config=config)
    except Exception as error:
        if config.failure is not None:
            raise config.failure from error
        raise


def _patient(original: _Get) -> _Get:
    async def get(self: HTTPApi, endpoint: str) -> object:
        """Ask the DevTools endpoint, waiting out a Chrome still opening it."""
        if endpoint != VERSION_ENDPOINT:
            return await original(self, endpoint)
        wait: _Wait | None = getattr(self, _WAIT, None)
        if wait is None:
            wait = _standing_wait(self)
            setattr(self, _WAIT, wait)
        if wait.process is None:
            return await original(self, endpoint)
        if wait.config is not None:
            return await _own_endpoint_get(original, self, endpoint, wait, wait.config)
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


@spawn_budget.phase("first-page wait", "POST_LAUNCH_BUDGET_SECONDS")
async def first_page(browser: Browser) -> Tab:
    """The tab of a Chrome WE launched, once it has opened its first page.

    ``Browser.start`` has read the target list once by now, and a Chrome that is
    still restoring a large profile has not created a page target yet. Ask again
    (nodriver's own ``update_targets``) until one exists, bounded by
    ``CONNECT_PATIENCE_SECONDS``; a launcher that has exited ends it at once.
    """
    deadline = _now() + CONNECT_PATIENCE_SECONDS
    while not any(target.type_ == "page" for target in browser.targets):
        if not process_exit.browser_is_alive(browser):
            raise NoFirstPageError(
                "Chrome answered on its DevTools endpoint but exited "
                "before opening a page"
            )
        if _now() >= deadline:
            raise NoFirstPageError(
                "Chrome answered on its DevTools endpoint but opened no page "
                f"within {CONNECT_PATIENCE_SECONDS:g} s"
            )
        await _sleep(POLL_SECONDS)
        await browser.update_targets()
    return browser.main_tab
