"""THE one home for keeping a test run out of the operator's REAL directories.

TWO of them, and they are one subject because they share one tripwire: the
backend **state dir** (``~/.stealth-mcp`` -- ``server.json``, the lock, the
heartbeats, ``browser_pids.json``, and the LIVE BACKENDS they name) and the
**browser-session root** (the ``master`` profile a human is logged into, and
every named session copied from it). Wrapping ``open`` twice, once per module,
would be the second-way defect; the policies differ by one flag in one table.

What differs is READS. The state dir forbids writes only, because
``release_gate_harness._reserved_ports()`` MUST read the real ``server.json``
to keep an isolated backend off a live backend's port. The session root forbids
reads TOO, because nothing in the harness reads a profile directory and copying
one is precisely how a test would take the operator's logged-in cookies into a
clone. Each asymmetry is argued where it is enforced.

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

Four parts, in the order they are installed.

0. **The session root** (:func:`_fence_session_root`). FORCED, not
   ``setdefault``-ed: ``setdefault`` cannot tell the release gate redirecting
   the suite from the operator's own root arriving in an INHERITED environment,
   and the three directories under it (master / clone root / snapshot) have
   their own env names that must be cleared with it. The proof it matters is on
   this machine -- ``e2e-warmup``, ``ci-warmup``, ``ci-cycle-0/1/2``,
   ``tree-kill-test``, ``integration-test-profile`` and ``ci-basic-test`` sit in
   the operator's real ``sessions/`` beside their ``master`` profile and 87 real
   sessions. Forced first, because ``get_settings()`` is ``@lru_cache``d and the
   E2E ``_warmup`` resolves the root during module setup.

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
   probe so a NEW derived global fails ``tests/test_operator_fence.py`` instead
   of silently escaping.

2. **The tripwire** (:func:`install`, same call) -- ONE wrapper set serving both
   designated roots. Redirecting is enumeration, and enumeration can only ever
   be as complete as the last audit, so the fence does not rely on it alone:
   **every door the PRODUCT goes through is wrapped**, and a target under a
   designated root raises. That is the backstop argument -- a path the redirect
   misses is caught at the moment of harm rather than after it.

   **It is not, and cannot be, every filesystem primitive** (F-903 review M2,
   measured on CPython 3.13.11 -- the docstring claimed the universal and the
   universal is false). These reach a designated root and do NOT raise:
   ``shutil.copy2`` (the ``_winapi.CopyFile2`` fast path opens no Python file
   object), ``Path.glob``/``rglob`` (``glob._StringGlobber.scandir`` is the
   BUILT-IN bound at ``glob`` import, so a later ``setattr(os, "scandir", …)``
   cannot reach it), ``os.chmod``/``link``/``symlink``, ``sqlite3``, any
   SUBPROCESS, and any fd opened BEFORE install (``os.write``, ``mmap``, a live
   ``logging`` stream -- there is no door left to guard). For those the REDIRECT
   is the cover and this is the backstop, which is the right way round and is
   why the section is still worth its cost.

   What IS wrapped catches the product's own profile copy -- ``_copy_profile_
   delta`` walks with ``os.walk`` (``clone_storage.py``:603) and dies at the walk
   before a byte is read -- plus ``os.walk``/``Path.walk``/``Path.iterdir``/
   ``glob.glob``/``shutil.copytree``/``copy``/``copyfile``/``rmtree``/``move``/
   ``tempfile.*(dir=)``/``logging.FileHandler``, all verified by the reviewer.
   ``Path.glob`` deserves its own line: ``logging_setup``, ``backend_launch``
   and ``file_based_element_cloner`` all glob state-dir paths today. Those are
   READS of the state dir and so legal — but a future glob under the SESSION
   root would be invisible to a guard that advertises catching reads there.

   **Writes for the state dir, reads AND writes for the session root**, for the
   reasons in the header. The per-root flag lives in one table
   (:func:`_designated_roots`), so neither policy can be applied to the wrong
   root and there is still exactly one ``open`` wrapper.

   It is a ``BaseException`` and that is load-bearing: the product is full of
   ``except Exception`` by design (``backend_registry`` is a never-raise cache,
   ``proxy_selfheal`` never raises, ``observability`` never raises), so an
   ``Exception`` here is swallowed at the first fail-open handler it meets and
   the node goes green over a real write. Measured on the F-900 branch, whose
   ``_RealStartupReached(Exception)`` was eaten at ``proxy_selfheal.py:~325``.

3. **The kill guard** (:func:`install`, same call). The record is read ONCE, at
   install, and the pids it names are the operator's LIVE backends. Terminating
   one is the harm F-886 exists to prevent, reached from the suite instead of
   from a cold start. Separate from the write guard because a kill leaves no
   filesystem trace.

   **It guards the two doors a kill goes through, not the callers** (F-903
   review M1): ``psutil.Process.terminate``/``kill``/``send_signal`` and
   ``os.kill``, each checking ``self.pid`` / the ``pid`` argument -- the number
   the OS is about to act on. The first version guarded
   ``backend_eviction.terminate`` and walked its ARGUMENTS, which was wrong in
   both directions and measured so by the reviewer: that function takes a PORT
   and computes the pid locally, so the guard never saw the pid and ALLOWED a
   call that would have killed pid 47424, while REFUSING a call whose port
   happened to equal a protected pid. Guarding the primitive also covers the
   nine other kill sites the arg-walking guard could not see, in
   ``process_cleanup``, ``spawn_leak``, ``browser_manager`` and
   ``desktop_launch``. On POSIX ``terminate``/``kill`` are thin wrappers over
   ``send_signal`` and the check runs twice, which is free and is why all three
   are wrapped rather than just the one Windows needs.

**Why HOME is not redirected.** Setting ``HOME``/``USERPROFILE`` would fence all
ten bindings at once and every future one for free, and it was rejected for a
measured reason: ``release_gate_harness._reserved_ports()`` calls ``Path.home()``
to find the ports the operator's real backends hold. Under a redirected HOME it
would read the EMPTY fence record, exclude nothing, and an isolated backend could
bind the port a live backend is serving on. The fence would have created the
collision it exists to prevent. It would also not have fenced the session root
at all on Windows, where the product's default is the hardcoded absolute
``C:\\stealth-mcp-browser-sessions`` and owes nothing to ``Path.home()``. Child
processes keep their own HOME redirection
(``release_gate_harness._isolated_env``), which is a different mechanism for a
different process and is untouched.

**Why import time and not a fixture.** An autouse FUNCTION-scoped fixture is
ordered after a module-scoped one, and the E2E modules' ``_warmup`` is exactly
that -- it starts a backend AND resolves the session root during module setup,
before any function fixture runs. Installing at conftest import time is ahead of
collection, every fixture of every scope, and every import-time side effect.

**What this module may not do: import the product at module level.** Both
redirects have to be in place before ``stealth_chrome_devtools_mcp`` is first
imported, which is why :func:`_product_session_root` re-spells a default rather
than asking ``clone_storage`` for it, and why every product import here is
inside a function.
"""

