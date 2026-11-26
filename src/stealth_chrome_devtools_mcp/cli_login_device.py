"""``stealthy login bind`` and ``export`` (T-22092): the vault's second factor.

A vault made by ``init`` is tied to this OS account when the machine has a key
store (``embedded.device_key``): its key needs the passphrase AND a secret the
account keeps, so a copy of the file is useless elsewhere. Two commands follow
from that, and live here because ``cli_login`` is at its line cap:

- ``bind`` ties an existing passphrase-only vault (one made before 2.1.70, or
  one carried here by ``export``) to this account.
- ``export FILE`` writes a passphrase-only copy, the one thing that can be
  opened on another machine, after an OS reinstall or by another account.

Both ask for the passphrase at a terminal, and neither prints a secret. The key
changes on ``bind``, so every backend is locked and ``unlock`` is asked for again.

Module scope imports only the standard library, as ``cli_login`` does.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable

    from stealth_chrome_devtools_mcp.embedded.vault_crypto import Document

_STORES = {
    "dpapi": "Windows account (DPAPI)",
    "keychain": "macOS login keychain",
    "secret-service": "desktop keyring (Secret Service)",
}


def add_verbs(verb: Callable[[str, str], argparse.ArgumentParser]) -> None:
    """Contribute ``bind`` and ``export`` to ``cli_login``'s verbs."""
    verb(
        "bind",
        "tie the vault to this OS account, so a copy of the file cannot be opened "
        "on another machine (a terminal only)",
    )
    export = verb(
        "export",
        "write a passphrase-only copy to carry to another machine or keep before "
        "an OS reinstall (a terminal only)",
    )
    export.add_argument("file", help="where to write the copy; must not exist")


def describe(document: Document) -> str:
    """One line: what, beside the passphrase, this vault's key needs."""
    from stealth_chrome_devtools_mcp.embedded import device_key, vault_crypto

    if vault_crypto.is_bound(document):
        device = document.get("device")
        store = device.get("store") if isinstance(device, dict) else None
        unsaved = (
            " A keyring that is not saved across restarts loses the key: make "
            "that export now."
            if store == "secret-service"
            else ""
        )
        return (
            f"second factor: this {_STORES.get(str(store), 'OS account')}; a copy of "
            "the file cannot be opened elsewhere. Before a new machine, a new "
            "account, an OS reinstall or an administrator resetting your "
            f"password: `stealthy login export FILE`.{unsaved}"
        )
    if device_key.store_name() is None:
        return (
            "second factor: none (this machine has no account key store); the "
            "passphrase alone protects the file"
        )
    return (
        "second factor: none, the passphrase alone protects the file; "
        "`stealthy login bind` ties it to this OS account"
    )


def describe_file() -> str:
    """:func:`describe` for the vault on disk, after asking the store for its
    key (no passphrase, no scrypt): a tied vault whose key is not there says so."""
    from stealth_chrome_devtools_mcp.cli_login import _store
    from stealth_chrome_devtools_mcp.embedded import device_key, vault_crypto

    try:
        document = vault_crypto.read_file(_store().VAULT_PATH)
    except vault_crypto.VaultError as exc:
        return f"second factor: unknown ({exc})"
    if vault_crypto.is_bound(document):
        try:
            device_key.load(document.get("device"))
        except device_key.DeviceKeyError as exc:
            return f"second factor: ! {exc}"
    return describe(document)


def _device_on_disk() -> object:
    """The ``device`` object of the vault file, or ``None``."""
    from stealth_chrome_devtools_mcp.cli_login import _store
    from stealth_chrome_devtools_mcp.embedded import vault_crypto

    try:
        return vault_crypto.read_file(_store().VAULT_PATH).get("device")
    except vault_crypto.VaultError:
        return None


def forgetter() -> Callable[[], None]:
    """Read NOW which key-store item the vault on disk names; the call returned
    drops it. ``destroy`` makes the call only after it deleted the file: the
    other order leaves a tied file with no key when the delete fails."""
    from stealth_chrome_devtools_mcp.embedded import device_key

    device = _device_on_disk()
    return lambda: device_key.forget(device)


