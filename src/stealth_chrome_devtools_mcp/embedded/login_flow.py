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
import time
import urllib.parse
from collections.abc import Callable
from typing import TYPE_CHECKING, cast

from nodriver import cdp

from stealth_chrome_devtools_mcp.embedded import (
    browser_cmdline,
    browser_trust,
    login_evidence,
    login_form,
    login_seal,
    secret_registry,
    tab_open,
    text_entry,
    vault_crypto,
    vault_store,
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
POLL_S = 0.4
#: The delay between key presses; a person's, so a page's key handlers behave.
KEY_DELAY_S = 0.03
#: A live Linux Chrome reads an EMPTY argv for a moment while its launcher execs.
_ARGV_ATTEMPTS = 5
_ARGV_RETRY_S = 0.2
#: Outside-the-login-hosts must be seen on this many polls in a row (a redirect
#: chain passes through hosts on its way).
_STABLE_POLLS = 2
#: A login form that is back after the password must be SEEN THIS MANY consistent
#: polls in a row before it counts as a rejection (and freezes the entry): a page
#: that clears the field while its request is still in flight shows it for one.
_REJECT_POLLS = 3

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


async def run(
    browser_manager: BrowserManager, instance_id: str, label: str, caller: str
) -> dict[str, str]:
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
) -> dict[str, str]:
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
    login_seal.begin(instance_id)
    tab = None
    result: dict[str, str] = {}
    try:
        tab = await _open_sealed_tab(browser, instance_id)
        outcome, detail = await _sealed_login(tab, entry)
        if outcome == "rejected":
            _freeze(label)
        result = {"outcome": outcome, "label": label, "detail": detail}
    finally:
        closed = await _finish(browser_manager, instance_id, tab)
    if not closed:
        result["detail"] += UNCLOSED_NOTE
    return result


async def _open_sealed_tab(browser: Browser, instance_id: str) -> Tab:
    """Open the login's own tab and seal it, with no moment in which it exists and
    can be reached: every target that is not in the list read BEFORE the open is
    held sealed (:func:`login_seal.hold_new`) until the tab is sealed by id."""
    await tab_open.update_targets(browser)
    known = [str(getattr(t.target, "target_id", t)) for t in browser.tabs]
    hold = login_seal.hold_new(instance_id, known)
    try:
        tab = await tab_open.open_tab(browser, "about:blank", background=True)
        tab_open.mark_internal(browser, tab)
        login_seal.seal_tab(instance_id, str(tab.target.target_id))
        return tab
    finally:
        login_seal.release_new(instance_id, hold)


async def _provider(
    browser_manager: BrowserManager,
    instance_id: str,
    label: str,
    caller: str,
    entry: dict[str, object],
) -> dict[str, str]:
    via = str(entry["via"])
    inner = await run(browser_manager, instance_id, via, caller)
    if inner["outcome"] in ("logged_in", "already_logged_in"):
        return {
            "outcome": "use_provider",
            "label": label,
            "detail": "the provider account is signed in; use the site's own "
            "sign-in-with button, then choose that account",
        }
    return {"outcome": inner["outcome"], "label": label, "detail": inner["detail"]}


def _freeze(label: str) -> None:
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
    return await asyncio.shield(task)


async def _cleanup(
    browser_manager: BrowserManager, instance_id: str, tab: Tab | None
) -> bool:
    """Clear, close and verify; then end the seal (keeping a tab that stayed open
    sealed). Each step is tried even if the one before failed."""
    closed = True
    target = str(tab.target.target_id) if tab is not None else None
    try:
        if tab is not None:
            try:
                await login_form.clear_passwords(tab)
            except Exception as error:  # noqa: BLE001  PERMANENT(cleanup runs every step; the tab is closed next)
                debug_logger.log_warning(
                    "login_flow",
                    "_cleanup",
                    f"clearing failed ({type(error).__name__})",
                )
            closed = await _close_tab(browser_manager, instance_id, tab)
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
    navigated = await tab.send(cdp.page.navigate(str(entry["login_url"])))
    if len(navigated) > 2 and navigated[2]:  # noqa: PLR2004  PERMANENT(errorText of Page.navigate)
        return "needs_user", "the login page could not be loaded"

    state, url = await _wait_for(tab, FIELD_WAIT_S, _has_form)
    if not _has_form(state, url):
        return await _no_form(tab, hosts, str(entry["login_url"]), url)
    if state["otp"] and not (state["username"] or state["password"]):
        return _TOTP
    if state["username"]:
        await _type_into(tab, "username", entry["username"], hosts)
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
    await text_entry.press_enter(tab)
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


