"""The sealed login: guards, then username, password, outcome (T-19366).

:func:`run` is what the ``login`` tool calls. It signs a stored account in to a
site inside a tab only this backend may touch (:mod:`login_seal`) and returns an
OUTCOME, never a credential: ``logged_in``, ``already_logged_in``, ``needs_totp``,
``needs_user``, ``rejected`` or ``use_provider``.

**The sequence** (the design's §6), every step of which is a guard or a cleanup:

1. the entry exists and is not frozen; the browser passes :func:`launch_refusal`;
2. :func:`login_seal.begin`, then a backend-owned tab opened BEHIND the caller's
   (``background=True``), marked internal and sealed. It is never adopted by
   ``tab_binding``, so the network interceptor and dynamic hooks never arm it;
3. navigate to the entry's ``login_url`` and wait up to :data:`FIELD_WAIT_S` for a
   field (:mod:`login_form`);
4. before EACH field, re-read that frame's URL from the browser (not from page
   script) and require :func:`origin_allowed`; a page that fails it gets nothing
   typed and the call raises;
5. type with :func:`text_entry.type_characters` (real key events, as a person
   types), Enter, and wait up to :data:`OUTCOME_WAIT_S` for an outcome;
6. in ``finally``, always: clear every password field, close the tab, drop the
   seal. A call cancelled mid-login still runs the cleanup to its end.

Every exit, success or refusal, appends one audit line (:func:`vault_store.audit`).

**Where the secret is.** In one ``reveal()`` call, passed straight to the key
presses. It is in no exception message, log call, return value or audit line, and
:mod:`secret_registry` masks it if one is ever written by mistake.

**Rejected means frozen.** A password field that comes back after submit (a new
document with the form again, or the same document with the field emptied) is a
rejected password: the entry is frozen (:func:`vault_store.freeze`) so no caller
retries it into an account lockout, until a person runs ``stealthy login edit``.
A username step that does not advance is ``needs_user``, never a freeze.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import time
import urllib.parse
from collections.abc import Callable
from typing import TYPE_CHECKING, NamedTuple, cast

from nodriver import cdp

from stealth_chrome_devtools_mcp.embedded import (
    browser_cmdline,
    browser_trust,
    debug_port_guard,
    login_evidence,
    login_form,
    login_seal,
    secret_registry,
    tab_open,
    text_entry,
    vault_crypto,
    vault_store,
    window_sizing,
)
from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.embedded.tool_errors import (
    ToolError,
    instance_not_found,
)

if TYPE_CHECKING:
    from nodriver import Browser, Tab

    from stealth_chrome_devtools_mcp.embedded.browser_manager import BrowserManager

#: Flags that let a page, a resolver or a certificate be something it is not: with
#: any of them the "this is the host in ``hosts``" check proves nothing. Owned by
#: ``browser_trust``, which also reads the environment, the profile and policy.
FORBIDDEN_FLAGS = browser_trust.FORBIDDEN_FLAGS

FIELD_WAIT_S = 15.0
OUTCOME_WAIT_S = 15.0
#: The longest a whole login (open the tab, type, read the outcome) may take; a
#: two-step login with every wait spent is about 70 s.
LOGIN_BUDGET_S = 120.0
#: The longest the caller waits for the cleanup (clear, close, verify). The cleanup
#: itself is not cancelled: it ends the seal when it finishes.
CLEANUP_BUDGET_S = 30.0
#: The cleanup's own steps. A step that runs out leaves the tab NOT known closed, and
#: a tab not known closed stays sealed for good (``login_seal.end(..., unclosed)``),
#: so a browser that never answers costs a hidden tab, never an exposed one.
CLEAR_BUDGET_S = 10.0
CLOSE_BUDGET_S = 15.0
POLL_S = 0.4
#: The delay between key presses; a person's, so a page's key handlers behave.
KEY_DELAY_S = 0.03
#: A live Linux Chrome reads an EMPTY argv for a moment while its launcher execs.
_ARGV_ATTEMPTS = 5
_ARGV_RETRY_S = 0.2
#: Outside-the-login-hosts must be seen on this many polls in a row (a redirect
#: chain passes through hosts on its way).
_STABLE_POLLS = 2
#: A rejection (and the freeze it causes) needs POSITIVE evidence, seen on this
#: many polls in a row AND for at least this long: the login form is back in a NEW
#: document, or the page shows an error it did not show before Enter. An emptied
#: password field is not evidence: a single-page app blanks it while its request is
#: still in flight and signs in seconds later.
_REJECT_POLLS = 3
_REJECT_SETTLE_S = 2.0
#: How long the login page is given, after a session cookie appeared, to show that
#: it no longer offers its form (a signed-in site sends the person on).
_CONFIRM_WAIT_S = 4.0

_HTTPS_PORT = 443
_GENERIC_ORIGIN_REFUSAL = (
    "the page is not on a host this login may be used on; nothing was typed"
)
FROZEN_REFUSAL = (
    "this login was rejected earlier and is frozen so it is not retried into an "
    "account lockout; a person must run `stealthy login edit {label}`"
)


def launch_refusal(options: object, cmdline: list[str]) -> str | None:
    """Why this browser may NOT be used to log in, or ``None``.

    A proxy, a host-resolver rule or a relaxed certificate check lets the page on
    the other end be a different host than its URL says, so the origin check below
    would pass a phishing page. Every spelling Chrome accepts counts (``--flag``,
    ``-flag``, ``/flag``, any case, ``=value``): see :mod:`browser_trust`."""
    return browser_trust.switch_refusal(options, cmdline)


def origin_allowed(url: str, hosts: list[str]) -> bool:
    """Whether a password may be typed on ``url``: ``https`` on the default port
    and a host that is EXACTLY one of ``hosts`` (no suffix, no prefix, no userinfo
    trick, no other port: ``accounts.example.com:8443`` is another service). With
    the test variable, ``http://127.0.0.1`` on any port too."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError:  # a port that is not a number or is out of range
        return False
    if not host or parts.username is not None or parts.password is not None:
        return False
    if parts.scheme == "https":
        return host in hosts and port in (None, _HTTPS_PORT)
    return (
        parts.scheme == "http"
        and host == vault_store.LOOPBACK_HOST
        and vault_store.loopback_allowed()
        and host in hosts
    )


