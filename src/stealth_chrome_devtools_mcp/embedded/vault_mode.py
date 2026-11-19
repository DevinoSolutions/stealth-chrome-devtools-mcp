"""THE one home for vault mode (T-19366): what an agent's tools may not do while a
login vault exists on this machine.

**The policy, in one sentence.** While a vault file exists (``vault_store.VAULT_PATH``,
locked or unlocked, in every backend, always), every tool capability that is RAW or
UNBOUNDED, meaning one that lets an agent run code as the OS user, write a file of its
choosing where code loads, or read a file the secret-location rule protects, is
refused, with a sentence that says why and how to leave the mode (``stealthy login
destroy``). With no vault file every one of them behaves exactly as it always has.

Four reviews closed these routes one at a time and each time a sibling was found, so
the rule is a mode, decided by DURABLE state and not by process history, and it lives
here (``tool_registry`` consults :func:`admit`; ``debug_port_guard``,
``debug_logger`` and the CLI ask :func:`active`, :func:`file_url_refusal`,
:func:`export_format` and :func:`download_pin`). The capability classes
(:data:`RESTRICTIONS`, the one list the ``stealthy login init`` notice, ``status``,
the tool descriptions and the census tests read):

1. **caller Python** (:data:`RUNS_CALLER_PYTHON`): ``create_python_binding`` and
   ``create_dynamic_hook`` ``exec`` / ``eval`` an agent's Python inside the backend,
   which holds the key. Refused, whole.
2. **raw CDP** (:data:`RAW_CDP`): ``execute_cdp_command`` reaches every protocol
   method. ``Browser.setDownloadBehavior`` and ``Page.setDownloadBehavior`` make
   Chrome write a page's bytes under a page's name into any folder,
   ``DOM.setFileInputFiles`` reads a local file into a page, ``Extensions.*`` loads
   code, and ``Target.sendMessageToTarget`` / ``attachToTarget`` carry a second
   message as a string, so no method deny-list is sound. Refused, whole;
   ``list_cdp_commands`` stays.
3. **browser arguments**: ``spawn_browser(browser_args=...)`` is checked against an
   ALLOW-list (:data:`ALLOWED_SWITCHES`, :func:`check_browser_args`): Chrome has
   switches that write a named file (``--log-file``, ``--net-log``,
   ``--trace-startup-file``) and start programs (``--renderer-cmd-prefix``,
   ``--utility-cmd-prefix``, ``--gpu-launcher``, ``--browser-subprocess-path``), and a
   list of what to refuse is the shape that failed. ``user_data_dir`` takes a NAME.
4. **local files read by a page** (``file:`` URLs): refused by every URL tool
   (:func:`file_url_refusal`, asked by ``debug_port_guard.refusal``), and a target
   that reaches one by any route is hidden and blanked like an inspector.
5. **pickle** (:func:`export_format`): ``export_debug_logs`` writes JSON only, since a
   pickle keeps an agent's strings raw and puts an attacker's line start in a file a
   tolerant reader acts on.
6. **downloads** (:func:`download_pin`): every browser the guard protects is pinned
   to one fixed folder under generated names (``allowAndName``), so a page cannot
   choose where its bytes land, nor what they are called. See ``debug_port_guard``.
7. **browser WebUI pages** (:func:`webui_refusal`): ``chrome://``, ``chrome-untrusted://``
   and ``devtools://`` pages can send commands to the browser itself (``chrome.send``),
   so while the mode is on the URL tools refuse them (a login refuses more, only while
   it runs) and a target that is one is hidden from the tools and refused by
   ``tab_id``, ALWAYS. ``about:blank`` and the new-tab page keep working. They are
   hidden, not blanked: the product's own session-restore opens ``chrome://settings``
   in a persistent profile, and blanking it would stop logins from being kept.

A caller-named PATH that a tool reads or writes (``upload_file``,
``export_network_data``, ``import_network_data``, ``take_screenshot``,
``export_debug_logs``) takes ``path_confinement``'s rules in every mode.

**What this does not cover, and says so** (README and the design spec carry the same
paragraph): the guarantee is against an agent that has only this MCP's tools. If
caller Python ever ran on this machine before the vault was created, or the agent
has any other way to run code as the same OS user (a shell tool, say), nothing here
can protect the vault from what that code left behind.

**Caller Python and the order of events.** A vault is not created while caller Python
is resident in any backend the registry knows (``cli_login``, through :func:`ran`):
the CLI writes the file, then asks each backend; :func:`admit` marks the process
BEFORE it reads the file. One of the two always sees the other, so the first secret
is never stored beside code that is already running.

JavaScript-only tools (``execute_script``, ``inject_and_execute_script``,
``create_persistent_function``, ``execute_python_in_browser``, which TRANSPILES to
JavaScript) run in the page, never here, and stay available.

A leaf: it imports ``vault_store``, ``profile_seed``, ``browser_trust`` and
``path_confinement`` only inside functions.
"""

