# F-808 — headed browsing works wherever a desktop exists

> **For agentic workers:** REQUIRED SUB-SKILL: use `superpowers:subagent-driven-development`
> (recommended) or `superpowers:executing-plans` to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `spawn_browser(headless=False)` opens a browser the user can actually see —
from SSH, from the desktop, from anywhere — or fails loudly saying why not.

**Architecture:** Whether a window can be seen is a property of the process that
launches Chrome, not of the caller. Make that property explicit
(`display_context`), record it against each backend, and have discovery **prefer a
window-capable backend**. An SSH proxy then forwards headed work to the desktop
backend and the window opens on the real desktop. Where no such backend exists, a
headed spawn raises instead of returning a ghost.

**Tech stack:** Python 3.11+, ctypes (no new deps), pytest, existing
`tests/fakes.py` harness.

**Non-goals.** No cross-session process injection (`CreateProcessAsUser`,
`psexec -i`, scheduled-task trampolines): they need privileges this tool must not
require, and they break on the multi-desktop case measured below. No silent
headed→headless degradation — that is the same defect wearing a different hat.

---

## Background: why the obvious fixes are wrong

Measured on the reporting machine (Windows 11 Pro 26200, 2026-08-01):

```
active console session : 2
explorer.exe session   : 1     ← the user's actual desktop
backend session        : 0     ← isolated; never visible
```

So **`WTSGetActiveConsoleSessionId()` is not the answer** — the console session is
not where the desktop lives once RDP or fast-user-switching is involved. Any fix
that picks "the" interactive session is wrong on somebody's machine. We therefore
never *choose* a session; we only ever *observe* our own and prefer a backend that
reports a window-capable one.

Full evidence and the 1.0.0 regression analysis:
`finding_F808_headed_spawn_is_invisible_when_the_backend_was_cold_started_from_a_non_interactive_session.md`.

## Invariant change (deliberate, must land in the docs)

`CLAUDE.md`'s glossary defines **backend** as "the single shared detached process".
This plan changes it to **one backend per (source fingerprint, display context)** —
in practice one on a headless box, at most two on a desktop box that is also
SSH'd into. The old invariant bought "exactly one process" by letting whichever
client won the cold-start race silently decide whether headed browsing worked for
everyone else. That is not a property worth keeping.

## File structure

| File | Responsibility |
|---|---|
| `embedded/display_context.py` **(new)** | THE one home for "can a window launched by this process be seen, and on which desktop" |
| `embedded/backend_registry.py` **(new)** | THE one home for the `server.json` record: schema, read/write/clear, per-context lookup. Moved out of `singleton.py`, which is at **999/1000 LOC** and has no room |
| `embedded/singleton.py` | keeps lifecycle only (probe, lock, spawn, evict, proxy); delegates all state I/O to the registry |
| `embedded/server.py` | `spawn_browser` gains the headed-capability guard |
| `cli.py` | `doctor`/`status` report each backend's context |

`display_context.py` and `backend_registry.py` are leaf modules: neither imports
`server`, per the one-import convention.

---

### Task 1: `display_context.py`

**Files:**
- Create: `src/stealth_chrome_devtools_mcp/embedded/display_context.py`
- Test: `tests/test_display_context.py`

- [ ] **Step 1: Write the failing tests**