#: What ``run`` answers: outcome, label and detail (text), and how many DevTools
#: windows the login closed in that browser (``closed_devtools_windows``).
LoginResult = dict[str, str | int]


#: What the login is doing now, for the message of a login that ran out of its
#: budget (never a secret: a fixed phrase per step). A stall used to say only
#: "the browser stopped answering", and a CI log could not say where.
_STEP: contextvars.ContextVar[str] = contextvars.ContextVar(
    "login_step", default="starting"
)


def _at(step: str) -> None:
    _STEP.set(step)


class LoginTimedOutError(ToolError):
    """The login ran out of :data:`LOGIN_BUDGET_S`. Audited as ``timed_out``."""


async def run(
    browser_manager: BrowserManager, instance_id: str, label: str, caller: str
) -> LoginResult:
    """Log ``label``'s account in to ``instance_id``'s browser. See the module
    docstring. Returns ``{"outcome", "label", "detail"}``; raises ``ToolError``
    for a refusal (locked, unknown, frozen, a browser or a page that fails a
    guard)."""
    outcome = "error"
    try:
        result = await _run(browser_manager, instance_id, label, caller)
    except vault_store.VaultLocked:
        outcome = "locked"
        raise
    except LoginTimedOutError:
        # Its own outcome: the submit may already have gone through, so it is not
        # a refusal, and nothing says the site rejected the password (no freeze).
        outcome = "timed_out"
        raise
    except ToolError:
        outcome = "refused"
        raise
    else:
        outcome = result["outcome"]
        return result
    finally:
        vault_store.audit(label, instance_id, caller, outcome)


