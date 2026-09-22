"""``stealth-chrome-devtools`` — an ops CLI for the stealth browser MCP server.

A thin management layer over the *same* backend the MCP server uses: inspect the
singleton, list profiles, reclaim disk (the storage sweep), check the
environment, or start the server. It never reimplements browser logic — to drive
a browser, use the MCP server (or its HTTP backend) directly.

Two surfaces, one backend (F-700/F-109): this project ships two entry points —
``stealth-chrome-devtools-mcp`` (the MCP server: the tool surface an AI client
drives) and ``stealth-chrome-devtools`` (this ops CLI: the surface a human
inspects and operates). They are deliberately separate surfaces over the one
backend, not a single merged command; the registry side of this note lives in
``embedded/tool_registry.py``. Recorded for M14 (CONTRIBUTING/DESIGN).

Read-only commands (``status``, ``profiles``, ``cleanup`` without ``--apply``,
``doctor``) import the package with ``STEALTH_MCP_NO_AUTO_RECOVERY=1`` so merely
running the CLI never kills a running server's browsers or touches its profiles.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from stealth_chrome_devtools_mcp.observability import sentry_init

if TYPE_CHECKING:
    # Type-only: every embedded import in this file is LAZY, inside the function
    # that needs it, so a read-only verb never drags the backend in at import.
    from stealth_chrome_devtools_mcp.embedded.backend_liveness import Surveyed


def _server():
    """Import the embedded server module, reusing its profile/storage helpers.

    Forces read-only import semantics: no orphan-process recovery, no atexit
    teardown handlers — so the CLI never disturbs a running backend.

    This is the OTHER door onto an ``import fastmcp`` (F-889 review N3;
    ``server.main()`` is the first), and every verb but ``profiles`` comes
    through it — including the two an operator runs to find out why nothing
    starts. So the F-890 scrub joins the environment write that was already
    here, in front of the same import. It removes third-party names only: the
    read-only guard set one line above is ours and survives it.
    """
    os.environ.setdefault(  # noqa: TID251  PERMANENT(env write before import)
        "STEALTH_MCP_NO_AUTO_RECOVERY", "1"
    )
    from stealth_chrome_devtools_mcp.embedded import backend_env

    backend_env.scrub_process_env()

    from stealth_chrome_devtools_mcp.embedded import server

    return server


def _clone_storage():
    """Import the clone-storage subsystem (the profile/storage helpers),
    forcing the same read-only import semantics as :func:`_server`: the
    NO_AUTO_RECOVERY guard is set before the import so the CLI never disturbs
    a running backend."""
    os.environ.setdefault(  # noqa: TID251  PERMANENT(env write before import)
        "STEALTH_MCP_NO_AUTO_RECOVERY", "1"
    )
    from stealth_chrome_devtools_mcp.embedded import clone_storage

    return clone_storage


_BINARY_UNIT = 1024


def _human(num: int) -> str:
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < _BINARY_UNIT or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= _BINARY_UNIT
    return f"{value:.1f} TB"


def _role(cs, path: Path) -> str:
    if cs.clone_is_auto(path):
        return "auto-clone"
    if cs.clone_is_named(path):
        return "named"
    return "unmarked"


# The SEED's own role word in the `profiles` listing — "the copyable form of
# `default`", never "snapshot", which named a mechanism a reader can neither
# open nor act on (F-896). The SHARED session's word is not spelled here at
# all: it is `profile_seed.DEFAULT_SESSION`, read at call time through the same
# lazy import the rest of this block uses, so the CLI's role column and a
# spawn's `profile_role` cannot drift apart.
_SEED_ROLE = "default-seed"


def _seed_roles() -> frozenset[str]:
    """The two roles that ARE the seed, so nothing seeded them."""
    from stealth_chrome_devtools_mcp.embedded import profile_seed

    return frozenset({profile_seed.DEFAULT_SESSION, _SEED_ROLE})


def _collect_profiles(cs) -> list[dict]:
    """Every profile under the session root with size, role, in-use flag and
    seed provenance (F-895). The provenance is ``profile_seed.provenance``'s —
    the same three fields ``spawn_diagnostics.profile_selection`` reports, read
    from the same marker, so the CLI and a spawn can never disagree about where
    a session came from."""
    from stealth_chrome_devtools_mcp.embedded import profile_copy, profile_seed

    rows: list[dict] = []

    def _row(path: Path, role: str) -> dict:
        return {
            "name": path.name,
            "path": path,
            "role": role,
            "size": profile_copy.dir_size_bytes(path),
            "in_use": cs._profile_has_running_browser(path),
            **profile_seed.provenance(path),
        }

    master = cs.master_profile_dir()
    snapshot = cs.master_snapshot_dir()
    if master.exists():
        rows.append(_row(master, profile_seed.DEFAULT_SESSION))  # the shared session
    if snapshot.exists():
        rows.append(_row(snapshot, _SEED_ROLE))

    clone_root = cs.clone_root_dir()
    if clone_root.exists():
        rows.extend(
            _row(child, _role(cs, child))
            for child in sorted(clone_root.iterdir())
            if child.is_dir()
        )
    return rows


def _seed_line(row: dict[str, object]) -> str:
    """One profile's seed provenance as a line (F-895), or "" for a row where
    the question does not arise.

    This function is the QUESTION and ``profile_seed.seed_sentence`` is the
    phrasing (F-897), because ``stealthy spawn`` says the same thing about the
    session it just made and two phrasings of one marker would drift. What
    stays here is the part that is this verb's own: the shared session and its
    seed ARE the seed, so asking what seeded them is a category error; they
    carry no marker and reported "seeded from unknown" about themselves, on
    exactly the two rows an operator reads first (review m6). An unmarked
    SESSION directory still says unknown — there the answer is genuinely not
    known, which is the thing worth printing, and it is why the gate is the
    ROLE and never the absence of a marker.
    """
    from stealth_chrome_devtools_mcp.embedded import profile_seed

    if row.get("role") in _seed_roles():
        return ""
    return profile_seed.seed_sentence(row)


def _gb_to_bytes(gb: float | None, fallback: int) -> int:
    if gb is None:
        return fallback
    return 0 if gb <= 0 else int(gb * (1024**3))


# ── commands ────────────────────────────────────────────────────────────────


def _format_backend_status(status: str, port: int | None) -> str:
    """Human-readable backend status for status/doctor, formatting what
    `_probe_backend_status` (plan_M1 SS2.1-D) reported instead of
    `_find_running_server`'s binary reuse-or-not answer. Closes F-301's "status
    prints *running* through the whole outage" half: a wedged backend (socket
    open, dispatch loop dead) now reports UNRESPONSIVE instead of a plain
    "running" indistinguishable from a genuinely healthy one.

    The probe is the CALLER's (F-868): every line of the status block —
    liveness, pid, log path, port occupant — must describe ONE backend, and the
    only way to guarantee that is to select it once and pass it down. This
    function is pure formatting and performs no I/O at all.
    """
    # "down" (a stale record but nothing actually listening) and "none" (no
    # record at all) both read as "not running" to an operator - there is no
    # live process to reconnect to either way; plan_M1 SS2.1-D's three
    # display strings map 1:1 to what matters operationally: not running /
    # responsive / wedged.
    if status in ("none", "down"):
        return "not running"
    if status == "wedged":
        return (
            f"running but UNRESPONSIVE on port {port} — wedged; "
            "a new session will evict and respawn it"
        )
    return f"running (responsive) on port {port}"


def _recorded_backend_pid(port: int | None) -> int | None:
    """The pid singleton recorded for the backend on ``port``, or None when
    that port names no entry. Independent of liveness — status/doctor combine
    this with `_format_backend_status()`'s liveness read separately (F-305).

    The backend on THAT port, never the first recorded one (F-868). The record
    holds one entry per display context (F-808), so "first" is routinely a
    different backend from the one the status line just reported — that split
    is what printed a dead sibling's pid under a live backend's status. Same
    agree-on-one-port rule `singleton.stop_backend` and `restart_backend`
    already apply; naming every entry belongs to `_doctor_backend_lines`."""
    from stealth_chrome_devtools_mcp.embedded import backend_registry, singleton

    entry = backend_registry.backend_on_port(singleton._read_server_state(), port)
    return backend_registry.recorded_int(entry, "pid")


def _other_records_note(port: int | None) -> str:
    """The backends recorded BESIDE the one reported, or "" when the reported
    backend is the only entry there is (F-868).

    The status block is one summary line over a record that can hold several
    backends, and a summary that silently drops the rest reads as "this is all
    there is".

    "Other" is decided on the (DISPLAY CONTEXT, PORT) PAIR, and needs both.
    Context alone was the rule until F-886 made a context able to hold one
    backend per IDENTITY: a stranger's backend and ours then share a token, so
    on exactly the machine that fix creates neither entry was ever an "other"
    and the summary went back to reading "this is all there is" while a second
    backend served the same desktop. Port alone is the trap the old docstring
    named: a hand-edited entry whose `port` is a string reads as `None` and
    would match a `None` reported port, hiding itself in the "nothing is
    running" case where the operator most needs to see it. The pair has neither
    failure — that entry differs from the reported one on port, and when
    nothing is reported at all `mine` is `None`, which no pair equals, so every
    recorded entry is correctly an "other".

    Each is named with its port for the same reason: two entries under one
    context are indistinguishable by token, and "2 backends recorded
    (win-session-1, win-session-1)" tells an operator nothing to act on.

    It never re-decides which backend to report — it only names what the
    reported one is not, and points at the verb that probes them all."""
    from stealth_chrome_devtools_mcp.embedded import backend_registry, singleton

    state = singleton._read_server_state()
    reported = backend_registry.backend_on_port(state, port)
    mine = (reported.get("display_context"), reported.get("port")) if reported else None
    others = [
        f"{entry.get('display_context')}:{entry.get('port')}"
        for entry in backend_registry.backends_in(state)
        if (entry.get("display_context"), entry.get("port")) != mine
    ]
    if not others:
        return ""
    return (
        f"{len(others)} backends recorded ({', '.join(others)}) — "
        "run `doctor` for each one's state"
    )


def _backend_log_location(pid: int | None) -> str:
    """Where to look for backend logs (F-503's log-path half: M3 delivered
    "there is now a log"; this delivers "here is where"). Names the exact
    per-pid file when a pid is recorded, else the shared boot log."""
    from stealth_chrome_devtools_mcp.embedded.logging_setup import resolve_log_dir

    filename = f"backend-{pid}.log" if pid is not None else "backend-boot.log"
    return str(resolve_log_dir() / filename)


def _survey_records() -> list[Surveyed]:
    """THE one probe pass over every recorded backend, for the two CLI verbs
    that need per-entry answers (F-880).

    `backend_liveness.survey` owns both the liveness vocabulary (including the
    one word the down/wedged/responsive ladder cannot reach, `NO_PORT`, which
    `_probe_recorded_backend` used to add here before this function replaced it)
    and the two-witness deadness rule. This is only the BINDING: our record
    path, and the two witnesses reached THROUGH `singleton` at call time, never
    imported by name, so a test that patches `singleton._probe_port` or
    `singleton._is_our_backend` still wins.

    Ordering is `backend_registry.window_capable_first`'s, so doctor presents
    the same preference discovery applies rather than re-deriving one; the
    survey itself is order-agnostic.
    """
    from stealth_chrome_devtools_mcp.embedded import (
        backend_liveness,
        backend_registry,
        singleton,
    )

    return backend_liveness.survey(
        backend_registry.window_capable_first(singleton.SERVER_STATE_FILE),
        probe=singleton._probe_port,
        pid_is_ours=singleton._is_our_backend,
    )


def _dead_record_line(surveyed: list[Surveyed]) -> str:
    """The one-line summary of dead records, or "" when none is (F-880).

    READ-ONLY, and it names the verb that writes. `doctor` must stay read-only
    by contract (module docstring, `STEALTH_MCP_NO_AUTO_RECOVERY=1`), so it may
    report residue but may not reclaim it; `cleanup --apply` does that, through
    the same `backend_liveness` home rather than a second sweep."""
    from stealth_chrome_devtools_mcp.embedded import backend_liveness

    dead = backend_liveness.dead_entries(surveyed)
    if not dead:
        return ""
    contexts = ", ".join(str(e.get("display_context")) for e in dead)
    return (
        f"{len(dead)} dead record(s) ({contexts}) — nothing is listening and the "
        "recorded pid is not a backend of ours; run `cleanup --apply` to forget them"
    )


def _doctor_backend_lines() -> list[str]:
    """One line per RECORDED backend — context, port, pid, version, liveness,
    and whether a window launched there could be seen — plus the remedy when
    none of them can show one.

    The `backend :` line above answers "is the backend up"; this answers "which
    desktops have one", the operational question F-808 created. A headed spawn
    is refused when the backend serving it can neither display a window nor hand
    the launch to a logged-on desktop (F-810), and that refusal points the
    operator at this command, so it must be able to name every context — the
    summary line above, which reports the ONE backend this shell would be served
    by (`_probe_backend_status`'s adoption walk, F-868), cannot: every entry a
    proven-capable client will not adopt is missing from it by design.
    Ordering is `backend_registry.window_capable_first`'s, so doctor presents
    the same preference discovery applies rather than re-deriving one.

    Since F-880 both the per-entry verdict and the `(dead record)` marker come
    from ONE `_survey_records()` pass, so a wedged sibling is probed once per
    doctor run and not once per question asked about it. The marker is appended
    AFTER the capability note, which stays token-driven: where that backend's
    windows WOULD appear is true of a record whether or not anything is there.

    The remedy is suppressed only by a window-capable backend that is actually
    RESPONSIVE. A desktop backend recorded but dead — the desktop logged out —
    would otherwise hide the advice in precisely the state that needs it: an
    SSH session's headed spawn is served by no live capable backend, so
    "start one from a desktop session" is still worth saying. A wedged one is no
    better: it cannot serve the spawn either. The per-line "(can show windows)"
    note stays token-driven — it describes where that backend's windows WOULD
    appear, which is true whether or not it is currently answering.

    What that state MEANS changed with F-810, so the remedy has two forms. With
    a user logged on at the desktop, headed spawns self-heal (the OS places the
    launch there), and telling the operator they "will fail" would be a lie
    about their own machine — the advice degrades to an optimisation. Only with
    nobody logged on is the spawn genuinely refused.
    """
    from stealth_chrome_devtools_mcp.embedded import backend_registry, desktop_launch
    from stealth_chrome_devtools_mcp.embedded.display_context import HEADLESS

    lines: list[str] = []
    serviceable = False
    surveyed = _survey_records()
    for item in surveyed:
        entry = item.entry
        context = str(entry.get("display_context"))
        port = backend_registry.recorded_int(entry, "port")
        pid = backend_registry.recorded_int(entry, "pid")
        version = entry.get("version")
        status = item.verdict
        serviceable = serviceable or (context != HEADLESS and status == "responsive")
        lines.append(
            f"backend  {context}  port {port if port is not None else '-'}  "
            f"pid {pid if pid is not None else '-'}  "
            f"version {version if isinstance(version, str) else '-'}  "
            f"{status}  "
            f"({'headless only' if context == HEADLESS else 'can show windows'})"
            f"{'  (dead record)' if item.dead else ''}"
        )
    if not lines:
        # Not the headless-only diagnosis: with nothing recorded the next
        # session cold-starts a backend in whatever context it runs in, so
        # there is no remedy to give yet.
        return ["backend  (none recorded)"]
    dead_line = _dead_record_line(surveyed)
    if dead_line:
        lines.append(dead_line)
    if not serviceable:
        # "no LIVE backend": the lines above may well show a capable one that
        # is down, and a remedy contradicting the list it follows is worse than
        # no remedy. The instruction deliberately echoes the spawn refusal's
        # advice (embedded/server.py's headed-visibility guard) so an operator
        # who arrives here from that error recognises it — a close paraphrase,
        # not a shared constant; keep the two saying the same thing. Both forms
        # keep the leading phrase verbatim: it is the greppable diagnosis, and
        # only the CONSEQUENCE differs between them.
        if desktop_launch.available():
            lines.append(
                "no live backend can display a window, but a user is logged on "
                "at the desktop: headed spawns are launched there automatically "
                "(F-810) — start a backend from a desktop session to skip that hop"
            )
        else:
            lines.append(
                "no live backend can display a window and nobody is logged on at "
                "the desktop: headed spawns will fail — start one from a desktop "
                "session and any session will use it automatically"
            )
    return lines


def _doctor_port_occupant_line(port: int | None) -> str:
    """F-509 visibility: is the target port free, ours, or a NON-stealth
    process squatting it (which would otherwise silently block a backend
    from binding)? Uses only existing helpers — no new port logic.

    ``port`` is the one the status block is already about (F-868), so this line
    cannot describe a different backend's port than the two lines above it; with
    nothing reported it falls back to the default the next spawn would prefer."""
    from stealth_chrome_devtools_mcp.embedded import singleton

    port = port if port is not None else singleton.DEFAULT_PORT
    our_pid = singleton._backend_pid_on_port(port)
    if our_pid is not None:
        return f"port {port} held by our backend (pid {our_pid})"
    if singleton._port_is_foreign_held(port):
        return f"port {port} held by a NON-stealth process — a backend cannot bind here"
    return f"port {port} free"


def _cmd_status(_args) -> int:
    cs = _clone_storage()
    from stealth_chrome_devtools_mcp.embedded import singleton

    root = cs.default_session_root()
    # ONE selection for the whole block (F-868): probe first, then report that
    # backend's pid, its log and the records this line is not about.
    status, port = singleton._probe_backend_status()
    pid = _recorded_backend_pid(port)
    others = _other_records_note(port)
    print(f"backend     : {_format_backend_status(status, port)}")
    print(f"pid         : {pid if pid is not None else '-'}")
    print(f"log         : {_backend_log_location(pid)}")
    if others:
        print(f"others      : {others}")
    print(f"version     : {singleton._server_version()}")
    print(f"browser-session root: {root}  (exists: {root.exists()})")
    print(
        f"clone cap   : {_human(cs.clone_storage_cap_bytes())}  "
        f"[STEALTH_MCP_CLONE_STORAGE_CAP_GB]"
    )
    print(
        f"browser-session cap : {_human(cs.browser_session_storage_cap_bytes())}  "
        f"[STEALTH_MCP_BROWSER_SESSION_STORAGE_CAP_GB]"
    )
    return 0


def _cmd_profiles(_args) -> int:
    rows = _collect_profiles(_clone_storage())
    if not rows:
        print("no profiles found.")
        return 0
    for row in sorted(rows, key=lambda r: r["size"], reverse=True):
        print(
            f"  {row['name'][:44]:44s} {row['role']:11s} "
            f"{_human(row['size']):>10s}  in_use={row['in_use']}"
        )
        seed = _seed_line(row)
        if seed:
            print(f"  {'':44s} {seed}")
    print(f"  {'total':44s} {'':11s} {_human(sum(r['size'] for r in rows)):>10s}")
    return 0


def _cleanup_backend_records(apply: bool) -> None:
    """Report — and with ``--apply`` reclaim — dead entries in `server.json`
    (F-880). A record naming a backend that does not exist is residue on disk,
    and reclaiming residue is exactly this verb's job.

    Both halves go through `backend_liveness`, the ONE home for the deadness
    rule: the dry run surveys, `--apply` calls `forget_dead`, which surveys
    again under the read-merge-write protocol rather than trusting a list the
    operator has had time to invalidate. No second sweep, and the dry run
    cannot disagree with what `--apply` then does — the same property the
    profile selectors above already have.
    """
    from stealth_chrome_devtools_mcp.embedded import backend_liveness, singleton

    surveyed = _survey_records()
    if not apply:
        dead = backend_liveness.dead_entries(surveyed)
        named = (
            f" ({', '.join(str(e.get('display_context')) for e in dead)})"
            if dead
            else ""
        )
        print(
            f"backend records: {len(surveyed)} recorded, {len(dead)} dead{named}"
            + (" — re-run with --apply to forget" if dead else "")
        )
        return
    forgotten = backend_liveness.forget_dead(
        singleton.SERVER_STATE_FILE,
        probe=singleton._probe_port,
        pid_is_ours=singleton._is_our_backend,
    )
    if forgotten:
        print(f"backend records: forgot {len(forgotten)} dead ({', '.join(forgotten)})")
    else:
        print(f"backend records: {len(surveyed)} recorded, 0 dead")


def _cmd_cleanup(args) -> int:
    from stealth_chrome_devtools_mcp.embedded import profile_copy

    cs = _clone_storage()
    clone_root = cs.clone_root_dir()
    clone_cap = _gb_to_bytes(args.clone_cap_gb, cs.clone_storage_cap_bytes())
    session_cap = _gb_to_bytes(
        args.browser_session_cap_gb, cs.browser_session_storage_cap_bytes()
    )

    # Same selectors the live sweep uses — dry-run and apply can't disagree.
    to_delete = cs._idle_autoclones_over_cap(clone_root, clone_cap)
    to_trim = cs._named_profiles_over_session_cap(clone_root, session_cap)
    delete_bytes = sum(profile_copy.dir_size_bytes(p) for p in to_delete)
    trim_bytes = sum(profile_copy.regenerable_size(p) for p in to_trim)

    print(f"clone root  : {clone_root}")
    print(
        f"caps        : clone {_human(clone_cap)} | "
        f"browser-session {_human(session_cap)}"
    )
    # Before the disk section's early return: a dead backend record is residue
    # whether or not any profile is over cap (F-880).
    _cleanup_backend_records(args.apply)
    if not to_delete and not to_trim:
        print("nothing to reclaim - storage is within caps.")
        return 0

    if to_delete:
        print(
            f"\ndelete {len(to_delete)} idle auto-clone(s) - frees "
            f"{_human(delete_bytes)}:"
        )
        for path in to_delete:
            size = _human(profile_copy.dir_size_bytes(path))
            print(f"   - {path.name[:50]:50s} {size:>10s}")
    if to_trim:
        print(
            f"\ntrim {len(to_trim)} idle named profile(s) - frees "
            f"~{_human(trim_bytes)} (logins kept):"
        )
        for path in to_trim:
            size = _human(profile_copy.regenerable_size(path))
            print(f"   - {path.name[:50]:50s} ~{size:>10s}")
    print(f"\ntotal reclaimable: ~{_human(delete_bytes + trim_bytes)}")

    if not args.apply:
        print("\n(dry run - nothing deleted. Re-run with --apply to reclaim.)")
        return 0

    removed = cs._enforce_clone_storage_cap_in(clone_root, clone_cap, "cli")
    freed = cs._enforce_named_profile_trim_in(clone_root, session_cap, "cli")
    print(
        f"\napplied: deleted {removed} auto-clone(s); trimmed "
        f"{_human(freed)} from named profiles."
    )
    return 0


def _cmd_doctor(_args) -> int:
    import platform

    from stealth_chrome_devtools_mcp.embedded import singleton

    cs = _clone_storage()

    ok = True
    print(f"python      : {platform.python_version()}")
    print(f"platform    : {platform.platform()}")
    root = cs.default_session_root()
    print(f"browser-session root: {root}  (exists: {root.exists()})")
    status, port = singleton._probe_backend_status()
    pid = _recorded_backend_pid(port)
    print(f"backend     : {_format_backend_status(status, port)}")
    print(f"pid         : {pid if pid is not None else '-'}")
    print(f"log         : {_backend_log_location(pid)}")
    print(f"port        : {_doctor_port_occupant_line(port)}")
    print("contexts    :")
    for line in _doctor_backend_lines():
        print(f"  {line}")

    chrome = _find_chrome()
    print(f"chrome      : {chrome or 'NOT FOUND — install Google Chrome'}")
    if not chrome:
        ok = False
    return 0 if ok else 1


def _find_chrome() -> str | None:
    import shutil

    for name in (
        "google-chrome",
        "google-chrome-stable",
        "chrome",
        "chromium",
        "chromium-browser",
    ):
        found = shutil.which(name)
        if found:
            return found
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    return None


def _cmd_stop(_args) -> int:
    """Thin front-end over `singleton.stop_backend()` — no matching/kill
    logic of its own (that lives in singleton.py, reused from eviction)."""
    _server()
    from stealth_chrome_devtools_mcp.embedded import singleton

    result, pid = singleton.stop_backend()
    if result == "stopped":
        print(f"stopped backend (pid {pid}).")
        return 0
    if result == "already stopped":
        print("backend already stopped (stale state cleared).")
        return 0
    if result == "not running":
        print("backend not running.")
        return 0
    print("busy: another session is starting/stopping the backend right now — retry.")
    return 1


def _cmd_restart(_args) -> int:
    """Thin front-end over `singleton.restart_backend()` — terminate then a
    fresh cold-start spawn under the same lock cold start uses; no lifecycle
    logic of its own (that lives in singleton.py)."""
    _server()
    from stealth_chrome_devtools_mcp.embedded import singleton

    status, pid = singleton.restart_backend()
    if status == "busy":
        print(
            "busy: another session is starting/stopping the backend right now — retry."
        )
        return 1
    if status == "responsive":
        print(f"backend restarted (responsive) (pid {pid}).")
        return 0
    if status == "wedged":
        print(
            f"backend restarted but is UNRESPONSIVE (wedged) (pid {pid}) — "
            "it came up but is not answering; a new session will evict and "
            "respawn it, or try `restart` again."
        )
        return 1
    # "down": spawned, but the socket on the port we spawned on never came up -
    # the restart did not produce a running backend, so report honestly rather
    # than implying success. It used to read "or 'none' (no state at all
    # afterward)"; since F-868 restart reports `singleton._probe_port` for that
    # one port, whose vocabulary is down/wedged/responsive and has no "none" -
    # that word belonged to the record-wide walk, which restart no longer uses.
    # The branch stays as written: it is the "down" arm, and a status this
    # function does not recognise still has to land somewhere truthful.
    print(f"backend restart did not bring the backend up (state: {status}, pid {pid}).")
    return 1


def _persistent_profile_preflight(force: bool) -> list[str]:
    """The PHRASING of `persistent_profile_risk`'s answer, printed before
    anything dies (F-921) — a line and never a prompt: agents drive this CLI
    and a blocking `input()` on a non-tty hangs them, and `--force` is already
    the opt-in, so only the disclosure was missing. Three shapes on purpose:
    an empty record, and one whose profiles are all CLOSED, must not be told
    that logins are about to be lost — "in the record" is the reap's scope."""
    from stealth_chrome_devtools_mcp.embedded import (
        persistent_profile_risk,
        process_cleanup,
    )

    cs = _clone_storage()
    risk = persistent_profile_risk.assess(
        process_cleanup.process_cleanup._load_tracked_pids(),
        is_open=cs._profile_has_running_browser,
    )
    if not risk.tracked:
        return ["profiles    : none tracked on a persistent profile"]
    if not risk.open_names:
        return [
            f"profiles    : {risk.tracked} persistent profile(s) in the record, "
            "none open — this reap ends no logged-in browser"
        ]
    ends = "--force ends EVERY tracked browser" if force else "only --force ends them"
    return [
        f"profiles    : {risk.tracked} persistent profile(s) in the record, "
        f"{len(risk.open_names)} open now ({', '.join(risk.open_names)})",
        f"              {ends}; these keep logins that must be re-entered BY HAND",
    ]


def _cmd_kill_orphans(args) -> int:
    """Thin, gated trigger of the existing orphan reaper — a direct call on
    the already-constructed `process_cleanup` module singleton (import-time
    recovery was skipped because `_server()` sets
    `STEALTH_MCP_NO_AUTO_RECOVERY=1` before the import). No new matching
    logic; the canonical create_time+user-data-dir matcher stays in
    process_cleanup.py (plan_M8 SS2.1-C; M11a adds a public seam later, per
    state.json's recorded decision).

    Guarded off a LIVE backend: reaping would kill that backend's own browsers.
    `restart` is the verb for "backend alive but bad"; this verb is for
    "backend gone, browsers orphaned" — a clean behavioral partition.

    `--force` overrides the guard, and is passed THROUGH to the reaper rather
    than merely getting past the gate. The reaper has THREE spares now and
    `--force` skips all of them: the live-backend refusal above, the per-entry
    ownership check (plan_F808 Task 10 — without the pass-through, `--force`
    would be a no-op against exactly the wedged backend it exists for), and
    F-888's persistent-profile spare. That last one is why `--force` is the only
    verb left that can still end a human's logged-in browser: every other path
    now re-attaches to it instead. Since F-921 it is PRINTED too, with a count,
    before anything dies; `--dry-run` prints that pre-flight and reaps nothing.
    """
    _server()
    from stealth_chrome_devtools_mcp.embedded import process_cleanup, singleton

    status, port = singleton._probe_backend_status()
    if status in ("responsive", "wedged") and not args.force:
        pid = _recorded_backend_pid(port)
        print(
            f"a backend is running (pid {pid if pid is not None else '-'}); "
            "use restart to recover it, or pass --force."
        )
        return 1

    for line in _persistent_profile_preflight(args.force):
        print(line)
    if args.dry_run:
        print("(dry run - nothing was reaped. Drop --dry-run to act.)")
        return 0

    process_cleanup.process_cleanup.recover_orphans(force=args.force)
    print(
        "orphan recovery triggered: reaped any browsers left over from a dead backend."
    )
    return 0


def _cli_call():
    """The six tool-driving verbs (F-891) — parsers, bodies and dispatch —
    imported lazily.

    They are the only verbs that speak MCP, so the ops verbs must not pay for
    the client library: ``profiles`` and ``status`` reach a running backend
    through nothing heavier than a socket and a probe. Same reason every
    ``embedded`` import in this file is inside the function that needs it.

    Building the parser now imports that module, so the laziness is thinner
    than it was — but what it protects is unchanged and measured: ``cli_call``
    imports only stdlib (``argparse``, ``contextlib``, ``sys``, ``typing``) at
    module scope, and every reach for ``backend_client`` (and through it
    ``httpx`` and the ``mcp`` SDK) is still inside the function that needs it.
    """
    from stealth_chrome_devtools_mcp import cli_call

    return cli_call


def _cmd_serve(args) -> int:
    # Delegate to the same entrypoint as `stealth-chrome-devtools-mcp` so server
    # lifecycle (incl. orphan recovery) behaves exactly as normal.
    from stealth_chrome_devtools_mcp import server as shim

    if args.http:
        sys.argv = [
            "stealth-chrome-devtools-mcp",
            "--transport",
            "http",
            "--port",
            str(args.port),
            "--host",
            args.host,
        ]
    else:
        sys.argv = ["stealth-chrome-devtools-mcp", "--transport", "stdio"]
    shim.main()
    return 0


#: The OPS verbs. The six tool-driving verbs are `cli_call.DISPATCH`'s, beside
#: the bodies they name; :func:`main` consults it for anything not here, so one
#: table covers the lifecycle and the other covers the tool surface, each next
#: to what it dispatches to.
_DISPATCH = {
    "status": _cmd_status,
    "profiles": _cmd_profiles,
    "cleanup": _cmd_cleanup,
    "doctor": _cmd_doctor,
    "stop": _cmd_stop,
    "restart": _cmd_restart,
    "kill-orphans": _cmd_kill_orphans,
    "serve": _cmd_serve,
}

#: The console-script names this ONE ``main`` is installed under
#: (``pyproject.toml`` ``[project.scripts]``), canonical first. F-891 added
#: ``stealthy``; ``stealth-chrome-devtools`` stays because it is in every
#: operator's muscle memory and in this repo's own RUNBOOK. One CLI, two names —
#: never two CLIs (convention 4).
SCRIPT_NAMES = ("stealthy", "stealth-chrome-devtools")


def _prog_name() -> str:
    """The name this process was invoked as, for help text and usage lines.

    A CLOSED set, and that is the point: ``argparse``'s own default is
    ``basename(sys.argv[0])``, which under pytest prints ``pytest`` and under
    ``python -m`` prints ``__main__``, so help text would advertise a command
    that does not exist. Anything unrecognised falls back to the canonical name.
    """
    invoked = Path(sys.argv[0] or "").name.removesuffix(".exe")
    return invoked if invoked in SCRIPT_NAMES else SCRIPT_NAMES[0]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=_prog_name(),
        description="CLI for the stealth Chrome DevTools MCP server: inspect and "
        "operate the backend, and drive its tools from a shell.",
    )
    sub = parser.add_subparsers(dest="command")
    # ONE parser tree; the six tool verbs contribute their own surface, which
    # is why their flags and their bodies can no longer drift apart (F-891).
    _cli_call().add_parsers(sub)

    sub.add_parser(
        "status", help="show backend state, browser-session root, and storage caps"
    )
    sub.add_parser("profiles", help="list profiles with size, role, and in-use flag")

    clean = sub.add_parser(
        "cleanup",
        help="reclaim disk: delete idle auto-clones over the clone cap and trim "
        "idle named profiles over the browser-session cap (dry run unless --apply)",
    )
    clean.add_argument(
        "--apply", action="store_true", help="actually reclaim (default: dry run)"
    )
    clean.add_argument(
        "--browser-session-cap-gb",
        type=float,
        default=None,
        dest="browser_session_cap_gb",
        help="override the named-profile trim cap for this run (GB; 0 disables)",
    )
    clean.add_argument(
        "--clone-cap-gb",
        type=float,
        default=None,
        dest="clone_cap_gb",
        help="override the auto-clone delete cap for this run (GB; 0 disables)",
    )

    sub.add_parser(
        "doctor",
        help="check Python, platform, browser-session root, backend, and Chrome",
    )

    sub.add_parser(
        "stop", help="terminate the shared backend (kills all live browser sessions)"
    )

    sub.add_parser(
        "restart",
        help="restart the shared backend (kills all live browser sessions)",
    )

    kill_orphans = sub.add_parser(
        "kill-orphans",
        help="reap orphaned browser processes left behind by a dead backend "
        "(refuses against a live backend unless --force)",
    )
    kill_orphans.add_argument(
        "--force",
        action="store_true",
        help="override the live-backend guard AND F-888's persistent-profile "
        "spare: this can terminate a browser holding a logged-in profile, whose "
        "logins must then be re-entered by hand. Preview it with --force "
        "--dry-run; a plain --dry-run still refuses while a backend is live",
    )
    kill_orphans.add_argument(
        "--dry-run",
        action="store_true",
        help="print the persistent profiles at risk, then exit without reaping",
    )

    serve = sub.add_parser(
        "serve", help="start the MCP server (stdio by default, or --http)"
    )
    serve.add_argument(
        "--http", action="store_true", help="serve over HTTP instead of stdio"
    )
    from stealth_chrome_devtools_mcp.embedded.singleton import DEFAULT_PORT

    serve.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"HTTP port (default {DEFAULT_PORT})",
    )
    serve.add_argument(
        "--host", default="127.0.0.1", help="HTTP host (default 127.0.0.1)"
    )
    return parser