from __future__ import annotations

import builtins
import io
import os
from pathlib import Path

# The operator's real state dir, captured before anything is redirected. Read
# from Path.home() exactly as backend_registry does, so the two cannot drift.
REAL_STATE_DIR = Path.home() / ".stealth-mcp"

# The four env names that place the browser-session root and the three
# directories under it. ``clone_storage`` derives the last three from the first
# when they are unset, so forcing the root alone is not enough: an INHERITED
# ``BROWSER_MASTER_USER_DATA_DIR`` would still name the operator's real master
# profile under a redirected root.
SESSION_ROOT_ENV = "STEALTH_MCP_BROWSER_SESSION_ROOT"
_DERIVED_ROOT_ENV = (
    "BROWSER_MASTER_USER_DATA_DIR",
    "BROWSER_PROFILE_CLONE_ROOT",
    "BROWSER_MASTER_SNAPSHOT_DIR",
)


def _product_session_root() -> Path:
    """``clone_storage.default_session_root()``'s answer with no env set.

    Spelled here rather than imported because it must be known BEFORE the
    product is imported, and because importing ``clone_storage`` to ask would
    read the very env this module is about to change. Kept in step by
    ``tests/test_operator_fence.py``, which calls the real function with the
    env cleared and compares.
    """
    if os.name == "nt":
        return Path(r"C:\stealth-mcp-browser-sessions")
    return Path.home() / ".stealth-mcp-browser-sessions"