async def _run(
    browser_manager: BrowserManager, instance_id: str, label: str, caller: str
) -> LoginResult:
    try:
        entry = vault_store.entry(label)
    except KeyError:
        raise ToolError(f"there is no stored login named {label!r}") from None
    if entry["kind"] == "provider":
        return await _provider(browser_manager, instance_id, label, caller, entry)
    if entry.get("frozen"):
        raise ToolError(FROZEN_REFUSAL.format(label=label))
    data = await browser_manager.get_instance(instance_id)
    if not data:
        raise instance_not_found(instance_id)
    browser = data["browser"]
    await _check_launch(data.get("options"), browser)
    login_seal.begin(instance_id, int(browser.config.port))
    tab = None
    timed_out = False
    result: LoginResult = {}
    closed_before = debug_port_guard.closed_windows(browser)
    try:
        # If the guard ends mid-login (its connection dropped, the browser lives)
        # the login is cancelled, its cleanup clears what it typed, and the caller
        # is told why.
        async with debug_port_guard.watch(browser):
            # Every page wait inside has its own bound, but a CDP call has none:
            # one Chrome never answers (a key event that opened a popup, a tab that
            # closed under a command) would hold the login, its seal and the
            # caller for ever. The whole login is bounded, and the cleanup below
            # still runs when it is.
            budget = asyncio.timeout(LOGIN_BUDGET_S)
            try:
                async with budget:
                    _at("opening its tab")
                    tab = await _open_sealed_tab(browser, instance_id)
                    outcome, detail = await _sealed_login(tab, entry)
            except TimeoutError:
                if not budget.expired():
                    raise
                # Said AFTER the cleanup, below, when what became of the tab is
                # known. No freeze: nothing says the site rejected the password.
                timed_out = True
            else:
                if outcome == "rejected":
                    detail += _FROZEN_NOTE if _freeze(label) else _NOT_FROZEN_NOTE
                result = {
                    "outcome": outcome,
                    "label": label,
                    "detail": detail,
                    "closed_devtools_windows": debug_port_guard.closed_windows(browser)
                    - closed_before,
                }
    finally:
        closed = await _finish(browser_manager, instance_id, tab)
    if timed_out:
        raise LoginTimedOutError(
            f"the login did not finish within {LOGIN_BUDGET_S:g} s (the browser "
            "stopped answering) and was stopped; whether the form was already "
            "submitted is not known, and nothing further was typed"
            f" (it was {_STEP.get()})"
            + ("; its tab is closed" if closed else UNCLOSED_NOTE)
        )
    if not closed:
        result["detail"] = f"{result['detail']}{UNCLOSED_NOTE}"
    return result


async def _open_sealed_tab(browser: Browser, instance_id: str) -> Tab:
    """Open the login's own tab and seal it, with no moment in which it exists and
    can be reached: every target that is not in the list read BEFORE the open is
    held sealed (:func:`login_seal.hold_new`) until the tab is sealed by id.

    Before it opens, the browser must be guarded and hold no inspector
    (:func:`debug_port_guard.verify_clean`). If the open is cancelled or fails
    after Chrome made the tab but before it was sealed by id, the tab is closed by
    a task that outlives the cancellation, and stays held until it is gone."""
    await debug_port_guard.verify_clean(browser)
    await tab_open.update_targets(browser)
    known = [str(getattr(t.target, "target_id", t)) for t in browser.tabs]
    hold = login_seal.hold_new(instance_id, known)
    try:
        tab = await tab_open.open_tab(browser, "about:blank", background=True)
        tab_open.mark_internal(browser, tab)
        login_seal.seal_tab(instance_id, str(tab.target.target_id))
        # The tab is opened BEHIND the window's current one, so in a headed browser it
        # is hidden and unfocused: its page reads ``visibilityState: hidden`` and
        # ``hasFocus() == false``, and Chrome drops the key events typed into it (both
        # fields stayed empty, measured, Chrome 158). The product's one cure for a
        # background tab (F-1011) is focus emulation, which makes the PAGE read visible
        # and focused with no window switch and no OS focus taken; it is applied here
        # because this tab is never adopted (``tab_binding``) and so never armed. Before
        # the first navigation, so the login page loads as a visible one.
        await window_sizing.enable_focus_emulation(tab)
    except BaseException:
        task = asyncio.ensure_future(_discard_strays(browser, instance_id, known, hold))
        _STRAYS.add(task)
        task.add_done_callback(_STRAYS.discard)
        raise
    login_seal.release_new(instance_id, hold)
    return tab


