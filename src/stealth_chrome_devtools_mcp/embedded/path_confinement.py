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

**Win32 name folding (N1).** Win32 drops trailing dots and spaces from every path
segment before it reaches the file system, so ``~/.kube.`` IS ``~/.kube`` and a
destination that does not exist yet (``precreate_private`` creates it) slipped past
a plain string comparison. :func:`_win_canonical` strips them per segment and
:func:`_fold`, the one place the deny-list compares, applies it on Windows.

**Chrome profiles wherever they sit (N3).** ``spawn_browser(user_data_dir=<path>)``
may put a profile anywhere, outside every root above. A directory is a profile
when it carries our clone marker or one of ``profile_seed.LOGIN_WITNESSES`` (the
repo's own two definitions, not a third), and everything under one is refused.

**Tool-written files must not become code (T-19366, fourth review).** The
deny-list above protects what a path HOLDS; a file a tool WRITES can also be
something the machine runs by itself. ``export_debug_logs`` writes bytes the agent
influenced (a failing tool echoes its argument into the log ring, newline and all,
and a pickle holds strings raw), so a file named ``evil.pth`` in a ``site-packages``
directory is a line ``import ...`` that every Python on the machine runs at start,
the backend, the CLI and the person's own prompt included. Writers therefore also
pass :func:`code_location_rule` and :func:`code_name_rule`, through
:func:`refuse_write_path`: (a) a file in a place code is loaded or started from
(every ``sys.path`` / site directory but the working directory, the Python and
virtual-environment trees, this package, OS autostart folders, shell and PowerShell
profile files and folders, ``/etc``), and (b) a file named like something that runs
or loads by itself (:data:`CODE_EXTENSIONS`, folded case-insensitively after the
real path is taken, with Win32's trailing dots and spaces dropped; a Windows name
carrying a stream (``file.txt:stream``) is refused outright). A READ of a caller's
path (``import_network_data``) takes the secret-location rule too. Not covered:
``execute_cdp_command`` (raw CDP, e.g. a download directory) and a page's own
downloads.

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
import re
import site
import sys
import sysconfig
from pathlib import Path
from typing import BinaryIO, TextIO

from stealth_chrome_devtools_mcp.embedded import profile_seed
from stealth_chrome_devtools_mcp.embedded.tool_errors import CallerError, ToolError

#: Created files are owner-only. ``os.open``'s mode is masked by the umask, which
#: can only remove bits, so this is an upper bound that a 022 umask cannot widen.
PRIVATE_FILE_MODE = 0o600

#: Extensions of files the machine runs, loads or starts by itself, or that a
#: shell or script host executes when opened. Compared lower-case, on the final
#: suffix of the name after Win32 folding. A tool-written file never needs one.
CODE_EXTENSIONS = frozenset(
    {
        ".pth", ".py", ".pyw", ".pyc", ".pyo", ".pyd", ".pyz", ".so", ".dylib",
        ".cmd", ".bat", ".com", ".ps1", ".psm1", ".psd1", ".ps1xml", ".sh", ".bash",
        ".zsh", ".fish", ".command", ".lnk", ".url", ".exe", ".dll", ".msi", ".scr",
        ".cpl", ".reg", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".hta",
        ".desktop", ".plist", ".service", ".timer", ".jar",
    }
)  # fmt: skip

_CODE_EXTENSION_RULE = "a file of a type the machine runs or loads by itself"
_CODE_STREAM_RULE = "a Windows alternate data stream"
_CODE_LOCATION_RULE = "a folder code is loaded or started from"

#: Files in the home directory a shell reads when it starts.
_SHELL_PROFILE_NAMES = (
    ".bashrc", ".bash_profile", ".bash_login", ".bash_logout", ".profile",
    ".zshrc", ".zshenv", ".zprofile", ".zlogin", ".zlogout", ".kshrc", ".cshrc",
    ".tcshrc", ".login", ".xprofile", ".xinitrc", ".xsessionrc", ".inputrc",
)  # fmt: skip

#: ``.env`` variants that are templates, not secrets.
_ENV_TEMPLATE_SUFFIXES = (".example", ".sample", ".template", ".dist")

_CASE_INSENSITIVE = sys.platform in {"win32", "darwin"}

#: Files that hold credentials wherever they sit, so matched by NAME like ``.env``.
_CREDENTIAL_FILE_NAMES = frozenset(
    {".netrc", "_netrc", ".git-credentials", ".npmrc", ".pypirc"}
)

#: The label for a UNC / device path (never contains the path itself).
_REMOTE_RULE = "a network share or device path (UNC or a device namespace)"

#: A path the OS cannot resolve is refused by rule, never with its own error text.
_UNRESOLVABLE = (
    "path tools refuse a path the operating system cannot resolve (rule: "
    "secret-location deny-list, F-1025). Choose a plain local path."
)

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


_SEPARATORS = re.compile(r"([\\/])")

#: ``/proc/<pid>/environ`` is the process's environment, i.e. every secret passed
#: by variable. ``self`` and ``thread-self`` resolve to a pid under ``realpath``;
#: the pattern keeps them so the rule holds for a path that was never resolved.
_PROC_ENVIRON = re.compile(r"^/proc/(?:self|thread-self|\d+)(?:/task/\d+)?/environ\Z")


def _win_canonical(path: str) -> str:
    r"""*path* spelled as Win32 reads it: every segment loses trailing dots/spaces.

    Pure string logic (testable on any OS; :func:`_fold` applies it on Windows
    only, because on POSIX ``.kube.`` is a different directory). ``.`` and ``..``
    are navigation, not names, and a segment made only of dots is left alone.
    """
    parts = _SEPARATORS.split(path)
    for index in range(0, len(parts), 2):
        stripped = parts[index].rstrip(". ")
        if parts[index] not in {".", ".."} and stripped:
            parts[index] = stripped
    return "".join(parts)


def _fold(path: str) -> str:
    """The comparison form of a real path: case-folded where the FS folds, and
    on Windows with Win32's trailing dot/space dropping applied."""
    normal = os.path.normcase(path)
    if os.name == "nt":
        normal = _win_canonical(normal)
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
    text = os.fspath(path)
    if "\0" in text:
        raise CallerError(_UNRESOLVABLE)
    try:
        real = os.path.realpath(text)
    except (OSError, ValueError):
        # Mostly the caller's path, but an OSError can be the filesystem's own
        # trouble; both are refused with the same text, and ``from None`` leaves
        # no cause, so the class changes nothing for Sentry here.
        # ``~/.pgpass::$INDEX_ALLOCATION`` makes realpath raise
        # NotADirectoryError, whose text embeds the absolute path (F-877).
        raise CallerError(_UNRESOLVABLE) from None
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
        ("~/.pgpass (PostgreSQL passwords)", home / ".pgpass"),
        ("~/.vault-token (HashiCorp Vault token)", home / ".vault-token"),
        ("~/.azure (Azure credentials)", home / ".azure"),
        ("~/.claude (agent credentials and transcripts)", home / ".claude"),
        ("macOS keychains", home / "Library" / "Keychains"),
    ]
    # %APPDATA% / %LOCALAPPDATA% are these two unless the machine redirects them;
    # env access has one home (settings), so the redirect is not read here.
    app_data = [home / "AppData" / "Roaming", home / "AppData" / "Local"]
    roots.append(
        (
            "AppData/Roaming/postgresql (PostgreSQL passwords)",
            app_data[0] / "postgresql",
        )
    )
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


