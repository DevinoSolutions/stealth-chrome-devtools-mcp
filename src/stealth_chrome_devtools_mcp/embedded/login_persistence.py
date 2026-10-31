"""THE one home for what makes a login survive a browser close (F-937).

Two independent causes sign a persistent profile out on every relaunch, each
measured by hand on Chrome 154 / Windows 11, and each fixed here by default.

**Session cookies are dropped at every close.** Apple ID / App Store Connect,
Xero, Walmart Seller and SaaSHub keep their login in session cookies (no
expiry). Chrome writes those to the Cookies database only when the profile pref
``session.restore_on_startup`` is ``1`` ("Continue where you left off"); the
default, ``5``, drops them. The pref is MAC-protected (Secure Preferences), so
editing ``Preferences`` on disk is reverted at the next start. The one write
Chrome accepts is the one its own settings page makes, so
:func:`ensure_session_restore` opens ``chrome://settings`` and calls
``chrome.settingsPrivate.setPref``.

That exchange is needed ONCE per profile and is off the spawn's own path (T-9479).
Chrome writes the value it accepted to the profile (``Default/Secure Preferences``,
measured on Windows; ``Preferences`` where nothing is MAC-tracked), so a profile that
already says ``1`` is skipped with no tab at all, once Chrome has had time to say
whether it believes the file (below), and the exchange runs as a background task
(:func:`start_session_restore`) that a close waits for
(:func:`settle_session_restore`). It used to run inside the spawn: on the
v2.1.49 gate's 4-vCPU Windows runner, with four Chromes cold-starting at once, its
first stage (``Target.createTarget``) starved for 17 s, was cut off, and left the pref
unset, so that named session lost its session cookies at its close. The launch switch
``--restore-last-session`` was measured as the alternative and rejected (Chrome
155): with the start page on the command line (F-936) the session cookie survived
none of five headless relaunches, and without a start page one of five still lost it.

The pref has a side effect: Chrome reopens the previous run's tabs, and a
profile that was driven all day comes back with dozens. Chrome decides session
cookie restore from the pref, not from the ``Sessions`` files (measured: the
cookie survives with the directory deleted), so :func:`remove_saved_tabs` deletes
them before every launch and ``profile_copy.REGENERABLE_NAMES`` keeps them out of
every copy. A spawn still opens on the one ``about:blank`` F-936 guarantees.

**Google signs out after every relaunch.** Device Bound Session Credentials
bind the short-lived ``__Secure-1PSIDTS`` / ``__Secure-3PSIDTS`` cookies to a
TPM key that does not outlive the browser, so after a relaunch they are gone and
the account chooser says "Signed out". Two implementations run on Windows by
default, and their ``chrome://flags`` entries are either "Not available on your
platform" (the legacy one, Mac/Linux only) or already off without effect, so the
only switch that reaches them is ``--disable-features``. Names are the C++
feature identifiers with their ``k`` prefix dropped (``BASE_FEATURE`` does
``#feature.substr(1)``), read from Chromium ``154.0.8037.97``:

* ``EnableBoundSessionCredentials`` —
  ``components/signin/public/base/signin_switches.cc``, the legacy Google
  implementation (enabled by default on Windows only);
* ``DeviceBoundSessions`` — ``net/base/features.cc``, the standard implementation
  (enabled by default on Windows and Mac), with the two features that register
  sessions through it, ``DeviceBoundSessionsFederatedRegistration`` and
  ``DeviceBoundSessionsForRestrictedSites``;
* ``EnableBoundSessionCredentialsContinuity`` —
  ``bound_session_cookie_refresh_service_impl.cc``, enabled by default on
  Windows. It builds the legacy service and re-initialises a bound session
  already saved in the profile's ``Preferences`` even with
  ``EnableBoundSessionCredentials`` off, so a profile that once bound
  ``__Host-GAPS`` keeps rotating it (``RotateBoundGaps``);
* ``EnableChromeRefreshTokenBinding`` (Windows default on),
  ``EnableChromeRefreshTokenBindingUpgrade`` and
  ``EnableCookieBindingCookieUpgrade`` — ``signin_switches.cc``, the DICE-side
  binding of refresh tokens and of the Gaia cookies minted from them.

**Chrome's own sign-in is off** (F-938). Every browser here runs Account
Consistency ``DICE``: the account reconcilor compares the Gaia cookies with the
profile's token service and, finding cookies for an account that has no token
(every clone and seed, and the master after a web sign-in), logs them out
server-side. That kills the SAME session in the master. :func:`block_browser_signin`
launches with ``--allow-browser-signin=false``, which makes Account Consistency
``None`` and the reconcilor ``Inactive``; web sign-in to Google still works, it
just is not mirrored into the browser.

Chrome keeps the LAST ``--disable-features`` switch, and nodriver already emits
``--disable-features=IsolateOrigins,site-per-process`` FIRST, so the final argv
carries two. :func:`disable_dbsc` therefore puts ours last and makes it a
superset: nodriver's own names plus the DBSC ones, so the earlier switch is
overridden without losing anything.

Close to a leaf: stdlib plus ``debug_logger``, ``settings`` and the one home for
"is this profile held" (``profile_lock``). It knows nothing about sessions,
seeds or roles — every path and browser arrives as an argument.
"""