_STRAYS: set[asyncio.Task[None]] = set()
_STRAY_SCANS = 3


async def _discard_strays(
    browser: Browser, instance_id: str, known: list[str], hold: frozenset[str]
) -> None:
    """Close every tab that appeared since ``known`` was read (the login's own,
    that nothing holds yet), then end the hold. A tab that will not close is kept
    sealed for good."""
    stray: set[str] = set()
    try:
        for scan in range(_STRAY_SCANS):
            with contextlib.suppress(Exception):
                await tab_open.update_targets(browser)
                for tab in browser.tabs:
                    target = str(getattr(tab.target, "target_id", tab))
                    if target not in known:
                        stray.add(target)
                        await tab_open.send(
                            browser, cdp.target.close_target, tab.target.target_id
                        )
            if scan < _STRAY_SCANS - 1:
                await asyncio.sleep(_CLOSE_RETRY_S)
        for target in stray:
            with contextlib.suppress(Exception):
                if await _is_open(browser, target):
                    login_seal.keep(instance_id, target)
    finally:
        login_seal.release_new(instance_id, hold)


async def _provider(
    browser_manager: BrowserManager,
    instance_id: str,
    label: str,
    caller: str,
    entry: dict[str, object],
) -> LoginResult:
    via = str(entry["via"])
    inner = await run(browser_manager, instance_id, via, caller)
    if inner["outcome"] in ("logged_in", "already_logged_in"):
        return {
            "outcome": "use_provider",
            "label": label,
            "detail": "the provider account is signed in; use the site's own "
            "sign-in-with button, then choose that account",
            "closed_devtools_windows": inner.get("closed_devtools_windows", 0),
        }
    return {
        "outcome": inner["outcome"],
        "label": label,
        "detail": inner["detail"],
        "closed_devtools_windows": inner.get("closed_devtools_windows", 0),
    }


_FROZEN_NOTE = "; this login is now frozen"
_NOT_FROZEN_NOTE = (
    "; it could NOT be frozen (the vault could not be written), so it will not "
    "stop another try: do not retry it"
)


def _freeze(label: str) -> bool:
    """Freeze ``label``; ``False`` when the vault would not take the write."""
    try:
        vault_store.freeze(label)
    except (
        vault_store.VaultLocked,
        KeyError,
        OSError,
        vault_crypto.VaultError,
    ) as error:
        debug_logger.log_warning(
            "login_flow",
            "_freeze",
            f"could not freeze a login ({type(error).__name__})",
        )
        return False
    return True


async def _check_launch(options: object, browser: object) -> None:
    pid = getattr(browser, "_process_pid", None) or getattr(
        getattr(browser, "_process", None), "pid", None
    )
    cmdline: list[str] = []
    for attempt in range(_ARGV_ATTEMPTS):
        cmdline = browser_cmdline.arguments(pid) if pid else []
        if cmdline:
            break
        if attempt < _ARGV_ATTEMPTS - 1:
            await asyncio.sleep(_ARGV_RETRY_S)
    if not cmdline:
        raise ToolError(
            "the browser's command line could not be read, so it cannot be shown "
            "free of proxy and certificate overrides; vault logins are refused"
        )
    reason = (
        launch_refusal(options, cmdline)
        or browser_trust.environment_refusal(
            browser_trust.process_environment(pid) if pid else None
        )
        or browser_trust.profile_refusal(
            getattr(options, "user_data_dir", None)
            or browser_trust.user_data_dir_of(cmdline)
        )
        or browser_trust.policy_refusal()
    )
    if reason:
        raise ToolError(reason)


#: Said when the login tab could not be closed (it stays sealed, hidden from tools).
UNCLOSED_NOTE = (
    "; the login's own tab could not be closed and stays hidden from every tool "
    "until the backend restarts"
)
_CLOSE_ATTEMPTS = 3
_CLOSE_RETRY_S = 0.3
#: Cleanups in flight: a task the loop holds weakly could be collected mid-close.
_CLEANUPS: set[asyncio.Task[bool]] = set()


