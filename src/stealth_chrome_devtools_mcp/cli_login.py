"""The ``login`` verb (T-19366): a person's side of the login vault.

``stealthy login init | add | list | edit | remove | unlock | lock | status |
audit``. The vault's format and the meaning of an entry are ``embedded.vault_crypto``'s
and ``embedded.vault_store``'s; this file is the parser, the prompts and the one
HTTP conversation with a running backend (the unlock routes of
``embedded.vault_routes``). Its own module because ``cli.py`` is at its line cap,
on ``cli_supervise``'s precedent.

**The rule: no command prints a password, a passphrase or a key.** There is no
"show password" verb. Secrets are read from a hidden prompt, or from stdin with
``--password-stdin``, and are never an argument or an environment variable: the
parser has no ``--password VALUE`` option, and abbreviations are off so a
half-typed flag cannot turn into one.

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
import getpass
import json
import re
import sys
import urllib.parse
from http import HTTPStatus
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

    from stealth_chrome_devtools_mcp.embedded.vault_crypto import Document

VERB = "login"

_DURATION = re.compile(r"(\d+(?:\.\d+)?)([smhd]?)")
_UNIT_SECONDS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}
_AUDIT_DEFAULT_LINES = 20


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
    verbs = login.add_subparsers(dest="login_command", required=True)

    def verb(name: str, help_text: str) -> argparse.ArgumentParser:
        return verbs.add_parser(name, help=help_text, allow_abbrev=False)

    verb("init", "create the vault (asks for a new passphrase twice)")

    add = verb("add", "add a login; the password is asked for at a hidden prompt")
    add.add_argument("label", help="a-z, 0-9 and '-'; what agents call it by")
    add.add_argument("--site", required=True, help="the site this login is for")
    kind = add.add_mutually_exclusive_group(required=True)
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
    edit.add_argument("label")
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
    remove.add_argument("label")

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
    verb("status", "is there a vault, and is it unlocked")

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
    return _new_secret("password")


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
        key = vault_crypto.derive_key(_hidden("passphrase: "), document)
        plaintext = vault_crypto.open_sealed(key, document)
    except vault_crypto.VaultError:
        raise _RefusedError("wrong passphrase, or the vault file was changed") from None
    entries = plaintext.get("entries")
    if not isinstance(entries, dict):
        raise _RefusedError("the vault file is not a valid vault document")
    # The nonce names the version of the file this was read from: `_write` passes
    # it back, so a freeze the backend made meanwhile is refused, not erased.
    header = {"v": document["v"], "kdf": document["kdf"], "nonce": document["nonce"]}
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


def _cmd_init(_args: argparse.Namespace) -> int:
    from stealth_chrome_devtools_mcp.embedded import vault_crypto

    _require_terminal("init")
    path = _store().VAULT_PATH
    if path.exists():
        raise _RefusedError(f"a vault already exists at {path}; nothing was changed")
    header = vault_crypto.new_header()
    key = vault_crypto.derive_key(_new_secret("new passphrase"), header)
    _write(header, key, {})
    print(f"created {path}")
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


def _cmd_add(args: argparse.Namespace) -> int:
    _check_add(args)
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
        username = args.user or input("username: ").strip()
        entries[args.label] = {
            "kind": "password",
            "site": args.site,
            "login_url": args.login_url,
            "hosts": [h.lower() for h in args.host] or ([host] if host else []),
            "username": username,
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
    try:
        key = vault_crypto.derive_key(_hidden("passphrase: "), document)
        vault_crypto.open_sealed(key, document)
    except vault_crypto.VaultError:
        raise _RefusedError("wrong passphrase, or the vault file was changed") from None
    code, reply = _post_key(key, args.duration)
    if code == HTTPStatus.OK:
        until = (
            f" for {args.duration:g}s" if args.duration else " until the backend exits"
        )
        print(f"unlocked ({reply.get('entries')} logins){until}")
        return 0
    if code == HTTPStatus.UNAUTHORIZED:
        raise _RefusedError("the backend refused this shell's token (HTTP 401)")
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
    path = _store().VAULT_PATH
    print(f"vault: {path} ({'exists' if path.exists() else 'not created'})")
    try:
        code, state = _request("GET", _status_path())
    except _NoBackendError as exc:
        print(f"backend: {exc}")
        return 0
    if code != HTTPStatus.OK:
        raise _RefusedError(
            f"the backend did not answer the status request (HTTP {code})"
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
}


def cmd_login(args: argparse.Namespace) -> int:
    """Run one ``login`` subcommand; a refusal is one line on stderr and its code."""
    from stealth_chrome_devtools_mcp import cli_call

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