```python
"""tests/test_display_context.py"""
import sys
import pytest
from stealth_chrome_devtools_mcp.embedded import display_context as dc


def test_windows_session_zero_is_headless(monkeypatch):
    monkeypatch.setattr(dc.sys, "platform", "win32")
    monkeypatch.setattr(dc, "_windows_session_id", lambda: 0)
    assert dc.display_context() == dc.HEADLESS
    assert dc.can_show_windows() is False


def test_windows_interactive_session_is_named_by_id(monkeypatch):
    monkeypatch.setattr(dc.sys, "platform", "win32")
    monkeypatch.setattr(dc, "_windows_session_id", lambda: 2)
    assert dc.display_context() == "win-session-2"
    assert dc.can_show_windows() is True


def test_linux_prefers_wayland_then_x11_then_headless(monkeypatch):
    monkeypatch.setattr(dc.sys, "platform", "linux")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("DISPLAY", ":0")
    assert dc.display_context() == "wayland-wayland-0"
    monkeypatch.delenv("WAYLAND_DISPLAY")
    assert dc.display_context() == "x11-:0"
    monkeypatch.delenv("DISPLAY")
    assert dc.display_context() == dc.HEADLESS


def test_unknown_platform_never_claims_headless(monkeypatch):
    """Fail toward 'interactive' on platforms we cannot classify: a wrong
    'headless' would BLOCK headed browsing that works today, which is a worse
    regression than the ghost window we are fixing."""
    monkeypatch.setattr(dc.sys, "platform", "sunos5")
    assert dc.display_context() == "unverified"
    assert dc.can_show_windows() is True


def test_windows_probe_failure_is_unverified_not_headless(monkeypatch):
    monkeypatch.setattr(dc.sys, "platform", "win32")
    monkeypatch.setattr(dc, "_windows_session_id", lambda: None)
    assert dc.display_context() == "unverified"
```

- [ ] **Step 2: Run and watch it fail**

Run: `uv run python -m pytest tests/test_display_context.py -v`
Expected: FAIL — `ModuleNotFoundError: ... display_context`

- [ ] **Step 3: Implement**

```python
"""Where a window launched by THIS process can be seen.

THE one home for that question. Chrome inherits its parent's window station, so
whether a headed browser is visible is decided by the process that launches it —
never by the caller and never by the ``headless`` flag (F-808). ``singleton.py``
records this token per backend so discovery can prefer a window-capable one.

Deliberately observational: it reports OUR OWN context and never tries to pick or
enter someone else's session. On the machine F-808 was found on, the active
console session (2) was NOT the session holding the user's desktop (1), so any
"find the interactive session" heuristic is wrong somewhere.
"""

import os
import sys

# No desktop: a window launched here can never be displayed. PROVEN, not guessed.
HEADLESS = "headless"
# We could not classify this platform. Treated as window-capable on purpose —
# see test_unknown_platform_never_claims_headless.
UNVERIFIED = "unverified"


def _windows_session_id() -> int | None:
    """This process's Windows Terminal Services session id, or None if the
    probe fails. Session 0 is the isolated services session: since Vista its
    desktop is never composited onto a user's screen.
    """
    import ctypes

    try:
        session = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.ProcessIdToSessionId(
            ctypes.windll.kernel32.GetCurrentProcessId(), ctypes.byref(session)
        )
        return int(session.value) if ok else None
    except Exception:
        return None


def display_context() -> str:
    """An opaque token naming the desktop this process can put a window on.

    ``HEADLESS`` means proven-invisible; ``UNVERIFIED`` means unclassifiable and
    is treated as capable. Any other value identifies a specific desktop, so two
    backends on different desktops are distinguishable.
    """
    if sys.platform == "win32":
        session = _windows_session_id()
        if session is None:
            return UNVERIFIED
        return HEADLESS if session == 0 else f"win-session-{session}"
    if sys.platform.startswith("linux"):
        wayland = os.environ.get("WAYLAND_DISPLAY")
        if wayland:
            return f"wayland-{wayland}"
        x11 = os.environ.get("DISPLAY")
        return f"x11-{x11}" if x11 else HEADLESS
    if sys.platform == "darwin":
        return _macos_context()
    return UNVERIFIED


def _macos_context() -> str:
    """macOS GUI access. A process in an SSH session belongs to a Background
    launchd domain and cannot own a window; an "Aqua" manager means it can.
    Any probe failure is UNVERIFIED (capable), never HEADLESS: macOS headed
    browsing works today and must not regress on an unrecognized launchctl.
    """
    import subprocess

    try:
        out = subprocess.run(
            ["launchctl", "managername"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return UNVERIFIED
    name = (out.stdout or "").strip()
    if name == "Aqua":
        return f"aqua-{os.getuid()}"
    return HEADLESS if name else UNVERIFIED


def can_show_windows() -> bool:
    """True unless we PROVED no window launched here could be seen."""
    return display_context() != HEADLESS
```