import asyncio
import contextlib
import json
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from nodriver import Browser

from stealth_chrome_devtools_mcp.embedded import profile_lock, tab_open
from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.embedded.process_cleanup import process_cleanup
from stealth_chrome_devtools_mcp.settings import get_settings

if TYPE_CHECKING:
    from stealth_chrome_devtools_mcp.embedded.models import BrowserOptions


class SettingsTab(Protocol):
    """The slice of a nodriver ``Tab`` the pref setter drives."""

    async def evaluate(
        self, expression: str, await_promise: bool = False
    ) -> object: ...

    async def get(self, url: str) -> object: ...

    async def close(self) -> None: ...


#: The features that bind Google's session cookies to the device. See the module
#: docstring for where each name was read.
DBSC_FEATURES = (
    "EnableBoundSessionCredentials",
    "DeviceBoundSessions",
    "DeviceBoundSessionsFederatedRegistration",
    "DeviceBoundSessionsForRestrictedSites",
    "EnableBoundSessionCredentialsContinuity",
    "EnableChromeRefreshTokenBinding",
    "EnableChromeRefreshTokenBindingUpgrade",
    "EnableCookieBindingCookieUpgrade",
)

#: The launch switch that turns Chrome's own sign-in off (see the docstring).
BROWSER_SIGNIN_SWITCH = "--allow-browser-signin"

#: What nodriver's ``Config.__call__`` puts in its own ``--disable-features``
#: switch. It is emitted BEFORE caller args, and Chrome keeps the LAST such
#: switch, so a merged switch that dropped these would silently re-enable site
#: isolation trials. Pinned against the installed nodriver by a test.
NODRIVER_DISABLED_FEATURES = ("IsolateOrigins", "site-per-process")

_DISABLE_FEATURES_PREFIX = "--disable-features="

#: The pref that makes Chrome persist session cookies, and the value that does.
RESTORE_PREF = "session.restore_on_startup"
RESTORE_CONTINUE = 1

#: Upper bound on the whole settings-page exchange. It runs OFF the spawn's path
#: (T-9479), so this number no longer costs a spawn anything: it only has to end a
#: task whose browser stopped answering, and it is long enough that a Chrome
#: starved by its neighbours (F-995: 3-vCPU macOS cells, three named spawns at once,
#: the 8 s then in force overrun in three runs; the 17 s of v2.1.49's Windows gate
#: with four Chromes cold-starting) finishes the exchange instead of being cut off.
#: A timeout is still not free: the pref stays unset and a named session's session
#: cookies do not survive its close, which is why the warning below names the stage
#: that stalled. It is ONE deadline for the exchange and the close of its tab
#: together.
PREF_TIMEOUT_SECONDS = 60.0