async def _finish(
    browser_manager: BrowserManager, instance_id: str, tab: Tab | None
) -> bool:
    """Clean up, close the tab, and ONLY THEN end the seal; ``True`` when the tab
    is gone.

    It runs as a task of its own behind a shield. A cancellation of the login
    (anyio's scopes are level-triggered: every ``await`` in a cancelled scope
    raises again) therefore raises HERE at once, while the task keeps going and
    ends the seal itself after the close, so there is never a moment when the tab
    is open and the seal is down."""
    task = asyncio.ensure_future(_cleanup(browser_manager, instance_id, tab))
    _CLEANUPS.add(task)
    task.add_done_callback(_CLEANUPS.discard)
    try:
        async with asyncio.timeout(CLEANUP_BUDGET_S):
            return await asyncio.shield(task)
    except TimeoutError:
        # The task goes on behind the shield and ends the seal itself; until it
        # does, the tab stays sealed. The caller is told it is not known closed.
        debug_logger.log_warning(
            "login_flow",
            "_finish",
            f"the cleanup took over {CLEANUP_BUDGET_S:g} s; not waiting for it",
        )
        return False


async def _cleanup(
    browser_manager: BrowserManager, instance_id: str, tab: Tab | None
) -> bool:
    """Clear, close and verify; then end the seal (keeping a tab that stayed open
    sealed). Each step is tried even if the one before failed."""
    closed = True
    target = str(tab.target.target_id) if tab is not None else None
    try:
        if tab is not None:
            closed = False  # until the close is read back from the browser
            try:
                async with asyncio.timeout(CLEAR_BUDGET_S):
                    await login_form.clear_passwords(tab)
            except Exception as error:  # noqa: BLE001  PERMANENT(cleanup runs every step; the tab is closed next)
                debug_logger.log_warning(
                    "login_flow",
                    "_cleanup",
                    f"clearing failed ({type(error).__name__})",
                )
            try:
                async with asyncio.timeout(CLOSE_BUDGET_S):
                    closed = await _close_tab(browser_manager, instance_id, tab)
            except TimeoutError:
                debug_logger.log_warning(
                    "login_flow",
                    "_cleanup",
                    f"closing the tab took over {CLOSE_BUDGET_S:g} s",
                )
    finally:
        login_seal.end(instance_id, None if closed else target)
    if not closed:
        debug_logger.log_warning(
            "login_flow",
            "_cleanup",
            "the login tab could not be closed; it stays sealed",
        )
    return closed


async def _close_tab(
    browser_manager: BrowserManager, instance_id: str, tab: Tab
) -> bool:
    """Close ``tab`` and check it is gone, retrying: ``close_tab`` answers
    ``False`` both for "already gone" and for "Chrome refused", so the answer is
    read from the browser's own target list."""
    browser = await browser_manager.get_browser(instance_id)
    target = tab.target.target_id
    for attempt in range(_CLOSE_ATTEMPTS):
        try:
            await browser_manager.close_tab(instance_id, str(target))
            if browser is not None and not await _is_open(browser, str(target)):
                return True
            if browser is not None:
                await tab_open.send(browser, cdp.target.close_target, target)
                if not await _is_open(browser, str(target)):
                    return True
        except Exception as error:  # noqa: BLE001  PERMANENT(a failed close is retried, then reported by the caller)
            debug_logger.log_warning(
                "login_flow",
                "_close_tab",
                f"closing the tab failed ({type(error).__name__})",
            )
        if attempt < _CLOSE_ATTEMPTS - 1:
            await asyncio.sleep(_CLOSE_RETRY_S)
    return False


async def _is_open(browser: Browser, target: str) -> bool:
    """Whether Chrome still lists ``target`` (read fresh, not from a cache)."""
    await tab_open.update_targets(browser)
    return tab_open.find(browser, target) is not None


# ---------------------------------------------------------------- page reads