from __future__ import annotations

import errno
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from stealth_chrome_devtools_mcp.embedded.tool_errors import CallerError, ToolError

if TYPE_CHECKING:
    from collections.abc import Callable

#: Every tool that runs, or lets a caller choose, Python in the backend process.
#: ``create_simple_dynamic_hook`` is NOT here: its code is a fixed template with the
#: caller's values written in as string literals (``dynamic_hook_ai_interface``).
RUNS_CALLER_PYTHON = frozenset({"create_python_binding", "create_dynamic_hook"})

#: Every tool that forwards a caller's raw CDP method and parameters.
RAW_CDP = frozenset({"execute_cdp_command"})

#: The tool whose ``browser_args`` and ``user_data_dir`` reach Chrome's command line.
TAKES_BROWSER_ARGS = frozenset({"spawn_browser"})

#: The tools that load a caller's URL (``debug_port_guard.URL_ARGUMENTS`` names the
#: argument): ``file:`` is refused for each of them.
LOADS_URLS = frozenset({"navigate", "new_tab", "create_simple_dynamic_hook"})

#: The tool that picks a file format for a file it writes.
CHOOSES_EXPORT_FORMAT = frozenset({"export_debug_logs"})


@dataclass(frozen=True)
class Restriction:
    """One capability class: its key, the tools it touches and what it says."""

    key: str
    tools: frozenset[str]
    summary: str


#: THE list of what vault mode restricts. ``stealthy login init`` prints it before a
#: passphrase is asked for, ``status`` prints it while the mode is on, and the census
#: tests fail when a tool that has the capability is not named here.
RESTRICTIONS = (
    Restriction(
        "caller_python",
        RUNS_CALLER_PYTHON,
        "create_python_binding and create_dynamic_hook are disabled (they run an "
        "agent's Python inside the backend, where the key is)",
    ),
    Restriction(
        "raw_cdp",
        RAW_CDP,
        "execute_cdp_command is disabled (raw CDP can point Chrome's downloads at "
        "any folder, read a local file into a page or load code); "
        "list_cdp_commands still works",
    ),
    Restriction(
        "browser_args",
        TAKES_BROWSER_ARGS,
        "spawn_browser(browser_args=...) takes only a short list of switches "
        "(window size and position, language, user agent; the feature-list "
        "switches are refused) and user_data_dir takes a session NAME, not a path",
    ),
    Restriction(
        "file_urls",
        LOADS_URLS,
        "file: URLs are refused by navigate, new_tab and create_simple_dynamic_hook, "
        "and a tab that reaches one is hidden and blanked",
    ),
    Restriction(
        "browser_pages",
        LOADS_URLS,
        "chrome://, chrome-untrusted:// and devtools:// pages are hidden from the "
        "tools and cannot be opened (about:blank and the new-tab page work)",
    ),
    Restriction(
        "export_format",
        CHOOSES_EXPORT_FORMAT,
        "export_debug_logs writes JSON only (no pickle)",
    ),
    Restriction(
        "downloads",
        frozenset(),
        "a page's downloads are saved by Chrome under generated names in one fixed "
        "folder, in every browser a backend drives",
    ),
)

#: Every tool a restriction names.
RESTRICTED_TOOLS = frozenset().union(*(item.tools for item in RESTRICTIONS))

LEAVE = (
    "A login vault is set up on this machine, so this is refused in every backend "
    "for as long as the vault exists. A person can remove the vault with "
    "`stealthy login destroy` (that deletes every stored login)."
)

