"""THE one home for keeping a test run out of the operator's REAL state dir.

F-903. The hermetic suite could reach ``~/.stealth-mcp`` and cold-start or evict
a REAL backend: on 2026-09-21 a hermetic node drove ``singleton._proxy_streams``
with a failing bridge, which reached ``proxy_selfheal.heal_backend`` ->
``ensure_server_running``, and a real backend (pid 55240, port 21770) was spawned
into the operator's live record. F-886 spared the live siblings; nothing in the
suite would have.

Before this module the fence was PER-FILE: roughly thirty test files each grew
their own copy of an ``isolated_state`` fixture, ten used subprocess HOME
redirection, eight needed nothing because their API takes explicit paths -- and
the files that had none were fenced by luck (which collaborator a node happened
to mock), not by construction. ``tests/conftest.py`` redirected the clone output
dir and the browser-session root and nothing else.

Three parts, in the order they are installed.

1. **The redirect** (:func:`install`). The state dir is read from ``Path.home()``
   at IMPORT time and the derived paths are bound then, so a single
   ``setattr`` on ``backend_registry.STATE_DIR`` reaches nothing else --
   ``singleton`` FROM-imports the same three names into its own namespace,
   ``process_cleanup`` and ``response_handler`` FROM-import ``STATE_DIR`` again,
   ``singleton.LOCK_FILE`` is derived at ITS import, and ``settings`` recomputes
   the path a second time because it may not import the package. Ten bindings
   across five modules. They are not grepped, they are MEASURED: a probe imports
   every module in the package and reports every global that is a ``Path`` at or
   under the real state dir, and :func:`derived_globals` re-runs exactly that
   probe so a NEW derived global fails ``tests/test_state_dir_fence.py`` instead
   of silently escaping.

2. **The write guard** (:func:`install`, same call). Enumerating bindings can
   only ever be as complete as the last audit, so the fence does not rely on it:
   every filesystem WRITE primitive is wrapped, and one whose target resolves
   under the real state dir raises :class:`RealStateDirWrite`. That is the
   completeness argument -- a path the redirect misses is caught at the moment
   of harm rather than after it.

   **Writes only, deliberately.** A READ of the real record is required and must
   keep working: ``release_gate_harness._reserved_ports()`` reads the operator's
   own ``server.json`` through ``Path.home()`` precisely so an isolated backend
   never binds a port a LIVE backend holds. Guarding reads would break the one
   mechanism protecting the live backends from a port collision, to prevent a
   harm reading cannot do.

   It is a ``BaseException`` and that is load-bearing: the product is full of
   ``except Exception`` by design (``backend_registry`` is a never-raise cache,
   ``proxy_selfheal`` never raises, ``observability`` never raises), so an
   ``Exception`` here is swallowed at the first fail-open handler it meets and
   the node goes green over a real write. Measured on the F-900 branch, whose
   ``_RealStartupReached(Exception)`` was eaten at ``proxy_selfheal.py:~325``.

3. **The kill guard** (:func:`install`, same call). The record is read ONCE, at
   install, and the pids it names are the operator's LIVE backends. Terminating
   one is the harm F-886 exists to prevent, reached from the suite instead of
   from a cold start, so ``backend_eviction.terminate`` refuses a recorded pid.
   Separate from the write guard because a kill leaves no filesystem trace.

**Why HOME is not redirected.** Setting ``HOME``/``USERPROFILE`` would fence all
ten bindings at once and every future one for free, and it was rejected for a
measured reason: ``release_gate_harness._reserved_ports()`` calls ``Path.home()``
to find the ports the operator's real backends hold. Under a redirected HOME it
would read the EMPTY fence record, exclude nothing, and an isolated backend could
bind the port a live backend is serving on. The fence would have created the
collision it exists to prevent. Child processes keep their own HOME redirection
(``release_gate_harness._isolated_env``), which is a different mechanism for a
different process and is untouched.

**Why import time and not a fixture.** ``conftest.py`` already argues this for
the browser-session root: an autouse FUNCTION-scoped fixture is ordered after a
module-scoped one, and the E2E modules' ``_warmup`` is exactly that -- it starts
a backend during module setup, before any function fixture runs. Installing at
conftest import time is ahead of collection, every fixture of every scope, and
every import-time side effect.
"""