def _frame_url(tree: cdp.page.FrameTree, frame_id: str | None) -> str:
    """The URL Chrome reports for ``frame_id`` (the main frame when ``None``).

    From the browser's frame tree, not from page script: a page cannot make
    ``location`` lie to this check. A page that failed to load reports its
    ``chrome-error://`` URL, which no host list admits."""
    stack = [tree]
    while stack:
        node = stack.pop()
        if frame_id is None or str(node.frame.id_) == frame_id:
            return str(node.frame.url or "")
        stack.extend(node.child_frames or [])
    return ""


async def _main(tab: Tab) -> tuple[str, str]:
    """``(URL, loader id)`` of the main frame: the loader id changes with every
    new document, which is how "the page reloaded" is told from "the page stayed"."""
    tree = await tab.send(cdp.page.get_frame_tree())
    return _frame_url(tree, None), str(tree.frame.loader_id)


Ready = Callable[[dict[str, bool], str], bool]


async def _wait_for(
    tab: Tab, seconds: float, ready: Ready
) -> tuple[dict[str, bool], str]:
    """Poll ``(probe, main URL)`` until ``ready(probe, url)`` or the time is up;
    the last reading either way."""
    deadline = time.monotonic() + seconds
    while True:
        state = await login_form.probe(tab)
        url, _loader = await _main(tab)
        if ready(state, url) or time.monotonic() >= deadline:
            return state, url
        await asyncio.sleep(POLL_S)


# ---------------------------------------------------------------- the login


async def _sealed_login(tab: Tab, entry: dict[str, object]) -> tuple[str, str]:
    hosts = [str(host) for host in cast("list[object]", entry["hosts"])]
    _at("loading the login page")
    navigated = await tab.send(cdp.page.navigate(str(entry["login_url"])))
    if len(navigated) > 2 and navigated[2]:  # noqa: PLR2004  PERMANENT(errorText of Page.navigate)
        # Chrome's own net error name (``net::ERR_...``): never page content.
        return "needs_user", f"the login page could not be loaded ({navigated[2]})"

    _at("waiting for the login form")
    state, url = await _wait_for(tab, FIELD_WAIT_S, _has_form)
    if not _has_form(state, url):
        return await _no_form(tab, hosts, str(entry["login_url"]), url)
    if state["otp"] and not (state["username"] or state["password"]):
        return _TOTP
    if state["username"]:
        if not await _type_into(tab, "username", entry["username"], hosts):
            return "needs_user", "the username field did not take all of the typed text"
        if not state["password"]:
            stopped = await _after_username(tab, hosts)
            if stopped is not None:
                return stopped
    return await _password_step(tab, entry, hosts)


_TOTP = ("needs_totp", "a one-time code is being asked for")


def _has_form(state: dict[str, bool], _url: str) -> bool:
    return state["username"] or state["password"] or state["otp"]


async def _after_username(tab: Tab, hosts: list[str]) -> tuple[str, str] | None:
    """Enter after the username, then wait for the password step. ``None`` means
    it is showing; otherwise the outcome that ends the login."""
    _at("pressing Enter after the username")
    await text_entry.press_enter(tab)
    _at("waiting for the password step")
    state, _url = await _wait_for(
        tab,
        OUTCOME_WAIT_S,
        lambda s, u: s["password"] or s["otp"] or _left(u, hosts),
    )
    if state["password"]:
        return None
    if state["otp"]:
        return _TOTP
    return "needs_user", "the page did not ask for a password after the username"


class _Baseline(NamedTuple):
    """What the page looked like just before Enter on the password."""

    urls: list[str]
    marks: frozenset[login_evidence.Mark]
    login_url: str
    error: bool


async def _password_step(
    tab: Tab, entry: dict[str, object], hosts: list[str]
) -> tuple[str, str]:
    took = await _type_into(tab, "password", entry["password"], hosts)
    _at("reading the form after the password was typed")
    typed = await login_form.probe(tab)
    if not (took and typed["password_has_value"]):
        return "needs_user", "the password field did not take the typed text"
    url, before = await _main(tab)
    urls = login_evidence.urls_for(hosts, url)
    marks = await login_evidence.session_marks(tab, urls)
    baseline = _Baseline(urls, marks, str(entry["login_url"]), bool(typed.get("error")))
    _at("pressing Enter after the password")
    await text_entry.press_enter(tab)
    _at("waiting for the page's answer")
    return await _outcome(tab, hosts, before, baseline)