# Filled by install(): the browser-session roots a test run must never touch.
REAL_SESSION_ROOTS: tuple[Path, ...] = ()

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


class RealSessionRootAccess(BaseException):
    """A test tried to READ OR WRITE the operator's real browser-session root.

    Reads count here and deliberately do NOT count for the state dir, and the
    asymmetry is the point of each. Nothing in the harness reads a profile
    directory, while copying one is how a test would take the operator's
    logged-in cookies into a clone -- so a read IS the harm. For the state dir
    the opposite holds: ``release_gate_harness._reserved_ports()`` MUST read the
    real ``server.json``.
    """


class RealBackendTerminated(BaseException):
    """A test tried to terminate a pid the operator's real record names."""


class RealStartupReached(BaseException):
    """A test reached ``singleton.ensure_server_running``, the real startup path.

    The ACT with no filesystem trace. The write guard cannot see this one: by the
    time anything is written a backend is already coming up, so the tripwire has
    to sit on the function. It says something none of the three above do -- a
    write, a profile read and a terminate are each a different event -- which is
    why this is a fourth class and not one of them reused under a wrong name.

    **Deliberately a ``BaseException`` and not an ``AssertionError``** (F-900
    review M2, whose wording this keeps). ``proxy_selfheal.heal_backend`` drives
    ``ensure_running`` inside ``except Exception:  # PERMANENT(a backstop must
    not raise)`` (``proxy_selfheal.py``:325), and an ``AssertionError`` IS an
    ``Exception``: the first version of that tripwire was swallowed there, logged
    once per attempt as ``heal attempt <n>/HEAL_ATTEMPTS failed``, retried for the
    rest of the budget, and the node PASSED -- with a real backend already
    cold-started (pid 55240, port 21770, in the real ``~/.stealth-mcp``). A
    tripwire a backstop can eat is decoration. That is the same argument the rest
    of this module's tripwires are built on, which is why they share a home.

    **This module does not INSTALL a guard for it** -- unlike the write and kill
    guards, which are suite-wide. A spawn cannot be refused by default without
    deciding for the integration tier and the herd test, which legitimately cold
    start; so the SYMBOL is shared and the INSTALL stays the arming test's
    (``tests/test_proxy_bridge_transport.py`` patches ``ensure_server_running``
    itself). Making it suite-wide is the named follow-up in F-903's finding.
    """


# One designated root: where it is, how a path under it is recognised, whether
# READS are forbidden too, and which exception says so.
_Designated = tuple[str, str, bool, type[BaseException]]

# Rebuilt whenever the inputs change, because the pins point ``REAL_STATE_DIR``
# at a decoy and the guard must follow them there.
_TABLE_CACHE: tuple[object, tuple[_Designated, ...]] | None = None


