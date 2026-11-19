"""The login vault's entries, its in-memory unlock, freeze and audit (T-19366).

:mod:`vault_crypto` owns the file format; this module owns what the plaintext
MEANS and the one fact that makes the vault safe to leave on a machine an agent
can run a shell on: **the key exists only in this process's memory**, put there
by a person typing the passphrase (``stealthy login unlock``) and gone when the
process exits, when :func:`lock` is called, or when the unlock's ``--for`` period
passes.

**Plaintext, version 1** (see the design's §3)::

    {"entries": {"<label>": {"kind": "password", "site", "login_url", "hosts",
                             "username", "password", "show_username", "frozen"},
                 "<label>": {"kind": "provider", "site", "via", "allow_consent"}}}

**What this module hands out.** Never a password as text: :func:`entry` wraps it
in :class:`secret_registry.Secret`; :func:`public_entries` (what ``list_logins``
shows a model) has no ``password`` key and has a ``username`` only for an entry
whose owner set ``show_username``. :func:`unlock` registers every password with
:mod:`secret_registry` so Sentry, the logs and the debug ring mask it from then
until :func:`lock`.

**The file is the truth.** Entries are read from ``vault.json`` on every call,
decrypted with the held key (no KDF, so cheap), so an edit by the CLI is seen by
the next login without a second unlock. A key that no longer opens the file (the
passphrase was changed) locks the vault. The backend writes the file in exactly
one case, :func:`freeze`.

The paths are module attributes read at call time, so a test points them at a
temporary directory.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import threading
import time
import urllib.parse
from datetime import datetime, timezone

from stealth_chrome_devtools_mcp.embedded import (
    backend_registry,
    backend_token,
    caller_python,
    secret_registry,
    vault_crypto,
)
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError
from stealth_chrome_devtools_mcp.settings import get_settings

VAULT_PATH = backend_registry.STATE_DIR / "vault.json"
AUDIT_PATH = backend_registry.STATE_DIR / "vault-audit.jsonl"

#: ``http://127.0.0.1`` passes the login URL and origin checks only while
#: ``STEALTH_MCP_VAULT_ALLOW_HTTP_LOOPBACK`` is true (the test suite; unsafe).
LOOPBACK_HOST = "127.0.0.1"

_LABEL_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_HOST_RE = re.compile(
    r"(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*"
)
_KINDS = ("password", "provider")
_AUDIT_KEYS = ("label", "instance", "caller", "outcome")

_logger = logging.getLogger("stealth.vault_store")
_lock = threading.RLock()
_audit_lock = threading.Lock()
_key: bytes | None = None
_deadline: float | None = None  # time.monotonic(); None = until the process exits


class VaultLocked(Exception):  # noqa: N818  PERMANENT(the name the login tools and the plan share)
    """The vault has no key in memory (never unlocked, locked, or expired)."""


Entry = dict[str, object]


def loopback_allowed() -> bool:
    """Whether ``http://127.0.0.1`` is acceptable (the test-suite variable)."""
    return get_settings().vault_allow_http_loopback


# ---------------------------------------------------------------- validation


def validate(entries: dict[str, Entry]) -> None:
    """Raise ``ValueError`` naming the label and the rule for the first problem.

    The message never echoes a username or password."""
    if not isinstance(entries, dict):
        raise ValueError(  # noqa: TRY004  PERMANENT(validate's contract is one ValueError for every rule)
            "the vault's entries must be an object keyed by label"
        )
    for label, entry in entries.items():
        if not isinstance(label, str) or not _LABEL_RE.fullmatch(label):
            raise ValueError(
                f"entry {label!r}: label must be 1-63 characters of a-z, 0-9 and "
                "'-', starting with a letter or digit"
            )
        if not isinstance(entry, dict):
            raise _fail(label, "must be an object")
        kind = entry.get("kind")
        if kind not in _KINDS:
            raise ValueError(f"entry {label!r}: kind must be 'password' or 'provider'")
        _require_text(label, entry, "site")
        _check_flags(label, entry)
        if kind == "password":
            _validate_password_entry(label, entry)
        else:
            _validate_provider_entry(label, entry, entries)


def _fail(label: str, rule: str) -> ValueError:
    return ValueError(f"entry {label!r}: {rule}")


def _require_text(label: str, entry: Entry, field: str) -> None:
    value = entry.get(field)
    if not isinstance(value, str) or not value.strip():
        raise _fail(label, f"{field} must be a non-empty string")