- [ ] **Step 4: Verify green**

Run: `uv run python -m pytest tests/test_display_context.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add src/stealth_chrome_devtools_mcp/embedded/display_context.py tests/test_display_context.py
git commit -F - <<'EOF'
F-808 step 1: display_context, the one home for "can a window be seen here"

Chrome inherits its parent's window station, so visibility is decided by the
process that launches it. Observational only: reports our own context and never
picks a session — on the reporting machine the active console session (2) was
not the session holding the desktop (1), so any "find the interactive session"
heuristic is wrong somewhere.

Unclassifiable platforms report UNVERIFIED and are treated as capable: a wrong
"headless" would block headed browsing that works today, which is worse than the
ghost window being fixed.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
```

---

### Task 2: move the state record into `backend_registry.py` (pure refactor)

`singleton.py` is at **999 of its 1000 LOC budget**. Task 3 adds a schema; that
room has to come from somewhere, and padding the cap is forbidden. The record is
also a genuinely separate responsibility from lifecycle. This task moves code
**without changing behaviour** so the existing suite proves the move.

**Files:**
- Create: `src/stealth_chrome_devtools_mcp/embedded/backend_registry.py`
- Modify: `src/stealth_chrome_devtools_mcp/embedded/singleton.py:125-172` (delete
  `_read_server_state`, `_write_server_state`, `_clear_server_state`; import them)

- [ ] **Step 1: Create the module** by moving the three functions verbatim,
  plus `STATE_DIR`, `SERVER_STATE_FILE`, `PORT_FILE`, `_ensure_state_dir`.
  Keep every docstring. `singleton.py` re-exports the names it already exposes:

```python
from stealth_chrome_devtools_mcp.embedded.backend_registry import (
    PORT_FILE, SERVER_STATE_FILE, STATE_DIR,
    _clear_server_state, _read_server_state, _write_server_state,
)
```

Re-export rather than rewrite call sites: `settings.py` and the tests reference
`singleton.STATE_DIR` and monkeypatch `singleton._read_server_state` by string
target, and an import-convention sweep cannot see string literals
(`[[import-convention-sweeps-patch-strings]]`).

- [ ] **Step 2: Prove the move changed nothing**

Run: `uv run python -m pytest tests/test_singleton_version_aware.py tests/test_singleton_stop_restart.py tests/test_singleton_port_fallback.py tests/test_cli.py -v`
Expected: all pass, zero edits to those files. If any test needed editing, the
move was not behaviour-preserving — revert and redo.

- [ ] **Step 3: Confirm the budget headroom**

Run: `uv run python tools/check_file_budgets.py`
Expected: exit 0, and `singleton.py` now well under 1000.

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -F - <<'EOF'
F-808 step 2: move the backend record out of singleton.py

Pure refactor, no behaviour change: the server.json record (schema, read, write,
clear, state dir) becomes backend_registry.py, leaving singleton.py owning
lifecycle only. Names are re-exported because settings.py and several tests
target singleton.STATE_DIR / singleton._read_server_state as strings.

singleton.py was at 999/1000 LOC with a schema change due next; the room comes
from moving code to its proper home, never from padding the cap.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
```

---

### Task 3: registry schema v2 — many backends, keyed by context

**Files:**
- Modify: `src/stealth_chrome_devtools_mcp/embedded/backend_registry.py`
- Test: `tests/test_backend_registry.py` (new)

- [ ] **Step 1: Write the failing tests**

```python
"""tests/test_backend_registry.py"""
from stealth_chrome_devtools_mcp.embedded import backend_registry as reg