#: What a cancelled exchange is given to close its settings tab. A close waits for
#: an exchange as long as the exchange itself may take (``PREF_TIMEOUT_SECONDS``)
#: plus this, so it never gives up on one that is about to finish.
_CANCEL_GRACE_SECONDS = 2.0

#: How long after a launch the pref FILE is not evidence of the live pref. A profile
#: whose MAC Chrome rejects (restored from another machine or OS user) still has
#: ``restore_on_startup: 1`` in ``Secure Preferences`` while the live value is 5:
#: Chrome resets it in memory at load and rewrites the file about 10 s later
#: (measured, review of T-9479: 10.3 s). So a file that says 1 is believed only after
#: this long, read again; a close that comes sooner does not wait it out, it runs
#: the exchange, which reads the live value.
FILE_SETTLE_SECONDS = 20.0

#: Where :func:`start_session_restore` keeps its state: on the browser it belongs to,
#: which is what ``tab_open`` does with its per-browser state, so it lives and dies
#: with that browser and a second registry of tasks is not needed.
_STATE_ATTR = "_session_restore_state"

#: The fraction of ``PREF_TIMEOUT_SECONDS`` held back for closing the settings
#: tab when the exchange itself runs out of time. A close is one CDP call and is
#: normally instant; it only needs enough to be tried.
TAB_CLOSE_SHARE = 0.15

#: The files under a profile's ``Default`` that can hold the pref, in the order
#: Chrome trusts them.
_PREFS_FILES = ("Secure Preferences", "Preferences")

#: Chrome's saved-tabs directories under a profile's ``Default``. Not a login.
SAVED_TABS_DIRS = ("Sessions", "Sessions_Encrypted")

#: True once the settings page's privileged bindings exist. They appear when the
#: navigation commits, which can be after ``Tab.get`` returns on a slow runner
#: (F-937 CI: ``chrome.settingsPrivate`` was undefined on Windows).
_READY = "typeof chrome !== 'undefined' && !!chrome.settingsPrivate"
_READY_POLL_SECONDS = 0.25

_GET_PREF = (
    "new Promise(r => chrome.settingsPrivate.getPref("
    f"'{RESTORE_PREF}', p => r(p ? p.value : null)))"
)
_SET_PREF = (
    "new Promise(r => chrome.settingsPrivate.setPref("
    f"'{RESTORE_PREF}', {RESTORE_CONTINUE}, '', ok => r(ok === undefined ? true : ok)))"
)


def merge_disable_features(args: Sequence[str], extra: Sequence[str]) -> list[str]:
    """*args* with every ``--disable-features=`` in it folded into ONE switch that
    also names *extra*. Tokens keep their first-seen order and are not repeated,
    and nodriver's own are first, so the switch is a superset of every one it
    replaces. It goes last: Chrome keeps the last such switch, which makes it
    win over the one nodriver emits before caller args (the final argv still
    carries both)."""
    tokens: list[str] = list(NODRIVER_DISABLED_FEATURES)
    rest: list[str] = []
    for arg in args:
        if arg.lower().startswith(_DISABLE_FEATURES_PREFIX):
            tokens.extend(arg[len(_DISABLE_FEATURES_PREFIX) :].split(","))
        else:
            rest.append(arg)
    tokens.extend(extra)
    merged = list(dict.fromkeys(token.strip() for token in tokens if token.strip()))
    return [*rest, f"{_DISABLE_FEATURES_PREFIX}{','.join(merged)}"]