from __future__ import annotations

import builtins
import io
import os
from pathlib import Path

# The operator's real state dir, captured before anything is redirected. Read
# from Path.home() exactly as backend_registry does, so the two cannot drift.
REAL_STATE_DIR = Path.home() / ".stealth-mcp"

# The ten bindings, as MEASURED by importing every module in the package and
# reporting every global that is a Path at or under REAL_STATE_DIR. Each row is
# (dotted module, attribute, basename under the fence root -- None means the
# fence root itself).
#
# Order is module-then-attribute so a diff against the probe's own sorted output
# is a plain comparison. ``derived_globals`` re-runs that probe; if a new global
# appears the pin fails and this table is where it is added.
STATE_DIR_BINDINGS: tuple[tuple[str, str, str | None], ...] = (
    (
        "stealth_chrome_devtools_mcp.embedded.backend_registry",
        "PORT_FILE",
        "server.port",
    ),
    (
        "stealth_chrome_devtools_mcp.embedded.backend_registry",
        "SERVER_STATE_FILE",
        "server.json",
    ),
    ("stealth_chrome_devtools_mcp.embedded.backend_registry", "STATE_DIR", None),
    ("stealth_chrome_devtools_mcp.embedded.process_cleanup", "STATE_DIR", None),
    ("stealth_chrome_devtools_mcp.embedded.response_handler", "STATE_DIR", None),
    ("stealth_chrome_devtools_mcp.embedded.singleton", "LOCK_FILE", "singleton.lock"),
    ("stealth_chrome_devtools_mcp.embedded.singleton", "PORT_FILE", "server.port"),
    (
        "stealth_chrome_devtools_mcp.embedded.singleton",
        "SERVER_STATE_FILE",
        "server.json",
    ),
    ("stealth_chrome_devtools_mcp.embedded.singleton", "STATE_DIR", None),
    ("stealth_chrome_devtools_mcp.settings", "_STATE_DIR_ENV_FILE", ".env"),
)


class RealStateDirWrite(BaseException):
    """A test tried to WRITE under the operator's real ``~/.stealth-mcp``.

    ``BaseException`` on purpose -- see this module's docstring, part 2. Nothing
    in the product catches it, so the node that did it is named in the traceback
    instead of going green over a real write.
    """


class RealBackendTerminated(BaseException):
    """A test tried to terminate a pid the operator's real record names."""


# (REAL_STATE_DIR as it was, normalised root, the root's final component).
# Memoised on the VALUE rather than computed once, because the pins point
# ``REAL_STATE_DIR`` at a decoy and the guard must follow them there.
_ROOT_CACHE: tuple[Path, str, str] | None = None


def _normalised_root() -> tuple[str, str]:
    """The normalised (and on Windows case-folded) root, plus its last part."""
    global _ROOT_CACHE
    current = REAL_STATE_DIR
    if _ROOT_CACHE is None or _ROOT_CACHE[0] != current:
        root = os.path.normpath(str(current))
        mark = Path(root).name
        if os.name == "nt":
            root = root.casefold()
            mark = mark.casefold()
        _ROOT_CACHE = (current, root, mark)
    return _ROOT_CACHE[1], _ROOT_CACHE[2]