def _use_tmp(monkeypatch, tmp_path):
    monkeypatch.setattr(reg, "STATE_DIR", tmp_path)
    monkeypatch.setattr(reg, "SERVER_STATE_FILE", tmp_path / "server.json")


def test_v1_record_is_read_as_one_unverified_backend(monkeypatch, tmp_path):
    """A backend from <=2.0.3 wrote a flat record with no display_context. It
    must still be reusable, classified UNVERIFIED (capable) rather than dropped:
    dropping it would evict a healthy backend on upgrade."""
    _use_tmp(monkeypatch, tmp_path)
    (tmp_path / "server.json").write_text(
        '{"port": 19222, "version": "2.0.3", "pid": 42, "source_fingerprint": "abc"}'
    )
    entries = reg.read_backends()
    assert len(entries) == 1
    assert entries[0]["port"] == 19222
    assert entries[0]["display_context"] == "unverified"


def test_write_then_read_round_trips_by_context(monkeypatch, tmp_path):
    _use_tmp(monkeypatch, tmp_path)
    reg.record_backend(port=1, version="v", pid=10,
                       source_fingerprint="fp", display_context="headless")
    reg.record_backend(port=2, version="v", pid=11,
                       source_fingerprint="fp", display_context="win-session-1")
    got = {e["display_context"]: e["port"] for e in reg.read_backends()}
    assert got == {"headless": 1, "win-session-1": 2}


def test_recording_the_same_context_replaces_not_appends(monkeypatch, tmp_path):
    _use_tmp(monkeypatch, tmp_path)
    reg.record_backend(port=1, version="v", pid=10,
                       source_fingerprint="fp", display_context="headless")
    reg.record_backend(port=9, version="v", pid=99,
                       source_fingerprint="fp", display_context="headless")
    assert [e["port"] for e in reg.read_backends()] == [9]


def test_window_capable_first_orders_the_search(monkeypatch, tmp_path):
    """Discovery must prefer a backend that can show windows, so an SSH client
    converges on the desktop backend instead of starting a blind one."""
    _use_tmp(monkeypatch, tmp_path)
    reg.record_backend(port=1, version="v", pid=10,
                       source_fingerprint="fp", display_context="headless")
    reg.record_backend(port=2, version="v", pid=11,
                       source_fingerprint="fp", display_context="win-session-1")
    assert [e["port"] for e in reg.window_capable_first()] == [2, 1]


def test_forget_removes_only_the_named_context(monkeypatch, tmp_path):
    _use_tmp(monkeypatch, tmp_path)
    reg.record_backend(port=1, version="v", pid=10,
                       source_fingerprint="fp", display_context="headless")
    reg.record_backend(port=2, version="v", pid=11,
                       source_fingerprint="fp", display_context="win-session-1")
    reg.forget_backend("headless")
    assert [e["display_context"] for e in reg.read_backends()] == ["win-session-1"]
```

- [ ] **Step 2: Run and watch it fail**

Run: `uv run python -m pytest tests/test_backend_registry.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'read_backends'`

- [ ] **Step 3: Implement**

Add to `backend_registry.py`:

```python
SCHEMA_VERSION = 2


def read_backends() -> list[dict]:
    """Every recorded backend, newest schema or old.

    A v1 record (flat ``{port, version, pid, source_fingerprint}``, written by
    <= 2.0.3) is returned as a single entry with ``display_context`` UNVERIFIED
    — reusable, because dropping it would evict a healthy backend the moment a
    user upgrades.
    """
    from stealth_chrome_devtools_mcp.embedded.display_context import UNVERIFIED

    state = _read_server_state()
    if not state:
        return []
    if state.get("schema") == SCHEMA_VERSION:
        backends = state.get("backends")
        return list(backends.values()) if isinstance(backends, dict) else []
    if isinstance(state.get("port"), int):
        return [{**state, "display_context": state.get("display_context", UNVERIFIED)}]
    return []


