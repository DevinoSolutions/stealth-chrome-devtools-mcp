"""The one home for what a path-taking tool may read or write (F-1025).

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

_BROWSER_PROFILE_DIRS = (
    ("Google", "Chrome"),
    ("Google", "Chrome Beta"),
    ("google-chrome",),
    ("Chromium",),
    ("chromium",),
    ("Microsoft", "Edge"),
    ("Microsoft Edge",),
    ("microsoft-edge",),
    ("BraveSoftware",),
    ("Firefox",),
    ("Mozilla",),
)


def _fold(path: str) -> str:
    """The comparison form of a real path: case-folded where the FS folds."""
    normal = os.path.normcase(path)
    return normal.lower() if _CASE_INSENSITIVE else normal


def _real(path: str | os.PathLike[str]) -> str:
    return os.path.realpath(os.fspath(path))


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
    ]
    # Browser profile roots, under every place the three platforms keep them.
    profile_bases = (
        home / ".config",
        home / ".mozilla",
        home / "Library" / "Application Support",
        home / "AppData" / "Local",
        home / "AppData" / "Roaming",
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

    roots.append(("the stealth state directory", backend_registry.STATE_DIR))
    roots.extend(
        ("the stealth browser-session root", directory)
        for directory in (
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
    real = _real(path)
    if _env_file_rule(Path(real).name):
        return ".env files (secrets by convention)"
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
            "touch credential stores, browser profiles or this server's own "
            "state (rule: secret-location deny-list, F-1025). Choose a path "
            "outside them."
        )
    return Path(_real(path))


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
    real.parent.mkdir(parents=True, exist_ok=True)
    created = not real.exists()
    os.close(_open_fd(real, overwrite=overwrite, tool=tool))
    return real, created


def discard_precreated(real: Path, created: bool) -> None:
    """Remove *real* if :func:`precreate_private` made it and nothing was written."""
    if created:
        with contextlib.suppress(OSError):
            if real.stat().st_size == 0:
                real.unlink()