def _under_real_state_dir(target: object) -> bool:
    """True iff ``target`` names a path at or under the real state dir.

    Deliberately string-based and allocation-light: this runs on EVERY open in
    the process, including every import. ``os.fspath`` rejects the int fds and
    file objects the wrapped primitives also accept, and a non-path argument is
    never a write to a directory.
    """
    try:
        raw = os.fspath(target)
    except TypeError:
        return False
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8", "surrogateescape")
        # A path we cannot decode is not one of ours, and this runs on every
        # open in the process, so it may not raise on an exotic encoding.
        except Exception:
            return False
    if not raw:
        return False

    root, mark = _normalised_root()
    absolute = os.path.isabs(raw)  # noqa: PTH117  PERMANENT(F-903: Path.is_absolute() builds a Path per open; this runs on every open in the process)
    if absolute and mark not in (raw.casefold() if os.name == "nt" else raw):
        # The cheap gate, and it is EXACT for an absolute path: if the path is
        # under the root then the root's whole string is a prefix of it, so the
        # root's final component must appear somewhere in it. Nearly every open
        # in a test session leaves here, on one substring scan and no syscall.
        # Deliberately NOT applied to a relative path, where the condition is
        # not necessary -- ``server.json`` read from a cwd inside the state dir
        # is under it and contains nothing. Those take the slow path below.
        return False

    # normpath, not resolve(): resolve() stats the filesystem (and on Windows
    # opens a handle), which is far too expensive for a hot wrapper and would
    # recurse into the very primitives being guarded. abspath is skipped
    # entirely when the path is already absolute -- it calls getcwd(), which was
    # the single largest cost in the wrapper (measured).
    probe = os.path.normpath(raw if absolute else os.path.abspath(raw))  # noqa: PTH100  PERMANENT(F-903: Path.resolve() stats the filesystem and would recurse into the primitives this guard wraps)
    if os.name == "nt":
        probe = probe.casefold()
    return probe == root or probe.startswith(root + os.sep)


def _writes(mode: object) -> bool:
    """True iff an ``open`` mode string asks for anything but a pure read."""
    if not isinstance(mode, str):
        return False
    return any(ch in mode for ch in "wxa+")


def _fence_root(root: Path) -> None:
    """Point all ten bindings at ``root``.

    Imports each module first: a binding cannot be redirected in a module that
    has not been loaded, and a module loaded LATER would recompute its own
    global from ``Path.home()`` and escape. Importing here is what makes the
    redirect total rather than a race with whichever test imports first.
    """
    import importlib

    root.mkdir(parents=True, exist_ok=True)
    for dotted, attr, basename in STATE_DIR_BINDINGS:
        module = importlib.import_module(dotted)
        setattr(module, attr, root if basename is None else root / basename)

    # ``settings`` does not read ``_STATE_DIR_ENV_FILE`` again: pydantic bound
    # its VALUE into ``Settings.model_config`` when the class body executed, so
    # the global above is the documentation and this is the binding that decides
    # which ``.env`` is actually read. Without it a hermetic run silently
    # absorbs the operator's own ``~/.stealth-mcp/.env`` -- their knobs, our
    # tests, and ``extra="forbid"`` turning one stale key into a suite-wide
    # crash.
    from stealth_chrome_devtools_mcp.settings import Settings

    Settings.model_config["env_file"] = root / ".env"


def _install_write_guard() -> None:
    """Wrap every write primitive so a write under the real state dir raises.

    The set is the doors the product actually writes through, reached from
    ``pathlib`` as well as from ``os``: ``Path.open``/``write_text``/
    ``write_bytes`` go through ``io.open`` (which is a SEPARATE module attribute
    from ``builtins.open``, so both are wrapped), ``Path.mkdir`` through
    ``os.mkdir``, ``Path.touch`` through ``os.open``, ``Path.replace`` through
    ``os.replace``, ``Path.unlink`` through ``os.unlink``.
    """

    def _guard_open(original):
        def wrapper(file, mode="r", *args, **kwargs):
            if _writes(mode) and _under_real_state_dir(file):
                raise RealStateDirWrite(
                    f"test wrote the operator's real state dir: {file!r} (mode {mode!r})"
                )
            return original(file, mode, *args, **kwargs)

        return wrapper

    def _guard_os_open(original):
        def wrapper(path, flags, *args, **kwargs):
            writing = bool(
                flags
                & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC)
            )
            if writing and _under_real_state_dir(path):
                raise RealStateDirWrite(
                    f"test wrote the operator's real state dir: {path!r}"
                )
            return original(path, flags, *args, **kwargs)

        return wrapper

    def _guard_paths(original, name):
        def wrapper(*args, **kwargs):
            for arg in args[:2]:
                if _under_real_state_dir(arg):
                    raise RealStateDirWrite(
                        f"test called {name} on the operator's real state dir: {arg!r}"
                    )
            return original(*args, **kwargs)

        return wrapper

    builtins.open = _guard_open(builtins.open)
    io.open = _guard_open(io.open)
    os.open = _guard_os_open(os.open)
    for name in ("mkdir", "makedirs", "replace", "rename", "remove", "unlink", "rmdir"):
        setattr(os, name, _guard_paths(getattr(os, name), f"os.{name}"))


