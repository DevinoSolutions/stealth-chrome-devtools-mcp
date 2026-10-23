r"""The one home for what a path-taking tool may read or write (F-1025).

Four tools take a path from the caller and a hostile page can steer an agent
into choosing it: ``upload_file`` READS one and hands it to the page's own
``<form>``; ``export_debug_logs``, ``export_network_data`` and ``take_screenshot``
WRITE one. None of them asked where the path pointed, so "attach
``~/.ssh/id_ed25519`` to this input" leaked a private key and "export to
``~/.aws/credentials``" truncated one.

**The rule: a deny-list of secret locations, checked on the REAL path.** There is
no allow-root setting in :mod:`stealth_chrome_devtools_mcp.settings` to extend,
and inventing one would make every working upload/export fail until an operator
configured it, on a tool that is local and single-user by design. A deny-list
refuses only what no legitimate call names (credential stores, browser profiles,
our own state) and leaves every other path exactly as it was. The check runs
after ``os.path.realpath``, so a symlink or Windows junction into a denied tree
is refused under the name it was reached by, and :func:`refuse_secret_path`
RETURNS that real path so a caller uses the checked path, not the one it was
handed (no check-then-swap).

Refusals raise :class:`~.tool_errors.ToolError` and NAME THE RULE, never the path
(F-877: an absolute path names the operating user and the message reaches the
debug ring and Sentry).

**Windows spellings.** ``os.path.realpath`` keeps a long ``\\?\C:\...`` prefix, so
such a path never starts with the plain-drive root of a deny-list entry; the
prefix is stripped before comparing. UNC (``\\server\share``, ``\\localhost\C$``,
``\\?\UNC\...``) and device (``\\.\``, ``\??\``) paths reach a file by a route no
local root can be compared against, so they are REFUSED outright, both as typed
and as resolved (a symlink to a share is the same route).

**One writer for the export tools.** :func:`open_text` / :func:`open_binary` are
the only places an export file is opened: the deny-list check, then ``os.open``
with ``O_EXCL`` (an existing file is refused unless the caller passed
``overwrite=True``) and mode ``0o600`` so a credential-bearing export is not
world-readable under a 022 umask (the mode is a no-op on Windows, where the file
inherits its folder's ACL). :func:`precreate_private` is the same for a writer we
do not own (``Tab.save_screenshot`` writes with ``Path.write_bytes``, which keeps
the mode of a file that already exists).
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path
from typing import BinaryIO, TextIO

from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

#: Created files are owner-only. ``os.open``'s mode is masked by the umask, which
#: can only remove bits, so this is an upper bound that a 022 umask cannot widen.
PRIVATE_FILE_MODE = 0o600

#: ``.env`` variants that are templates, not secrets.
_ENV_TEMPLATE_SUFFIXES = (".example", ".sample", ".template", ".dist")

_CASE_INSENSITIVE = sys.platform in {"win32", "darwin"}

#: Files that hold credentials wherever they sit, so matched by NAME like ``.env``.
_CREDENTIAL_FILE_NAMES = frozenset(
    {".netrc", "_netrc", ".git-credentials", ".npmrc", ".pypirc"}
)

#: The label for a UNC / device path (never contains the path itself).
_REMOTE_RULE = "a network share or device path (UNC or a device namespace)"

_BROWSER_PROFILE_DIRS = (
    ("Google", "Chrome"),
    ("Google", "Chrome Beta"),
    ("Google", "Chrome Dev"),
    ("Google", "Chrome SxS"),
    ("Google", "Chrome Canary"),
    ("google-chrome",),
    ("google-chrome-beta",),
    ("google-chrome-unstable",),
    ("Chromium",),
    ("chromium",),
    ("Microsoft", "Edge"),
    ("Microsoft", "Edge Beta"),
    ("Microsoft", "Edge Dev"),
    ("Microsoft", "Edge SxS"),
    ("Microsoft Edge",),
    ("microsoft-edge",),
    ("microsoft-edge-beta",),
    ("microsoft-edge-dev",),
    ("BraveSoftware",),
    ("Vivaldi",),
    ("vivaldi",),
    ("Opera Software",),
    ("com.operasoftware.Opera",),
    ("opera",),
    ("Firefox",),
    ("Mozilla",),
)


def _fold(path: str) -> str:
    """The comparison form of a real path: case-folded where the FS folds."""
    normal = os.path.normcase(path)
    return normal.lower() if _CASE_INSENSITIVE else normal


def _drive_path(text: str) -> bool:
    return text[:1].isalpha() and text[1:2] == ":"


def _remote_or_device(text: str) -> bool:
    r"""Whether *text* spells a UNC share or a Windows device path.

    Pure string logic (slashes are folded first, as Windows does), so it is
    testable on any OS; :func:`secret_location_rule` applies it on Windows only,
    because on POSIX a leading backslash is an ordinary file name. ``\\?\C:\...``
    is the long spelling of a LOCAL drive path (see :func:`_real`) and is not
    remote; every other ``\\?\`` form (``UNC``, a volume GUID, ``GLOBALROOT``)
    and all of ``\\.\`` and ``\??\`` are.
    """
    spelled = text.replace("/", "\\")
    if spelled.startswith("\\??\\"):
        return True
    if not spelled.startswith("\\\\"):
        return False
    if spelled[2:4] == "?\\":
        return not _drive_path(spelled[4:])
    return True


def _real(path: str | os.PathLike[str]) -> str:
    r"""The real path, with a ``\\?\<drive>:`` long prefix removed.

    ``os.path.realpath`` keeps the prefix it was handed, and a prefixed path
    never starts with the unprefixed deny-list root it lies in.
    """
    real = os.path.realpath(os.fspath(path))
    if real.startswith("\\\\?\\") and _drive_path(real[4:]):
        return real[4:]
    return real


def _is_under(target: str, root: str) -> bool:
    target, root = _fold(target), _fold(root)
    return target == root or target.startswith(root.rstrip("\\/") + os.sep)


def _secret_roots() -> list[tuple[str, Path]]:
    """``(rule label, directory)`` for every denied tree, computed at call time.

    Call time, not import time: ``HOME``, the settings and the session root are
    all per-process facts a test (or a ``STEALTH_MCP_*`` override) moves.
    """
    home = Path.home()
    roots: list[tuple[str, Path]] = [
        ("~/.ssh (SSH keys)", home / ".ssh"),
        ("~/.aws (AWS credentials)", home / ".aws"),
        ("~/.config/gcloud (Google Cloud credentials)", home / ".config" / "gcloud"),
        (
            "AppData/Roaming/gcloud (Google Cloud credentials)",
            home / "AppData" / "Roaming" / "gcloud",
        ),
        ("~/.gnupg (GPG keys)", home / ".gnupg"),
        ("~/.config/gh (GitHub CLI token)", home / ".config" / "gh"),
        ("~/.config/git (git credentials)", home / ".config" / "git" / "credentials"),
        ("~/.docker (registry credentials)", home / ".docker"),
        ("~/.kube (cluster credentials)", home / ".kube"),
        ("~/.azure (Azure credentials)", home / ".azure"),
        ("~/.claude (agent credentials and transcripts)", home / ".claude"),
        ("macOS keychains", home / "Library" / "Keychains"),
    ]
    # %APPDATA% / %LOCALAPPDATA% are these two unless the machine redirects them;
    # env access has one home (settings), so the redirect is not read here.
    app_data = [home / "AppData" / "Roaming", home / "AppData" / "Local"]
    roots.extend(
        ("the Windows credential store", base / "Microsoft" / name)
        for base in app_data
        for name in ("Credentials", "Protect", "Vault")
    )
    # Browser profile roots, under every place the three platforms keep them.
    profile_bases = (
        home / ".config",
        home / ".mozilla",
        home / "Library" / "Application Support",
        *app_data,
    )
    roots.append(("a browser profile root", home / ".mozilla"))
    roots.extend(
        ("a browser profile root", base.joinpath(*parts))
        for base in profile_bases
        for parts in _BROWSER_PROFILE_DIRS
    )

    # Our own state: session profiles hold the signed-in cookie jars, and the
    # state dir holds the registry, logs, spill files and earlier exports.
    # Imported here, not at the top: clone_storage imports debug_logger, which
    # imports this module, so a top-level import is a cycle.
    from stealth_chrome_devtools_mcp.embedded import backend_registry, clone_storage

    roots.extend(
        ("the stealth state directory", directory)
        for directory in (backend_registry.STATE_DIR, home / ".stealth-mcp")
    )
    roots.extend(
        ("the stealth browser-session root", directory)
        for directory in (
            # The default too, so redirecting the setting does not un-protect
            # the profiles left in the default place.
            clone_storage.platform_default_session_root(),
            clone_storage.default_session_root(),
            clone_storage.clone_root_dir(),
            clone_storage.master_profile_dir(),
            clone_storage.master_snapshot_dir(),
        )
    )
    return roots


def _env_file_rule(name: str) -> bool:
    lowered = name.lower()
    if lowered == ".env":
        return True
    return lowered.startswith(".env.") and not lowered.endswith(_ENV_TEMPLATE_SUFFIXES)


def secret_location_rule(path: str | os.PathLike[str]) -> str | None:
    """The label of the denied location *path* really lies in, or ``None``."""
    if os.name == "nt" and _remote_or_device(os.fspath(path)):
        return _REMOTE_RULE
    real = _real(path)
    if os.name == "nt" and _remote_or_device(real):
        return _REMOTE_RULE
    name = Path(real).name
    if _env_file_rule(name):
        return ".env files (secrets by convention)"
    if _fold(name) in _CREDENTIAL_FILE_NAMES:
        return f"{name} files (credentials by convention)"
    for label, root in _secret_roots():
        if _is_under(real, _real(root)):
            return label
    return None


def refuse_secret_path(path: str | os.PathLike[str], *, tool: str, action: str) -> Path:
    """Return the real path of *path*, or raise :class:`ToolError` naming the rule.

    *action* completes "refuses to ..." (``"attach"``, ``"write to"``). Use the
    returned path, not *path*: it is the one that was checked.
    """
    rule = secret_location_rule(path)
    if rule is not None:
        raise ToolError(
            f"{tool} refuses to {action} a file in {rule}: path tools may not "
            "touch credential stores, browser profiles, network or device "
            "paths or this server's own state (rule: secret-location "
            "deny-list, F-1025). Choose a local path outside them."
        )
    return Path(_real(path))


def _unwritable(exc: OSError, real: Path, tool: str) -> ToolError:
    """A ToolError for a destination that cannot be written: the file's NAME and
    the OS reason, never the directory (F-877: ``str(OSError)`` embeds the whole
    absolute path, and the message reaches the debug ring and Sentry)."""
    reason = exc.strerror or type(exc).__name__
    return ToolError(
        f"{tool} cannot write {real.name!r}: {reason}. Check that its folder "
        "exists and is writable, or choose another path."
    )


def _open_fd(real: Path, *, overwrite: bool, tool: str) -> int:
    """Open *real* for writing, owner-only; never replaces a file unless asked.

    ``O_EXCL`` makes "refuse to overwrite" one atomic step rather than an
    exists-then-open race.
    """
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_BINARY", 0)
    flags |= os.O_TRUNC if overwrite else os.O_EXCL
    try:
        fd = os.open(real, flags, PRIVATE_FILE_MODE)
    except FileExistsError:
        raise ToolError(
            f"{tool} refuses to overwrite an existing file (rule: no silent "
            "overwrite, F-1025). Pass overwrite=True to replace it, or choose "
            "a new path."
        ) from None
    except OSError as exc:
        raise _unwritable(exc, real, tool) from None
    if hasattr(os, "fchmod"):
        # An export overwriting an older, looser file must not keep its mode.
        os.fchmod(fd, PRIVATE_FILE_MODE)
    return fd


def open_text(
    path: str | os.PathLike[str], *, tool: str, overwrite: bool = False
) -> TextIO:
    """Open a UTF-8 text export: deny-list, no silent overwrite, owner-only."""
    real = refuse_secret_path(path, tool=tool, action="write to")
    fd = _open_fd(real, overwrite=overwrite, tool=tool)
    try:
        return os.fdopen(fd, "w", encoding="utf-8")
    except BaseException:
        os.close(fd)
        raise


def open_binary(
    path: str | os.PathLike[str], *, tool: str, overwrite: bool = False
) -> BinaryIO:
    """:func:`open_text`, for bytes."""
    real = refuse_secret_path(path, tool=tool, action="write to")
    fd = _open_fd(real, overwrite=overwrite, tool=tool)
    try:
        return os.fdopen(fd, "wb")
    except BaseException:
        os.close(fd)
        raise


def precreate_private(
    path: str | os.PathLike[str], *, tool: str, overwrite: bool = False
) -> tuple[Path, bool]:
    """The same checks as :func:`open_text`, leaving an empty owner-only file.

    For a writer we do not own (``Tab.save_screenshot``). Returns
    ``(real path, created)``; *created* says whether this call made the file, so
    a failed write can remove exactly the empty file it left.
    """
    real = refuse_secret_path(path, tool=tool, action="write to")
    try:
        real.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _unwritable(exc, real, tool) from None
    created = not real.exists()
    os.close(_open_fd(real, overwrite=overwrite, tool=tool))
    return real, created


def discard_precreated(real: Path, created: bool) -> None:
    """Remove *real* if :func:`precreate_private` made it and nothing was written."""
    if created:
        with contextlib.suppress(OSError):
            if real.stat().st_size == 0:
                real.unlink()