def _check_flags(label: str, entry: Entry) -> None:
    for field in ("show_username", "allow_consent"):
        if field in entry and not isinstance(entry[field], bool):
            raise _fail(label, f"{field} must be true or false")
    if entry.get("frozen") is not None and not isinstance(entry["frozen"], str):
        raise _fail(label, "frozen must be null or a timestamp string")


def _validate_password_entry(label: str, entry: Entry) -> None:
    _require_text(label, entry, "username")
    _require_text(label, entry, "password")
    host = _login_url_host(entry.get("login_url"))
    if host is None:
        raise _fail(
            label,
            "login_url must be an https:// URL without credentials "
            "(http://127.0.0.1 only when the test variable is set)",
        )
    hosts = entry.get("hosts")
    if (
        not isinstance(hosts, list)
        or not hosts
        or not all(isinstance(h, str) and _HOST_RE.fullmatch(h) for h in hosts)
    ):
        raise _fail(
            label,
            "hosts must be a non-empty list of bare lowercase hostnames "
            "(no scheme, port, path or wildcard)",
        )
    if host not in hosts:
        raise _fail(label, f"hosts must include the login URL's host {host}")


def _validate_provider_entry(
    label: str, entry: Entry, entries: dict[str, Entry]
) -> None:
    if "password" in entry or "username" in entry:
        raise _fail(label, "a provider entry holds no secret and no username")
    via = entry.get("via")
    target = entries.get(via) if isinstance(via, str) else None
    if not isinstance(target, dict) or target.get("kind") != "password":
        raise _fail(label, "via must name a password entry")


def _login_url_host(url: object) -> str | None:
    """The lower-case host of an acceptable login URL, else ``None``."""
    if not isinstance(url, str):
        return None
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    host = parts.hostname
    if not host or parts.username is not None or parts.password is not None:
        return None
    if parts.scheme == "https":
        return host
    if parts.scheme == "http" and host == LOOPBACK_HOST and loopback_allowed():
        return host
    return None


# ---------------------------------------------------------------- the key


def unlock(key: bytes, ttl_seconds: float | None) -> int:
    """Hold ``key`` in memory, after proving it opens the vault file.

    Raises :class:`vault_crypto.VaultError` (and changes nothing) for a missing
    file, a wrong key or a damaged file, and :class:`caller_python.Tainted` (also
    changing nothing, whatever the key) once a tool has run caller Python in this
    process. Registers every password with :mod:`secret_registry`. Returns the
    number of entries.
    """
    if ttl_seconds is not None and not ttl_seconds > 0:
        raise ValueError("ttl_seconds must be a positive number of seconds")
    with caller_python.unlock_gate():
        entries = _entries_of(vault_crypto.open_sealed(key, _read_document()))
        with _lock:
            global _key, _deadline  # noqa: PLW0603  PERMANENT(the key lives only in this module's memory)
            _key = bytes(key)
            _deadline = None if ttl_seconds is None else time.monotonic() + ttl_seconds
    _register_passwords(entries)
    return len(entries)


def is_unlocked() -> bool:
    """Whether a key is held (an expired unlock is dropped first)."""
    _expire()
    with _lock:
        return _key is not None


def lock() -> None:
    """Drop the key and forget every registered secret."""
    global _key, _deadline  # noqa: PLW0603  PERMANENT(the key lives only in this module's memory)
    with _lock:
        _key = None
        _deadline = None
    secret_registry.clear()


def status() -> dict[str, object]:
    """``{"exists", "unlocked", "expires_in"}``; locks first if the unlock expired."""
    _expire()
    with _lock:
        unlocked = _key is not None
        expires_in = (
            None
            if _deadline is None or not unlocked
            else max(0.0, _deadline - time.monotonic())
        )
    return {
        "exists": VAULT_PATH.exists(),
        "unlocked": unlocked,
        "expires_in": expires_in,
        # True once a tool has run caller Python here: no unlock until a restart.
        "unlock_blocked": caller_python.tainted(),
    }


def _expire() -> None:
    with _lock:
        expired = _deadline is not None and time.monotonic() >= _deadline
    if expired:
        lock()


# ---------------------------------------------------------------- reading