def recorded_backend_pids() -> frozenset[int]:
    """The pids the operator's REAL record names, read once at install.

    Read through ``json`` directly rather than through ``backend_registry``:
    this runs while the fence is being installed, so the module's own reader may
    already point at the fence root.
    """
    import json

    try:
        raw = json.loads((REAL_STATE_DIR / "server.json").read_text(encoding="utf-8"))
    # No record, or one we cannot read, names nobody to protect.
    except Exception:
        return frozenset()
    entries = raw.get("backends") if isinstance(raw, dict) else None
    if isinstance(entries, dict):  # v2 keyed shape
        entries = list(entries.values())
    if not isinstance(entries, list):
        entries = [raw] if isinstance(raw, dict) else []
    pids = set()
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("pid"), int):
            pids.add(entry["pid"])
    return frozenset(pids)


def _install_kill_guard(live: frozenset[int]) -> None:
    """Refuse to terminate a pid the operator's real record names.

    ``backend_eviction.terminate`` is the ONE act that ends a backend
    (``singleton``'s four bindings all route through it, and ``stop_backend`` /
    ``restart_backend`` call it directly), so one wrapper covers every door.
    """
    from stealth_chrome_devtools_mcp.embedded import backend_eviction

    original = backend_eviction.terminate

    def guarded(*args, **kwargs):
        for candidate in (*args, *kwargs.values()):
            if isinstance(candidate, int) and candidate in live:
                raise RealBackendTerminated(
                    f"test tried to terminate pid {candidate}, a backend the "
                    f"operator's real {REAL_STATE_DIR / 'server.json'} names"
                )
        return original(*args, **kwargs)

    backend_eviction.terminate = guarded


def install(root: Path) -> frozenset[int]:
    """Install the whole fence at ``root``; answer the live pids it protects.

    Order matters: the live pids are read from the real record BEFORE the
    redirect points the readers elsewhere, and the write guard is armed AFTER
    the redirect so the fence's own ``mkdir`` is not the first thing it stops.
    """
    live = recorded_backend_pids()
    _fence_root(root)
    _install_kill_guard(live)
    _install_write_guard()
    return live


# Importing this module RUNS THE PRODUCT. ``__main__.py`` is three lines and the
# third is a bare ``main()`` at module level -- correct for ``python -m``, and a
# live grenade for any sweep that imports by name. MEASURED, 2026-09-21: the
# census probe written for THIS finding walked every module in the package,
# imported ``stealth_chrome_devtools_mcp.__main__``, and thereby started a real
# stdio proxy which cold-started a real backend (proxy pid 188108 -> backend pid
# 189088 on port 64986) into the operator's live ``~/.stealth-mcp`` -- F-886
# correctly stepped aside from port 3881 rather than evicting the backend
# holding two of the operator's logged-in browsers, which is the only reason
# this cost a stray process and not a lost session. The probe ran before the
# fence existed; it is the finding reproducing itself, and it is why this set is
# a DENY-LIST rather than a comment telling the next author to be careful.
_NEVER_IMPORT = frozenset({"stealth_chrome_devtools_mcp.__main__"})


def derived_globals() -> dict[str, str]:
    """Every package global that is a ``Path``, by dotted name.

    THE probe the binding table is measured with, kept here so the table and the
    thing that checks it cannot drift. ``tests/test_state_dir_fence.py`` calls it
    and compares against a DECOY root, so a NEW derived global fails a test
    instead of silently escaping the redirect.
    """
    import importlib
    import pkgutil
    import sys

    import stealth_chrome_devtools_mcp as pkg

    for found in pkgutil.walk_packages(pkg.__path__, pkg.__name__ + "."):
        if found.name in _NEVER_IMPORT:
            continue
        importlib.import_module(found.name)

    out: dict[str, str] = {}
    for name, module in sys.modules.items():
        if not name.startswith("stealth_chrome_devtools_mcp"):
            continue
        for attr in dir(module):
            try:
                value = getattr(module, attr)
            # A descriptor that raises on access is not a Path binding.
            except Exception:
                continue
            if isinstance(value, Path):
                out[f"{name}.{attr}"] = str(value)
    return out
