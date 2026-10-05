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
from pathlib import Path
from typing import Protocol

from stealth_chrome_devtools_mcp.embedded import profile_lock
from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.embedded.process_cleanup import process_cleanup
from stealth_chrome_devtools_mcp.settings import get_settings


class SettingsTab(Protocol):
    """The slice of a nodriver ``Tab`` the pref setter drives."""

    async def evaluate(
        self, expression: str, await_promise: bool = False
    ) -> object: ...

    async def get(self, url: str) -> object: ...

    async def close(self) -> None: ...


class PrefBrowser(Protocol):
    """The slice of a nodriver ``Browser`` the pref setter drives."""

    async def get(self, url: str, new_tab: bool = False) -> SettingsTab: ...


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

#: Upper bound on the whole settings-page exchange. ``chrome://settings`` is the
#: slowest page a spawn touches; a hang here must cost seconds, not the spawn.
PREF_TIMEOUT_SECONDS = 8.0

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
    ``STEALTH_MCP_NO_DISABLE_DBSC`` opts out."""
    if get_settings().no_disable_dbsc:
        return args
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
    """Whether the profile's own ``Preferences`` already says
    ``session.restore_on_startup == 1``. False when it cannot be read."""
    try:
        prefs = json.loads(
            (Path(user_data_dir) / "Default" / "Preferences").read_text(
                encoding="utf-8"
            )
        )
        return prefs["session"][RESTORE_PREF.split(".", 1)[1]] == RESTORE_CONTINUE
    except (OSError, ValueError, KeyError, TypeError):
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


async def _set_restore_pref(browser: PrefBrowser, tab_box: list[SettingsTab]) -> bool:
    """The body of :func:`ensure_session_restore`. The tab is opened on
    ``about:blank`` and put into *tab_box* BEFORE it navigates, so the caller can
    close it even when this coroutine is cancelled mid-await."""
    tab = await browser.get("about:blank", new_tab=True)
    tab_box.append(tab)
    await tab.get("chrome://settings")
    await _await_settings_bindings(tab)
    current = await tab.evaluate(_GET_PREF, await_promise=True)
    if current == RESTORE_CONTINUE:
        return True
    accepted = await tab.evaluate(_SET_PREF, await_promise=True)
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


async def ensure_session_restore(browser: PrefBrowser) -> bool:
    """Set ``session.restore_on_startup`` to ``1`` through ``chrome://settings``
    and report whether it is ``1`` afterwards. Idempotent: it reads first and
    writes only when the value differs. Never raises, never takes longer than
    ``PREF_TIMEOUT_SECONDS`` (a CDP await that never resolves is not an
    exception), and always closes the settings tab it opened."""
    if get_settings().no_persist_session_cookies:
        return False
    tab_box: list[SettingsTab] = []
    try:
        return await asyncio.wait_for(
            _set_restore_pref(browser, tab_box), PREF_TIMEOUT_SECONDS
        )
    except TimeoutError:
        debug_logger.log_warning(
            "login_persistence",
            "ensure_session_restore",
            f"setting {RESTORE_PREF} took over {PREF_TIMEOUT_SECONDS}s; "
            "session cookies may not survive a close",
        )
    except Exception as err:  # noqa: BLE001  PERMANENT(F-937): any CDP failure is a warning, never a failed spawn
        debug_logger.log_warning(
            "login_persistence",
            "ensure_session_restore",
            f"could not set {RESTORE_PREF}: {err}",
        )
    finally:
        for tab in tab_box:
            try:
                await asyncio.wait_for(tab.close(), PREF_TIMEOUT_SECONDS)
            except Exception as err:  # noqa: BLE001  PERMANENT(F-937): a tab that will not close is a warning
                debug_logger.log_warning(
                    "login_persistence",
                    "ensure_session_restore",
                    f"could not close the settings tab: {err!r}",
                )
    return False