def _left(url: str, hosts: list[str]) -> bool:
    """Whether the page is on a real web host that is not one of ``hosts``."""
    parts = urllib.parse.urlsplit(url)
    return parts.scheme in ("http", "https") and not origin_allowed(url, hosts)


async def _no_form(
    tab: Tab, hosts: list[str], login_url: str, url: str
) -> tuple[str, str]:
    """No field showed up. A page that sent the browser on (off the login hosts,
    or to another path of them) and still shows no form, on two polls in a row, is
    a signed-in session (``already_logged_in``); anything else is an unknown form."""
    for attempt in range(_STABLE_POLLS):
        if not _moved_on(url, hosts, login_url) or any(
            (await login_form.probe(tab)).values()
        ):
            return "needs_user", "no username or password field was found on the page"
        if attempt < _STABLE_POLLS - 1:
            await asyncio.sleep(POLL_S)
            url, _loader = await _main(tab)
    return "already_logged_in", "the login page sent the browser on without a form"


def _moved_on(url: str, hosts: list[str], login_url: str) -> bool:
    if _left(url, hosts):
        return True
    here, there = urllib.parse.urlsplit(url), urllib.parse.urlsplit(login_url)
    return here.scheme in ("http", "https") and (
        here.hostname,
        here.path.rstrip("/"),
    ) != (there.hostname, there.path.rstrip("/"))


async def _type_into(tab: Tab, kind: str, text: object, hosts: list[str]) -> bool:
    """Type ``text`` into the visible ``kind`` field, after three checks, each read
    from the browser (never from page script): the field's own frame is on a login
    host, the TOP frame is too (a login host's page framed inside another site is
    not typed into), and the form around the field submits to a login host.

    ``True`` when the field then holds as many characters as were typed. A dropped
    key would otherwise submit a WRONG username or password, which the site rejects
    and the vault then FREEZES a good entry for; ``False`` ends the login as
    ``needs_user`` with nothing submitted."""
    _at(f"looking for the {kind} field")
    await debug_port_guard.verify_clean(tab.browser)
    found = await login_form.find_field(tab, kind)
    if found is None:
        raise ToolError(f"the {kind} field went away before it could be typed into")
    element, frame_id = found
    tree = await tab.send(cdp.page.get_frame_tree())
    if not origin_allowed(_frame_url(tree, frame_id), hosts) or not origin_allowed(
        _frame_url(tree, None), hosts
    ):
        raise ToolError(_GENERIC_ORIGIN_REFUSAL)
    action = await login_form.form_action(tab, kind, frame_id)
    if action is not None and not origin_allowed(action, hosts):
        raise ToolError(_GENERIC_ORIGIN_REFUSAL)
    value = text.reveal() if isinstance(text, secret_registry.Secret) else str(text)
    _at(f"waiting for the {kind} field's frame")
    await login_form.wait_for_frame(tab, frame_id)
    _at(f"typing the {kind}")
    await text_entry.type_characters(tab, element, value, KEY_DELAY_S)
    _at(f"reading back the {kind}")
    return await login_form.holds(element, len(value))


async def _snapshot(tab: Tab) -> tuple[dict[str, bool], str, str | None]:
    """``(probe, main URL, loader id)`` of ONE document: the loader id is read before
    and after the probe, and is ``None`` when they differ (a navigation committed
    between the reads, so the probe may describe the page that was just left)."""
    _url, first = await _main(tab)
    state = await login_form.probe(tab)
    url, second = await _main(tab)
    return state, url, second if first == second else None