def public_entries() -> list[dict[str, object]]:
    """What a model may see: label, kind, site, ``via`` (providers), ``frozen``
    and, only when the owner set ``show_username``, the username. Never a
    password. Raises :class:`VaultLocked`."""
    _header, entries = _load()
    rows: list[dict[str, object]] = []
    for label in sorted(entries):
        entry = entries[label]
        row: dict[str, object] = {
            "label": label,
            "kind": entry.get("kind"),
            "site": entry.get("site"),
        }
        if entry.get("kind") == "provider":
            row["via"] = entry.get("via")
        row["frozen"] = entry.get("frozen")
        if entry.get("kind") == "password" and entry.get("show_username") is True:
            row["username"] = entry.get("username")
        rows.append(row)
    return rows


def entry(label: str) -> Entry:
    """A COPY of the full entry, its password wrapped in a ``Secret``.

    Raises :class:`VaultLocked`, or ``KeyError`` for an unknown label."""
    _header, entries = _load()
    found = copy.deepcopy(entries[label])
    if isinstance(found.get("password"), str):
        found["password"] = secret_registry.Secret(found["password"])
    return found


def _load() -> tuple[dict[str, object], dict[str, Entry]]:
    """``(header, entries)`` from the file under the held key, or VaultLocked."""
    _expire()
    key = _key
    if key is None:
        raise VaultLocked("the login vault is locked")
    try:
        document = _read_document()
        entries = _entries_of(vault_crypto.open_sealed(key, document))
    except vault_crypto.VaultError as exc:
        lock()
        raise VaultLocked("the login vault no longer opens with the held key") from exc
    _register_passwords(entries)
    return {"v": document["v"], "kdf": document["kdf"]}, entries


def _read_document() -> vault_crypto.Document:
    return vault_crypto.read_file(VAULT_PATH)


def _entries_of(plaintext: vault_crypto.Document) -> dict[str, Entry]:
    entries = plaintext.get("entries")
    if not isinstance(entries, dict):
        raise vault_crypto.VaultError("the vault file is not a valid vault document")
    return entries


def _register_passwords(entries: dict[str, Entry]) -> None:
    for entry_ in entries.values():
        password = entry_.get("password") if isinstance(entry_, dict) else None
        if isinstance(password, str):
            secret_registry.register(password)


# ---------------------------------------------------------------- writing


_FREEZE_ATTEMPTS = 5


def freeze(label: str) -> None:
    """Mark ``label`` frozen (a login was rejected) and rewrite the vault file
    under the held key. The one write the backend makes.

    **It never overwrites someone else's write.** The file is read, changed and
    replaced only if it still is the version that was read
    (:func:`vault_crypto.write_file`'s ``expect``, under the lock the CLI's writes
    take too); when a ``stealthy login edit`` landed in between, the freeze is
    applied again to what that edit wrote. Every top-level key of the plaintext is
    kept, not only ``entries``. Raises :class:`VaultLocked`, ``KeyError`` for an
    unknown label, or :class:`vault_crypto.VaultError` when the file will not
    hold still."""
    for _attempt in range(_FREEZE_ATTEMPTS):
        with _lock:
            _expire()
            key = _key
            if key is None:
                raise VaultLocked("the login vault is locked")
            try:
                document = _read_document()
                plaintext = vault_crypto.open_sealed(key, document)
            except vault_crypto.VaultError as exc:
                lock()
                raise VaultLocked(
                    "the login vault no longer opens with the held key"
                ) from exc
            entries = _entries_of(plaintext)
            _register_passwords(entries)
            stamp = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
            entries[label]["frozen"] = stamp
            header = {"v": document["v"], "kdf": document["kdf"]}
            try:
                vault_crypto.write_file(
                    VAULT_PATH,
                    vault_crypto.seal(key, header, plaintext),
                    expect=document.get("nonce"),
                )
            except vault_crypto.VaultChanged:
                continue
            return
    raise vault_crypto.VaultError("the vault file kept changing; nothing was frozen")


def audit(label: str, instance_id: str, caller: str, outcome: str) -> None:
    """Append one line to the audit log: time, label, instance, caller, outcome.

    Never a secret and never a username. Best effort: a log that cannot be
    written is reported (without detail) and does not break the login."""
    record = {
        "time": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        **dict(zip(_AUDIT_KEYS, (label, instance_id, caller, outcome), strict=True)),
    }
    line = secret_registry.redact(json.dumps(record, separators=(",", ":"))) + "\n"
    try:
        with _audit_lock:
            created = not AUDIT_PATH.exists()
            AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(
                AUDIT_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
            )
            with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                if created:
                    backend_token.owner_only(AUDIT_PATH)
                handle.write(line)
    except (OSError, ToolError) as exc:
        _logger.warning(
            "a vault audit line could not be written (%s)", type(exc).__name__
        )