def record_backend(
    *, port: int, version: str, pid: int,
    source_fingerprint: str, display_context: str,
) -> None:
    """Record one backend under its display context, replacing any previous
    entry for that same context (one backend per desktop, not a growing list).
    """
    entries = {e["display_context"]: e for e in read_backends()}
    entries[display_context] = {
        "port": port, "version": version, "pid": pid,
        "source_fingerprint": source_fingerprint,
        "display_context": display_context,
    }
    _write_state({"schema": SCHEMA_VERSION, "backends": entries})


def window_capable_first() -> list[dict]:
    """Recorded backends, window-capable ones first. THE search order: it is
    what lets an SSH client (headless context) adopt the desktop backend and so
    open a browser the user can actually see.
    """
    from stealth_chrome_devtools_mcp.embedded.display_context import HEADLESS

    return sorted(read_backends(), key=lambda e: e.get("display_context") == HEADLESS)


def forget_backend(display_context: str) -> None:
    """Drop one context's record, leaving the others intact."""
    entries = {e["display_context"]: e for e in read_backends()
               if e.get("display_context") != display_context}
    _write_state({"schema": SCHEMA_VERSION, "backends": entries})
```

`_write_state(obj)` is `_write_server_state`'s body with the dict passed in;
keep `_write_server_state(port, version, pid, source_fingerprint)` as a thin
wrapper that calls `record_backend` with `display_context()` so existing callers
keep working unchanged.

- [ ] **Step 4: Verify green**

Run: `uv run python -m pytest tests/test_backend_registry.py tests/test_singleton_version_aware.py -v`
Expected: all pass.

- [ ] **Step 5: Commit** (message: "F-808 step 3: server.json records one backend per display context, v1 records still readable")

---

### Task 4: discovery prefers a window-capable backend

**Files:**
- Modify: `src/stealth_chrome_devtools_mcp/embedded/singleton.py` —
  `_same_identity_backend_ready:200`, `_find_running_server:~578`,
  `_select_backend_port:661`
- Test: `tests/test_singleton_display_routing.py` (new)

The identity gate keeps its current meaning (version + fingerprint + live probe).
**Display context is NOT an equality test** — requiring a match would make an SSH
client refuse the desktop backend, which is the opposite of the goal. It is a
*preference*: try window-capable backends first, fall back to our own context,
cold-start last.

- [ ] **Step 1: Write the failing tests**

```python
"""tests/test_singleton_display_routing.py"""
from stealth_chrome_devtools_mcp.embedded import backend_registry as reg
from stealth_chrome_devtools_mcp.embedded import singleton


def test_headless_client_adopts_the_desktop_backend(monkeypatch, tmp_path):
    """The F-808 fix, stated as a test: a client that cannot show windows
    (SSH) must reuse the desktop backend so its headed spawns are visible."""
    monkeypatch.setattr(reg, "STATE_DIR", tmp_path)
    monkeypatch.setattr(reg, "SERVER_STATE_FILE", tmp_path / "server.json")
    reg.record_backend(port=1111, version="v", pid=1,
                       source_fingerprint="fp", display_context="headless")
    reg.record_backend(port=2222, version="v", pid=2,
                       source_fingerprint="fp", display_context="win-session-1")
    monkeypatch.setattr(singleton, "_server_version", lambda: "v")
    monkeypatch.setattr(singleton, "_source_fingerprint", lambda: "fp")
    monkeypatch.setattr(singleton, "_backend_http_ready", lambda port, **kw: True)
    monkeypatch.setattr(singleton, "_server_is_healthy", lambda port: True)
    assert singleton._find_running_server() == 2222