DISABLED = (
    "{tool} runs your Python inside the backend process, which could read the login "
    "vault's key and passwords. A login vault is set up on this machine, so this "
    "tool is disabled in every backend for as long as the vault exists. A person "
    "can remove the vault with `stealthy login destroy` (that deletes every stored "
    "login); until then use execute_script or inject_and_execute_script, which run "
    "in the page."
)

#: The sentence for an unlock refused because caller Python ran in this backend.
#: ``vault_store.unlock`` is the one place a key enters a backend (the CLI's route and
#: a direct POST both end there), so the check is there and not only in the CLI.
TAINTED = (
    "this backend has run {tool} in this session, so code an agent wrote could read "
    "the key and every password once the vault is unlocked here. Restart the backend "
    "(or run `stealthy login unlock` against a fresh one), then unlock."
)

_lock = threading.RLock()
_ran: str | None = None


def refusal_text(what: str, why: str) -> str:
    """The ONE refusal sentence of vault mode: what was asked, why it is not allowed
    and how to leave the mode. Names the vault and ``stealthy login destroy``, never
    a path or a value."""
    return f"{what} is refused while a login vault exists: {why}. {LEAVE}"


def refused(what: str, why: str) -> CallerError:
    """:func:`refusal_text` as the error a tool raises (a refusal of what the caller
    asked for, so ``CallerError``)."""
    return CallerError(refusal_text(what, why))


def vault_exists() -> bool:
    """Whether the vault file is there, as a LINK or a file (a dangling symlink
    counts: ``init`` would otherwise replace it). Fails CLOSED: a path that cannot be
    read (permissions, a loop) counts as existing."""
    from stealth_chrome_devtools_mcp.embedded import vault_store

    try:
        os.lstat(vault_store.VAULT_PATH)
    except OSError as error:
        return error.errno not in {errno.ENOENT, errno.ENOTDIR}
    return True


def active() -> bool:
    """Whether vault mode is on. The one name every caller asks."""
    return vault_exists()


# ----------------------------------------------------------------- caller Python


def ran() -> str | None:
    """The first tool that ran caller Python in THIS process since it started, or
    ``None``. Not "live" bindings: the code of a tool runs as it is called (module
    level statements, threads it starts), so a binding table cannot say what is
    resident; only a restart clears this."""
    return _ran


def _admit_caller_python(tool: str) -> None:
    """Mark this process BEFORE the file is read (so a vault being created at the
    same moment sees it), and un-mark again when the call is refused: a refused call
    ran nothing."""
    global _ran  # noqa: PLW0603  PERMANENT(one process-wide fact, cleared only by a restart)
    with _lock:
        first = _ran is None
        if first:
            _ran = tool
        if vault_exists():
            if first:
                _ran = None
            raise CallerError(DISABLED.format(tool=tool))


# --------------------------------------------------------------- browser arguments

_DIGITS = re.compile(r"\d{1,5},\d{1,5}")
_POSITION = re.compile(r"-?\d{1,5},-?\d{1,5}")
_LOCALE = re.compile(r"[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})*")
_LOCALES = re.compile(rf"{_LOCALE.pattern}(?:,{_LOCALE.pattern})*")
_ANY = re.compile(r"[^\x00-\x1f\x7f]{1,512}")

#: Switch name (``browser_trust.switch_name``: no prefix, lower case) -> (why it is
#: harmless, a pattern its value must match or ``None`` for "takes no value"). From
#: what the product's own docs, tests and README use (window size, language, user
#: agent) plus the window-position and start-maximized switches that belong with the
#: size. Everything else, including every switch that names a file or a program, is
#: refused by name. The three FEATURE-LIST switches (``--disable-features``,
#: ``--enable-blink-features``, ``--disable-blink-features``) are not here either
#: (audit 6, N1): their names are an open vocabulary of hundreds of Chrome features,
#: some of which are security controls (site isolation, private-network protection,
#: the certificate and resolver paths), and no short list of names can be argued from
#: a use the product documents; the product sets the ones it needs itself.
#: ``--proxy-server`` is not here: ``browser_trust`` refuses a login in a proxied
#: browser.
ALLOWED_SWITCHES: dict[str, tuple[str, re.Pattern[str] | None]] = {
    "window-size": ("the window's size, in pixels", _DIGITS),
    "window-position": ("the window's place on the screen", _POSITION),
    "start-maximized": ("opens the window maximised", None),
    "lang": ("the browser's UI language", _LOCALE),
    "accept-lang": ("the Accept-Language header", _LOCALES),
    "user-agent": ("the User-Agent string (text only)", _ANY),
}

