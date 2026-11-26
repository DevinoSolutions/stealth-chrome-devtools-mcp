"""The ``login`` verb (T-19366): a person's side of the login vault.

``stealthy login init | add | list | edit | remove | unlock | lock | status |
audit | destroy``, and ``bind | export`` from ``cli_login_device``. The vault's
format and the meaning of an entry are ``embedded.vault_crypto``'s and
``embedded.vault_store``'s; this file is the
parser, the prompts and the one HTTP conversation with a running backend (the
unlock routes of ``embedded.vault_routes``). Its own module because ``cli.py`` is
at its line cap, on ``cli_supervise``'s precedent.

**The rule: no command prints a password, a passphrase or a key.** There is no
"show password" verb. Secrets are read from a hidden prompt, or from stdin with
``--password-stdin``, and are never an argument or an environment variable: the
parser has no ``--password VALUE`` option, and abbreviations are off so a
half-typed flag cannot turn into one.

**Vault mode (``embedded.vault_mode``).** While a vault file exists every raw or
unbounded tool capability is refused in every backend: caller Python, raw CDP,
browser switches off a short list, ``file:`` URLs, pickle exports and page
downloads outside one folder. So ``init`` names each in a few words before it asks for a
passphrase, refuses while any backend the registry knows has run caller Python
(or cannot say), and checks again once the file exists (one of the two always sees
the other); ``unlock`` and ``status`` run the same census; ``destroy`` locks every
backend the registry records and deletes the file, which turns the mode off.

**Who may type the passphrase.** ``init`` and ``unlock`` refuse to run unless
stdin is a terminal, so a script or an agent cannot feed them. ``add``, ``edit``
and ``remove`` ask for the passphrase each time and rewrite the file themselves
(the backend writes the vault in exactly one case, freezing a rejected login);
``list`` asks because the labels are encrypted too.

Module scope imports only the standard library; every ``embedded`` import is
inside the function that needs it, so building the parser for any other verb
costs nothing.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import os
import re
import sys
import urllib.parse
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING

from stealth_chrome_devtools_mcp import cli_login_device

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from types import ModuleType

    import httpx

    from stealth_chrome_devtools_mcp.embedded.vault_crypto import Document

VERB = "login"

_DURATION = re.compile(r"(\d+(?:\.\d+)?)([smhd]?)")
_UNIT_SECONDS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}
_AUDIT_DEFAULT_LINES = 20
_SHORT_PASSPHRASE = 12


class _RefusedError(Exception):
    """A failure the person is told about in one line; ``code`` is the exit code."""

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


class _NoBackendError(_RefusedError):
    """No backend answered (exit 3): the one refusal some verbs treat as news."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code=3)


# --------------------------------------------------------------------- parser


def duration(text: str) -> float:
    found = _DURATION.fullmatch(text.strip().lower())
    if found is None or float(found.group(1)) <= 0:
        raise ValueError(
            f"{text!r} is not a duration: use a number with s, m, h or d (8h)"
        )
    return float(found.group(1)) * _UNIT_SECONDS[found.group(2)]


def _secret_source(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="read the password from the first line of stdin instead of a hidden "
        "prompt (the only non-interactive way; never an argument)",
    )