def test_falls_back_to_the_headless_backend_when_no_desktop_one_exists(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(reg, "STATE_DIR", tmp_path)
    monkeypatch.setattr(reg, "SERVER_STATE_FILE", tmp_path / "server.json")
    reg.record_backend(port=1111, version="v", pid=1,
                       source_fingerprint="fp", display_context="headless")
    monkeypatch.setattr(singleton, "_server_version", lambda: "v")
    monkeypatch.setattr(singleton, "_source_fingerprint", lambda: "fp")
    monkeypatch.setattr(singleton, "_backend_http_ready", lambda port, **kw: True)
    monkeypatch.setattr(singleton, "_server_is_healthy", lambda port: True)
    assert singleton._find_running_server() == 1111
```

- [ ] **Step 2: Run and watch both fail.**

Run: `uv run python -m pytest tests/test_singleton_display_routing.py -v`

- [ ] **Step 3: Implement**

Replace `_find_running_server` (`singleton.py:237-250`) with:

```python
def _find_running_server() -> int | None:
    """Return the port of a *reusable* backend, or None.

    The one reuse gate (`_clear_stale_backend`'s eviction and both cold-start
    callers all route through it): same version, same source fingerprint, and
    a live `initialize` answer — the full contract, with its history, lives in
    :func:`_same_identity_backend_ready`. Single-shot on purpose: this runs on
    every proxy start's hot path and must never sleep.

    Window-capable backends are tried FIRST (F-808). Display context is a
    PREFERENCE, never an equality test: a client that cannot show windows (an
    SSH session) must be able to adopt the desktop backend — that adoption is
    exactly what makes a headed spawn visible from SSH. Requiring a context
    match would instead give the SSH client its own blind backend, which is the
    bug this fixes.
    """
    for entry in backend_registry.window_capable_first():
        port = entry.get("port")
        if isinstance(port, int) and _same_identity_backend_ready(port, patience=0.0):
            return port
    return None
```

And `_select_backend_port` (`singleton.py:661`) prefers the port recorded for
**our own** context, so two contexts never fight over one port:

```python
    own = display_context.display_context()
    recorded = next(
        (e.get("port") for e in backend_registry.read_backends()
         if e.get("display_context") == own),
        None,
    )
    target = recorded if isinstance(recorded, int) else preferred
    return _free_port() if _port_is_foreign_held(target) else target
```

`_same_identity_backend_ready` currently re-reads the single record to compare
version and fingerprint (`singleton.py:218-224`); change it to look up the entry
**for the port it was handed** via `backend_registry.read_backends()`, leaving
its probe/patience logic byte-identical.

- [ ] **Step 4: Verify** the new file plus
  `tests/test_singleton_version_aware.py tests/test_singleton_port_fallback.py
  tests/test_singleton_cold_start_patience.py` — the F-807 patience behaviour must
  be untouched.

- [ ] **Step 5: Commit** ("F-808 step 4: discovery prefers a window-capable backend").

---

### Task 5: `spawn_browser` refuses to hand back an invisible browser

**Files:**
- Modify: `src/stealth_chrome_devtools_mcp/embedded/server.py:332` (`spawn_browser`)
- Test: `tests/test_spawn_headed_requires_display.py` (new)

- [ ] **Step 1: Write the failing test**

```python
"""tests/test_spawn_headed_requires_display.py"""
import pytest
from stealth_chrome_devtools_mcp.embedded import tool_errors
from fakes import call_tool, load_server_module


@pytest.mark.asyncio
async def test_headed_spawn_raises_when_no_window_can_be_shown(monkeypatch):
    server_mod = load_server_module()
    monkeypatch.setattr(
        server_mod.display_context, "can_show_windows", lambda: False
    )
    monkeypatch.setattr(
        server_mod.display_context, "display_context", lambda: "headless"
    )
    with pytest.raises(tool_errors.ToolError) as err:
        await call_tool(server_mod, "spawn_browser", headless=False)
    msg = str(err.value)
    assert "cannot display a window" in msg
    assert "headless=True" in msg          # the honest alternative
    assert "stealth-chrome-devtools doctor" in msg   # how to see the diagnosis


@pytest.mark.asyncio
async def test_headless_spawn_is_unaffected_without_a_display(monkeypatch):
    """CI and scripted use must keep working from a headless context."""
    server_mod = load_server_module()
    monkeypatch.setattr(
        server_mod.display_context, "can_show_windows", lambda: False
    )
    result = await call_tool(server_mod, "spawn_browser", headless=True)
    assert result["state"] == "ready"
```

- [ ] **Step 2: Run and watch it fail.**

- [ ] **Step 3: Implement** — first statement in `spawn_browser`'s body:

```python
    if not headless and not display_context.can_show_windows():
        raise ToolError(
            f"This backend runs in a context that cannot display a window "
            f"({display_context.display_context()}), so a headed browser would "
            f"launch invisibly (F-808). Start the backend from a desktop "
            f"session and this session will use it automatically, or pass "
            f"headless=True. Run `stealth-chrome-devtools doctor` to see which "
            f"contexts have a backend."
        )
```

Import at module top: `from stealth_chrome_devtools_mcp.embedded import display_context`.

Also fix the now-wrong `viewport_width` docstring at `server.py:352-356`: the clamp
is to the launching context's desktop, not "the desktop work area" (see the F-804
correction in Task 7).

- [ ] **Step 4: Verify** both tests, then the full unit lane:
  `uv run python -m pytest tests/ -x -q` — expect the lane count to rise, nothing to fall.

- [ ] **Step 5: Commit** ("F-808 step 5: a headed spawn with no displayable desktop raises instead of returning a ghost").

---

### Task 6: `doctor` shows the contexts

> *Corrected 2026-08-02, after the task landed (`172e014`, `60e48da`, `69e48ad`,
> `d84323e`).* This heading read "`doctor` **and** `status` show the contexts".
> Only `doctor` does, deliberately: `status`'s `backend :` line answers "is the
> backend up" and stays one value, while naming every recorded context belongs to
> `_doctor_backend_lines` (the decision is recorded at `cli.py:148-151`). The
> remedy string below is also a draft — the landed wording is "no **live** backend
> can display a window: headed spawns will fail — start one from a desktop session
> **and any session will use it automatically**", and the suppression rule is
> narrower than this draft: only a window-capable backend that is *responsive*
> silences it.

**Files:**
- Modify: `src/stealth_chrome_devtools_mcp/cli.py`
- Test: `tests/test_cli.py` (extend)

- [ ] **Step 1: Write the failing test** asserting `doctor` output contains a line
  per recorded backend of the form
  `backend  win-session-1  port 55296  responsive  (can show windows)` and
  `backend  headless  port 19222  responsive  (headless only)`, plus, when no
  window-capable backend exists, the explicit remedy line
  `no backend can display a window: headed spawns will fail — start one from a desktop session`.
- [ ] **Step 2: Run, watch it fail.**
- [ ] **Step 3: Implement**

```python
def _doctor_backend_lines() -> list[str]:
    """One line per recorded backend. Reuses `_probe_backend_status` per port —
    ONE liveness vocabulary, no second prober (plan_M8 SS2.1-B).
    """
    from stealth_chrome_devtools_mcp.embedded import backend_registry, singleton
    from stealth_chrome_devtools_mcp.embedded.display_context import HEADLESS

    lines, capable = [], False
    for entry in backend_registry.window_capable_first():
        ctx = entry.get("display_context", "unverified")
        status, _ = singleton._probe_backend_status_for(entry.get("port"))
        note = "headless only" if ctx == HEADLESS else "can show windows"
        capable = capable or ctx != HEADLESS
        lines.append(f"backend  {ctx}  port {entry.get('port')}  {status}  ({note})")
    if not lines:
        lines.append("backend  (none recorded)")
    elif not capable:
        lines.append(
            "no backend can display a window: headed spawns will fail — "
            "start one from a desktop session"
        )
    return lines
```

`_probe_backend_status_for(port)` is `_probe_backend_status()`'s body taking an
explicit port; keep `_probe_backend_status()` as a wrapper over the first
recorded backend so existing callers and `test_probe_backend_status.py` are
untouched.
- [ ] **Step 4: Verify** `uv run python -m pytest tests/test_cli.py tests/test_cli_status_wedged.py -v`.
- [ ] **Step 5: Commit** ("F-808 step 6: doctor reports a backend per display context").

---

### Task 7: docs, glossary, and the F-804 correction

**Files:**
- Modify: `CLAUDE.md` (glossary "backend"; navigation map gains the two new modules)
- Modify: `DESIGN.md` (new section: display context and why identity is a preference, not an equality test)
- Modify: `audit/stage2/finding_F804_headed_window_size_clamped_and_misreported.md`
- Modify: `audit/stage2/finding_F808_...md` (status → FIXED, naming the commits)
- Modify: `CHANGELOG.md` under 2.0.4
- Modify: `README.md` — one short "headed browsing over SSH" note

- [ ] **Step 1:** `CLAUDE.md` glossary: **backend** becomes "the shared detached
  `--transport http` process; **one per (source fingerprint, display context)** —
  at most one per desktop, plus one headless".
- [ ] **Step 2:** F-804: keep the finding and its remedy; replace the "clamped to
  the desktop work area" mechanism with a pointer to F-808 (the clamp was
  Session 0's default desktop, which is why an RTX 3080 machine reported ~1024x768).
- [ ] **Step 3:** Regenerate the contract: `PYTHONUTF8=1 uv run python tools/gen_release_contract.py --write`.
- [ ] **Step 4:** Verify `uv run python -m pytest tests/test_doc_claims.py tests/test_doc_examples.py -v`
  and `PYTHONUTF8=1 uv run python tools/gen_release_contract.py --check` (exit 0).
- [ ] **Step 5: Commit** ("F-808 step 7: docs, glossary invariant change, and the F-804 mechanism correction").

---

### Task 8: prove it against real Chrome, then gate

**Files:**
- Test: `tests/test_browser_integration.py` (extend)

- [ ] **Step 1:** Add an integration node asserting that on a window-capable
  context a headed spawn yields a Chrome process with a **non-zero main window
  handle** — the assertion whose absence let F-808 ship. Windows-only via the
  existing platform markers; `[[ci-headless-lanes-lack-a-window-manager]]` means
  the Linux cell must not claim this.
- [ ] **Step 2:** Run the integration lane at file level (never a narrow `-k`,
  which loses Chrome warmup): `uv run python -m pytest tests/test_browser_integration.py -v`.
- [ ] **Step 3:** Full unit lane + budgets + ruff:
  `uv run python -m pytest tests/ -q && uv run python tools/check_file_budgets.py && uv run ruff check .`
- [ ] **Step 4:** Open the PR; the release gate must be green **on the tag run**
  (`Publish to PyPI`), not merely on `CI` — see
  `[[release-2-0-0-resume-point]]` for how those two runs get confused.
- [ ] **Step 5:** Manual confirmation on the reporting machine: with a backend
  cold-started from the desktop session, an SSH-driven `spawn_browser(headless=False)`
  must put a **visible window on the physical desktop**. This is the acceptance
  test the user reported against; nothing else closes F-808.

---

## Risks

**A second backend doubles Chrome's memory ceiling on a mixed box.** Accepted: the
alternative is invisible browsing. `clone_storage` quotas are per-profile and
unaffected.

**Task 4 touches the F-807 patience gate**, the most race-prone code in the
repo. Task 4's step 4 re-runs `test_singleton_cold_start_patience.py` for exactly
this reason; treat any change in that file's behaviour as a stop-and-rethink, not
a golden to update.

**The v1→v2 record migration runs on every user's first 2.0.4 start.** Task 3's
first test pins that a 2.0.3 record stays reusable, because getting this wrong
evicts a healthy backend — and eviction during a live session is exactly the
class of failure that made this release cycle painful.