def main(argv=None) -> int:
    # Ship errors to Sentry (on by default; no-op under
    # STEALTH_MCP_NO_ERROR_REPORTING, and never raises).
    sentry_init()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        # USAGE, never 1 (F-891 review M1): naming no verb is argparse's own
        # kind of mistake, and exit 1 now means "the tool answered and said no".
        return _delivered(_cli_call().EXIT_USAGE)
    handler = _DISPATCH.get(args.command)
    if handler is None:
        handler = _cli_call().DISPATCH[args.command]
    try:
        return _delivered(handler(args))
    except OSError as exc:
        # The DISPATCH is guarded and not only the flush (round-5 M1): an ops
        # verb has no handler of its own, so a `print` that crosses the 8 KB
        # buffer raises mid-body — `stealthy profiles | head -1` with many
        # sessions — and left `main` as a traceback and exit 1, which the set
        # defines as "the tool said no". Only the reader-gone shape converts;
        # any other OSError is the verb's own failure and keeps propagating.
        calls = _cli_call()
        if not calls._reader_gone(exc):
            raise
        calls._abandon_stdout()
        return calls.EXIT_BROKEN_PIPE


def _delivered(code: int) -> int:
    """THE one flush, for every verb of BOTH dispatch tables (F-891 delta
    review M1 + round-4 S2).

    ``print`` leaves the tail of any output above the 8 KB ``TextIOWrapper``
    buffer unwritten, and it lands at interpreter finalisation — after
    ``head`` has gone, outside every handler — as ``Exception ignored on
    flushing sys.stdout`` and exit **120**, outside the advertised set. So
    ``stealthy call get_page_content | head -1`` and ``stealthy profiles |
    head -1`` both exited 120 while the docs said 141. Flushing HERE brings the
    failure into a handler where it becomes 141; putting the ONE flush in
    ``main`` rather than in ``cli_call._run`` reaches the eight ops verbs for
    the answer that FITS the buffer, and ``main``'s guard around the dispatch
    covers the one that crosses it — one home for "the reader went away".

    This is a second guarded site, deliberately NOT routed through
    ``cli_call._verdict``: it catches ``OSError`` and not ``BrokenPipeError``
    because the measured Windows finalisation error is ``EINVAL`` (errno 22),
    not a pipe error at all — and ``_verdict`` would route that shape to the
    transport row and answer 3, "could not reach the backend", about a round
    trip that succeeded. The cost is that any other ``OSError`` from this
    flush (a full disk, ENOSPC) also reads as 141 — finding §6.16. It is the
    INNER half of one region: ``main`` guards the dispatch around it, for the
    output that crosses the buffer before the verb returns.
    """
    try:
        sys.stdout.flush()
    except OSError:
        calls = _cli_call()
        calls._abandon_stdout()
        return calls.EXIT_BROKEN_PIPE
    return code


if __name__ == "__main__":
    sys.exit(main())