def _root_spellings(path: Path) -> list[str]:
    """Every spelling of ONE designated root the OS would accept for it.

    F-903 review S1. The hot guard compares strings, so a root known by one
    spelling is fenced by one spelling -- and Windows hands out several for the
    same directory: a junction or directory symlink pointing at it, the 8.3
    short name (``C:\\Users\\amind\\.STEAL~1``), the ``\\\\?\\`` prefix. Each is
    a path the product could be handed and the guard would not recognise.

    Resolving here is affordable because it happens ONCE per table rebuild
    (install, plus the pins repointing ``REAL_STATE_DIR`` at a decoy) and never
    in :func:`_designated`, which stays on ``normpath`` -- ``realpath`` stats
    the filesystem and would recurse into the primitives being guarded.

    What this does NOT close, and the finding says so: a junction MID-path in
    the TARGET (``C:\\link\\server.json`` where ``C:\\link`` -> the state dir)
    is still a spelling of a fenced file that no root string is a prefix of.
    Closing that needs a resolve per call, which is the cost this whole guard
    is built to avoid; the redirect is what covers it.
    """
    spellings = [os.path.normpath(str(path)), os.path.realpath(str(path))]  # noqa: PTH100  PERMANENT(F-903: Path.resolve() is realpath plus a Path allocation; the string is what the guard compares)
    if os.name == "nt":
        import ctypes

        for spelling in tuple(spellings):
            buffer = ctypes.create_unicode_buffer(32768)
            # Answers 0 for a path that does not exist, which is not an error
            # here -- an absent root has no short name to be reached by.
            if ctypes.windll.kernel32.GetShortPathNameW(spelling, buffer, 32768):
                spellings.append(buffer.value)
        # The two LOCAL admin-share spellings, for the same reason. This is the
        # one family that cannot be enumerated -- any name that resolves to this
        # machine works, and a remote host's share of the same volume would too
        # -- so the two the reviewer actually reached are covered and the rest is
        # named as a residual rather than pretended away (review N3).
        for host in ("localhost", "127.0.0.1"):
            spellings += [
                f"\\\\{host}\\{s[0]}$" + s[2:]
                for s in tuple(spellings)
                if len(s) > 2 and s[1] == ":"
            ]
        # The extended-length prefix is a spelling of every one of the above and
        # `normpath` keeps it verbatim, so `\\?\C:\…\server.json` is a fenced
        # file no bare-drive root string is a prefix of.
        spellings += [
            "\\\\?\\" + s for s in tuple(spellings) if not s.startswith("\\\\")
        ]
    return spellings


def _designated_roots() -> tuple[_Designated, ...]:
    """The roots this process may not touch, normalised once per change."""
    global _TABLE_CACHE
    key = (REAL_STATE_DIR, REAL_SESSION_ROOTS)
    if _TABLE_CACHE is not None and _TABLE_CACHE[0] == key:
        return _TABLE_CACHE[1]

    rows: list[_Designated] = []
    seen: set[str] = set()
    for path, reads_too, error in (
        (REAL_STATE_DIR, False, RealStateDirWrite),
        *((root, True, RealSessionRootAccess) for root in REAL_SESSION_ROOTS),
    ):
        for spelling in _root_spellings(path):
            root = spelling
            mark = Path(root).name
            if os.name == "nt":
                root = root.casefold()
                mark = mark.casefold()
            # A root with no final component is a filesystem root (``C:\``,
            # ``/``). Designating one would fence the whole disk off from the
            # suite, so a misconfigured value is dropped rather than obeyed.
            if mark and root not in seen:
                seen.add(root)
                rows.append((root, mark, reads_too, error))
    _TABLE_CACHE = (key, tuple(rows))
    return _TABLE_CACHE[1]


def _designated(target: object, *, writing: bool) -> _Designated | None:
    """The designated root ``target`` falls under, or None.

    Deliberately string-based and allocation-light: this runs on EVERY open in
    the process, including every import. ``os.fspath`` rejects the int fds and
    file objects the wrapped primitives also accept, and a non-path argument is
    never a write to a directory.
    """
    try:
        raw = os.fspath(target)
    except TypeError:
        return None
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8", "surrogateescape")
        # A path we cannot decode is not one of ours, and this runs on every
        # open in the process, so it may not raise on an exotic encoding.
        except Exception:
            return None
    if not raw:
        return None

    absolute = os.path.isabs(raw)  # noqa: PTH117  PERMANENT(F-903: Path.is_absolute() builds a Path per open; this runs on every open in the process)
    folded = raw.casefold() if os.name == "nt" else raw
    probe: str | None = None

    for root, mark, reads_too, error in _designated_roots():
        if not writing and not reads_too:
            continue
        if absolute and mark not in folded:
            # The cheap gate, and it is EXACT for an absolute path: if the path
            # is under the root then the root's whole string is a prefix of it,
            # so the root's final component must appear somewhere in it. Nearly
            # every open in a test session leaves here, on one substring scan
            # and no syscall. Deliberately NOT applied to a relative path, where
            # the condition is not necessary -- ``server.json`` read from a cwd
            # inside the state dir is under it and contains nothing.
            continue
        if probe is None:
            # normpath, not resolve(): resolve() stats the filesystem (and on
            # Windows opens a handle), which is far too expensive for a hot
            # wrapper and would recurse into the very primitives being guarded.
            # abspath is skipped when the path is already absolute -- it calls
            # getcwd(), which was the single largest cost here (measured).
            probe = os.path.normpath(raw if absolute else os.path.abspath(raw))  # noqa: PTH100  PERMANENT(F-903: Path.resolve() stats the filesystem and would recurse into the primitives this guard wraps)
            if os.name == "nt":
                probe = probe.casefold()
        if probe == root or probe.startswith(root + os.sep):
            return (root, mark, reads_too, error)
    return None


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