def disable_dbsc(args: list[str]) -> list[str]:
    """Launch args with Device Bound Session Credentials turned off, unless
    ``STEALTH_MCP_NO_DISABLE_DBSC`` opts out.

    The opt-out adds no DBSC feature but still folds a ``--disable-features`` already
    in the args (the caller's, or ``platform_utils.default_disabled_features``, which
    every Windows spawn carries, F-1010) into the one switch Chrome honors: two would
    make it keep only the last and drop nodriver's own names."""
    if get_settings().no_disable_dbsc:
        if not any(arg.lower().startswith(_DISABLE_FEATURES_PREFIX) for arg in args):
            return args
        return merge_disable_features(args, ())
    return merge_disable_features(args, DBSC_FEATURES)


def block_browser_signin(args: list[str]) -> list[str]:
    """Launch args with ``--allow-browser-signin=false`` added, unless
    ``STEALTH_MCP_ALLOW_BROWSER_SIGNIN`` opts out or the caller already passed
    the switch (the caller's value wins and it is never repeated)."""
    if get_settings().allow_browser_signin:
        return args
    if any(arg.lower().startswith(BROWSER_SIGNIN_SWITCH) for arg in args):
        return args
    return [*args, f"{BROWSER_SIGNIN_SWITCH}=false"]


def protect_logins(args: list[str]) -> list[str]:
    """Every launch switch that keeps a Google login alive, in one call."""
    return block_browser_signin(disable_dbsc(args))


def _pref_is_continue(user_data_dir: str) -> bool:
    """Whether the profile on disk already says ``session.restore_on_startup == 1``.
    False when it cannot be read.

    Chrome keeps a MAC-tracked pref in ``Default/Secure Preferences`` where it
    tracks them (Windows, macOS; measured on Chrome 155: the value is there and
    ``Preferences`` has no ``session`` key at all) and in ``Preferences`` where it
    does not, so the first file that holds the key decides."""
    for name in _PREFS_FILES:
        try:
            prefs = json.loads(
                (Path(user_data_dir) / "Default" / name).read_text(encoding="utf-8")
            )
            return prefs["session"][RESTORE_PREF.split(".", 1)[1]] == RESTORE_CONTINUE
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return False


def remove_saved_tabs(user_data_dir: str | None) -> list[str]:
    """Delete the saved-tabs directories of the profile at *user_data_dir*, so a
    launch with ``session.restore_on_startup=1`` opens on one page and not on
    every tab the last run left. Returns the names removed. Never raises: a tab
    list that cannot be deleted is a cosmetic problem, not a failed spawn.

    Nothing is deleted while a live process holds the profile: the files are
    that browser's open session. Under the opt-out, a profile whose pref is
    ALREADY ``1`` (set by an earlier run) is still cleaned, because the opt-out
    stops the library from enabling the pref, not from undoing its tab pile."""
    if not user_data_dir:
        return []
    if get_settings().no_persist_session_cookies and not _pref_is_continue(
        user_data_dir
    ):
        return []
    hold = profile_lock.profile_hold(
        Path(user_data_dir),
        getattr(process_cleanup, "_get_browser_pids_for_profile", None),
    )
    if hold is not None:
        return []
    removed = []
    for name in SAVED_TABS_DIRS:
        target = Path(user_data_dir) / "Default" / name
        if not target.is_dir():
            continue
        shutil.rmtree(target, ignore_errors=True)
        if not target.exists():
            removed.append(name)
    return removed


async def _await_settings_bindings(tab: SettingsTab) -> None:
    """Return once ``chrome.settingsPrivate`` exists in *tab*. Unbounded on its
    own: the caller's timeout is the bound."""
    while True:
        # A context torn down by the navigation is "not ready yet", not a failure.
        with contextlib.suppress(Exception):
            if await tab.evaluate(_READY) is True:
                return
        await asyncio.sleep(_READY_POLL_SECONDS)