def proc_environ_path(real: str) -> bool:
    """Whether *real* names a Linux process environment (``/proc/<pid>/environ``).

    Pure string logic, applied on every OS: a resolved Windows path starts with a
    drive, so it can never match, and no platform branch is needed to keep it so.
    """
    return _PROC_ENVIRON.match(real) is not None


def _profile_rule(real: str) -> str | None:
    """The label when *real* lies in a Chrome profile directory wherever it is.

    A profile is a directory with our clone marker or a login store
    (``profile_seed.MARKER_NAME`` / ``PROFILE_STATE_FILE`` / ``LOGIN_WITNESSES``,
    the last also under ``Profile N/``), so a profile that
    ``spawn_browser(user_data_dir=...)`` placed outside every named root is
    still refused. A directory that cannot be read is not a profile.
    """
    witnesses = (
        profile_seed.MARKER_NAME,
        profile_seed.PROFILE_STATE_FILE,
        *profile_seed.LOGIN_WITNESSES,
    )
    # Every ``Default/`` witness is equally ``Profile 1/``, ``Profile 2/`` and so on.
    numbered = [
        "Profile */" + witness.partition("/")[2]
        for witness in profile_seed.LOGIN_WITNESSES
    ]
    start = Path(real)
    for directory in (start, *start.parents):
        with contextlib.suppress(OSError):
            if any((directory / witness).is_file() for witness in witnesses) or any(
                next(directory.glob(pattern), None) is not None for pattern in numbered
            ):
                return "a Chrome profile directory (clone marker or login store)"
    return None


def _env_file_rule(name: str) -> bool:
    lowered = name.lower()
    if lowered == ".env":
        return True
    return lowered.startswith(".env.") and not lowered.endswith(_ENV_TEMPLATE_SUFFIXES)