def _refuse(hit: _Designated, what: str, target: object) -> BaseException:
    """The one place a designated-root hit becomes the exception that says so."""
    root, _mark, reads_too, error = hit
    subject = "browser-session root" if reads_too else "state dir"
    return error(
        f"test {what} the operator's real {subject} ({root}): {target!r}. "
        "This is F-903's fence; redirect the path, never the guard."
    )


def _install_write_guard() -> None:
    """Wrap every filesystem primitive a designated root could be reached by.

    The set is the doors the product actually goes through, reached from
    ``pathlib`` as well as from ``os``: ``Path.open``/``write_text``/
    ``write_bytes`` go through ``io.open`` (which is a SEPARATE module attribute
    from ``builtins.open``, so both are wrapped), ``Path.mkdir`` through
    ``os.mkdir``, ``Path.touch`` through ``os.open``, ``Path.replace`` through
    ``os.replace``, ``Path.unlink`` through ``os.unlink``.

    ``writing=`` is passed per call rather than baked in, because the two
    designated kinds answer differently: the state dir forbids writes only, the
    browser-session root forbids reads too. ``scandir``/``listdir`` join the set
    for the session root's sake -- ``_copy_profile_tree`` walks a directory
    before it opens a single file in it, so a copy of the operator's master
    profile is caught at the walk rather than at its first byte.
    """

    def _guard_open(original):
        def wrapper(file, mode="r", *args, **kwargs):
            hit = _designated(file, writing=_writes(mode))
            if hit is not None:
                raise _refuse(hit, f"opened (mode {mode!r})", file)
            return original(file, mode, *args, **kwargs)

        return wrapper

    def _guard_os_open(original):
        def wrapper(path, flags, *args, **kwargs):
            writing = bool(
                flags
                & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC)
            )
            hit = _designated(path, writing=writing)
            if hit is not None:
                raise _refuse(hit, "opened", path)
            return original(path, flags, *args, **kwargs)

        return wrapper

    def _guard_paths(original, name, *, writing=True):
        def wrapper(*args, **kwargs):
            # KEYWORD forms are inspected too (F-903 review M4): `os.remove(
            # path=…)`, `os.rmdir(path=…)`, `os.mkdir(path=…)` and
            # `os.replace(src=…, dst=…)` all walked straight through an
            # args-only guard -- measured, the fenced file was deleted. Nothing
            # in the product uses those spellings today, but "this door is
            # closed" may not be true of one call syntax out of two. No
            # signature knowledge is needed: kwargs on these eight carry paths,
            # ints (`dir_fd`) and bools, and `_designated` already answers None
            # for anything `os.fspath` refuses.
            for arg in (*args[:2], *kwargs.values()):
                hit = _designated(arg, writing=writing)
                if hit is not None:
                    raise _refuse(hit, f"called {name} on", arg)
            return original(*args, **kwargs)

        return wrapper

    builtins.open = _guard_open(builtins.open)
    io.open = _guard_open(io.open)
    os.open = _guard_os_open(os.open)
    for name in (
        "mkdir",
        "makedirs",
        "replace",
        "rename",
        "remove",
        "unlink",
        "rmdir",
        # F-903 review M2: both are one-arg-path WRITES that reached a
        # designated root and did not raise -- `os.truncate` emptied a fenced
        # file, and `os.utime` is the fast path `Path.touch()` takes on a file
        # that already exists, so a touch bumped a fenced mtime. Zero extra
        # machinery; they simply belong in this loop.
        "truncate",
        "utime",
    ):
        setattr(os, name, _guard_paths(getattr(os, name), f"os.{name}"))
    # Reading doors: only the session root forbids reads, so these pass
    # writing=False and the state dir's row skips itself.
    for name in ("scandir", "listdir"):
        setattr(os, name, _guard_paths(getattr(os, name), f"os.{name}", writing=False))


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
    """Refuse to end a process the operator's real record names as a backend.

    **Guard the ACT, not the argument** (F-903 review M1). The first version
    wrapped ``backend_eviction.terminate`` and walked its arguments for an
    ``int`` in ``live``, which does not work and was measured failing BOTH ways:
    that function's kill target is a LOCAL, computed inside it by a machine-wide
    ``psutil.net_connections`` scan (``pid = pid_on_port(port)``), so the guard
    never saw it and ALLOWED a call that would have killed the operator's pid
    47424; and the only ints it DID see were ``port`` and ``recorded_pid``, so a
    test-owned backend binding TCP port 47424 was refused about a process the
    operator never owned. The kill guard's whole reason for being separate from
    the write guard is that a kill leaves no filesystem trace -- which was
    exactly the case it did not cover.

    So the wrapper goes where every kill in the tree converges: the two psutil
    methods and ``os.kill``. That is strictly smaller (no ``backend_eviction``
    import, no argument walk), it sees the real pid at the real moment, and it
    also covers the nine kill sites the argument walk missed --
    ``process_cleanup._kill_process_by_pid``, ``spawn_leak
    .reap_launched_browsers``, ``browser_manager._blocking_teardown`` (the
    tree's only ``os.kill``) and ``desktop_launch._kill_delegated``, all of
    which reach live Chrome pids from a cmdline scan, and the operator's two
    backends own human-login Chromes.

    ``send_signal`` is included because ``terminate``/``kill`` are thin wrappers
    over it on POSIX but NOT on Windows, so wrapping only the two would leave
    a door open on one platform and double-report on the other.
    """
    import psutil

    def _check(pid: object) -> None:
        if isinstance(pid, int) and pid in live:
            raise RealBackendTerminated(
                f"test tried to end pid {pid}, a backend the operator's real "
                f"{REAL_STATE_DIR / 'server.json'} names"
            )

    for name in ("terminate", "kill", "send_signal"):
        original = getattr(psutil.Process, name)

        def wrapper(self, *args, _original=original, **kwargs):
            _check(self.pid)
            return _original(self, *args, **kwargs)

        setattr(psutil.Process, name, wrapper)

    original_kill = os.kill

    def guarded_kill(pid, *args, **kwargs):
        _check(pid)
        return original_kill(pid, *args, **kwargs)

    os.kill = guarded_kill


