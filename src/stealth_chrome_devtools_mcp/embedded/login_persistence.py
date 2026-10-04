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
  ``DeviceBoundSessionsForRestrictedSites``.

Chrome honors ONE ``--disable-features`` switch, and nodriver already emits
``--disable-features=IsolateOrigins,site-per-process``, so :func:`disable_dbsc`
folds every value into a single switch instead of appending a second.

A leaf: stdlib plus ``debug_logger`` and ``settings``. It knows nothing about
sessions, seeds or roles — every path and browser arrives as an argument.
"""

import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.settings import get_settings


class SettingsTab(Protocol):
    """The slice of a nodriver ``Tab`` the pref setter drives."""

    async def evaluate(
        self, expression: str, await_promise: bool = False
    ) -> object: ...

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
)

#: What nodriver's ``Config.__call__`` puts in its own ``--disable-features``
#: switch. It is emitted BEFORE caller args, and Chrome keeps the LAST such
#: switch, so a merged switch that dropped these would silently re-enable site
#: isolation trials. Pinned against the installed nodriver by a test.
NODRIVER_DISABLED_FEATURES = ("IsolateOrigins", "site-per-process")

_DISABLE_FEATURES_PREFIX = "--disable-features="

#: The pref that makes Chrome persist session cookies, and the value that does.
RESTORE_PREF = "session.restore_on_startup"
RESTORE_CONTINUE = 1

#: Chrome's saved-tabs directories under a profile's ``Default``. Not a login.
SAVED_TABS_DIRS = ("Sessions", "Sessions_Encrypted")

_GET_PREF = (
    "new Promise(r => chrome.settingsPrivate.getPref("
    f"'{RESTORE_PREF}', p => r(p ? p.value : null)))"
)
_SET_PREF = (
    "new Promise(r => chrome.settingsPrivate.setPref("
    f"'{RESTORE_PREF}', {RESTORE_CONTINUE}, '', ok => r(ok === undefined ? true : ok)))"
)


def merge_disable_features(args: Sequence[str], extra: Sequence[str]) -> list[str]:
    """*args* with every ``--disable-features=`` folded into ONE switch that also
    names *extra*. Tokens keep their first-seen order and are not repeated, and
    nodriver's own are first, so the merged switch is a superset of the two it
    replaces. The switch goes last: that is the one Chrome keeps."""
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


def remove_saved_tabs(user_data_dir: str | None) -> list[str]:
    """Delete the saved-tabs directories of the profile at *user_data_dir*, so a
    launch with ``session.restore_on_startup=1`` opens on one page and not on
    every tab the last run left. Returns the names removed. Never raises: a tab
    list that cannot be deleted is a cosmetic problem, not a failed spawn."""
    if not user_data_dir or get_settings().no_persist_session_cookies:
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


async def ensure_session_restore(browser: PrefBrowser) -> bool:
    """Set ``session.restore_on_startup`` to ``1`` through ``chrome://settings``
    and report whether it is ``1`` afterwards. Idempotent: it reads first and
    writes only when the value differs. Never raises, and always closes the
    settings tab it opened."""
    if get_settings().no_persist_session_cookies:
        return False
    tab = None
    try:
        tab = await browser.get("chrome://settings", new_tab=True)
        current = await tab.evaluate(_GET_PREF, await_promise=True)
        if current == RESTORE_CONTINUE:
            return True
        accepted = await tab.evaluate(_SET_PREF, await_promise=True)
        current = await tab.evaluate(_GET_PREF, await_promise=True)
        if accepted and current == RESTORE_CONTINUE:
            return True
        debug_logger.log_warning(
            "login_persistence",
            "ensure_session_restore",
            f"{RESTORE_PREF} is {current!r} after setPref(accepted={accepted!r}); "
            "session cookies will not survive a close",
        )
    except Exception as err:  # noqa: BLE001  PERMANENT(F-937): any CDP failure is a warning, never a failed spawn
        debug_logger.log_warning(
            "login_persistence",
            "ensure_session_restore",
            f"could not set {RESTORE_PREF}: {err}",
        )
    finally:
        if tab is not None:
            try:
                await tab.close()
            except Exception as err:  # noqa: BLE001  PERMANENT(F-937): a tab that will not close is a warning
                debug_logger.log_warning(
                    "login_persistence",
                    "ensure_session_restore",
                    f"could not close the settings tab: {err}",
                )
    return False