def add_parsers(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Contribute ``login`` to ``cli``'s ONE parser tree."""
    login = sub.add_parser(
        VERB,
        allow_abbrev=False,
        help="the login vault: store logins an agent can use without ever "
        "seeing the password (stealthy login --help)",
        description="Store logins in an encrypted vault, and unlock it so agents "
        "can ask the backend to sign in by label. Nothing here prints a password.",
    )
    verbs = login.add_subparsers(dest="login_command")
    login.set_defaults(login_help=login.print_help)  # bare `stealthy login`

    def verb(name: str, help_text: str) -> argparse.ArgumentParser:
        return verbs.add_parser(name, help=help_text, allow_abbrev=False)

    verb("init", "create the vault (asks for a new passphrase twice)")

    add = verb("add", "add a login; the password is asked for at a hidden prompt")
    add.epilog = "Whatever is left out is asked for when this runs at a terminal."
    add.add_argument(
        "label", nargs="?", help="a-z, 0-9 and '-'; what agents call it by"
    )
    add.add_argument("--site", help="the site this login is for")
    kind = add.add_mutually_exclusive_group()
    kind.add_argument("--login-url", help="https:// page that shows the sign-in form")
    kind.add_argument(
        "--via", metavar="OTHER", help="sign in with that login's account"
    )
    add.add_argument(
        "--host",
        action="append",
        default=[],
        help="a host the password may be typed on (repeat; default: the login "
        "URL's host)",
    )
    add.add_argument("--user", help="the username (asked for when omitted)")
    add.add_argument(
        "--show-username",
        action="store_true",
        help="let list_logins show this username to an agent",
    )
    add.add_argument(
        "--allow-consent",
        action="store_true",
        help="(--via) let a later phase accept a consent screen",
    )
    _secret_source(add)

    listing = verb("list", "labels, kinds, sites and hosts; never a password")
    listing.add_argument(
        "--usernames", action="store_true", help="also print each username"
    )

    edit = verb("edit", "change a login and clear its frozen state")
    edit.add_argument("label", nargs="?")
    edit.add_argument("--site")
    edit.add_argument("--login-url")
    edit.add_argument(
        "--host",
        action="append",
        default=None,
        help="replace the allowed hosts (repeat)",
    )
    edit.add_argument("--user")
    edit.add_argument("--show-username", action=argparse.BooleanOptionalAction)
    edit.add_argument("--via")
    edit.add_argument("--allow-consent", action=argparse.BooleanOptionalAction)
    edit.add_argument(
        "--password",
        action="store_true",
        help="ask for a new password (a flag, not a value)",
    )
    _secret_source(edit)

    remove = verb("remove", "remove a login")
    remove.add_argument("label", nargs="?")

    unlock = verb(
        "unlock", "put the key in the running backend's memory (a terminal only)"
    )
    unlock.add_argument(
        "--for",
        dest="duration",
        type=duration,
        metavar="DURATION",
        help="lock again after this long (30m, 8h, 1d); default: until the "
        "backend exits",
    )

    verb("lock", "drop the key from the running backend")
    cli_login_device.add_verbs(verb)
    verb("status", "is there a vault, and is it unlocked")
    verb(
        "destroy",
        "delete the vault and every stored login (a terminal only); turns vault "
        "mode off, so the tools it restricts work again",
    )

    audit = verb("audit", "print the login attempts the backend recorded")
    audit.add_argument(
        "-n", type=int, default=_AUDIT_DEFAULT_LINES, help="how many lines (20)"
    )


# --------------------------------------------------------------------- prompts


def _require_terminal(what: str) -> None:
    if not sys.stdin.isatty():
        raise _RefusedError(
            f"`stealthy login {what}` asks for the passphrase at a terminal and "
            "will not take it from a pipe or a script",
            code=2,
        )


def _hidden(prompt: str) -> str:
    return getpass.getpass(prompt)


def _missing(flags: Iterable[str]) -> _RefusedError:
    return _RefusedError(
        f"missing {', '.join(flags)}; nothing can be asked without a terminal, "
        "so pass each on the command line",
        code=2,
    )


def _ask(name: str, flag: str, question: str, *, optional: bool = False) -> str:
    """One visible question, asked until its answer fits *name*'s rule (an empty
    one too, unless *optional*). Never a secret: those go through ``_hidden``."""
    while True:
        try:
            answer = input(f"{question}: ").strip()
        except EOFError:  # Windows calls NUL a terminal; its first read ends
            raise _missing([flag]) from None
        problem = _store().problem(name, answer) if answer else None
        if problem:
            print(f"  {problem}")
        elif answer or optional:
            return answer


def _fill(
    args: argparse.Namespace,
    questions: dict[str, tuple[str, str]],
    also: Iterable[str] = (),
) -> None:
    """Ask for each of *questions* (attribute -> its flag and its question) that
    the command line left out. Without a terminal nothing can be asked, so the
    refusal names every one that is missing, and *also* what a later question
    would have asked for."""
    missing = {name: q for name, q in questions.items() if not getattr(args, name)}
    flags = [flag for flag, _question in missing.values()] + list(also)
    if flags and not sys.stdin.isatty():
        raise _missing(flags)
    for name, (flag, question) in missing.items():
        setattr(args, name, _ask(name, flag, question))


_LABEL = ("LABEL", "label")


def _new_secret(what: str) -> str:
    """A secret typed twice, so a typo cannot be stored unnoticed."""
    first = _hidden(f"{what}: ")
    if not first:
        raise _RefusedError(f"{what} cannot be empty")
    if _hidden(f"{what} (again): ") != first:
        raise _RefusedError(f"the two {what} entries differ; nothing was changed")
    return first


def _password(args: argparse.Namespace) -> str:
    if args.password_stdin:
        line = sys.stdin.readline().rstrip("\r\n")
        if not line:
            raise _RefusedError(
                "--password-stdin: stdin had no password on its first line"
            )
        return line
    return _new_secret(f"password for {args.label}")


# ----------------------------------------------------------------------- vault


def _store() -> ModuleType:
    from stealth_chrome_devtools_mcp.embedded import vault_store

    return vault_store


def _read_header() -> Document:
    from stealth_chrome_devtools_mcp.embedded import vault_crypto

    try:
        return vault_crypto.read_file(_store().VAULT_PATH)
    except vault_crypto.VaultError as exc:
        raise _RefusedError(f"{exc}; `stealthy login init` creates one") from None


def _open() -> tuple[Document, bytes, dict[str, dict[str, object]]]:
    """``(header, key, entries)``: ask for the passphrase and decrypt the file."""
    from stealth_chrome_devtools_mcp.embedded import vault_crypto

    document = _read_header()
    try:
        key = vault_crypto.derive_key(_hidden("vault passphrase: "), document)
        plaintext = vault_crypto.open_sealed(key, document)
    except vault_crypto.VaultError as exc:
        raise _RefusedError(vault_crypto.refusal(exc)) from None
    entries = plaintext.get("entries")
    if not isinstance(entries, dict):
        raise _RefusedError("the vault file is not a valid vault document")
    # The nonce names the version of the file this was read from: `_write` passes
    # it back, so a freeze the backend made meanwhile is refused, not erased.
    header = {**vault_crypto.header_of(document), "nonce": document["nonce"]}
    return header, key, entries


def _write(header: Document, key: bytes, entries: dict[str, dict[str, object]]) -> None:
    """Validate, then replace the file. Nothing is written for a bad entry."""
    from stealth_chrome_devtools_mcp.embedded import vault_crypto

    store = _store()
    try:
        store.validate(entries)
    except ValueError as exc:
        raise _RefusedError(f"{exc}; nothing was changed") from None
    try:
        vault_crypto.write_file(
            store.VAULT_PATH,
            vault_crypto.seal(key, header, {"entries": entries}),
            expect=header.get("nonce"),
        )
    except vault_crypto.VaultChanged:
        raise _RefusedError(
            "the vault file changed while you were editing it (a login was frozen, "
            "or another edit landed); nothing was written; run the command again"
        ) from None


# --------------------------------------------------------------------- backend


def _request(method: str, path: str, body: object = None) -> tuple[int, dict]:
    """One request to the running backend's vault routes: ``(status, json)``.

    Raises :class:`_NoBackendError` when no backend answers. Never started on
    demand: starting a backend is not this verb's business, and an unlock has
    nothing to unlock in one that does not exist. ``trust_env=False`` because the
    body can carry the key and nothing may route it through ``HTTP_PROXY``.
    """
    import httpx

    from stealth_chrome_devtools_mcp.embedded import backend_token, singleton

    status, port = singleton._probe_backend_status()
    if status != "responsive" or port is None:
        raise _NoBackendError(
            f"no backend is serving this shell (state: {status}); any `stealthy` "
            "tool call starts one"
        )
    url = urllib.parse.urlsplit(singleton._backend_http_url(port))._replace(path=path)
    try:
        with httpx.Client(trust_env=False, timeout=10.0) as client:
            reply = client.request(
                method, url.geturl(), json=body, headers=backend_token.auth_headers()
            )
    except httpx.HTTPError as exc:
        raise _NoBackendError(
            f"could not reach the backend ({type(exc).__name__})"
        ) from None
    try:
        payload = reply.json()
    except ValueError:
        payload = {}
    return reply.status_code, payload if isinstance(payload, dict) else {}


def _post_key(key: bytes, ttl_seconds: float | None) -> tuple[int, dict]:
    import base64

    from stealth_chrome_devtools_mcp.embedded import vault_routes

    return _request(
        "POST",
        vault_routes.UNLOCK_PATH,
        {"key": base64.b64encode(key).decode("ascii"), "ttl_seconds": ttl_seconds},
    )


def _status_path() -> str:
    from stealth_chrome_devtools_mcp.embedded import vault_routes

    return vault_routes.STATUS_PATH


def _reload_backend(key: bytes) -> None:
    """After a write: if a running backend holds the vault unlocked, hand it the
    key again so it registers the new passwords at once. Keeps the unlock's
    remaining time (a limited unlock must not become unlimited here)."""
    try:
        code, state = _request("GET", _status_path())
        if code != HTTPStatus.OK or state.get("unlocked") is not True:
            return
        code, _ = _post_key(key, state.get("expires_in"))
    except _NoBackendError:
        return
    except _RefusedError as exc:
        print(f"note: the running backend was not refreshed: {exc}", file=sys.stderr)
        return
    if code != HTTPStatus.OK:
        print(
            f"note: the running backend did not take the new vault (HTTP {code}); "
            "run `stealthy login unlock` again",
            file=sys.stderr,
        )


# -------------------------------------------------------------------- commands


@dataclass
class _Answer:
    """What one recorded backend said to one request."""

    port: int
    entry: dict[str, object]
    reply: httpx.Response


def _registry_problem() -> str | None:
    """Why the backend registry cannot list the running backends, or ``None``. A
    missing file is no backends (documented); one that is there and cannot be read
    is "cannot say"."""
    from stealth_chrome_devtools_mcp.embedded import backend_registry

    path = backend_registry.SERVER_STATE_FILE
    try:
        os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        pass  # there, and not even readable: the same answer as a corrupt file
    if backend_registry.read_record(path) is None:
        return (
            "the backend registry (server.json) is unreadable, so the running "
            "backends cannot be listed"
        )
    return None


def _backend_gone(entry: dict[str, object]) -> bool:
    """Whether a recorded backend that refused a connection is DEAD: its process is
    gone, or the pid now belongs to something that is not this product. A process
    that is still running (or cannot be read) is "cannot say", never "gone"."""
    import psutil

    from stealth_chrome_devtools_mcp.embedded import pid_alive

    pid = entry.get("pid")
    if not isinstance(pid, int):
        return False
    # THE one liveness question (T-19642); a pid that cannot be asked about is
    # "cannot say", so unknown=True (never read as dead)
    if not pid_alive.alive(pid, unknown=True):
        return True
    # alive: is it still this product, or a pid something else reused? NOT
    # ``pid_alive.same_process`` / ``identified``: they identify a process by the
    # pid PLUS the start time that was recorded with it, and a ``server.json`` entry
    # (``backend_registry.record_backend``: port, version, pid, source_fingerprint,
    # display_context) records no start time, so there is nothing to compare. The
    # command line is the one identity this record can be checked against (audit 7
    # F3); when the entry gains a start time, replace this with ``same_process``
    # (``unknown=True``).
    try:
        return not any(
            "stealth" in part.lower() for part in psutil.Process(pid).cmdline()
        )
    except psutil.NoSuchProcess:
        return True
    except (psutil.Error, OSError, SystemError):
        return False


def _ask_each_backend(
    path: str, method: str = "GET"
) -> tuple[list[_Answer], list[str]]:
    """Ask every backend the registry records: ``(answers, problems)``. A problem is
    a backend nobody can vouch for: it did not answer, or refused a connection while
    its process runs, or the registry itself cannot be read."""
    import httpx

    from stealth_chrome_devtools_mcp.embedded import (
        backend_registry,
        backend_token,
        singleton,
    )

    answers: list[_Answer] = []
    problems = []
    registry = _registry_problem()
    if registry is not None:
        problems.append(registry)
    for entry in backend_registry.read_backends(backend_registry.SERVER_STATE_FILE):
        port = entry.get("port")
        if not isinstance(port, int):
            continue
        url = urllib.parse.urlsplit(singleton._backend_http_url(port))._replace(
            path=path
        )
        try:
            with httpx.Client(trust_env=False, timeout=5.0) as client:
                reply = client.request(
                    method, url.geturl(), headers=backend_token.auth_headers()
                )
        except httpx.ConnectError:
            if not _backend_gone(entry):
                problems.append(
                    f"the backend on port {port} (pid {entry.get('pid', '?')}) "
                    "refused a connection but its process is still running"
                )
            continue
        except httpx.HTTPError as exc:
            problems.append(
                f"the backend on port {port} did not answer ({type(exc).__name__})"
            )
            continue
        answers.append(_Answer(port, entry, reply))
    return answers, problems


def _backend_problems() -> list[str]:
    """One sentence per backend the registry knows that has run caller Python, or
    cannot say it has not. Empty means every backend is clean."""
    from stealth_chrome_devtools_mcp.embedded import vault_routes

    answers, problems = _ask_each_backend(vault_routes.CALLER_PYTHON_PATH)
    for answer in answers:
        ran: object = 0  # anything but a clear answer is "cannot say"
        if answer.reply.status_code == HTTPStatus.OK:
            with contextlib.suppress(ValueError):
                payload = answer.reply.json()
                if isinstance(payload, dict) and "ran" in payload:
                    ran = payload["ran"]
        if ran is None:
            continue
        problems.append(
            f"the backend on port {answer.port} has run {ran} in this session"
            if isinstance(ran, str)
            else f"the backend on port {answer.port} (release "
            f"{answer.entry.get('version', '?')}) cannot say whether it has run "
            f"caller Python (HTTP {answer.reply.status_code}); a release before "
            "vault mode has no such gate"
        )
    return problems


def _refuse_while_resident(problems: list[str], outcome: str) -> None:
    if problems:
        raise _RefusedError(
            f"{outcome}: "
            + "; ".join(problems)
            + ". Code those tools ran could read the vault's key, so restart "
            "each backend (`stealth-chrome-devtools restart`), then run the "
            "command again."
        )


#: Said once, in words, when ``init`` is interrupted after the file was written.
_LEFT_BEHIND = (
    "the vault file was created at {path} and could not be removed ({reason}); "
    "`stealthy login destroy` removes it"
)


def _cmd_init(_args: argparse.Namespace) -> int:
    from stealth_chrome_devtools_mcp.embedded import vault_crypto, vault_mode

    _require_terminal("init")
    path = _store().VAULT_PATH
    if vault_mode.vault_exists():
        raise _RefusedError(f"a vault already exists at {path}; nothing was changed")
    _refuse_while_resident(_backend_problems(), "no vault was created")
    print(vault_mode.notice())
    passphrase = _new_secret("new passphrase")
    if len(passphrase) < _SHORT_PASSPHRASE:
        print(
            f"warning: this passphrase has fewer than {_SHORT_PASSPHRASE} "
            "characters. Anyone who gets a copy of the vault file can try "
            "guesses offline; `stealthy login destroy` and `init` start over.",
            file=sys.stderr,
        )
    header = vault_crypto.new_header()  # after the prompts: it may store a key
    try:
        _write(header, vault_crypto.derive_key(passphrase, header), {})
        # The file exists now, so every tool admitted from here on is refused. One
        # that was admitted before it is seen here: take the vault back rather than
        # keep a key's worth of secrets next to code that is already running.
        problems = _backend_problems()
        if problems:
            path.unlink(missing_ok=True)
            _refuse_while_resident(problems, "no vault was created")
    except BaseException:
        # An interrupt, or a crash, between writing the file and finishing the
        # check: the file goes, or the person is told it stays.
        if vault_mode.vault_exists():
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                raise _RefusedError(
                    _LEFT_BEHIND.format(
                        path=path, reason=exc.strerror or type(exc).__name__
                    )
                ) from None
        cli_login_device.forget_unused(header)
        raise
    print(f"created {path}")
    print(cli_login_device.describe(header))
    print("next: `stealthy login add` stores a login (it asks for each detail),")
    print("      then `stealthy login unlock` lets agents use it")
    return 0


def _lock_everywhere() -> list[str]:
    """Drop the key in every backend the registry records and in the one this shell
    resolves; the sentences are the ones that could not be locked."""
    from stealth_chrome_devtools_mcp.embedded import vault_routes

    answers, warnings = _ask_each_backend(vault_routes.LOCK_PATH, "POST")
    warnings.extend(
        f"the backend on port {a.port} did not lock (HTTP {a.reply.status_code})"
        for a in answers
        if a.reply.status_code != HTTPStatus.OK
    )
    try:
        code, _ = _request("POST", vault_routes.LOCK_PATH)  # the resolved one
    except _NoBackendError:
        return warnings
    if code != HTTPStatus.OK:
        warnings.append(f"the backend this shell resolves did not lock (HTTP {code})")
    return warnings


def _cmd_destroy(_args: argparse.Namespace) -> int:
    from stealth_chrome_devtools_mcp.embedded import vault_mode

    _require_terminal("destroy")
    path = _store().VAULT_PATH
    if not vault_mode.vault_exists():
        raise _RefusedError("there is no vault to delete")
    if path.is_dir() and not path.is_symlink():
        raise _RefusedError(
            f"{path} is a directory, not a vault file; remove it yourself if it is "
            "in the way (vault mode stays on while anything is at that path)"
        )
    print(
        f"This deletes {path} and every login in it (the audit log stays), and "
        "turns vault mode off in every backend: the tools it restricts work again."
    )
    if input("type DESTROY to go on: ").strip() != "DESTROY":
        raise _RefusedError("nothing was deleted")
    warnings = _lock_everywhere()  # drop every key a backend still holds, first
    forget = cli_login_device.forgetter()
    try:
        path.unlink()
    except OSError as exc:
        raise _RefusedError(
            f"could not delete {path} ({exc.strerror or type(exc).__name__}); "
            "vault mode stays on"
        ) from None
    forget()
    for warning in warnings:
        print(
            f"warning: {warning}; it may still hold the key until it restarts",
            file=sys.stderr,
        )
    print("deleted the vault")
    return 0


def _check_add(args: argparse.Namespace) -> None:
    """Refuse a flag that does not fit the kind BEFORE asking for a passphrase."""
    if args.via:
        stray = [
            flag
            for flag, given in (
                ("--host", args.host),
                ("--user", args.user),
                ("--show-username", args.show_username),
                ("--password-stdin", args.password_stdin),
            )
            if given
        ]
        if stray:
            raise _RefusedError(
                f"{', '.join(stray)} do not apply to a --via login", code=2
            )
    elif args.allow_consent:
        raise _RefusedError("--allow-consent applies to a --via login", code=2)
    elif args.password_stdin and not args.user:
        raise _RefusedError("--password-stdin reads stdin, so give --user", code=2)


def _ask_add(args: argparse.Namespace) -> None:
    """Ask for what an ``add`` was not given: its label, its site, how it signs
    in (an empty sign-in page means through another login) and its username.
    Every visible question comes before the first hidden one."""
    kind_known = bool(args.login_url or args.via)
    if kind_known:
        _check_add(args)  # what the flags alone decide, before any question
    later = [] if kind_known else ["--login-url or --via"]
    if args.login_url and not args.user:
        later.append("--user")
    _fill(
        args,
        {
            "label": ("LABEL", "label (a-z, 0-9 and '-'; what agents call it by)"),
            "site": ("--site", "site this login is for (example.com)"),
        },
        also=later,
    )
    if not kind_known:
        args.login_url = _ask(
            "login_url",
            "--login-url or --via",
            "sign-in page, https://... (empty: sign in through another login)",
            optional=True,
        )
        if not args.login_url:
            _fill(args, {"via": ("--via", "label of the login to sign in through")})
    _check_add(args)
    if not args.via:
        _fill(args, {"user": ("--user", "username")})


def _cmd_add(args: argparse.Namespace) -> int:
    _ask_add(args)
    header, key, entries = _open()
    if args.label in entries:
        raise _RefusedError(
            f"{args.label!r} already exists; `stealthy login edit` changes it"
        )
    if args.via:
        entries[args.label] = {
            "kind": "provider",
            "site": args.site,
            "via": args.via,
            "allow_consent": args.allow_consent,
        }
    else:
        host = urllib.parse.urlsplit(args.login_url).hostname
        entries[args.label] = {
            "kind": "password",
            "site": args.site,
            "login_url": args.login_url,
            "hosts": [h.lower() for h in args.host] or ([host] if host else []),
            "username": args.user,
            "password": _password(args),
            "show_username": args.show_username,
            "frozen": None,
        }
    _write(header, key, entries)
    _reload_backend(key)
    print(f"added {args.label}")
    return 0


def _row(label: str, entry: dict[str, object], usernames: bool) -> str:
    kind = entry.get("kind")
    if kind == "provider":
        detail = f"via {entry.get('via')}"
    else:
        hosts = entry.get("hosts")
        names = [str(host) for host in hosts] if isinstance(hosts, list) else []
        detail = f"hosts {','.join(names)}"
        if usernames:
            detail += f"  user {entry.get('username')}"
    state = f"FROZEN {entry['frozen']}" if entry.get("frozen") else "ok"
    return f"{label}  {kind}  {entry.get('site')}  {detail}  {state}"


def _cmd_list(args: argparse.Namespace) -> int:
    _header, _key, entries = _open()
    if not entries:
        print("the vault has no logins")
    for label in sorted(entries):
        print(_row(label, entries[label], args.usernames))
    return 0


def _cmd_edit(args: argparse.Namespace) -> int:
    _fill(args, {"label": _LABEL})
    header, key, entries = _open()
    entry = entries.get(args.label)
    if entry is None:
        raise _RefusedError(f"there is no login {args.label!r}")
    provider = entry.get("kind") == "provider"
    only_password = {
        "--login-url": args.login_url,
        "--host": args.host,
        "--user": args.user,
        "--show-username": args.show_username,
        "--password": args.password or None,
        "--password-stdin": args.password_stdin or None,
    }
    only_provider = {"--via": args.via, "--allow-consent": args.allow_consent}
    wrong = only_provider if not provider else only_password
    stray = [flag for flag, given in wrong.items() if given is not None]
    if stray:
        raise _RefusedError(
            f"{', '.join(stray)} do not apply to a "
            f"{'provider' if provider else 'password'} login",
            code=2,
        )
    changes = {
        "site": args.site,
        "login_url": args.login_url,
        "hosts": [h.lower() for h in args.host] if args.host else None,
        "username": args.user,
        "show_username": args.show_username,
        "via": args.via,
        "allow_consent": args.allow_consent,
    }
    entry.update(
        {field: value for field, value in changes.items() if value is not None}
    )
    if args.password or args.password_stdin:
        entry["password"] = _password(args)
    if not provider:
        entry["frozen"] = None
    _write(header, key, entries)
    _reload_backend(key)
    print(f"updated {args.label}")
    return 0


def _cmd_remove(args: argparse.Namespace) -> int:
    _fill(args, {"label": _LABEL})
    header, key, entries = _open()
    if args.label not in entries:
        raise _RefusedError(f"there is no login {args.label!r}")
    del entries[args.label]
    _write(header, key, entries)
    _reload_backend(key)
    print(f"removed {args.label}")
    return 0


def _cmd_unlock(args: argparse.Namespace) -> int:
    from stealth_chrome_devtools_mcp.embedded import vault_crypto

    _require_terminal("unlock")
    document = _read_header()
    # A vault restored from a backup, or copied here, never went through ``init``:
    # the census runs here too, before a passphrase is typed.
    _refuse_while_resident(_backend_problems(), "nothing was unlocked")
    try:
        key = vault_crypto.derive_key(_hidden("vault passphrase: "), document)
        vault_crypto.open_sealed(key, document)
    except vault_crypto.VaultError as exc:
        raise _RefusedError(vault_crypto.refusal(exc)) from None
    code, reply = _post_key(key, args.duration)
    if code == HTTPStatus.OK:
        until = (
            f" for {args.duration:g}s" if args.duration else " until the backend exits"
        )
        print(f"unlocked ({reply.get('entries')} logins){until}")
        return 0
    if code == HTTPStatus.UNAUTHORIZED:
        raise _RefusedError("the backend refused this shell's token (HTTP 401)")
    if code == HTTPStatus.CONFLICT and isinstance(reply.get("error"), str):
        raise _RefusedError(f"the backend refused the key: {reply['error']}")
    raise _RefusedError(f"the backend did not unlock (HTTP {code})")


def _cmd_lock(_args: argparse.Namespace) -> int:
    from stealth_chrome_devtools_mcp.embedded import vault_routes

    try:
        code, _ = _request("POST", vault_routes.LOCK_PATH)
    except _NoBackendError as exc:
        print(f"nothing to lock: {exc}")
        return 0
    if code != HTTPStatus.OK:
        raise _RefusedError(f"the backend did not lock (HTTP {code})")
    print("locked")
    return 0


def _cmd_status(_args: argparse.Namespace) -> int:
    from stealth_chrome_devtools_mcp.embedded import vault_mode

    path = _store().VAULT_PATH
    here = vault_mode.vault_exists()
    print(f"vault: {path} ({'exists' if here else 'not created'})")
    print(f"vault mode: {'ON' if here else 'OFF'}")
    if here:
        for line in vault_mode.describe():
            print(f"  - {line}")
        print(cli_login_device.describe_file())
    problems = _backend_problems()
    if problems:
        for problem in problems:
            print(f"backends: ! {problem}")
    else:
        print("backends: no registered backend has run caller Python")
    try:
        code, state = _request("GET", _status_path())
    except _NoBackendError as exc:
        print(f"backend: {exc}")
        return 0
    if code != HTTPStatus.OK:
        raise _RefusedError(
            f"the backend did not answer the status request (HTTP {code})"
        )
    if state.get("exists") is not here:
        print(
            "warning: this shell and the backend disagree about whether a vault "
            f"exists (shell: {here}, backend: {state.get('exists')}); they are "
            "using different home directories, and the backend is not in vault mode "
            "unless it sees the file"
        )
    if not state.get("unlocked"):
        print("backend: locked")
    elif state.get("expires_in") is None:
        print("backend: unlocked until it exits")
    else:
        print(f"backend: unlocked, {state['expires_in']:.0f}s left")
    return 0


def _cmd_audit(args: argparse.Namespace) -> int:
    path = _store().AUDIT_PATH
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        print("no login has been attempted yet")
        return 0
    for line in lines[-args.n :] if args.n > 0 else []:
        try:
            row = json.loads(line)
            print(
                f"{row['time']}  {row['label']}  {row['instance']}  "
                f"{row['caller']}  {row['outcome']}"
            )
        except (ValueError, KeyError, TypeError):
            print(line)
    return 0


_COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "init": _cmd_init,
    "add": _cmd_add,
    "list": _cmd_list,
    "edit": _cmd_edit,
    "remove": _cmd_remove,
    "unlock": _cmd_unlock,
    "lock": _cmd_lock,
    "status": _cmd_status,
    "audit": _cmd_audit,
    "destroy": _cmd_destroy,
    **cli_login_device.COMMANDS,
}


def cmd_login(args: argparse.Namespace) -> int:
    """Run one ``login`` subcommand; a refusal is one line on stderr and its code."""
    from stealth_chrome_devtools_mcp import cli_call

    if args.login_command is None:
        args.login_help()
        return 0
    try:
        return _COMMANDS[args.login_command](args)
    except _RefusedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.code
    except (KeyboardInterrupt, EOFError):
        print("cancelled; nothing was changed", file=sys.stderr)
        return cli_call.EXIT_INTERRUPTED


#: Verb name -> body, for ``cli``'s ``_DISPATCH`` (``cli_supervise.DISPATCH``'s shape).
DISPATCH: dict[str, Callable[[argparse.Namespace], int]] = {VERB: cmd_login}