async def _set_restore_pref(
    browser: Browser, tab_box: list[SettingsTab], stage: list[str]
) -> bool:
    """The body of :func:`ensure_session_restore`. The tab is opened on
    ``about:blank`` and put into *tab_box* BEFORE it navigates, so the caller can
    close it even when this coroutine is cancelled mid-await. The last entry of
    *stage* names the await it is in, so a timeout can say where it stalled."""
    stage.append("opening the settings tab")
    # Behind the caller's page: in front it makes a headless page read its window
    # as its viewport (``outerHeight`` 513 for 600) for as long as it is open (T-9479).
    tab = await tab_open.open_tab(browser, "about:blank", background=True)
    tab_open.mark_internal(browser, tab)
    tab_box.append(tab)
    stage.append("loading chrome://settings")
    await tab.get("chrome://settings")
    stage.append("waiting for the settings bindings")
    await _await_settings_bindings(tab)
    stage.append("reading the pref")
    current = await tab.evaluate(_GET_PREF, await_promise=True)
    if current == RESTORE_CONTINUE:
        return True
    stage.append("writing the pref")
    accepted = await tab.evaluate(_SET_PREF, await_promise=True)
    stage.append("re-reading the pref")
    current = await tab.evaluate(_GET_PREF, await_promise=True)
    if accepted is True and current == RESTORE_CONTINUE:
        return True
    debug_logger.log_warning(
        "login_persistence",
        "ensure_session_restore",
        f"{RESTORE_PREF} is {current!r} after setPref(accepted={accepted!r}); "
        "session cookies will not survive a close",
    )
    return False


async def ensure_session_restore(browser: Browser) -> bool:
    """Set ``session.restore_on_startup`` to ``1`` through ``chrome://settings``
    and report whether it is ``1`` afterwards. It reads the LIVE value, which the
    profile's file may not be telling the truth about (``FILE_SETTLE_SECONDS``).
    Idempotent: it reads first and writes only when the value differs. Never
    raises, never takes longer than
    ``PREF_TIMEOUT_SECONDS`` IN TOTAL (a CDP await that never resolves is not an
    exception), and always closes the settings tab it opened.

    One deadline covers the exchange AND the close of its tab: the exchange gets
    ``1 - TAB_CLOSE_SHARE`` of it, and the close gets whatever is left, so a
    stuck exchange followed by a stuck close still costs one bound and not two
    (F-995: this once ran inside the spawn's post-launch phase, whose own budget
    turned an overrun into a failed spawn)."""
    if get_settings().no_persist_session_cookies:
        return False
    tab_box: list[SettingsTab] = []
    stage: list[str] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + PREF_TIMEOUT_SECONDS
    close_share = PREF_TIMEOUT_SECONDS * TAB_CLOSE_SHARE
    exchange_bound = PREF_TIMEOUT_SECONDS - close_share
    cut_short = False
    try:
        async with asyncio.timeout(exchange_bound):
            return await _set_restore_pref(browser, tab_box, stage)
    except TimeoutError:
        cut_short = True
        where = stage[-1] if stage else "starting"
        debug_logger.log_warning(
            "login_persistence",
            "ensure_session_restore",
            f"setting {RESTORE_PREF} stalled while {where} and was cut off "
            f"after {exchange_bound:.1f}s; session cookies may not survive a close",
        )
    except Exception as err:  # noqa: BLE001  PERMANENT(F-937): any CDP failure is a warning, never a failed spawn
        debug_logger.log_warning(
            "login_persistence",
            "ensure_session_restore",
            f"could not set {RESTORE_PREF}: {err}",
        )
    except asyncio.CancelledError:
        cut_short = True  # a close gave up on us: the tab still gets its share
        raise
    finally:
        for tab in tab_box:
            # An exchange that ran out of time has spent exactly its own bound,
            # so what is left IS the held-back share: stated, not read off a
            # clock that can tick a whole quantum late (Windows' is ~15.6 ms,
            # which made a stuck pair overrun the deadline). An exchange that
            # ended early leaves the rest of the deadline, never less than the
            # share, because a bound of zero would cancel the close at its first await.
            close_bound = (
                close_share if cut_short else max(deadline - loop.time(), close_share)
            )
            try:
                async with asyncio.timeout(close_bound):
                    await tab.close()
            except Exception as err:  # noqa: BLE001  PERMANENT(F-937): a tab that will not close is a warning
                debug_logger.log_warning(
                    "login_persistence",
                    "ensure_session_restore",
                    f"could not close the settings tab: {err!r}",
                )
    return False


