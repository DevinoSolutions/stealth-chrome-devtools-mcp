"""THE one home for Chrome's own log file on a persistent-profile launch (F-964).

nodriver launches Chrome with ``--disable-breakpad`` and no logging, so a
Chrome-side exit or freeze left nothing to read: on 2026-10-11 three browsers
(``default``, ``fleet`` and a clone) exited together and the only trace was the
backend's "browser process is not running". A persistent-profile launch now adds
``--enable-logging --log-level=1 --log-file=<log dir>/chrome-<session key>-<instance
id[:8]>-<UTC timestamp>[-<n>].log`` — warnings and above, one file per LAUNCH so a
relaunch never overwrites the evidence it exists to explain. A heal relaunches under
the SAME instance id, so a second launch inside the same second takes ``-2``, a third
``-3``: the name is claimed with an exclusive create, never ``touch`` (T-9477).

**Breakpad stays DISABLED.** Crash dumps can carry cookie memory and may upload; this
asks Chrome for its text log only.

*Persistent* is ``browser_pid_registry.on_persistent_profile`` — the predicate the
health loop scopes by — asked of what the launch will record. A disposable clone
gets no log: it is cheap to respawn and nothing is lost with it.

**Retention is per session key**: the newest :data:`KEEP_PER_SESSION` files, pruned at
launch. ``logging_setup.prune_old_logs`` leaves these names alone (they are not the
backend's, and twenty per session would otherwise push backend logs out of its
50-file sweep).

The file can hold what Chrome logs at WARNING and above, console messages included,
so it is local evidence: its PATH is what the spawn diagnostics and the health log
lines carry, never its content. A leaf: ``logging_setup`` (the log dir) and
``fleet_session`` (the session key) only.
"""

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

from stealth_chrome_devtools_mcp.embedded import (
    browser_pid_registry,
    fleet_session,
    logging_setup,
)
from stealth_chrome_devtools_mcp.embedded.models import BrowserOptions

PREFIX = logging_setup.CHROME_LOG_PREFIX
KEEP_PER_SESSION = 20
#: How many names :func:`_claim` tries for one second before it gives up.
MAX_CLAIMS = 1000
LOG_FILE_FLAG = "--log-file="

#: ``chrome-<key>-<8 hex>-<UTC timestamp>[-<n>].log``. The structure is what lets
#: ``fleet`` and ``fleet-2`` be told apart: a glob on ``chrome-fleet-*`` is both.
_NAME = re.compile(
    rf"^{PREFIX}(?P<key>.+)-[0-9a-f]{{8}}-\d{{8}}T\d{{6}}Z(?:-(?P<n>\d+))?\.log$"
)
_PLAIN_KEY = re.compile(r"[a-z0-9._-]{1,40}")


def session_key(user_data_dir: str) -> str:
    """The file-name form of ``fleet_session.lock_key`` (a name under the session
    root, ``default`` for the shared profile, the whole path for any other
    directory — which is hashed, as it is not a file name)."""
    key = fleet_session.lock_key(user_data_dir)
    if _PLAIN_KEY.fullmatch(key):
        return key
    return "path-" + hashlib.sha1(key.encode(), usedforsecurity=False).hexdigest()[:10]


def flags(
    options: BrowserOptions, instance_id: str, *, now: datetime | None = None
) -> list[str]:
    """The launch flags that make Chrome keep a log, or ``[]`` for a launch that
    is not on a persistent profile. Creates the file's directory and an empty
    file at the path (so retention counts it), and prunes the session's old ones."""
    if not options.user_data_dir or not browser_pid_registry.on_persistent_profile(
        {"uses_custom_data_dir": True, "auto_clone": options.auto_clone}
    ):
        return []
    key = session_key(options.user_data_dir)
    stamp = (now or datetime.now(tz=timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    log_dir = logging_setup.resolve_log_dir()
    stem = f"{PREFIX}{key}-{instance_id[:8]}-{stamp}"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        path = _claim(log_dir, stem)
        prune(log_dir, key)
    except OSError:
        return []  # a log is evidence, never a reason a spawn fails
    return ["--enable-logging", "--log-level=1", f"{LOG_FILE_FLAG}{path}"]


def _claim(log_dir: Path, stem: str) -> Path:
    """Create ``<stem>.log`` — or ``<stem>-2.log``, ``-3`` … when it is taken — with
    an exclusive create, so two launches never share a file and never race for one.
    A heal relaunches under the same instance id, so a relaunch within the same
    second carries the same stem. Bounded by :data:`MAX_CLAIMS`: the
    ``FileExistsError`` it raises is an ``OSError``: ``flags`` reads it as no log."""
    for number in range(1, MAX_CLAIMS + 1):
        name = f"{stem}.log" if number == 1 else f"{stem}-{number}.log"
        try:
            (log_dir / name).open("xb").close()
        except FileExistsError:
            continue
        return log_dir / name
    raise FileExistsError(f"{MAX_CLAIMS} logs already named {stem}")


def diagnostics(launch_args: list[str]) -> dict[str, str]:
    """``{"chrome_log": <path>}`` when *launch_args* ask for a log, else ``{}``."""
    for arg in launch_args:
        if arg.startswith(LOG_FILE_FLAG):
            return {"chrome_log": arg.removeprefix(LOG_FILE_FLAG)}
    return {}


def is_chrome_log(path: Path) -> bool:
    """Whether *path* is one of these files (``logging_setup``'s sweep skips them)."""
    return _NAME.match(path.name) is not None


def _age(path: Path) -> tuple[float, str, int]:
    """Newest-last sort key: mtime, then the name without its launch number, then the
    number as a number (the plain name is launch 1), so ``-10`` outranks ``-2`` when
    a relaunch storm lands several files on one mtime."""
    m = _NAME.match(path.name)
    number = int(m.group("n")) if m and m.group("n") else 1
    stem = path.name.removesuffix(".log").removesuffix(
        f"-{number}" if number > 1 else ""
    )
    return (path.stat().st_mtime, stem, number)


def prune(log_dir: Path, key: str, keep: int = KEEP_PER_SESSION) -> None:
    """Delete all but the newest *keep* logs of session *key*. Never raises past
    ``OSError`` and never touches another key's."""
    mine = sorted(
        (
            p
            for p in log_dir.glob(f"{PREFIX}*.log")
            if (m := _NAME.match(p.name)) and m.group("key") == key
        ),
        key=_age,
        reverse=True,
    )
    for stale in mine[keep:]:
        try:
            stale.unlink()
        except OSError:
            continue