def _fence_session_root(env: dict[str, str], root: Path) -> tuple[Path, ...]:
    """Force the browser-session root to ``root``; answer the roots now forbidden.

    **``setenv``, not ``setdefault``, and that is the whole fix.** The
    ``setdefault`` this replaces was deliberate -- it let the release gate's own
    ``runner.temp`` value win -- but it cannot tell the gate redirecting the
    suite from the OPERATOR'S OWN root arriving in an inherited environment, and
    those are the same string-shaped thing. An agent shell that inherited
    ``STEALTH_MCP_BROWSER_SESSION_ROOT`` from the MCP client's config therefore
    ran the whole E2E tier against the real root. Forcing costs the gate
    nothing: its value is a throwaway temp dir and so is ours.

    The three DERIVED names are cleared rather than set, so they go back to
    deriving from the root above -- ``clone_storage`` only reads them when they
    are non-empty, and an inherited ``BROWSER_MASTER_USER_DATA_DIR`` would
    otherwise still name the operator's real master profile underneath a
    redirected root. ``tmp_session_root`` / ``tmp_empty_root`` set all four
    per-test through ``patch.dict``, which still wins over this baseline.

    What is DESIGNATED is the union of what the operator's own run would have
    used: the inherited value (if any) and the product's own default. The
    default has to be in there even when the env was set, because on Windows it
    is the hardcoded ``C:\\stealth-mcp-browser-sessions`` -- the real root on
    this machine, holding the ``master`` profile a human is logged into and 87
    named sessions, reachable with no env var at all.
    """
    inherited = env.get(SESSION_ROOT_ENV, "").strip()
    forbidden = {_product_session_root()}
    if inherited:
        forbidden.add(Path(inherited).expanduser())

    env[SESSION_ROOT_ENV] = str(root)
    for name in _DERIVED_ROOT_ENV:
        value = env.pop(name, "").strip()
        if value:
            forbidden.add(Path(value).expanduser())

    root.mkdir(parents=True, exist_ok=True)
    # Never designate the directory the suite is about to use. It cannot be the
    # operator's today, but a future caller passing one of these paths in would
    # otherwise fence the suite out of its own workspace.
    return tuple(sorted(forbidden - {root}, key=str))