@dataclass(eq=False)
class _Pending:
    """One browser's exchange: its task, and the signal that its close has come."""

    task: asyncio.Task[bool]
    closing: asyncio.Event


async def _keep_session_restore(
    browser: Browser, user_data_dir: str, closing: asyncio.Event
) -> bool:
    """The task: believe the profile's file only when Chrome has had time to
    contradict it (``FILE_SETTLE_SECONDS``), otherwise run the exchange. A close
    that arrives first sets *closing*, which ends the wait early and means the
    exchange, because the file is not yet evidence."""
    if not get_settings().no_persist_session_cookies and _pref_is_continue(
        user_data_dir
    ):
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(FILE_SETTLE_SECONDS):
                await closing.wait()
        if not closing.is_set() and _pref_is_continue(user_data_dir):
            return True
    return await ensure_session_restore(browser)


def start_session_restore(browser: Browser, options: "BrowserOptions") -> None:
    """Begin keeping ``session.restore_on_startup`` at ``1`` for a spawned or
    re-attached browser WITHOUT making the caller wait for it (T-9479). Only a
    persistent profile the caller named has anything to keep: a clone is
    disposable, and an unnamed spawn is a clone.

    The state is kept on *browser*, so :func:`settle_session_restore` finds it and
    the task cannot be garbage-collected mid-run (RUF006)."""
    if not options.user_data_dir or options.auto_clone:
        return
    closing = asyncio.Event()
    task = asyncio.create_task(
        _keep_session_restore(browser, options.user_data_dir, closing)
    )
    setattr(browser, _STATE_ATTR, _Pending(task, closing))


async def settle_session_restore(browser: Browser) -> None:
    """Before a close ends *browser*: let the pending exchange finish, so the pref
    is in place before the profile's last shutdown. It waits as long as the
    exchange may itself take, then cancels it (it closes its own settings tab) and
    goes on. Call it BEFORE the connection is marked closed, because the exchange
    needs the connection. No-op when nothing is pending.

    **A close that is cancelled while it waits kills nothing** (T-11521), the way
    ``process_exit.settle``'s cancelled grace does not: this runs on a profile the
    caller named, whose cookie store Chrome is still committing, and a terminate
    before Chrome was ever asked to leave is the F-910 login loss. The exchange is
    cancelled, the connection marked closed, and the cancellation re-raised at once;
    ``close_instance`` hands the browser it already claimed to the held background
    task of T-10842, which asks Chrome to leave, gives it its own exit and only then
    runs Phase 3."""
    state = getattr(browser, _STATE_ATTR, None)
    if not isinstance(state, _Pending) or state.task.done():
        return
    state.closing.set()
    try:
        _, pending = await asyncio.wait(
            {state.task}, timeout=PREF_TIMEOUT_SECONDS + _CANCEL_GRACE_SECONDS
        )
        if not pending:
            return
        state.task.cancel()
        await asyncio.wait({state.task}, timeout=_CANCEL_GRACE_SECONDS)
    except asyncio.CancelledError:  # in either wait: the second one used to leak
        state.task.cancel()
        tab_open.mark_closed(browser)
        raise
    debug_logger.log_warning(
        "login_persistence",
        "settle_session_restore",
        f"{RESTORE_PREF} was still being set after "
        f"{PREF_TIMEOUT_SECONDS + _CANCEL_GRACE_SECONDS:g}s; cancelled it to close "
        "the browser, so this profile's session cookies may not survive the close",
    )