def _file_name_rule(real: str) -> str | None:
    """The label when what *real* IS, rather than where it lies, is denied: a
    process environment, a ``.env`` file, or a credential file by name."""
    if proc_environ_path(real):
        return "/proc/<pid>/environ (a process's environment variables)"
    name = Path(real).name
    if os.name == "nt":
        name = _win_canonical(name)
    if _env_file_rule(name):
        return ".env files (secrets by convention)"
    if _fold(name) in _CREDENTIAL_FILE_NAMES:
        return f"{name} files (credentials by convention)"
    return None


def secret_location_rule(path: str | os.PathLike[str]) -> str | None:
    """The label of the denied location *path* really lies in, or ``None``."""
    if os.name == "nt" and _remote_or_device(os.fspath(path)):
        return _REMOTE_RULE
    real = _real(path)
    if os.name == "nt" and _remote_or_device(real):
        return _REMOTE_RULE
    by_name = _file_name_rule(real)
    if by_name is not None:
        return by_name
    for label, root in _secret_roots():
        if _is_under(real, _real(root)):
            return label
    return _profile_rule(real)


def _code_roots() -> list[Path]:
    """Every folder or file a tool-written file must not land in, at call time.

    The working directory is left out of the ``sys.path`` part on purpose: ``python
    -m`` puts it first, and a person's export folder is very often exactly there;
    a file named like code is refused by :func:`code_name_rule` wherever it is. A
    root that contains the home directory (an interpreter installed at ``~``) is
    left out too, because refusing it would refuse everything.
    """
    home = Path.home()
    roots: list[Path] = []

    def add(*candidates: object) -> None:
        roots.extend(Path(str(candidate)) for candidate in candidates if candidate)

    with contextlib.suppress(AttributeError, OSError):
        add(*site.getsitepackages(), site.getusersitepackages())
    paths = sysconfig.get_paths()
    add(
        *(
            paths.get(key)
            for key in ("purelib", "platlib", "stdlib", "platstdlib", "scripts")
        )
    )
    add(sys.prefix, sys.exec_prefix, sys.base_prefix, sys.base_exec_prefix)
    working = _fold(_real(Path.cwd()))
    for entry in sys.path:
        if entry and Path(entry).is_dir() and _fold(_real(entry)) != working:
            add(entry)
    add(Path(__file__).resolve().parent.parent)  # this package
    roots.extend(home / name for name in _SHELL_PROFILE_NAMES)
    add(
        home / ".config" / "autostart", home / ".config" / "systemd",
        home / ".config" / "fish", home / ".config" / "powershell",
        home / "Library" / "LaunchAgents", home / ".local" / "share" / "systemd",
        home / "AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup",
    )  # fmt: skip
    for documents in (home / "Documents", *home.glob("OneDrive*/Documents")):
        add(documents / "PowerShell", documents / "WindowsPowerShell")
    if os.name == "nt":
        add(
            Path(home.anchor)
            / "ProgramData/Microsoft/Windows/Start Menu/Programs/StartUp"
        )
    else:
        add("/etc", "/Library/LaunchAgents", "/Library/LaunchDaemons")
    return [root for root in roots if not _is_under(str(home), _real(root))]


def code_name_rule(real: str) -> str | None:
    """The label when the NAME of *real* is something the machine runs or loads by
    itself (:data:`CODE_EXTENSIONS`), or a Windows alternate data stream."""
    name = Path(real).name
    if os.name == "nt":
        if ":" in name:
            return _CODE_STREAM_RULE
        name = _win_canonical(name)
    return (
        _CODE_EXTENSION_RULE if Path(name).suffix.lower() in CODE_EXTENSIONS else None
    )


def code_location_rule(real: str) -> str | None:
    """The label when *real* lies where code is loaded or started from."""
    for root in _code_roots():
        if _is_under(real, _real(root)):
            return _CODE_LOCATION_RULE
    return None


def refuse_write_path(path: str | os.PathLike[str], *, tool: str) -> Path:
    """:func:`refuse_secret_path` for a file a tool WRITES: also refuses a code
    location and a code-like name. Returns the real path that was checked."""
    real = refuse_secret_path(path, tool=tool, action="write to")
    rule = code_name_rule(str(real)) or code_location_rule(str(real))
    if rule is not None:
        raise ToolError(
            f"{tool} refuses to write {rule}: a file a tool writes may not be one "
            "the machine would run (Python's site directories and this package, "
            "startup folders, shell profiles, scripts and programs). Choose a "
            "plain data file (.json, .txt, .png, .html) in an ordinary folder "
            "(rule: tool-written files are never code, T-19366)."
        )
    return real


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
        raise CallerError(
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
    real = refuse_write_path(path, tool=tool)
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
    real = refuse_write_path(path, tool=tool)
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
    real = refuse_write_path(path, tool=tool)
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