def forget_unused(header: Document) -> None:
    """Drop the key *header* names unless the file on disk is the one using it:
    for an ``init`` or a ``bind`` that did not finish."""
    from stealth_chrome_devtools_mcp.embedded import device_key

    device = header.get("device")
    if device is not None and _device_on_disk() != device:
        device_key.forget(device)


def _reopen(document: Document) -> tuple[str, dict[str, dict[str, object]]]:
    """``(passphrase, entries)``: both commands re-derive a key under a new
    header, so they need the passphrase itself and not only the old key."""
    from stealth_chrome_devtools_mcp.cli_login import _hidden, _RefusedError
    from stealth_chrome_devtools_mcp.embedded import vault_crypto

    passphrase = _hidden("vault passphrase: ")
    try:
        plaintext = vault_crypto.open_sealed(
            vault_crypto.derive_key(passphrase, document), document
        )
    except vault_crypto.VaultError as exc:
        raise _RefusedError(vault_crypto.refusal(exc)) from None
    entries = plaintext.get("entries")
    if not isinstance(entries, dict):
        raise _RefusedError("the vault file is not a valid vault document")
    return passphrase, entries


def cmd_bind(_args: argparse.Namespace) -> int:
    from stealth_chrome_devtools_mcp.cli_login import (
        _lock_everywhere,
        _read_header,
        _RefusedError,
        _require_terminal,
        _write,
    )
    from stealth_chrome_devtools_mcp.embedded import device_key, vault_crypto

    _require_terminal("bind")
    document = _read_header()
    if vault_crypto.is_bound(document):
        print(describe(document))
        return 0
    if device_key.store_name() is None:
        raise _RefusedError(
            "this machine has no account key store to keep a second factor in "
            "(on Linux: a Secret Service keyring and `secret-tool`); nothing was "
            "changed"
        )
    passphrase, entries = _reopen(document)
    header = vault_crypto.new_header()
    if not vault_crypto.is_bound(header):
        raise _RefusedError(
            "the account key store did not keep a new key; nothing was changed"
        )
    try:
        key = vault_crypto.derive_key(passphrase, header)
        _write({**header, "nonce": document["nonce"]}, key, entries)
    except BaseException:
        forget_unused(header)  # unless the write landed, the file has the old key
        raise
    for warning in _lock_everywhere():  # every held key is the old one now
        print(f"warning: {warning}", file=sys.stderr)
    print(describe(header))
    print("Copies of the file made before now (backups, sync history) still open")
    print("with the passphrase alone.")
    print("the key changed: `stealthy login unlock` again to let agents use it")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from stealth_chrome_devtools_mcp.cli_login import (
        _read_header,
        _RefusedError,
        _require_terminal,
        _store,
    )
    from stealth_chrome_devtools_mcp.embedded import vault_crypto

    _require_terminal("export")
    target = Path(args.file).expanduser().resolve()
    if target.exists() or target.is_symlink():
        raise _RefusedError(f"{target} already exists; nothing was written")
    passphrase, entries = _reopen(_read_header())
    header = vault_crypto.new_header(bound=False)
    key = vault_crypto.derive_key(passphrase, header)
    try:
        # Claim the name first: a file that appeared since the check above is
        # refused here, never replaced.
        target.parent.mkdir(parents=True, exist_ok=True)
        os.close(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    except OSError as exc:
        raise _RefusedError(
            f"could not create {target} ({exc.strerror or type(exc).__name__}); "
            "nothing was written"
        ) from None
    try:
        vault_crypto.write_file(
            target, vault_crypto.seal(key, header, {"entries": entries})
        )
    except BaseException as exc:
        target.unlink(missing_ok=True)
        if isinstance(exc, (OSError, vault_crypto.VaultError)):
            raise _RefusedError(f"could not write {target} ({exc})") from None
        raise
    print(f"wrote {target}")
    print("It opens with the passphrase alone, on any machine: keep it somewhere")
    print("safe and delete it once it is no longer needed. To use it elsewhere,")
    print(f"put it at {_store().VAULT_PATH} there, then `stealthy login bind`.")
    return 0


#: Verb name -> body, merged into ``cli_login``'s command table.
COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "bind": cmd_bind,
    "export": cmd_export,
}