class _Rejection:
    """Positive evidence of a rejection, held for long enough
    (:data:`_REJECT_POLLS` polls in a row and :data:`_REJECT_SETTLE_S`)."""

    def __init__(self) -> None:
        self.polls = 0
        self.since = 0.0

    def reset(self) -> None:
        self.polls = 0

    def confirmed(self, evidence: bool) -> bool:
        if not evidence:
            self.polls = 0
            return False
        self.polls += 1
        if self.polls == 1:
            self.since = time.monotonic()
        return (
            self.polls >= _REJECT_POLLS
            and time.monotonic() - self.since >= _REJECT_SETTLE_S
        )


async def _outcome(
    tab: Tab, hosts: list[str], before: str, baseline: _Baseline
) -> tuple[str, str]:
    """After the password's Enter: what did the page do?

    ``logged_in`` needs positive evidence (:func:`_verdict`). ``rejected`` (which
    freezes the entry) needs the login form back in a NEW document, or an error the
    page did not show before, on :data:`_REJECT_POLLS` polls in a row and for
    :data:`_REJECT_SETTLE_S`; an emptied field alone is ``needs_user`` at the end."""
    deadline = time.monotonic() + OUTCOME_WAIT_S
    settled = 0
    rejection = _Rejection()
    while True:
        state, url, loader = await _snapshot(tab)
        if loader is None:
            settled = 0
            rejection.reset()
        else:
            reloaded = loader != before
            if state["otp"] and not state["password"]:
                return "needs_totp", "a one-time code is being asked for"
            if state["password"]:
                settled = 0
                new_error = bool(state.get("error")) and not baseline.error
                if rejection.confirmed(reloaded or new_error):
                    return (
                        "rejected",
                        "the login form came back after the attempt"
                        + ("" if reloaded else " with an error showing"),
                    )
            elif state["username"] and reloaded:
                return "needs_user", "the page went back to its first step"
            elif _left(url, hosts) or reloaded:
                rejection.reset()
                settled += 1
                if settled >= _STABLE_POLLS:
                    return await _verdict(tab, hosts, url, baseline)
            else:
                settled = 0
                rejection.reset()
        if time.monotonic() >= deadline:
            return "needs_user", "the page gave no clear answer within the time limit"
        await asyncio.sleep(POLL_S)


async def _verdict(
    tab: Tab, hosts: list[str], url: str, baseline: _Baseline
) -> tuple[str, str]:
    """The page settled with no login form: ``logged_in`` only on evidence, and
    ``needs_user`` whenever the evidence is ambiguous.

    * the page left the login's hosts: signed in (the spec's rule);
    * otherwise a cookie for the login hosts must be new or changed since before
      Enter AND the login page must then no longer offer its form
      (:func:`_confirm`). A cookie alone proves nothing: sites set one on every
      response (a CSRF token, a load balancer), a "locked" page included."""
    if _left(url, hosts):
        return "logged_in", "signed in (the page left the login's hosts)"
    now = await login_evidence.session_marks(
        tab, login_evidence.urls_for(hosts, url) or baseline.urls
    )
    if not login_evidence.gained(baseline.marks, now):
        return (
            "needs_user",
            "the page changed after the password but gave no sign of a session (no "
            "cookie was set and it did not leave the login's hosts); look at it",
        )
    return await _confirm(tab, hosts, baseline.login_url)


async def _confirm(tab: Tab, hosts: list[str], login_url: str) -> tuple[str, str]:
    """A cookie appeared: visit the login page again. A site that took the sign-in
    sends the person on; one that shows its form again did not (or cannot be told,
    and then the answer is ``needs_user``, never a guessed ``logged_in``)."""
    navigated = await tab.send(cdp.page.navigate(login_url))
    if len(navigated) > 2 and navigated[2]:  # noqa: PLR2004  PERMANENT(errorText of Page.navigate)
        return "needs_user", "a cookie was set but the login page could not be checked"
    state, url = await _wait_for(
        tab,
        _CONFIRM_WAIT_S,
        lambda s, u: _has_form(s, u) or _moved_on(u, hosts, login_url),
    )
    if _has_form(state, url) or not _moved_on(url, hosts, login_url):
        return (
            "needs_user",
            "a cookie was set, but the login page still does not show a signed-in "
            "session (it offers its form, or stays put); look at it",
        )
    return "logged_in", "signed in (a session cookie, and the login page sent us on)"
