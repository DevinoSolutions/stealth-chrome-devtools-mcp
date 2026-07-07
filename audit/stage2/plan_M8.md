# Stage 2 Plan — M8 Recovery CLI

- **Pinned SHA:** `2267b83d3efda03f93936db2c34ded33aaa0d701`
- **Branch (audit):** `fix/singleton-version-aware-backend` · **Fix branch (Stage 3):** `audit/fixes-2026-07-02`
- **Date:** 2026-07-02
- **Batch:** {M8 recovery CLI}
- **Base tree:** **post-`plan_M3` + post-`plan_M1`** (M3 = {observability spine, M10a} and M1 = {app-level liveness} are APPROVED and execute FIRST/SECOND; M8 is THIRD, serial. This plan is written against the tree those two leave behind — see §1.3 for every anchor they shift and the exact symbols M8 consumes from them.)
- **Findings closed:** **F-302** (High), **F-305** (Medium) — plus the log-path half of **F-503**. **F-509** (Medium) — **partially IN** (detection/reporting) / **partially OUT** (auto-port-fallback), justified §8. **F-607** (Medium) and **F-608** (Medium) — **ruled OUT** with justification, §8.
- **Status:** **APPROVED** (human, 2026-07-02) with one human-ordered re-scope: **F-509 auto-port-fallback is IN** (the human overturned §8's OUT ruling) — being added as **Amendment A1** by the planner; A1 is DRAFT until cross-reviewed and confirmed. Decisions: kill-orphans uses the **direct** `_recover_orphaned_processes()` call (M11a adds the public seam later); PR shape per §6 (single PR, checkpoint commits).
- **Context (do not re-derive):** LOCAL single-user tool, **0 users, breaking changes are FREE.** Priorities strict: (1) maintainability (2) operability (3) performance. `uv run` is BROKEN here — always `.venv\Scripts\python.exe` directly.

All line anchors below were **re-opened and confirmed at the pinned SHA** while writing this plan (§1.1). Because M8 runs **serially after M3 and M1 merge**, the executable tree is post-M3+M1; §1.3 records exactly which anchors those plans move so Stage 3 re-anchors by **symbol**, not by the raw line numbers here.

---

## The one fact that shapes this plan

The recovery machinery **already exists and is well-built** — the audit's "What's good, preserve" list names `process_cleanup`'s orphan-Chrome matching and singleton's version-aware eviction explicitly. What is missing is not machinery but a **trigger surface**: `_clear_stale_backend` (the identity-verified backend terminator) is only ever called mid-cold-start; `_recover_orphaned_processes` (the create_time+user-data-dir orphan reaper) only runs inside `ProcessCleanup.__init__`. The ops CLI exposes five verbs and **none** of them can reach either one (F-302). So M8 **adds no kill/respawn/matching logic** — like M1, it exposes and re-triggers the machine that is already there. Every new verb is a thin front-end over an existing, tested singleton/`process_cleanup` primitive; the only genuinely new singleton code is a **refactor-extraction** (`_terminate_backend`) that lets `stop` reuse the exact terminate body eviction already uses, minus eviction's "skip if reusable" guard.

The second shaping fact: **M1 already delivered the diagnosis vocabulary.** `_probe_backend_status()` returns `responsive | wedged | down | none`. M8 does **not** add a health check (binding cross-review ruling (a)); every verb *consumes* that reporter — `wedged` is the state that makes `restart` the escape hatch, `responsive` is the state that makes `kill-orphans` refuse.

---

## 1. Scope

### 1.1 Confirmed code anchors at HEAD (re-opened while writing this plan)

**`src/stealth_chrome_devtools_mcp/cli.py` — the ops CLI (primary edit target):**

| Anchor @ pinned SHA | What it is | M8 action |
|---|---|---|
| `_cmd_status:95-106` | prints `backend : running on port … / not running`, version, session root, caps. **No pid, no log path.** (F-305) | **EXTEND** (add pid + log-dir/file lines) — on top of M1's rewrite to responsive/wedged/down |
| `_cmd_doctor:162-180` | prints python/platform/session-root/backend/chrome; same port-only backend line (F-305) | **EXTEND** (pid + log dir + F-509 foreign-port detection) — on top of M1's rewrite |
| `_find_chrome:183-198` | chrome discovery helper | untouched |
| `_cmd_serve:201-216` | delegates to `shim.main()` | untouched |
| `_DISPATCH:219-225` | `{status, profiles, cleanup, doctor, serve}` — **no stop/restart/kill-orphans** (F-302) | **ADD** three entries |
| `build_parser:228-260` | subparser registration | **ADD** three subparsers |
| `_server:29-39` | sets `STEALTH_MCP_NO_AUTO_RECOVERY=1` (via `os.environ.setdefault`, `:35`) + `_ensure_embedded_on_path()` + `import server` | **REUSE as-is** (all new verbs bootstrap through it — one convention) |

**`src/stealth_chrome_devtools_mcp/embedded/singleton.py` — the lifecycle machinery to reuse:**

| Anchor @ pinned SHA | What it is | M8 action |
|---|---|---|
| `STATE_DIR:34`, `SERVER_STATE_FILE:40` (`server.json` = `{port, version, pid}`), `DEFAULT_PORT = 19222:41` | state dir + backend-identity record + the fixed port | reuse; **note:** the brief's "`BACKEND_PORT`" is `DEFAULT_PORT` — there is **no** `BACKEND_PORT` symbol (grep-confirmed) |
| `_read_server_state:92-103` | returns `{port, version, pid}` or None | reuse (source of pid for F-305, and of the port for `stop`) |
| `_is_our_backend:139-154` | pid is ours iff cmdline has `stealth_chrome_devtools_mcp` **and** `--transport` | reuse (the identity check that makes kill safe) |
| `_backend_pid_on_port:157-177` | our backend's pid by LISTEN on a port; **foreign holder ⇒ None** | reuse (stop's primary pid source; doctor's F-509 detector) |
| `_clear_stale_backend:180-217` | **guard `:188`** `if _find_running_server()==port: return`; pid resolve `:191-198`; **terminate→wait(5)→kill `:200-208`**; **port-release wait `:210-215`** | **REFACTOR:** extract `:191-215` into `_terminate_backend(port)`; `_clear_stale_backend` becomes guard + call. Canonical backend-terminate lives here. |
| `_start_server_process:218-245` | builds cmd `:219-226`; `kwargs` DEVNULL `:228-232`; detach flags `:234-239`; `Popen` `:241`; writes `server.json` `:245`. **Inherits parent env (no `env=`).** | **EDIT:** scrub `STEALTH_MCP_NO_AUTO_RECOVERY` from the child env so a spawned backend always reaps (see §2.1-D). *M3 also edits `:228-232` (boot-log redirect) — same `kwargs` dict; re-anchor by symbol.* |
| `_wait_for_server:248-256` | post-spawn socket bind-wait | reuse (restart's readiness wait) |
| `_exclusive_lock:54-80` | **non-blocking** file lock (`LK_NBLCK`/`LOCK_NB`), `yields got: bool` | reuse (stop/restart hold it, exactly like cold-start does) |
| `_start_backend_holding_lock:272-294` | the cold-start template: `with _exclusive_lock() as got` → `_clear_stale_backend` → `_start_server_process` → `_wait_for_server` | **template only** (restart mirrors this discipline; no edit) |
| `ensure_server_running:297-313` | non-blocking spawn entry | reuse pattern; no edit |

**M1-provided symbols M8 CONSUMES (present in base tree, absent at HEAD — grep-confirmed):**
- `singleton._probe_backend_status() -> tuple[str, int | None]` — `("none"|"down"|"wedged"|"responsive", port)`. **The one liveness source for every M8 verb** (binding ruling (a): do NOT re-add a health check).
- `singleton._backend_http_ready(port, *, timeout=LIVENESS_PROBE_TIMEOUT) -> bool` — single-shot MCP `initialize`→200 probe. Available if `restart` wants a real-readiness confirm; but `restart` reports via `_probe_backend_status` (one vocabulary), so it needs `_backend_http_ready` only transitively.

**M3-provided symbols M8 CONSUMES (present in base tree, absent at HEAD — grep-confirmed):**
- `logging_setup.resolve_log_dir() -> Path` — `STEALTH_MCP_LOG_DIR` else `~/.stealth-mcp/logs`. **The canonical log-dir resolver** — status/doctor call it (do not recompute the path).
- Per-pid log filenames `backend-<pid>.log`, `proxy-<pid>.log`, and `backend-boot.log` (M3 §2.1-A/C). status names the exact `backend-<pid>.log` from the recorded pid.

**`src/stealth_chrome_devtools_mcp/embedded/process_cleanup.py` — the orphan reaper to thin-trigger (PRESERVE, ideally no edit):**

| Anchor @ pinned SHA | What it is | M8 action |
|---|---|---|
| `ProcessCleanup.__init__:31-55` | sets `_init_time:47`; **honors `STEALTH_MCP_NO_AUTO_RECOVERY` `:52-53`** (early-return ⇒ no handlers, no recovery); else `_setup_cleanup_handlers()` + `_recover_orphaned_processes()` `:54-55` | reuse (the guard is why `kill-orphans` can trigger recovery *deliberately* on the import-created singleton) |
| `_recover_orphaned_processes:599-629` | reads `_load_tracked_pids`; `_kill_processes_for_metadata(recovery=True)` per entry (the **create_time-checked** branch); `_clear_pid_file:628`; `_sweep_orphaned_temp_profiles:629` | **thin-trigger target** for `kill-orphans` (called on `process_cleanup.process_cleanup`, the module singleton `:1023`) — **no edit** |
| `_kill_processes_for_metadata:348-422` | `recovery=True` branch `:371-410` verifies `create_time` vs `_init_time` + stored `create_time`; `recovery=False` fallback `:411-422` trusts `fallback_pid` unchecked (**F-608**) | **not touched** — `kill-orphans` uses only the `recovery=True` branch (§8 F-608 ruling) |
| `_file_lock:216-225` (**F-607**), `process_cleanup = ProcessCleanup():1023` (module singleton) | unlocked pid-file yield; import-time instantiation | **not touched** (§8 F-607 ruling; M11a owns this file) |

### 1.2 Files to be touched

**Modified source (2):**
- `src/stealth_chrome_devtools_mcp/cli.py` — three new verbs (`_cmd_stop`, `_cmd_restart`, `_cmd_kill_orphans`) + dispatch/parser entries; extend `_cmd_status`/`_cmd_doctor` (pid + log path + doctor's F-509 detection).
- `src/stealth_chrome_devtools_mcp/embedded/singleton.py` — extract `_terminate_backend(port)`; add `stop_backend()` + `restart_backend()` orchestrations; env-scrub in `_start_server_process`.

**New + modified tests (see §5):** `tests/test_cli.py` (extend), `tests/test_singleton_stop_restart.py` (new).

**No `process_cleanup.py` edit** (thin-trigger only — avoids overlap with M11a, which owns that file). **No `server.py` edit.**

### 1.3 Anchors that plan_M3 / plan_M1 SHIFT (re-anchor by symbol in Stage 3)

- **M3 shifts `singleton.py` below `:229`** by ~+8…+14 lines (boot-log redirect, cold-start logs). `_start_server_process` (M8 edits its `kwargs`) and everything below it move down. **M3 edits the same `kwargs` dict** at `:228-232` that M8 adds the env-scrub to — coordinate: M3's version has `stdout/stderr = <boot-log handle>`; M8 adds `env=<scrubbed>` to that dict.
- **M1 rewrites `cli.py` `_cmd_status`/`_cmd_doctor`** to call `_probe_backend_status()` and print `responsive/wedged/down`. **M8's status/doctor edits stack on M1's version** — M8 *adds* pid + log lines and the doctor F-509 line; it does not re-touch the state-vocabulary M1 installed.
- **M1 adds `_probe_backend_status`/`_backend_http_ready` + `LIVENESS_PROBE_TIMEOUT`** to `singleton.py` (near `_backend_http_url:259`/the watchdog) and leaves `_clear_stale_backend:180-217` **unshifted** (it sits above M3's first edit at `:229`). M8's `_terminate_backend` extraction is therefore on stable line numbers; the new `stop_backend`/`restart_backend` functions are additive (place near `ensure_server_running`).
- **Stage-3 rule:** locate the terminate body by the substring `proc.terminate()` **inside** `_clear_stale_backend`; locate the spawn env by `subprocess.Popen(cmd, **kwargs)` inside `_start_server_process`; locate the reporter by `def _probe_backend_status`. Do not trust raw line numbers after M3/M1 merge.

### 1.4 Explicit out-of-scope (stated so Stage 3 does not creep)

- **M2** (reuse-key / delete `hot_reload`). `restart` is M2's **manual escape hatch**, usable today; it is **not** M2's fix. M8 does not touch the version reuse key (`_find_running_server:132`) or `_server_version:263`.
- **M1** internals — the probe, the watchdog, `_await_backend_http`. M8 **consumes** `_probe_backend_status` as-is; it re-adds no liveness check (binding ruling (a)).
- **M4** — zero `server.py` edits.
- **New backend features/endpoints, supervisor daemons, auto-restart policies.** M1's watchdog already does auto-recovery; M8 is the **manual** surface only.
- **F-509 auto-port-fallback** — ruled OUT (detection stays in); §8.
- **F-607 / F-608** — process_cleanup-internal, not reached by any M8 verb; §8.
- No drive-by refactors; a mid-implementation discovery becomes a **new finding** (schema now carries `modularity|duplication|clarity`), not scope.

---

## 2. Approach + rejected alternatives

### 2.1 Chosen design

**A. `stop` — reuse the eviction terminator, minus eviction's reuse-guard.**
`_clear_stale_backend(port)` is *almost* `stop`, but its first line — `if _find_running_server() == port: return` — is exactly wrong for `stop`: it would make `stop` a no-op against a **healthy** same-version backend (the common case). The terminate body below the guard (`:191-215`: resolve our pid by port then by recorded pid, `terminate()`→`wait(5)`→`kill()`, then wait for port release) is *exactly* right. So **extract `:191-215` into `_terminate_backend(port) -> bool`** (returns whether a backend of ours was terminated). `_clear_stale_backend` becomes:
```python
def _clear_stale_backend(port: int) -> None:
    if _find_running_server() == port:
        return  # a reusable same-version backend is already there
    _terminate_backend(port)
```
`stop_backend()` (new, in singleton) reads `_read_server_state()` for the recorded port, acquires `_exclusive_lock()`, calls `_terminate_backend(port)`, clears `server.json`/`PORT_FILE`, and returns a `(result, pid)` tuple. **One canonical backend-terminate, two callers** (eviction keeps its skip-if-reusable policy; stop always terminates) — the deduplication lens made this an extraction, not a copy.
Identity safety is inherited free: `_terminate_backend` resolves the pid via `_backend_pid_on_port` (foreign holder ⇒ None) then the `_is_our_backend(recorded)`-guarded recorded pid — a recycled pid running something else is **never** terminated.

**B. `restart` — the cold-start sequence with an unconditional terminate.**
`restart_backend()` mirrors `_start_backend_holding_lock`'s discipline **using the same primitives** (no second spawn path): `with _exclusive_lock() as got` → if not `got`, return `("busy", None)` (a session is mid-cold-start; the operator retries) → `_terminate_backend(port)` → `_start_server_process(port)` → `_wait_for_server(port)`. It then reports the **true** post-restart state via `_probe_backend_status()` (one status vocabulary everywhere), so a restart that comes back `wedged`/`down` is visible, not assumed-good. `restart` is therefore `stop`-then-cold-start, and it recovers a `wedged` backend (M1's diagnosis → M8's action) and a stale same-version backend (M2's pain) with one command **today**.

**C. `kill-orphans` — a thin, gated trigger of the existing reaper.**
`_cmd_kill_orphans` calls `process_cleanup.process_cleanup._recover_orphaned_processes()` — the module singleton, created at import with `STEALTH_MCP_NO_AUTO_RECOVERY=1` so import did **not** already reap. The canonical create_time+user-data-dir matching (audit-praised) is reused **verbatim**; M8 adds no matching logic (binding ruling (d); canonical home stays `process_cleanup.py`).
**Guard (the one footgun):** `_recover_orphaned_processes` both reaps tracked browsers *and* `_clear_pid_file()`s the tracking store. Run against a **live** backend, that would kill its browsers and corrupt its bookkeeping. So `kill-orphans` first reads `_probe_backend_status()`: if `responsive` **or** `wedged` (a backend process is alive and owns the pid file), it **refuses** with guidance ("a backend is running (pid N); use `restart` to recover it, or pass `--force`") and exits non-zero; on `down`/`none` it proceeds. `--force` overrides. This is a clean behavioral partition (conventions lens): **`restart` handles "backend alive but bad"; `kill-orphans` handles "backend gone, browsers orphaned."**

**D. Spawned backends always reap — env-scrub in `_start_server_process`.**
The CLI sets `STEALTH_MCP_NO_AUTO_RECOVERY=1` (`cli.py:35`) and `_start_server_process` passes **no** `env=` to `Popen`, so today a backend spawned *from the CLI* (i.e. `restart`) would **inherit the flag and skip orphan recovery** — leaving the browsers `stop` just orphaned un-reaped. Fix it at the canonical spawn: build the child env as `os.environ` **minus** `STEALTH_MCP_NO_AUTO_RECOVERY`, so "spawn a backend" *always* produces a normal, reaping backend. This is a correctness property of the spawn function (no-op for the proxy path, which never sets the flag), not a CLI concern — placing it in `_start_server_process` keeps one kind of backend (conventions lens) and keeps `restart` trivial.

**E. `status` / `doctor` — surface pid + log path (+ doctor: F-509 detection).**
On M1's rewritten `_cmd_status`/`_cmd_doctor`, add: the recorded **pid** (`_read_server_state()['pid']` — F-305) and the **log location** (`logging_setup.resolve_log_dir()` + the exact `backend-<pid>.log`/`backend-boot.log` names — the log-path half of F-503, cross-review ruling (b)). `doctor` additionally detects an F-509 **foreign** port occupant: for the target port (recorded port else `DEFAULT_PORT`), if the socket is open (`_server_is_healthy`) **but** `_backend_pid_on_port` returns None, print "port N is held by a NON-stealth process — a backend cannot bind here"; if it returns our pid, "held by our backend (pid N)"; if closed, "free." Uses only existing helpers — no new port logic.

**Net M8 surface:** one singleton refactor-extraction (`_terminate_backend`), two singleton orchestrations (`stop_backend`, `restart_backend`) built from existing primitives, one spawn-env correctness fix, three thin CLI verbs, two CLI display extensions. Zero new kill/respawn/matching/liveness logic.

### 2.2 Rejected alternatives

1. **CLI-local `psutil` terminate in `cli.py` (hand-roll `stop`).** Rejected — hard defect under the deduplication lens: it creates a **second** backend-terminate divorced from `_is_our_backend`/`_backend_pid_on_port`, and cli.py's own docstring says it "never reimplements" backend logic. The extraction (A) keeps one terminate.
2. **Call `_clear_stale_backend(port)` directly for `stop`.** Rejected — its `_find_running_server()==port` guard makes it a **no-op against a healthy backend**, the exact case `stop` exists for. The guard is eviction *policy*, not terminate *mechanism*; splitting them is the fix.
3. **A new `stop` helper that re-derives the pid and kills (parallel to `_clear_stale_backend`).** Rejected — duplicates pid-resolution + kill-escalation + port-wait (a second way to terminate). Extraction reuses all three.
4. **`restart` = `signal`/SIGHUP the backend to reload in place.** Rejected — the backend is a detached `DETACHED_PROCESS` on Windows (no POSIX signals), and in-place reload is M2's concern. `restart` = evict + fresh spawn, which also correctly picks up a new package version.
5. **`restart` spawns without the singleton lock.** Rejected — races a concurrent proxy cold-start for port 19222 (two backends). Reusing `_exclusive_lock()` (the existing coordination primitive) makes them mutually exclusive; "busy" is the honest outcome when a cold-start already holds it.
6. **`kill-orphans` constructs a fresh `ProcessCleanup()` (env var unset) to force recovery.** Rejected — that path *also* runs `_setup_cleanup_handlers()` (signal + atexit handlers, unwanted in a one-shot CLI) and is a second recovery entry point. Triggering `_recover_orphaned_processes()` on the already-constructed module singleton is the thinner trigger.
7. **`kill-orphans` re-implements the create_time/user-data-dir matching in cli.py.** Rejected — a hard defect under dedup/conventions and it discards the audit-praised, well-tested matcher. Thin-trigger only (binding ruling (d)).
8. **`kill-orphans` runs unconditionally (no live-backend guard).** Rejected — `_recover_orphaned_processes` calls `_clear_pid_file()`, so running it against a live backend both kills its browsers and wipes its tracking. The `_probe_backend_status` gate (C) is the cheap, reuse-only guard.
9. **F-509: bind an OS-assigned free port with fallback in the shared spawn path, now.** Rejected for M8 — see §8; the fallback ripples through the proxy connect target, `_wait_for_server`, M1's fixed-port watchdog, and M2's reuse key, for a Medium/0.7/not-adversarially-reviewed finding. Detection (E) closes the *invisible* half within M8's surface; the fallback is filed as a focused follow-up that reuses `proxy_forwarder._free_port()` (no new port convention).
10. **Add a public `recover_orphans()` seam on `ProcessCleanup` for `kill-orphans` to call.** Rejected *for M8* (it would edit `process_cleanup.py` and overlap M11a) — but recommended **to M11a**, which already refactors that file's init/guard; §"open questions".

---

## 3. Sequencing (smallest-first, each independently verifiable)

> Baseline before starting (must be green, on the post-M3+M1 tree): `.venv\Scripts\python.exe -m pytest -m "not integration" -q` → **402 passed** (+ M3's and M1's added tests), coverage ≥ gate 39. Re-run the **full** suite after **every** step. One checkpoint commit per step (§6).

**Step 1 — `_terminate_backend(port)` extraction (behavior-preserving refactor).**
Extract `_clear_stale_backend:191-215` into `_terminate_backend(port) -> bool`; rewrite `_clear_stale_backend` to `guard + _terminate_backend(port)`. Pinning first: a unit test that `_terminate_backend` (via the recorded-pid path, no listener) terminates a **marked** sleeper (cmdline contains `stealth_chrome_devtools_mcp` + `--transport`) and **refuses** a plain sleeper (identity check). Existing eviction coverage (`test_singleton_version_aware.py` eviction cases, `test_proxy_backend_death.py`) must stay green **unchanged** — they exercise the shared body through `_clear_stale_backend`.
*Verify:* `.venv\Scripts\python.exe -m pytest tests/test_singleton_stop_restart.py tests/test_singleton_version_aware.py -q` green; full suite still 402(+).

**Step 2 — env-scrub in `_start_server_process` (spawned backends always reap).**
Build the child env = `os.environ` minus `STEALTH_MCP_NO_AUTO_RECOVERY`; pass `env=` to `Popen`. Pinning first: monkeypatch `subprocess.Popen`, assert the captured `env` **lacks** the key even when the parent sets it. Existing `test_singleton_version_aware.py::test_start_server_process_records_current_version_and_pid` stays green (it asserts pid/version recording, not env).
*Verify:* new env test + that spawn test green; full suite green.

**Step 3 — `status`/`doctor` surface pid + log path + doctor F-509 detection (display-only; closes F-305, F-503-half, F-509-visibility).**
Add pid + `resolve_log_dir()`/`backend-<pid>.log` lines to both; add the foreign-port branch to `doctor`. Pure additive display, no lifecycle risk. Pinning first (`tests/test_cli.py`): `status` with a written `server.json` shows `pid <N>` and the log-dir path; `doctor` with a simulated foreign occupant on the target port prints the NON-stealth warning (monkeypatch `_server_is_healthy`→True, `_backend_pid_on_port`→None).
*Verify:* new cli tests green; existing `test_status_runs` (asserts only "session root") green; full suite green.

**Step 4 — `stop_backend()` + `stop` verb.**
Add `stop_backend()` (lock → `_terminate_backend` → clear state → `(result, pid)`); `_cmd_stop` prints the result; register in `_DISPATCH`/`build_parser`. Pinning first: the **state matrix** via stubbed `_probe_backend_status`/`_read_server_state` and a marked sleeper — `responsive`→terminated+state cleared; `wedged`→terminated; `down`→"already stopped"; `none`→"not running"; lock contended→"busy"; recycled-foreign pid→**not** killed.
*Verify:* new tests green; full suite green.

**Step 5 — `restart_backend()` + `restart` verb.**
Add `restart_backend()` (lock → terminate → `_start_server_process` → `_wait_for_server` → report `_probe_backend_status`); `_cmd_restart` prints result incl. new pid; register. Pinning first: monkeypatch `_start_server_process` (record call + write a fake `server.json`) and `_probe_backend_status`; assert terminate-then-spawn ordering under the lock, "busy" when the lock is held, and that the printed final state is the reporter's.
*Verify:* new tests green; full suite green.

**Step 6 — `kill-orphans` verb (thin trigger + guard).**
`_cmd_kill_orphans` reads `_probe_backend_status`; refuse on `responsive`/`wedged` (unless `--force`), else call `process_cleanup.process_cleanup._recover_orphaned_processes()`; register with a `--force` flag. Pinning first: `responsive`→refuses + does **not** call the reaper; `--force`→calls it; `down`→calls it (spy/monkeypatch `_recover_orphaned_processes`, asserted, never really reaping).
*Verify:* new tests green; full suite green + coverage ≥ 39.

Steps 1–2 are singleton-only (refactor + correctness). Step 3 is display-only and depends on **no** other step (front-loads the F-305/F-509 visibility wins at zero lifecycle risk). Steps 4→5 chain (restart uses `stop_backend`'s helpers); Step 6 is independent of 4/5. Each step is one commit and individually revertible.

---

## 4. Breaking changes

**0 users — external-compatibility breaking changes: N/A.** No tool name, signature, config key, or MCP return shape changes (no `server.py` edit; the 96-tool surface is untouched).

**Observable behavior that changes (all intended, all operator-facing CLI):**
- **New verbs:** `stealth-chrome-devtools stop | restart | kill-orphans`. `stop`/`restart` **terminate the shared backend** (killing every live browser session — that is the verb's purpose); `kill-orphans` reaps orphaned browsers from a dead backend and refuses to run against a live one.
- **CLI output format changes** (additive): `status` and `doctor` now print the backend **pid** and the **log directory / file names**; `doctor` prints a **port-occupant** line. This stacks on M1's earlier change of the backend line to `responsive/wedged/down`. The only existing test asserting status output (`test_status_runs`) checks a substring that still holds.
- **`_start_server_process`** now hands spawned backends an env **without** `STEALTH_MCP_NO_AUTO_RECOVERY` (previously inherited whatever the parent had). Intended: a spawned backend must own its lifecycle.
- **`_clear_stale_backend`** is refactored to call `_terminate_backend`; its external behavior is **unchanged** (pinned by the untouched eviction tests).

---

## 5. Test strategy

Guiding rule (superpowers TDD): the behavior-pinning test is written and shown to pass **in the same step** as the code. Keep **402(+M3+M1) green and coverage ≥ 39 at every checkpoint**; cli.py is under-covered today (only `test_cli.py`'s parser/status/cleanup cases — no stop/restart/kill-orphans/doctor-backend coverage), so every new test is net-positive.

### 5.1 The hermetic fixtures (no real backend, no Chrome — stay in `not integration`)
- **Marked sleeper** = `subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)", "stealth_chrome_devtools_mcp", "--transport", "http"])`. Its psutil cmdline satisfies `_is_our_backend`; used to prove `_terminate_backend` **kills** it. Torn down in a `finally`.
- **Plain sleeper** = the same without the marker args ⇒ `_is_our_backend` False ⇒ proves the identity **refusal** (the recycled-pid nightmare). This is the mandated "a recorded pid now owned by a different process is NOT killed" test.
- **State/status stubs** = `monkeypatch.setattr(singleton, "_probe_backend_status", lambda: ("wedged", 19222))` etc., and a written `server.json` under `tmp_session_root`, to drive the verb×state matrix without a backend.
- **Reuse M1's stubs where they exist:** if plan_M1 left its `wedged`/`responsive` HTTP stubs module-local, Step 1 promotes them to `conftest.py` (test-fixture dedup) so M1 and M8 share one set; otherwise M8 uses the `_probe_backend_status` monkeypatch above (cheaper, no socket needed for the matrix).

### 5.2 Behavior-pinning tests written BEFORE each change
- **`_terminate_backend` identity** (Step 1): marked sleeper (recorded-pid path) → terminated; plain sleeper → **untouched**; never raises.
- **Spawn env-scrub** (Step 2): captured `Popen(env=…)` lacks `STEALTH_MCP_NO_AUTO_RECOVERY` even when the parent sets it.
- **status/doctor surfacing** (Step 3): `status` prints `pid <N>` + log-dir; `doctor` prints the foreign-port warning on a simulated squatter and "held by our backend (pid N)" when `_backend_pid_on_port` returns a pid.
- **stop matrix** (Step 4): responsive→stopped+state-cleared; wedged→stopped; down→"already stopped"; none→"not running"; lock-held→"busy"; foreign-recycled-pid→not killed.
- **restart** (Step 5): terminate-then-spawn ordering under the lock (monkeypatched spawn); busy on lock contention; final line is `_probe_backend_status`'s state.
- **kill-orphans guard** (Step 6): responsive/wedged→refuse + reaper **not** called; `--force`→called; down/none→called.

### 5.3 Existing tests that MUST change
- **None expected.** `_clear_stale_backend`'s external behavior is preserved (Step 1 is an extraction), `_start_server_process`'s recorded-state contract is preserved (Step 2 only adds `env=`), and `test_status_runs` asserts a substring untouched by the new lines. If Step 1 surfaces any eviction test asserting the *inlined* body's line structure, migrate it minimally to call through `_clear_stale_backend` (its public behavior) — do not delete.

### 5.4 Characterization for the undertested surface
- cli.py's backend-facing verbs are unit-test-dark today; the Step 3–6 tests are that net. The verb×state matrix (5.2) characterizes all four backend states for every mutating verb — the "define the behavior in all four states" mandate is the test itself.

---

## 6. Rollback + checkpoint commit boundaries

- **Branch:** `audit/fixes-2026-07-02`, **serial after M1's PR merges** (base = post-M3+M1). Stage-3 discipline: pinning tests before the change, **full suite green at every checkpoint**, deviation → **stop and report to Fable**.
- **One commit per Step (§3):** `M8-1 extract _terminate_backend` · `M8-2 spawn env-scrub (reaping backend)` · `M8-3 status/doctor surface pid+log+port (F-305/F-503/F-509-visibility)` · `M8-4 stop verb` · `M8-5 restart verb` · `M8-6 kill-orphans verb`.
- **PR shape (recommended):** a **single M8 PR** of six commits — one file pair, ~one cohesive feature. Keep the commit boundaries inside it. (If the human prefers, `M8-1..M8-3` [refactor + safe display] and `M8-4..M8-6` [the mutating verbs] split cleanly into two stacked PRs.)
- **What to revert if a step goes bad:** Steps 1–2 are singleton-only (refactor + additive kwarg) — revert in isolation; nothing above depends on them beyond the new verbs. Step 3 is display-only (revert with zero lifecycle impact). Steps 4/6 are independent verbs (revert individually); Step 5 depends on Step 4's `stop_backend`. Fastest safe partial rollback: keep 1–3 (strictly more observable + the terminate seam) and drop any misbehaving verb.

---

## 7. Risk (blast radius, worst case, early-warning signs)

1. **Killing the wrong process (the nightmare).** *Blast radius:* an unrelated user process. *Mitigation:* `_terminate_backend` only ever targets a pid that passes `_is_our_backend` (cmdline = module + `--transport`) — `_backend_pid_on_port` returns None for a foreign holder, and the recorded-pid fallback is `_is_our_backend`-guarded; the Step-1/Step-4 identity tests pin exactly the recycled-pid case. *Worst case:* a recycled pid that is itself *another* stealth backend is terminated — but that *is* our backend, acceptable. *Early warning:* the identity tests red, or a `stop` that reports success against a `none`/`down` state.
2. **`stop`/`restart` killing live browser sessions.** *Blast radius:* every tab on the shared backend. *Mitigation:* this is the verb's stated purpose; it is operator-initiated and named. `kill-orphans` (the one with a non-obvious blast radius) is gated off a live backend. *Worst case:* an operator runs `restart` mid-session and loses live tabs — intended and documented in `--help`. *Early warning:* n/a (intended).
3. **`restart` racing a concurrent proxy cold-start** for port 19222. *Mitigation:* `restart_backend` holds `_exclusive_lock()` — the same primitive cold-start uses — so the two are mutually exclusive; a racing proxy gets `got=False` and proxies to the restarting backend, and a racing `restart` reports "busy." *Worst case:* two `backend-boot.log` entries with overlapping timestamps (harmless, per M3). *Early warning:* repeated "busy" from `restart`, or two backend pids briefly in `netstat`.
4. **`kill-orphans` reaping a live backend's browsers / wiping its tracking.** *Blast radius:* a healthy session's tabs + `~/.stealth_browser_pids.json`. *Mitigation:* the `responsive`/`wedged` refusal gate (only `down`/`none` proceed); `--force` is the explicit override. *Worst case:* an operator `--force`s against a live backend and reaps it — explicit, and equivalent to `restart` without the respawn. *Early warning:* `kill-orphans` reporting killed processes while `status` shows `responsive`.
5. **`restart`-spawned backend not reaping orphans** (the env-inheritance footgun). *Mitigation:* Step-2 env-scrub, pinned. *Worst case (if unfixed):* orphaned Chrome accumulates after each `restart` until the next proxy cold-start — the env-scrub prevents it. *Early warning:* orphaned `uc_*` profiles / Chrome pids after a `restart`.
6. **F-509 detection false-positive** (calling our own backend "foreign"). *Mitigation:* the detector is `_backend_pid_on_port` (which returns our pid for our backend) — "foreign" is only reported when the socket is open **and** `_backend_pid_on_port` is None. *Early warning:* `doctor` calling a known-good backend's port foreign.
7. **`_clear_stale_backend` regression from the extraction.** *Blast radius:* cold-start eviction (M1 just un-jammed it). *Mitigation:* the extraction is line-for-line; the untouched eviction tests (`test_singleton_version_aware.py`, `test_proxy_backend_death.py`) gate it. *Early warning:* those suites red.

**Overall worst case:** a `--force`d `kill-orphans` against a live backend, or a `restart` mid-session — both operator-initiated, both now *visible* in M3's logs and reversible by re-running `restart`. Everything else degrades to "busy, retry" or "a little extra log," never a silent functional regression, because every kill path is `_is_our_backend`-verified and every mutating verb is gated on M1's `_probe_backend_status`.

---

## 8. Findings closed (each with how)

- **F-302 (High — no operational recovery surface).** **Closed.** `stop`/`restart`/`kill-orphans` added; `stop` reuses the extracted `_terminate_backend` (identity-verified); `restart` = terminate + the existing cold-start spawn under the lock (the manual escape hatch for M1's `wedged` and M2's stale-backend); `kill-orphans` thin-triggers the existing `_recover_orphaned_processes`. Every step of recovery is now a supported command.
- **F-305 (Medium — pid recorded but never surfaced).** **Closed.** `status` and `doctor` print `_read_server_state()['pid']` (e.g. `backend : running (responsive) on port 19222 (pid 104504)`).
- **F-503 (High — detached/DEVNULL/no-log) — the log-path half only.** **Closed here** (M3 delivered "there is now a log"; M8 delivers "here is *where*"): `status`/`doctor` print `logging_setup.resolve_log_dir()` and the exact `backend-<pid>.log` / `backend-boot.log`. (Cross-review ruling (b).)
- **F-509 (Medium — fixed port 19222, no fallback). Closed IN FULL** (detection [main plan] + auto-port-fallback [Amendment A1]). **Detection half (§2.1-E / Step 3):** `doctor` detects and names a **foreign** occupant of the target port (socket open + `_backend_pid_on_port` None), so a port collision is diagnosable. **Fallback half — IN via Amendment A1** (human-ordered 2026-07-02, §A1): the cold-start spawn (and `restart`) select the backend port via a new `_select_backend_port()` — keep the target port (recorded port else `DEFAULT_PORT`) when it is free or held by our own backend, else bind an OS-assigned free port from `proxy_forwarder._free_port():17` — and `_start_server_process` records the chosen port in `server.json`, so a foreign squatter on 19222 no longer causes a silent 120 s outage. The chosen port reaches every consumer **for free**: `ensure_server_running` returns it (so the proxy connect target, `_wait_for_server`, and M1's watchdog receive it by argument) and `server.json` records it (so discovery / `_probe_backend_status` / `stop` / `doctor` read it) — full consumer enumeration in **§A1.2**. The human overturned this plan's original OUT ruling (§2.2-#9, Appendix Q1), accepting the stated blast radius; A1 lands **before M2**, whose reuse key must read the recorded port, never assume 19222 (NOTICE recorded in `state.json`).
- **F-607 (Medium — `_file_lock` silently yields on lock failure). Ruled OUT of M8.** Justification: F-607's harm is **concurrent** contention on `~/.stealth_browser_pids.json`. `kill-orphans` is a **single-shot** trigger **gated off a live backend** (§2.1-C), so it never creates the two-writers race F-607 describes. The `_file_lock` fix changes the **live backend's** tracking path and is `process_cleanup.py`-internal — its natural home is **M11a** (guarded ProcessCleanup init), which already opens that file. **Overlap flagged to M11a.**
- **F-608 (Medium — non-recovery `fallback_pid` killed without create_time check). Ruled OUT of M8.** Justification: `kill-orphans` calls `_recover_orphaned_processes` → `_kill_processes_for_metadata(recovery=True)` — the branch that **already** verifies `create_time` vs `_init_time` + stored `create_time` (`:371-410`). F-608's gap is the `recovery=False` **explicit-close** branch (`:411-422`), exercised only by `close_instance`/`finalize` — **M7's** territory (or a standalone `process_cleanup` fix), unreachable from any M8 verb. The M8 singleton kill path (`_terminate_backend`) has the equivalent hardening via `_is_our_backend`. **Overlap flagged to M7.**

---

## The four lenses applied to this design (where each shaped a choice)

- **Deduplication →** `_terminate_backend` is an **extraction**, not a copy: one canonical backend-terminate for both eviction and `stop` (rejected CLI-local `psutil`, §2.2-1). `kill-orphans` reuses the one matcher (rejected re-implementation, §2.2-7). status/doctor call `logging_setup.resolve_log_dir()` and `_probe_backend_status()` rather than recomputing the log path or re-checking liveness.
- **Conventions →** `restart` spawns via the **same** `_start_server_process` + `_exclusive_lock` as cold-start (no second spawn path, §2.2-5); every verb bootstraps through `_server()` + `STEALTH_MCP_NO_AUTO_RECOVERY`; one status vocabulary (`responsive/wedged/down/none`) everywhere; the env-scrub keeps **one kind** of spawned backend. Refusing a second port-selection path (F-509 fallback OUT) is a conventions call as much as a scope call.
- **Clarity →** verb names (`stop`/`restart`/`kill-orphans`) and function names (`_terminate_backend`, `stop_backend`, `restart_backend`) say what they do without reading bodies; a lighter model can place a "surface the pid" change in `_cmd_status` and a "terminate" change in `_terminate_backend` by name alone.
- **Modularity →** backend lifecycle lives in `singleton.py` (its home); `cli.py` stays a thin front-end that invokes + prints; orphan matching stays in `process_cleanup.py`, reached by a thin trigger. No module needs another's internals to be understood, except the deliberate, documented consumption of M1's reporter and M3's log-dir resolver.

---

## Appendix — open questions (RESOLVED by human, 2026-07-02)

1. **F-509 auto-port-fallback:** ❗ **OVERTURNED — fallback is IN.** The human accepted the stated blast radius and ordered the OS-assigned free-port fallback now, before M2. Re-scoped as **Amendment A1** (drafted by the planner, appended below as §A1 when ready): fallback binding at spawn reusing `proxy_forwarder._free_port()`, chosen port threaded via `server.json` to the proxy connect target, `_wait_for_server`, M1's watchdog, and the CLI verbs; M2's planner is on notice that the reuse key must read the recorded port, never assume 19222.
2. **`kill-orphans` reaper seam:** ✅ **Direct private call** (`_recover_orphaned_processes()` on the module singleton), no `process_cleanup.py` edit; M11a adds the public `recover_orphans()` seam and repoints the verb (directive recorded).

---

## Amendment A1 — F-509 auto-port-fallback (human-ordered 2026-07-02)

**Status:** **APPROVED** — orchestrator cross-review passed 2026-07-02; stays strictly inside the human-pre-approved envelope (the blast radius is SMALLER than the overruled OUT-ruling feared: the port already flows as a single value, so A1 edits only the selection boundary). The human pre-approved this re-scope at the combined gate; the A1 summary is re-presented at the M2 gate for any final objection. Overturns §2.2-#9 / Appendix Q1: the human accepted the stated blast radius and ordered the OS-assigned free-port fallback **IN**, before M2. This amendment adds the *fallback* half of F-509; the *detection* half (`doctor` foreign-occupant line, §2.1-E/Step 3) is unchanged and still ships. All anchors below were re-opened at the pinned SHA while writing A1.

**The one fact that shapes A1 (and shrinks it to almost nothing).** The port already flows through the whole runtime as a **single value**: `ensure_server_running()` returns it, `server.py:main` hands that return value to `run_stdio_proxy(port)`, and it threads by argument down `_bridge(port)` → `_proxy_streams(…, port)` → `_backend_http_url(port)` / `run_backend` / M1's `_watch_backend_liveness(port)`; the *other* consumers (`_find_running_server`, M1's `_probe_backend_status`, `stop`) read the recorded port from `server.json`, which `_start_server_process` already writes at `:245`. So the fallback needs to change exactly **one decision** — *what value `ensure_server_running` chooses on a cold start* — and every consumer inherits the chosen port for free. There is **no** re-plumbing of the proxy path, `_wait_for_server`, or M1's watchdog: making the selection **synchronous, at the `ensure_server_running` boundary** (not inside the spawn thread) is what lets the one chosen value reach both the return (proxy) and the arg (spawn) in lock-step. The main plan's §8 rationale assumed the fallback must "thread the chosen port back through the proxy connect target / `_wait_for_server` / the watchdog"; on re-verification at HEAD **it already threads there by argument**, so that blast radius is smaller than the OUT ruling feared.

### A1.1 Scope

**Adds to M8's touch list — `singleton.py` only (+ tests):**
- **`_port_is_foreign_held(port) -> bool`** (new, ~2 lines): `_server_is_healthy(port) and _backend_pid_on_port(port) is None` — the canonical predicate for "socket open but NOT our backend." One definition of "foreign occupant" (dedup lens); the same condition `doctor`'s F-509 detection (Step 3) computes inline — **recommend** repointing that `doctor` line to this predicate so there is a single home (a 1-line change inside M8-3's own `cli.py` block, not new A1 surface).
- **`_select_backend_port(preferred=DEFAULT_PORT) -> int`** (new, ~6 lines): the port-selection *policy* wrapper (below). Delegates the actual free-port pick to `proxy_forwarder._free_port()` (function-local import; `singleton.py` already does lazy in-function imports and does **not** import `proxy_forwarder` at module top, so no cycle) — it invents **no** new port-picker (conventions lens; mirrors how `_terminate_backend` is the one terminate and `_free_port` stays the one port picker).
- **`ensure_server_running` (:297-313):** insert one line — `port = _select_backend_port(port)` — on the cold-start branch, before the daemon thread is started (so the same chosen value is passed to `_start_backend_holding_lock(port)` **and** returned to the proxy).
- **`restart_backend()` (built in M8-5, §2.1-B):** route its spawn port through `_select_backend_port()` so a `restart` also survives a squatter that appeared while the old backend was dead (symmetry with cold start). The common case (recorded port is ours) is unchanged: after `_terminate_backend` frees it, selection returns that same port and rebinds it.
- **Tests:** a new `tests/test_singleton_port_fallback.py` (squatted-port matrix) + additions to `tests/test_singleton_stop_restart.py` (restart-under-squat). No new source file.

**Explicitly NOT touched by A1 (stated so Stage 3 does not creep):**
- **`cli.py:258`** (`serve --port … default 19222`, the literal twice) and **`server.py:23`** (`--singleton-port … default 19222`) — these are argparse **literals**, owned by **F-722 → M15** (export `DEFAULT_PORT`, repoint the defaults to it). A1 changes *behavior*, not *where constants live*; it **consumes** `server.py:23`'s value as `ensure_server_running`'s `preferred` but does not retype or relocate it. `serve` is the standalone-server escape hatch (`shim.main()`), not a singleton discovery/spawn consumer — untouched.
- **M2** reuse-key semantics (`_find_running_server:132`, `_server_version:263`) — A1 only guarantees the recorded port is authoritative; the NOTICE to M2 ("read the recorded port, never assume 19222") is already in `state.json`.
- **`server.port` / `PORT_FILE`** — write-only legacy (def `:36`, write `:244`, **no reader in `src/`**); it carries the chosen port for free, and its removal is M15/F-722-adjacent, not A1.
- No true bind-failure retry loop, no port-range scan (§A1.3 rejected #1/#5).

### A1.2 The `19222` / `DEFAULT_PORT` consumer enumeration (A1's core deliverable, re-verified at HEAD)

Every site that references the fixed port or consumes the backend port, and whether it already flows from `server.json` / by-argument (→ **no change**) or assumes 19222 (→ **edit**):

| Consumer (symbol @ pinned SHA) | How it gets the port @ HEAD | Assumes 19222? | A1 action |
|---|---|---|---|
| `DEFAULT_PORT = 19222` (`singleton.py:41`) | constant definition | — (the literal home) | **none** — M15/F-722 exports it; A1 uses it as the fallback `preferred` |
| `ensure_server_running(port=DEFAULT_PORT)` (`:297`, returns `port` at `:313`) | default arg; returns the **requested** port on cold start | **YES** (returns 19222 unconditionally) | **EDIT** — `port = _select_backend_port(port)` before the spawn thread; return the chosen port |
| `_start_backend_holding_lock(port)` (`:272`) | `port` arg from the ensure_server_running thread | no (arg) | none — inherits the chosen port |
| `_clear_stale_backend(port)` (`:180`) | `port` arg (+ recorded-pid via `_read_server_state`) | no (arg) | none — evicts on the chosen port |
| `_start_server_process(port)` (`:218`) | `port` arg; writes `server.json` `:245` + `server.port` `:244` | no (arg) | none — **records the chosen port** (makes `server.json` canonical); [M3 + M8 env-scrub edit its `kwargs`, not the port] |
| `_wait_for_server(port)` (`:248`) | `port` arg | no (arg) | none — waits on the chosen port |
| `_find_running_server()` (`:117`) | `_read_server_state()["port"]` | no (server.json) | none — reuse already follows the recorded port |
| `_backend_http_url(port)` (`:259`) | `port` arg | no (arg) | none |
| `_proxy_streams(…, port)` (`:401`, url `:420`) | `port` arg from `_bridge` | no (arg) | none — **proxy connect target = chosen port** (ensure_server_running returned it) |
| `_bridge(port)` (`:549`) / `run_stdio_proxy(port)` (`:567`) | `port` arg | no (arg) | none |
| `run_backend` (nested `:459`) | `_proxy_streams`'s `url` | no | none |
| `monitor_backend` → M1 `_watch_backend_liveness(port)` | `port` arg (proxy subtree) | no (arg) | none — **watchdog targets the chosen port; no M1 edit, the invariant shifts** |
| M1 `_probe_backend_status()` | `_read_server_state()["port"]` | no (server.json) | none — `status`/`doctor`/`stop`/`restart` follow the recorded port |
| `server.py:main` (`:29`, `:31`) | `ensure_server_running(port=--singleton-port)` → `run_stdio_proxy(port)` | no (return value) | none — **linchpin: threads the chosen return value to the proxy** |
| M8 `stop_backend()` (Step 4) | `_read_server_state()` recorded port → `_terminate_backend` | no (server.json) | none |
| M8 `restart_backend()` (Step 5) | recorded port → terminate + spawn | partially (spawns on the target) | **EDIT** — route the spawn port through `_select_backend_port()` (squatter-survival symmetry) |
| M8-3 `doctor` F-509 detection | `_server_is_healthy` + `_backend_pid_on_port` on the target | reads target (recorded else DEFAULT) | none required; **recommend** repoint to shared `_port_is_foreign_held()` (dedup) |
| `cli.py:258` `serve --port 19222` (×2 on the line) | argparse literal (standalone `serve` path) | **YES** (literal) | **none — F-722 → M15**; not a discovery/spawn consumer |
| `server.py:23` `--singleton-port 19222` | argparse literal → ensure_server_running `preferred` | **YES** (literal) | **none — F-722 → M15**; A1 consumes its value, does not retype it |
| `server.port` / `PORT_FILE` (`:36` def, `:244` write) | write-only, **no reader in `src/`** | no | none — carries the chosen port for free; legacy (M15-adjacent) |

**Count:** **20 port-referencing sites** enumerated. Textual "assumes 19222" sites = **4** (`singleton.py:41` const, `:297` cold-start default/return, `cli.py:258`, `server.py:23`). Of the runtime port-flow consumers, **only 2 need A1 code edits** — `ensure_server_running` (cold-start selection) and `restart_backend` (spawn selection) — plus **2 new helpers** (`_select_backend_port`, `_port_is_foreign_held`). The remaining **~14** flow from `server.json` or by-argument and inherit the chosen port unchanged. **2** literals (`cli.py:258`, `server.py:23`) belong to **M15/F-722**, untouched here. This confirms the brief's expectation: **A1 adds nothing to M8's touch list beyond `singleton.py` + tests.**

### A1.3 Approach + rejected alternatives

**Chosen — synchronous selection at the `ensure_server_running` boundary, delegating the pick to `_free_port()`:**

```python
def _port_is_foreign_held(port: int) -> bool:
    """True iff ``port``'s socket is open but NOT held by our backend."""
    return _server_is_healthy(port) and _backend_pid_on_port(port) is None

def _select_backend_port(preferred: int = DEFAULT_PORT) -> int:
    """Port to spawn the backend on. Prefer the port recorded in server.json
    (so eviction + restart land where a prior backend ran), else ``preferred``.
    Keep it when free or held by OUR OWN backend (eviction rebinds it); only a
    FOREIGN occupant forces an OS-assigned fallback so a collision is
    recoverable instead of a silent 120 s outage (F-509)."""
    from proxy_forwarder import _free_port          # lazy; no module-top cycle
    state = _read_server_state()
    recorded = state.get("port") if state else None
    target = recorded if isinstance(recorded, int) else preferred
    return _free_port() if _port_is_foreign_held(target) else target
```
and in `ensure_server_running`, one inserted line on the cold-start branch:
```python
    existing = _find_running_server()
    if existing is not None:
        return existing
    port = _select_backend_port(port)               # A1
    threading.Thread(target=_start_backend_holding_lock, args=(port,), daemon=True).start()
    return port
```
Three cases, all via existing helpers: **free** (`_server_is_healthy` False) → keep target; **our own backend** (LISTEN + `_is_our_backend`, incl. a wedged same-version one) → keep target so `_clear_stale_backend` evicts and rebinds it; **foreign** → `_free_port()`. `_start_server_process(port)` then records the chosen port in `server.json` at `:245` — the single source of truth. *(Illustrative; Stage 3 re-anchors by symbol per §1.3.)*

**Rejected alternatives:**
1. **Sequential port-range retry (19222, 19223, 19224, …).** Rejected — invents a *new* port-selection convention (a scan window) when `_free_port()` (OS-assigned ephemeral, already used at `proxy_forwarder.py:66`) hands back a guaranteed-free port in one call. A fixed range can deterministically re-collide with the same neighbour service and is slower; a second port-picker is a dedup/conventions defect (an ADDENDUM "second way of doing something" = defect).
2. **Select inside `_start_server_process` (the daemon thread) + make the proxy re-read `server.json` for its connect target.** Rejected — "at spawn, in the thread" means `ensure_server_running` returns *before* the port is known, so the proxy (`_await_backend_http`/`run_backend`, the crown-jewel path) would have to **poll `server.json`** for the chosen port and gain an ordering dependency on the thread's write. Selecting synchronously at the `ensure_server_running` boundary lets the one chosen value flow to both the return (proxy) and the arg (spawn) with **zero** proxy-path edits. Both record via the same `:245` write.
3. **Detection-only (the original §2.2-#9 / §8 OUT ruling).** Overruled by the human. Detection alone *names* the squatter but the backend still cannot bind and the outage persists; A1 makes the spawn **survive** it. (Detection is retained — it is now the human-readable half of the same story.)
4. **A separate port-file as the source of truth for the chosen port** (promote the write-only `server.port`, or add a new one). Rejected — `server.json` already records `{port, version, pid}` atomically and is what **every** consumer reads for identity; a second port artifact is a second source of truth (dedup/conventions defect). `server.json` stays canonical; `server.port` stays the write-only legacy it is today.
5. **True bind-failure fallback via a respawn retry loop** (spawn on target; if `_wait_for_server` times out, respawn on `_free_port()`). Rejected for A1 as disproportionate for a Medium/0.7 finding: `_start_server_process` spawns a **child** that binds internally, so the parent sees a bind failure only as a `_wait_for_server` timeout — which also fires for a merely slow/wedged start, so retrying on it risks port thrash and masks M1's wedge detection. Detection-based selection deterministically handles the dominant scenario (a *persistent* foreign squatter). The residual TOCTOU (port taken in the millisecond between select and child bind) is the same window `_free_port()` already carries and is left as a documented risk (§A1.8), not engineered away.

### A1.4 Sequencing (slots after M8-6; each independently verifiable)

> Baseline unchanged: full suite green on post-M3+M1+M8-1..6, `.venv\Scripts\python.exe -m pytest -m "not integration" -q` → 402(+M3+M1+M8), coverage ≥ 39. Pinning tests **before** the change; one checkpoint commit per step.

**Step M8-7 — `_port_is_foreign_held` + `_select_backend_port` + cold-start fallback (closes F-509 fallback half).** Add both helpers; insert the one line in `ensure_server_running`. **Pinning first** (`tests/test_singleton_port_fallback.py`): (a) *squatted default* — bind a plain in-process listener on `DEFAULT_PORT` (foreign: not our backend, so `_backend_pid_on_port` → None) → `_select_backend_port(DEFAULT_PORT)` returns a **different** free port; drive `ensure_server_running` with `_find_running_server`→None and `_start_backend_holding_lock` stubbed to capture its arg → the thread is handed the fallback port **and** `ensure_server_running` returns the **same** value (proxy would connect there); (b) *default free* — nothing on `DEFAULT_PORT`, no state → returns `DEFAULT_PORT` (**regression guard: still 19222**); (c) *our own backend* — monkeypatch `_backend_pid_on_port`→a pid with `_server_is_healthy`→True → returns the target (evict+rebind, no fallback); (d) *server.json records the fallback* — with `subprocess.Popen` stubbed (reuse M8-2's Popen monkeypatch) let `_start_server_process` run on the chosen port under a squat → assert `_read_server_state()["port"]` == the fallback and == the child's `--port`.
*Verify:* `.venv\Scripts\python.exe -m pytest tests/test_singleton_port_fallback.py -q` green; full suite green.

**Step M8-8 — `restart` uses `_select_backend_port` (squatter-survival symmetry; depends on M8-5).** Route `restart_backend`'s spawn port through `_select_backend_port()`. **Pinning first** (extend `tests/test_singleton_stop_restart.py`): *recorded fallback port, default squatted* → `restart` rebinds the recorded fallback; *recorded port now foreign-held* → `restart` falls back to a fresh free port and records it; *normal case (recorded port is ours)* → after `_terminate_backend` frees it, selection returns that same port → **no behavior change vs M8-5**.
*Verify:* extended stop/restart tests green; full suite green + coverage ≥ 39.

M8-7 is independent of M8-4/5/6 (touches only `ensure_server_running` + two new helpers). M8-8 depends on M8-5 (`restart_backend` must exist). Both are additive; each is one commit — `M8-7 auto-port-fallback (F-509 spawn survives a squatter)` · `M8-8 restart selects port via _select_backend_port`.

### A1.5 Breaking changes

**0 users — external-compatibility N/A.** No tool/signature/config/return-shape change. **Operator-visible:** the backend port is **no longer guaranteed 19222** — when a *foreign* process squats 19222 the backend now binds an OS-assigned high port instead of dying. This is already visible: M1's `status`/`doctor` print `on port P` from `_probe_backend_status` (which reads `server.json`), and `doctor`'s F-509 line names the occupant. `--singleton-port` is still honoured as the `preferred`. A backend that fell back to port P **stays** on P across `restart` (stability); `stop` clears `server.json`, so the next cold start returns to `DEFAULT_PORT` when it is free — the reset path to 19222.

### A1.6 Test strategy

Pinning tests are written and shown green **in the same step** as the code (superpowers TDD), 402(+) and coverage ≥ 39 at every checkpoint. Hermetic — no real backend, no Chrome (stay in `not integration`):
- **Squatted-port fixture** = a plain `socket.socket` bound + `listen()` on the target port in the test process. It is a **foreign** holder by construction (`_is_our_backend` fails on the pytest process's cmdline → `_backend_pid_on_port` → None), so `_port_is_foreign_held` → True with a real socket and no subprocess. Torn down in `finally`.
- **"Our backend" branch** = `monkeypatch.setattr(singleton, "_backend_pid_on_port", lambda p: 4242)` with `_server_is_healthy`→True — proves the target is kept (evict+rebind), not fallen back.
- **server.json recording** = reuse M8-2's `subprocess.Popen` monkeypatch to capture the child's `--port`, let the real `_write_server_state` write into `tmp_session_root`, and assert `_read_server_state()["port"]` equals both the fallback and the child arg — pinning "`server.json` is the single source of truth."
- **Downstream-follows-recorded-port** = with a recorded fallback port, assert `_find_running_server()` returns it, `_probe_backend_status()` reports on it, and `stop_backend()`/`doctor` target it — i.e. discovery, the M1 status reporter, `stop`, and `doctor` all follow the recorded port (the watchdog follows it by argument via the proxy subtree — no separate test needed, it receives `_proxy_streams`'s `port`).

**Existing tests that assume 19222 (audited at HEAD — no mandatory migration):**
- `test_singleton_fast_handshake.py:115` `assert port == 19222` after `ensure_server_running(port=19222)` — **stays green**: with no recorded state and nothing on 19222 (the hermetic default), `_select_backend_port(19222)` returns 19222; even a *real* stealth backend on 19222 returns 19222 (the "our own backend" branch). Only a *foreign* squatter (never created by this suite) would change it. The non-blocking assertion (`elapsed < 0.5`, `:116`) holds — the added probe is one **instant** connection-refused on a free localhost port. *If* any timing flake appears, the minimal migration is `monkeypatch.setattr(singleton, "_select_backend_port", lambda p=…: 19222)` in that one test.
- `test_singleton_version_aware.py` — **unaffected**: it calls `_start_server_process(4321)`, `_clear_stale_backend(19222)`, and `_start_backend_holding_lock(19222)` **directly** (below `ensure_server_running`, where A1's selection lives), and its `_backend_pid_on_port(19222)` cases (`:193-222`) already pin the exact foreign→None predicate `_select_backend_port` relies on — A1 rests on already-tested ground.
- No test binds 19222 and asserts a fixed spawn port under an occupied-port condition, so nothing must change.

### A1.7 Rollback

M8-7 and M8-8 are individually revertible commits. Reverting **M8-8** restores M8-5's fixed-target `restart`. Reverting **M8-7** removes both helpers and the one `ensure_server_running` line, restoring pure fixed-port cold start — **with the detection half (Step 3 `doctor`) fully intact**, since detection lives in `cli.py` and never depended on A1. So a bad A1 degrades to exactly the main plan's approved "detect-but-don't-recover" F-509 posture, no worse.

### A1.8 Risk (blast radius, worst case, early-warning signs)

1. **Stale `server.json` pointing at a dead or foreign port.** *Handled by existing identity + probe layers:* on reuse, `_find_running_server` requires a **version match** *and* M1's `_backend_http_ready` probe, so a dead/foreign recorded port is rejected (returns None) and we fall to selection; in selection, a recorded port that is now foreign-held (`_port_is_foreign_held` True) triggers `_free_port()`, and a recorded port that is dead (socket closed) is simply reused. A recorded pid recycled onto a foreign process is never terminated — `_clear_stale_backend`/`_terminate_backend` gate on `_is_our_backend` / `_backend_pid_on_port`. *Early warning:* `doctor` naming the recorded port foreign while `status` shows `down`.
2. **Two backends on different ports during the evict overlap.** The wedged/stale backend on port A is evicted (via `_clear_stale_backend`'s recorded-pid path) while the fresh one binds port B (fallback). *Mitigation:* the whole cold start holds `_exclusive_lock`, so only one selection+spawn runs at a time; `_clear_stale_backend` terminates the old pid before `_wait_for_server` returns; M3's per-pid `backend-<pid>.log` makes a brief overlap a non-event (its own documented ≤5 s window). *Worst case:* two `backend-<pid>.log` with overlapping timestamps (harmless). *Early warning:* two of our backend pids in `netstat` for more than the evict wait.
3. **Port-release wait when the port changed.** `_clear_stale_backend`'s post-kill loop (`:210-215`) waits on the port it was asked to evict; because selection keeps the target port for the "our own backend" case, evict and rebind are the **same** port, so the release wait is meaningful. In the migrate-to-fallback case (old A foreign-held or dead) we do **not** wait on A — we bind a fresh B, so there is no release to wait for. *Early warning:* a `_wait_for_server(B)` timeout with a "port still bound" note in the boot log.
4. **TOCTOU between select and the child's bind.** `_select_backend_port` decides by probing, not by holding a bound socket; a foreign process could grab the chosen port in the millisecond before the child binds. *Mitigation/scope:* identical to the window `_free_port()` already carries (bind-close-return) and vanishingly rare on a single-user localhost; if it fires, the backend fails to bind and M1's existing recovery re-runs cold start (now with the squatter visible to `doctor`). Not engineered away in A1 (rejected #5). *Early warning:* a fresh `backend-boot.log` bind error immediately after a fallback selection.
5. **Backend "sticks" on a fallback port.** Because selection prefers the **recorded** port, once we fall back to P we keep P across restarts even after 19222 frees up (deliberate — stability over churn). *Reset path:* `stop` clears `server.json`; the next cold start returns to `DEFAULT_PORT`. *Early warning:* `doctor` reporting a high port long after the original squatter is gone — expected; run `stop` to reset.

### A1.9 Findings closed

- **F-509 (Medium — fixed port 19222, no fallback). Now closed IN FULL.** *Detection* (main plan §2.1-E/Step 3): `doctor` names a foreign occupant. *Fallback* (this amendment): the cold-start spawn and `restart` select the port via `_select_backend_port` — keep the target when free/ours, else `_free_port()` — recording the chosen port in `server.json`; the single-value port flow carries it to every consumer (§A1.2). A hard-coded-port collision is no longer a silent, unrecoverable 120 s outage — it is both **diagnosed** and **survived**.