async def _password_step(
    tab: Tab, entry: dict[str, object], hosts: list[str]
) -> tuple[str, str]:
    await _type_into(tab, "password", entry["password"], hosts)
    if not (await login_form.probe(tab))["password_has_value"]:
        return "needs_user", "the password field did not take the typed text"
    url, before = await _main(tab)
    urls = login_evidence.urls_for(hosts, url)
    marks = await login_evidence.session_marks(tab, urls)
    await text_entry.press_enter(tab)
    return await _outcome(tab, hosts, before, (urls, marks))


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


async def _type_into(tab: Tab, kind: str, text: object, hosts: list[str]) -> None:
    """Type ``text`` into the visible ``kind`` field, after three checks, each read
    from the browser (never from page script): the field's own frame is on a login
    host, the TOP frame is too (a login host's page framed inside another site is
    not typed into), and the form around the field submits to a login host."""
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
    await text_entry.type_characters(tab, element, value, KEY_DELAY_S)


async def _snapshot(tab: Tab) -> tuple[dict[str, bool], str, str | None]:
    """``(probe, main URL, loader id)`` of ONE document: the loader id is read before
    and after the probe, and is ``None`` when they differ (a navigation committed
    between the reads, so the probe may describe the page that was just left)."""
    _url, first = await _main(tab)
    state = await login_form.probe(tab)
    url, second = await _main(tab)
    return state, url, second if first == second else None


async def _outcome(
    tab: Tab,
    hosts: list[str],
    before: str,
    evidence: tuple[list[str], frozenset[login_evidence.Mark]],
) -> tuple[str, str]:
    """After the password's Enter: what did the page do?

    ``logged_in`` needs positive evidence (:mod:`login_evidence`); a form that came
    back is a rejection only once it has stayed for :data:`_REJECT_POLLS` polls."""
    urls, marks = evidence
    deadline = time.monotonic() + OUTCOME_WAIT_S
    settled = back = 0
    while True:
        state, url, loader = await _snapshot(tab)
        if loader is None:
            settled = back = 0
        else:
            reloaded = loader != before
            if state["otp"] and not state["password"]:
                return "needs_totp", "a one-time code is being asked for"
            if state["password"]:
                settled = 0
                back = back + 1 if reloaded or not state["password_has_value"] else 0
                if back >= _REJECT_POLLS:
                    return (
                        "rejected",
                        "the password field came back after the attempt; this login "
                        "is now frozen",
                    )
            elif state["username"] and reloaded:
                return "needs_user", "the page went back to its first step"
            elif _left(url, hosts) or reloaded:
                back = 0
                settled += 1
                if settled >= _STABLE_POLLS:
                    return await _verdict(tab, hosts, url, urls, marks)
            else:
                settled = back = 0
        if time.monotonic() >= deadline:
            return "needs_user", "the page gave no clear answer within the time limit"
        await asyncio.sleep(POLL_S)


async def _verdict(
    tab: Tab,
    hosts: list[str],
    url: str,
    urls: list[str],
    marks: frozenset[login_evidence.Mark],
) -> tuple[str, str]:
    """The page settled with no login form: ``logged_in`` only on evidence."""
    if _left(url, hosts):
        return "logged_in", "signed in (the page left the login's hosts)"
    now = await login_evidence.session_marks(
        tab, login_evidence.urls_for(hosts, url) or urls
    )
    if login_evidence.gained(marks, now):
        return "logged_in", "signed in (the site set a session cookie)"
    return (
        "needs_user",
        "the page changed after the password but gave no sign of a session (no "
        "cookie was set and it did not leave the login's hosts); look at it",
    )