#: Why a switch is not on the list, for the ones people ask about.
_WHY_NOT = (
    "it can name a file Chrome writes or a program Chrome starts, change name "
    "resolution or certificate checking, or load code"
)


def _start_page_refusal(argument: str) -> str | None:
    """Why a bare (non-switch) argument, which Chrome opens as a start page, is not
    allowed, or ``None``: only ``about:blank`` and http(s) URLs that are not Chrome's
    own debug port are."""
    from stealth_chrome_devtools_mcp.embedded import debug_port_guard

    text = argument.strip()
    if text == "about:blank":
        return None
    scheme = text.split(":", 1)[0].lower()
    if (
        scheme not in {"http", "https"}
        or _ANY.fullmatch(text) is None
        or " " in text
        or debug_port_guard.refusal(text) is not None
    ):
        return "only http(s) start pages are allowed"
    return None


def check_browser_args(arguments: object) -> None:
    """Raise unless every entry of ``browser_args`` is on :data:`ALLOWED_SWITCHES`
    (with a value of the right shape) or is an http(s) start page. The message names
    the SWITCH, never its value (a value can be a path or a credential)."""
    from stealth_chrome_devtools_mcp.embedded import browser_trust

    if arguments is None:
        return
    if not isinstance(arguments, (list, tuple)):
        raise refused("browser_args", "it must be a list of switches")
    for argument in arguments:
        if not isinstance(argument, str):
            raise refused("browser_args", "every entry must be a string")
        name = browser_trust.switch_name(argument)
        if name is None:
            reason = _start_page_refusal(argument)
            if reason is not None:
                raise refused("a browser_args entry that is not a switch", reason)
            continue
        allowed = ALLOWED_SWITCHES.get(name)
        if allowed is None:
            raise refused(
                f"the browser switch --{name}",
                f"{_WHY_NOT}; only {', '.join('--' + n for n in ALLOWED_SWITCHES)} "
                "are accepted",
            )
        text = browser_trust.canonical_switch(argument).strip()
        value = text.partition("=")[2] if "=" in text else None
        pattern = allowed[1]
        if (pattern is None and value is not None) or (
            pattern is not None and (value is None or pattern.fullmatch(value) is None)
        ):
            raise refused(
                f"the browser switch --{name}",
                f"its value is not of the allowed shape ({allowed[0]})",
            )


def check_user_data_dir(value: object) -> None:
    """Raise unless ``user_data_dir`` is a session NAME (the deprecated path alias is
    refused: it is the one spelling that can put a profile, and Chrome's files,
    anywhere)."""
    from stealth_chrome_devtools_mcp.embedded import profile_seed

    if not value:
        return
    try:
        profile_seed.require_name("user_data_dir", str(value), path_hint="")
    except ToolError:
        raise refused(
            "user_data_dir given as a path",
            "it takes a session NAME here; use session=NAME",
        ) from None


def _check_spawn(arguments: dict[str, object]) -> None:
    check_browser_args(arguments.get("browser_args"))
    check_user_data_dir(arguments.get("user_data_dir"))


#: Tool -> (the arguments it checks, the check). Run only while the mode is on and
#: only when the call names one of those arguments.
_Rule = tuple[tuple[str, ...], "Callable[[dict[str, object]], None]"]
_ARGUMENT_RULES: dict[str, _Rule] = {
    "spawn_browser": (("browser_args", "user_data_dir"), _check_spawn),
}

#: Every tool :func:`admit` decides something about (the registry binds the call's
#: arguments only for these).
GATED = RUNS_CALLER_PYTHON | RAW_CDP | frozenset(_ARGUMENT_RULES)