def install(root: Path, *, session_root: Path, env: dict[str, str]) -> frozenset[int]:
    """Install the whole fence; answer the live backend pids it protects.

    Order matters and every step depends on the one before. The live pids are
    read from the real record BEFORE the redirect points the readers elsewhere;
    the session root is forced BEFORE any product import, because
    ``get_settings()`` is ``@lru_cache``d and the E2E ``_warmup`` resolves the
    root during module setup; and the filesystem guard is armed LAST, after both
    redirects, so the fence's own ``mkdir`` is not the first thing it stops.
    """
    global REAL_SESSION_ROOTS

    live = recorded_backend_pids()
    REAL_SESSION_ROOTS = _fence_session_root(env, session_root)
    _fence_root(root)
    _install_kill_guard(live)
    _install_write_guard()
    return live


def derived_globals() -> dict[str, str]:
    """Every package global that is a ``Path``, by dotted name -- and every
    ``Path`` a module-level SINGLETON captured on itself.

    THE probe the binding table is measured with, kept here so the table and the
    thing that checks it cannot drift. ``tests/test_operator_fence.py`` calls it
    and compares against a DECOY root, so a NEW derived global fails a test
    instead of silently escaping the redirect.

    It imports EVERY module, with no exclusions, and that is only safe because
    no module body in this package does work. It carried a ``_NEVER_IMPORT``
    deny-list until F-903 fixed the one module that did: ``__main__.py`` called
    ``main()`` at module level, so this very sweep started a real stdio proxy
    which cold-started a real backend (proxy pid 188108 -> backend pid 189088 on
    port 64986) into the operator's live ``~/.stealth-mcp``. The deny-list is
    deleted rather than emptied, because the hazard is fixed at its source and a
    standing exclusion list is a second defence that rots; what replaces it is a
    POSITIVE pin over the whole package --
    ``tests/test_package_entrypoints.py::TestNoModuleBodyDoesWork`` -- which
    fails on any new module-level call rather than on the one name someone
    remembered.

    **Module-level singletons are walked one level deep** (F-903 review S3).
    A module GLOBAL is not the only way a state-dir path gets captured at import
    time: ``process_cleanup.process_cleanup`` is constructed in its own module
    body and keeps ``self.pid_file = STATE_DIR / RECORD_NAME``, and
    ``file_based_element_cloner``'s cloner keeps an ``output_dir`` the same way.
    Both are fenced today, and the reviewer measured that an attributes-only
    probe reported NEITHER -- so the promise "a new derived path fails a test"
    had a hole exactly where the redirect's own ORDER is what saves it
    (``backend_registry.STATE_DIR`` is rebound on row 3, before row 4 imports
    ``process_cleanup`` and constructs the singleton). Rather than write that
    order down as load-bearing and hope, the probe now sees the capture, so a
    singleton constructed too early is a failing pin.

    Only instances of the package's OWN classes are walked, and only their
    ``__dict__`` -- no recursion. That is where an import-time capture lives;
    anything deeper is runtime state, which the write guard covers.
    """
    import importlib
    import pkgutil
    import sys

    import stealth_chrome_devtools_mcp as pkg

    for found in pkgutil.walk_packages(pkg.__path__, pkg.__name__ + "."):
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
                continue
            owner = type(value).__module__ or ""
            if not owner.startswith("stealth_chrome_devtools_mcp"):
                continue
            # ``__slots__`` and C-level objects have no ``__dict__``; neither
            # can hold an import-time capture the way an ordinary instance can.
            for key, held in getattr(value, "__dict__", {}).items():
                if isinstance(held, Path):
                    out[f"{name}.{attr}.{key}"] = str(held)
    return out