def admit(tool: str, arguments: dict[str, object] | None = None) -> None:
    """Let ``tool`` run, or raise ``CallerError`` while a vault exists.

    A tool outside :data:`GATED` is none of this module's business. ``arguments`` is
    the call's bound arguments (only those the caller passed)."""
    if tool in RUNS_CALLER_PYTHON:
        _admit_caller_python(tool)
        return
    if tool in RAW_CDP:
        if active():
            raise refused(
                tool,
                "raw CDP can point Chrome's downloads at any folder, read a local "
                "file into a page, load code or carry another message inside "
                "Target.sendMessageToTarget, so no list of methods is safe",
            )
        return
    rule = _ARGUMENT_RULES.get(tool)
    if (
        rule is not None
        and arguments
        and any(name in arguments for name in rule[0])
        and active()
    ):
        rule[1](arguments)


# ---------------------------------------------------------------------- file URLs


def webui_refusal() -> str:
    """The sentence for a ``chrome://`` / ``chrome-untrusted://`` / ``devtools://``
    page. ``debug_port_guard`` decides whether a URL or a target IS one, and whether
    the mode is on, and asks this for the words."""
    return refusal_text(
        "a browser-internal (chrome://) page",
        "such a page can send commands to the browser itself",
    )


def file_url_refusal() -> str:
    """The sentence for a ``file:`` URL. ``debug_port_guard`` decides whether a URL
    IS a ``file:`` one (it owns the one reading of a URL the way Chrome reads it) and
    whether the mode is on, and asks this for the words."""
    return refusal_text("a file: URL", "a page at one could read any file you can read")


# ---------------------------------------------------------------------- the export


def export_format(fmt: str) -> str:
    """The format ``export_debug_logs`` may use: ``fmt`` itself outside the mode; in
    it, JSON always (``auto`` means JSON, and an explicit pickle is refused)."""
    if not active():
        return fmt
    if str(fmt).strip().lower() in {"pickle", "gzip-pickle"}:
        raise refused(
            f"export_debug_logs format={fmt!r}",
            "a pickle keeps an agent's text raw inside a file that other programs "
            "read; this tool writes JSON only",
        )
    return "json"


# --------------------------------------------------------------------- downloads

DOWNLOAD_FOLDER = "stealth-mcp-downloads"
_pin_cache: dict[str, dict[str, object]] = {}


def download_dir() -> Path:
    """The one folder every page download lands in while the mode is on. Under the
    person's own ``Downloads``, which no secret or code location contains."""
    return Path.home() / "Downloads" / DOWNLOAD_FOLDER


def download_pin() -> dict[str, object] | None:
    """The ``Browser.setDownloadBehavior`` parameters to pin a browser to while the
    mode is on, or ``None`` when it is off. BLOCKING (it resolves paths): call it in
    a thread.

    ``allowAndName``: Chrome saves each download under a generated name with no
    extension, so a page can neither choose the folder (the guard sets it, and raw
    CDP is refused) nor a name that code loads by (``evil.pth``). When the folder
    cannot be made, or lies where ``path_confinement`` refuses a write, downloads
    are DENIED instead: a pin that cannot be made safe is no pin."""
    if not active():
        return None
    folder = download_dir()
    key = str(folder)
    cached = _pin_cache.get(key)
    if cached is not None:
        return cached
    from stealth_chrome_devtools_mcp.embedded import path_confinement

    pin: dict[str, object]
    try:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        real = path_confinement.refuse_write_path(folder / "download", tool="downloads")
        pin = {
            "behavior": "allowAndName",
            "downloadPath": str(real.parent),
            "eventsEnabled": False,
        }
    except (OSError, ToolError):
        pin = {"behavior": "deny"}
    _pin_cache[key] = pin
    return pin


# ------------------------------------------------------------------------ status


def describe() -> list[str]:
    """One line per restriction, for ``init``'s notice and ``status``."""
    return [item.summary for item in RESTRICTIONS]


def _reset_for_tests() -> None:
    """Forget what ran and what was validated. Tests only: no tool, route or CLI
    command reaches this."""
    global _ran  # noqa: PLW0603  PERMANENT(test hook)
    with _lock:
        _ran = None
    _pin_cache.clear()
