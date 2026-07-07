# Stage 2 Plan — M2 Live Edits Take Effect (always-fresh dev backend) + DELETE `hot_reload`

**Pinned SHA** `2267b83d3efda03f93936db2c34ded33aaa0d701` · branch `fix/singleton-version-aware-backend` · **2026-07-02**
**Batch:** {M2} · **Base tree:** post-**M3** + **M1** + **M8 (incl. Amendment A1)** — this plan rebases over those three approved plans and re-anchors by symbol.
**Status:** **APPROVED as amended** (human, 2026-07-02) — cleared for Stage 3. Decisions: **whole-package fingerprint scope** (`src/stealth_chrome_devtools_mcp/**/*.py`); **source-only eviction logging** (version-change eviction stays issue-#14's existing behavior); both orchestrator touch-ups in force (corrected two-amendments note; **empty fingerprint never matches** hardening + its §5.1 test case).

> *Note on the base string (corrected by the orchestrator at cross-review):* the campaign has **two** amendments, both named A1 within their own plans — **plan_M3 Amendment A1** (F-764: `DebugLogger` RLock + re-entrancy pinning test, inside step M3-4, approved at the lens-delta gate) and **plan_M8 Amendment A1** (F-509 auto-port-fallback). Neither touches M2's regions (`debug_logger.py` and the port-selection boundary are outside M2's scope), so this plan's design is unaffected either way.

## The one load-bearing fact that shapes this whole plan

The backend reuse decision is keyed on a **version string** (`_server_version()` → `importlib.metadata.version("stealth-chrome-devtools-mcp")`), which is **frozen at `1.2.0`** (pyproject `version = "1.2.0"`, static, hatchling — no dynamic/scm versioning). This repo is an **editable install** (`pip show` reports it as editable at 1.2.0 — F-120, verified), so **the running backend imports the maintainer's checkout files directly**, yet the version key can never see an in-place source edit. Result: edit `embedded/server.py`, reconnect, and you silently talk to the **pre-edit** process. The codebase's own answer to this — the `hot_reload` tool — **structurally cannot work** (it reloads bare-named sibling modules through a stale `from`-import binding, never touches `server.py` where the 96 tools live, destroys live browser sessions for what it does reload, and returns a success string regardless — F-102/F-121/F-610). The fix is two independent moves: **(A)** extend the reuse key with a cheap, complete **source fingerprint** so a stale-source backend is evicted+respawned by the *existing* eviction path, exactly like a version mismatch; **(B)** **delete** `hot_reload`+`reload_status` so there is exactly one way code changes reach the backend — a fresh spawn (auto on staleness; manual via M8 `restart`).

---

## 1. Scope

### 1.1 Confirmed code anchors at HEAD (re-opened while writing this plan)

**The reuse gate + state record (`embedded/singleton.py`):**

| Anchor @ pinned SHA | What it is | M2 action |
|---|---|---|
| `:34` `STATE_DIR = Path.home()/".stealth-mcp"`, `:40` `SERVER_STATE_FILE = STATE_DIR/"server.json"`, `:41` `DEFAULT_PORT = 19222` | state dir + backend-identity record `{port, version, pid}` + fixed port | reuse; **add** a `source_fingerprint` field to `server.json` (place `SOURCE_ROOT` beside these constants) |
| `:92-103` `_read_server_state() -> dict\|None` | reads `server.json` | **no signature change** — readers ignore unknown keys; a legacy 3-key record simply lacks `source_fingerprint` |
| `:106-114` `_write_server_state(port, version, pid)` | writes `server.json` | **EDIT signature** → `(port, version, pid, source_fingerprint)`; write the 4th key |
| `:117-136` `_find_running_server()` | THE reuse gate: `port` valid `:130`, **version match `:132`**, health `:134` (M1 rewires `:134` TCP→app-probe) | **INSERT** one gate between `:132` and `:134`: `if state.get("source_fingerprint") != _source_fingerprint(): return None` |
| `:218-245` `_start_server_process(port)` | spawns backend; records state at **`:245`** `_write_server_state(port, _server_version(), proc.pid)` | **EDIT the `:245` call** → pass `_source_fingerprint()` as the 4th arg (records the fingerprint of the source the child will run) |
| `:263-269` `_server_version()` | version key (silent `"0.0.0"` fallback; M3 adds a `:268` DEBUG log) | **KEEP intact** (the version key is preserved — issue #14); **add `_source_fingerprint()` adjacent** to it (the two identity inputs live together) |
| `:272-294` `_start_backend_holding_lock(port)` | cold-start under the lock: `if _find_running_server() is not None: return` `:284` → `_clear_stale_backend(port)` `:290` → `_start_server_process(port)` `:291` | **INSERT** the eviction-reason `stealth.proxy` INFO log (once per spawn) before `_clear_stale_backend` |

**The dead hot-reload machinery to DELETE (`embedded/server.py`):**

| Anchor @ pinned SHA | What it is | M2 action |
|---|---|---|
| `:6` `import importlib` | used **only** at the deleted `:2993` (grep-confirmed: `importlib` appears at `:6` and `:2993` only) | **DELETE** (orphaned) |
| `:2974-3009` `@section_tool("debugging")` + `async def hot_reload()` | the lying tool (F-102/F-121/F-610) | **DELETE** |
| `:3012-3038` `@section_tool("debugging")` + `async def reload_status()` | module-status printer; wholly superseded by M8's `doctor` (pid/log/version) | **DELETE** |
| `:51` `SECTION_TOOLS = defaultdict(list)`, `:1215` `SECTION_TOOLS[section].append(func.__name__)`, `:1223` consumer | the section registry both tools register into via `@section_tool` | **no manual edit** — deleting the defs auto-drops them from `SECTION_TOOLS["debugging"]` **and** the FastMCP `mcp` tool set |

**Packaging / install facts (confirm the fingerprint scope):**
- `pyproject.toml:2-3` hatchling; `:7` static `version = "1.2.0"`; `:77-78` `[tool.hatch.build.targets.wheel] packages = ["src/stealth_chrome_devtools_mcp"]`; `:71-72` coverage `source = ["src/stealth_chrome_devtools_mcp"]`.
- `embedded/server.py:26-33` imports siblings by **bare name** (`from browser_manager import …`); `tests/conftest.py:20-21` puts `…/embedded` on `sys.path` — the same way the real entrypoint does. So the package tree under `src/stealth_chrome_devtools_mcp/` **is** the running tree (editable install).

### 1.2 Files to be touched

**Modified source (2):**
- `src/stealth_chrome_devtools_mcp/embedded/singleton.py` — add `SOURCE_ROOT` + `_source_fingerprint()`; extend `_write_server_state` signature + the `_start_server_process:245` call; insert the `source_fingerprint` gate in `_find_running_server`; add the once-per-spawn eviction-reason `stealth.proxy` INFO log in `_start_backend_holding_lock` (reusing M3's `logging` import / `stealth.proxy` logger — **no new import**).
- `src/stealth_chrome_devtools_mcp/embedded/server.py` — delete `hot_reload` (`:2974-3009`), `reload_status` (`:3012-3038`), and the orphaned `import importlib` (`:6`).

**Tests (new + changed) — see §5.**
- New: `tests/test_hot_reload_removed.py` (deletion assertion).
- Extended/migrated: `tests/test_singleton_version_aware.py` (new `TestSourceFingerprintReuse` class + 5 migrated cases + the eviction-log case).

**No other files.** No `cli.py`, no `process_cleanup.py`, no `pyproject.toml`, no docs.

### 1.3 Anchors that predecessors SHIFT (re-anchor by symbol in Stage 3)

M2 runs **fourth** (after M3, M1, M8+A1). None of them changes the *semantics* of my anchors, but they move line numbers:

- **`singleton.py` above `:229` is unshifted.** `_read_server_state:92-103`, `_write_server_state:106-114`, and **`_find_running_server:117-136`** all sit above M3's first edit (`:229` Popen boot-log redirect) → **stable line numbers**. M1 rewrites one line *inside* `_find_running_server` (`:134` `_server_is_healthy`→`_backend_http_ready`); my new gate goes **immediately above that line**, right after the version gate `:132`.
- **`singleton.py` below `:229` drifts down** (M3 boot-log ≈+8…14; M1 adds `_backend_http_ready`/`_probe_backend_status`/`LIVENESS_PROBE_TIMEOUT` near `:47`/`:259`; M8 adds env-scrub; A1 adds `_select_backend_port`/`_port_is_foreign_held` near `ensure_server_running`). So **`_server_version` (my `_source_fingerprint` neighbour), `_start_server_process:245` (my write-call edit), and `_start_backend_holding_lock:284-291` (my log)** are all shifted — re-anchor by symbol.
- **`_start_server_process`'s `kwargs`/`:245` are co-edited:** M3 sets `stdout/stderr`→boot-log, M8 adds `env=`(scrubbed). My edit is the **`_write_server_state(...)` call at `:245`**, not the `kwargs` — locate it by the substring `_write_server_state(` *after* `subprocess.Popen(cmd, **kwargs)`.
- **A1 keeps discovery port-agnostic (binding notice honored):** A1.2 explicitly lists `_find_running_server()` as reading `_read_server_state()["port"]` — **no change**. My fingerprint is computed from source files, independent of the port; I read the recorded port exactly as today and never assume `DEFAULT_PORT`.
- **`server.py` above `:2974` drifts down** (M3's `section_tool` correlation wrapper `:1212-1217`; M10a log lines at `:198/:997/:1226`). Re-anchor the deletions by `async def hot_reload`, `async def reload_status`, and the literal `import importlib` (verify no other `importlib` use before removing — a 1-line grep).

**Stage-3 rule:** locate the reuse gate by `if state.get("version") != _server_version():` inside `_find_running_server`; the state write by `_write_server_state(` inside `_start_server_process`; the eviction log by `_clear_stale_backend(port)` inside `_start_backend_holding_lock`; the deletions by `async def hot_reload` / `async def reload_status`. Do **not** trust the raw `:NNN` numbers after M3/M1/M8 merge.

### 1.4 Complete hot-reload deletion inventory (core deliverable — enumerated exhaustively)

1. **MCP tool `hot_reload`** — `server.py:2974-3009` (decorator + `async def`). DELETE.
2. **MCP tool `reload_status`** — `server.py:3012-3038` (decorator + `async def`). DELETE.
3. **Orphaned import** — `server.py:6 import importlib` (sole use was `:2993`). DELETE.
4. **Dedicated module** — **none exists** (`**/hot_reload*` glob → 0 files; the machinery is inline in `server.py`). Nothing to remove.
5. **Registry / manifest entries** — **none manual**; both register via `@section_tool("debugging")` into `SECTION_TOOLS` (`:1215`) and `mcp.tool` (`:1216`). Deleting the two `async def`s removes them from both automatically. No `__all__`/tool-list names them (grep-confirmed).
6. **Tests** — **none exist** (`grep -rn hot_reload tests/` → 0, per F-121; my own grep confirms). Nothing to delete; instead M2 **adds** a deletion-assertion test (§5).
7. **Helpers** — hot_reload/reload_status call only stdlib (`importlib.reload`, `sys.modules`, `BrowserManager()`/`NetworkInterceptor()`/`DOMHandler()`, `from debug_logger import debug_logger`). No dedicated helper to remove beyond item 3.
8. **Docs mentions → hand to M14 (do NOT edit here):** `MCP_TOOL_TEST_RESULTS.md` references both tools. `README.md` does **not** (repo-wide grep for `hot_reload|reload_status` outside `audit/` = only `server.py` + `MCP_TOOL_TEST_RESULTS.md`). Doc/tool-count reconciliation is **M14 / F-108** — flagged, not touched.
9. **Tool-count impact:** −2 tools (96→94 by the brief's count; the exact surface count is disputed — 90/96/99 — and **M14 (F-108)** owns reconciling it).

### 1.5 Explicit out-of-scope (stated so Stage 3 does not scope-creep)

- **M15** — the `server.json`/storage-model refactor and exporting `DEFAULT_PORT`/`STATE_DIR` (F-722). M2 *adds one field* to `server.json`; it does not relocate constants or reshape the record model.
- **M4** — `server.py` decomposition. M2 **deletes two tools + one import**; it does not extract, split, or touch the `section_tool` registry mechanics (M3 owns the correlation wrapper; M4-Ph1 owns registry formalization).
- **M7 / M11a** — teardown executor / guarded cleanup init.
- **No file-watcher daemon, no auto-restart-on-save.** Staleness is detected at **cold-start discovery** (the fingerprint) and recovered by the **existing** eviction+respawn; M1's watchdog handles mid-session death. A supervisor is explicitly rejected (§2.2).
- **No regression of version-aware cross-version eviction (issue #14).** The version key at `:132` is preserved; the fingerprint **composes** with it (reuse requires **both** match). 15+ existing tests keep proving the version semantics.
- **JS assets** (`embedded/js/**`) are **not** fingerprinted — the key targets Python module code, which is what freezes at import; runtime-read JS does not require a backend respawn. (The brief scopes the key to `src/**/*.py`.)
- **F-765** deadline-poll loops are **not** rewritten (see §8 disposition).
- No drive-by refactors; a mid-implementation discovery becomes a **new finding** (schema carries `modularity|duplication|clarity`), not scope.

---

## 2. Approach + rejected alternatives

### 2.1 Chosen design

**A. A source fingerprint that composes with the version key.**
Add, in `singleton.py` (fingerprint fn beside `_server_version`; `SOURCE_ROOT` beside the state constants):

```python
SOURCE_ROOT = Path(__file__).resolve().parent.parent   # the installed package dir

def _source_fingerprint() -> str:
    """SHA-256 over the package's *.py source, so a backend built from now-stale
    source is not reused. COMPLETE (every module the backend can import),
    STABLE (identical bytes → identical digest — immune to mtime/OneDrive/git
    quirks), CHEAP (~1 MB read+hash per cold-start discovery). Best-effort:
    any OS read error yields "" so a transient hiccup costs one respawn, never
    a crash of discovery."""
    import hashlib
    h = hashlib.sha256()
    try:
        for p in sorted(SOURCE_ROOT.rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            h.update(p.relative_to(SOURCE_ROOT).as_posix().encode("utf-8"))
            h.update(b"\0")
            h.update(p.read_bytes())
            h.update(b"\0")
    except OSError:
        return ""
    return h.hexdigest()
```

Record it at spawn and gate reuse on it (composed with M1's app-probe rewire of `:134`):

```python
def _write_server_state(port, version, pid, source_fingerprint) -> None:
    _ensure_state_dir()
    SERVER_STATE_FILE.write_text(json.dumps(
        {"port": port, "version": version, "pid": pid,
         "source_fingerprint": source_fingerprint}))

# in _find_running_server(), between the version gate and the health gate:
    if state.get("version") != _server_version():
        return None
    if state.get("source_fingerprint") != _source_fingerprint():   # M2
        return None
    if not _backend_http_ready(port):        # ← M1 rewired :134
        return None
    return port

# in _start_server_process(), the recorded state (:245):
    _write_server_state(port, _server_version(), proc.pid, _source_fingerprint())
```

> **ORCHESTRATOR CROSS-REVIEW ADDITION (2026-07-02, folded in at the gate):** an **empty fingerprint never matches**. `_source_fingerprint()`'s `except OSError: return ""` is fail-open at *record* time: if a transient read-lock fires at spawn (recording `""`) and again at discovery (computing `""`), `"" == ""` would **reuse** a possibly-stale backend — a rare false-fresh hole in the exact disease M2 kills. Harden the gate to fail-closed: `fp = _source_fingerprint()` then `if not fp or state.get("source_fingerprint") != fp or not state.get("source_fingerprint"): return None`. Cost: one extra respawn under a transient read error, never silent staleness. §5.1 gains one case: recorded `""` + computed `""` → `_find_running_server() is None`.

Because the gate returns `None`, **the existing eviction path fires unchanged**: `_start_backend_holding_lock`'s `if _find_running_server() is not None: return` falls through → `_clear_stale_backend(port)` (whose own `if _find_running_server()==port` guard is now also False) → M8's `_terminate_backend(port)` → `_start_server_process(port)`. **No second eviction is invented** — a source change is fed into the same, tested machine a version change already uses.

**Scope = the whole installed package tree** (`SOURCE_ROOT.rglob("*.py")`, `__pycache__` excluded): `embedded/*.py` (the bare-imported backend modules the maintainer edits) plus the package root (`__init__.py`, `__main__.py`, launcher `server.py`, `cli.py`). Completeness is the anti-false-fresh guarantee — **any** Python edit changes the digest. The package dir holds only source `.py` (no runtime-written files: `server.json`/clones live under `~/.stealth-mcp`, `__pycache__` excluded), so there is no runtime churn.

**Editable-vs-installed consequence (resolves the brief's question).** This repo is an **editable install** (F-120, `pip show`), so `SOURCE_ROOT` = the checkout = what the backend imports; the fingerprint detects the maintainer's edits **directly**. The fingerprint's true invariant is *"has the code the backend actually runs changed."* Under a hypothetical **non-editable** install it would hash the site-packages **copy** (exactly what Python imports) — which never changes without a reinstall, so it would correctly report "not stale" (and in that mode source edits genuinely don't take effect until reinstall). The key is correct in **both** modes; it never false-alarms.

**B. Delete `hot_reload` + `reload_status` (rip out, don't deprecate — 0 users).** Per §1.4. After this, the **only** way edits reach the backend is a fresh process — auto on staleness (A), manual via **M8 `restart`**. This is itself a **conventions** win: the second, broken way is gone.

**C. Log the eviction reason through M3's spine.** Once per spawn, in `_start_backend_holding_lock` (the single locked cold-start site — not inside the thrice-called gate, which would triple-log), emit via the `stealth.proxy` logger M3 already configured:

```python
    if _find_running_server() is not None:
        return
    state = _read_server_state()
    if (state is not None
            and state.get("version") == _server_version()
            and state.get("source_fingerprint") != _source_fingerprint()):
        logging.getLogger("stealth.proxy").info(
            "backend stale (source changed), evicting")
    _clear_stale_backend(port)
```

The maintainer now SEES *why* a fresh backend spawned (M3's `proxy-<pid>.log`, correlation-stamped). The gate **logic** stays single-homed in `_find_running_server`; this is a separate one-line diagnostic that re-reads state cheaply (dedup lens: a log-reason probe is not a second gate).

### 2.2 Rejected alternatives

1. **`mtime`-max instead of a content hash** (`max(st_mtime_ns)` over the tree). *Rejected* — reintroduces the exact bug we are killing: it is **false-fresh** when an edit sets mtime *backwards* (git checkout of an older revision, branch switch, `touch -d`, editor restore) — the max does not advance, so a stale backend is silently reused. It is also **false-stale-churn** when a git operation stamps *all* files to checkout-time despite identical content — a needless respawn that kills live browser sessions — and OneDrive sync can rewrite mtime metadata without a content change. A content hash is **byte-exact**: zero false-fresh, and identical bytes across a branch switch → identical digest → warm reuse preserved. The ~10 ms read cost (priority 3, order-of-magnitude) is dominated by correctness (priority 1). mtime's only win is avoiding the read — not worth the correctness hole, *especially on this OneDrive checkout*.
2. **An env-var "dev mode" gate** (`STEALTH_MCP_DEV=1` → always fresh). *Rejected hard, per the maintainer's standing preference* (persistent memory: reject config-knob workarounds; ship universal, default-on behavior). The auto-detect fingerprint costs ~10 ms and needs no opt-in, so an env knob buys nothing but a flag to forget — the failure mode is "edited, reconnected, still stale, and I didn't set the flag," i.e. the original bug. Default-on for every session is strictly better and is what the memory demands.
3. **Always-fresh every cold start (never reuse).** *Rejected* — kills warm reuse for **unchanged** source: every new session would evict the running backend and **destroy all live browser sessions** even when nothing changed (the backend is shared across sessions). The fingerprint's *equality* check is precisely what preserves warm reuse; unconditional freshness throws that away.
4. **Repair `hot_reload` instead of deleting it** (the BRIEF rules this out; recorded here as required). *Rejected* — a correct in-process repair would have to re-exec `server.py`'s 4,207 lines, re-register all 96 `@section_tool`s, rebind every stale `from`-import, and migrate the live `BrowserManager`/`NetworkInterceptor`/`DOMHandler` state without dropping open browsers or the idle-reaper task. That *is* "restart the process." Since the correct repair converges on a fresh process — which (A)+`restart` already deliver — repair is pure cost plus a permanently confusing surface. Deletion is strictly simpler and removes a tool that actively lies.
5. **A file-watcher / supervisor that auto-restarts on save.** *Rejected* — heavyweight for a single-user local tool and a *second* recovery mechanism competing with M1's watchdog. Cold-start staleness detection (A) + M1's liveness watchdog + M8's manual `restart` already cover "new code" and "dead backend" without a daemon.
6. **Hash only `embedded/` (narrow the scope).** *Considered, rejected* — marginally less over-eager (a `cli.py`-only edit wouldn't respawn the backend) but it **couples the fingerprint to the import graph** ("which dir the backend imports from"), a fragile distinction that risks false-fresh if the layout shifts (M4 will move files). Whole-package hashing is one simple rule; the cost is one harmless extra respawn on the rare package-root-only edit.

---

## 3. Sequencing (smallest-first, each independently verifiable; deletion and key separately revertible)

> Baseline before M2: `.venv\Scripts\python.exe -m pytest -m "not integration" -q` → **402(+M3+M1+M8) passed**, coverage **≥ 39**. Pinning tests written **before** each change (superpowers TDD). `uv run` is **BROKEN** in this checkout — always the venv python. One checkpoint commit per step.

**Step M2-1 — DELETE `hot_reload` + `reload_status` + orphaned `import importlib`.** *(Independent of the key; the smallest, highest-relief move — removes the actively-lying tool immediately.)*
- **Pinning first** (`tests/test_hot_reload_removed.py`): `import server`; assert `"hot_reload" not in server.SECTION_TOOLS["debugging"]` and `"reload_status" not in …`, and `not hasattr(server, "hot_reload")` / `not hasattr(server, "reload_status")`. (Written against HEAD it **fails**; after deletion it passes — that inversion is the pin.)
- Delete the two `async def`s + decorators; remove `import importlib` after confirming (grep) no other use.
- *Verify:* `.venv\Scripts\python.exe -m pytest tests/test_hot_reload_removed.py tests/test_server_call_conventions.py tests/test_server_entrypoint.py -q` green; then full suite green + coverage ≥ 39.

**Step M2-2 — Source-fingerprint reuse key (+ version-aware test migration).** *(The fix. Independent of M2-1.)*
- **Pinning first** — add `TestSourceFingerprintReuse` to `tests/test_singleton_version_aware.py` (cases in §5.1) and migrate the 5 schema-touching cases (§5.2).
- Add `SOURCE_ROOT` + `_source_fingerprint()`; extend `_write_server_state`; edit the `_start_server_process:245` call; insert the `source_fingerprint` gate in `_find_running_server`.
- *Verify:* `.venv\Scripts\python.exe -m pytest tests/test_singleton_version_aware.py tests/test_singleton_fast_handshake.py tests/test_proxy_backend_death.py -q` green; then full suite green + coverage ≥ 39.

**Step M2-3 — Log the source-change eviction (`stealth.proxy` INFO).** *(Depends on M2-2's fingerprint comparison.)*
- **Pinning first** — a caplog case (§5.1) asserting the exact line on a source-change eviction.
- Insert the once-per-spawn INFO log in `_start_backend_holding_lock`.
- *Verify:* the log test green; then full suite green + coverage ≥ 39.

M2-1 is independent of M2-2/M2-3; M2-3 depends on M2-2. Each is one commit. (M2-2+M2-3 may share a PR — the log is 3 lines — but stay separate commits so the log is revertible alone.)

---

## 4. Breaking changes

**0 users — external-compatibility N/A.** For the record:
- **Two MCP tools removed from the surface:** `hot_reload`, `reload_status` (both `@section_tool("debugging")`). Any caller reaching for them now gets "unknown tool" — correct, since both were broken/redundant. Tool count −2 (M14/F-108 reconciles the absolute number).
- **`server.json` gains a `source_fingerprint` field** → `{port, version, pid, source_fingerprint}`. A backend recorded before M2 (3-key legacy record) has no fingerprint → treated as stale → **one** respawn to upgrade (identical, safe semantics to a version-unknown legacy backend under issue #14). All other `server.json` readers (`_read_server_state`, `_clear_stale_backend`, A1's `_select_backend_port`, M8's `stop`/`restart`, M1's `_probe_backend_status`) ignore the new key — no reader change.
- **The backend respawns more often for the maintainer** — on *every* source edit. **That is the point** (fresh code every session), and it is the same eviction blast radius the version key already carries.
- **Tool-count docs drift** (`MCP_TOOL_TEST_RESULTS.md`) — **M14 owns** (F-108).

---

## 5. Test strategy

All hermetic (no real backend, no Chrome — stay in `not integration`). Pinning tests are written and shown **red→green in the same step** as the code they pin. **The fingerprint is stubbed, not driven by real edits** (per brief), except the two dedicated stability/sensitivity cases that point `SOURCE_ROOT` at a tmp tree.

### 5.1 New behavior-pinning tests (written BEFORE the change)

In `tests/test_singleton_version_aware.py`, new class `TestSourceFingerprintReuse` (reuses the `isolated_state` fixture; also stubs M1's `_backend_http_ready` per M1 §5.3):
1. **Stale fingerprint → evicted** (the fix): write `server.json {port, version:"1.2.1", pid, source_fingerprint:"OLD"}`; `monkeypatch _server_version→"1.2.1"`, `_source_fingerprint→lambda:"NEW"`, `_backend_http_ready→True` → `assert _find_running_server() is None`.
2. **Matching version + fingerprint + ready → reused** (protects warm-start perf): same but `source_fingerprint:"SAME"` and `_source_fingerprint→"SAME"` → `assert _find_running_server() == port`.
3. **Compose with version (both must match):** `source_fingerprint` matches but `version` differs → `None` (version gate); and version matches but fingerprint differs → `None` (fingerprint gate). Proves reuse needs **both**.
4. **Stability (anti-churn):** `assert _source_fingerprint() == _source_fingerprint()` on the real tree with no edits (guards false-stale respawn loops).
5. **Sensitivity:** `monkeypatch SOURCE_ROOT` → a `tmp_path` with a fake `a.py`; capture the digest; rewrite `a.py`; `assert` the digest changed (proves it detects source edits). A second assert: adding `b.py` also changes it (completeness).
6. **Eviction log** (Step M2-3): drive a source-change eviction through `_start_backend_holding_lock` with `_exclusive_lock`/`_clear_stale_backend`/`_start_server_process`/`_wait_for_server` stubbed (mirroring `TestStartBackendHoldingLockEvicts`); `caplog.at_level(INFO, "stealth.proxy")` → assert `"backend stale (source changed), evicting"` present.

Deletion assertion — `tests/test_hot_reload_removed.py` (Step M2-1): §3.

### 5.2 Existing tests that MUST change (enumerated) and why

All in `tests/test_singleton_version_aware.py`; each layers the **same one-more-stub + one-more-field** idiom M1 already applied (`_backend_http_ready`) — a mechanical migration, semantics unchanged:
- **`test_reuses_backend_with_matching_version` (:52)** — add `source_fingerprint` to the written `server.json` **and** `monkeypatch _source_fingerprint→` that value (else the new gate returns `None`).
- **`test_ignores_unhealthy_matching_version` (:91)** — add a **matching** fingerprint so the case still reaches the health gate it is named for (test integrity; otherwise it would short-circuit at the fingerprint gate and pass for the wrong reason).
- **`test_write_then_read_roundtrips` (:104)** — new `_write_server_state(port, version, pid, source_fingerprint)` signature; expected dict is now 4 keys.
- **`test_written_state_makes_backend_reusable` (:112)** — 4-arg `_write_server_state` + matching `_source_fingerprint` stub.
- **`test_start_server_process_records_current_version_and_pid` (:121)** — extend to assert `state["source_fingerprint"]` is recorded (rename → `…_version_pid_and_fingerprint`).

**Unaffected (no change):** the identity/pid/`_clear_stale_backend`/`_start_backend_holding_lock` cases that stub `_find_running_server` directly; `TestStaleBackendEvictionEndToEnd` (version `"0.0.0-old"` mismatches first); A1's `tests/test_singleton_port_fallback.py`; **`test_singleton_fast_handshake.py`** (patches `_find_running_server`'s *return*, above the gate — the fingerprint never runs, and its `elapsed < 0.5` handshake assertion is untouched because no real fingerprint is computed there).

### 5.3 Coverage

Deleting `hot_reload`+`reload_status`+`import importlib` removes ~63 **uncovered** lines from `server.py` (F-121: zero tests exercised them) → this *raises* the covered ratio (uncovered lines leave the denominator; the numerator is unchanged). The new `singleton.py` code (`_source_fingerprint`, the gate, the log) is fully covered by §5.1. **Net coverage rises; the `fail_under=39` gate is not threatened.** M2 does not ratchet the gate (a separate hygiene concern).

---

## 6. Rollback + checkpoint commit boundaries

- **Branch** `audit/fixes-2026-07-02`, **serial after M8+A1** (base = post-M3+M1+M8+A1). Stage-3 discipline: pinning tests before the change; **full suite green + coverage ≥ 39 at every checkpoint**; deviation → **stop and report to Fable**.
- **One commit per step:** `M2-1 delete hot_reload + reload_status (+orphaned importlib) + deletion test` · `M2-2 source-fingerprint reuse key (+version-aware test migration)` · `M2-3 log source-change eviction via stealth.proxy`.
- **Separately revertible, as the brief requires:** **M2-1 (deletion)** and **M2-2 (key)** are independent — revert either alone. **M2-3 (log)** depends on M2-2; revert it alone if the log is noisy. Reverting M2-2 restores pure version-string keying (the pre-M2 bug) but leaves the deletion intact.
- **PR shape:** one M2 PR (three commits) is simplest; a clean split is `{M2-1}` (deletion) and `{M2-2, M2-3}` (key + its log).

---

## 7. Risk (blast radius, worst case, early-warning signs)

1. **False-stale churn — the fingerprint changes when source didn't → respawn on every cold start → live browser sessions destroyed.** *Blast radius:* the shared backend and every session's live tabs (same as version-mismatch eviction). *Mitigations:* content hash is **byte-stable** (identical bytes → identical digest — immune to mtime/OneDrive/git); `__pycache__` and non-`.py` excluded; deterministic `sorted()` order; package dir holds no runtime-written files. *Worst case:* a transient OS read-lock (OneDrive mid-sync) trips the `except OSError: return ""` path once → **one** spurious respawn, then self-heals (the next spawn records `""` too, or the lock clears). *Early warning:* repeated `"backend stale (source changed), evicting"` in `proxy-<pid>.log` with **no** actual edit + rapid respawns in `backend-boot.log` timestamps.
2. **False-fresh — the fingerprint fails to change when source did → the original bug persists.** *Mitigation:* content hash over the **whole** package `.py` → zero false-fresh for any content change (the mtime hole is designed out). *Early warning:* the exact F-504 symptom — edit, reconnect, still old behavior — but now **diagnosable**: the *absence* of the eviction log in `proxy-<pid>.log` says "the key did not fire," pointing straight at the fingerprint rather than a phantom code bug.
3. **Cold-start latency from the hash.** `_find_running_server` runs up to ~3× per locked spawn; each computes ~1 MB read+hash (<~10 ms). *Mitigation:* negligible (perf is priority 3, order-of-magnitude); no caching added (avoids premature complexity — a per-process memo is available if ever needed). *Early warning:* `proxy-<pid>.log` discovery timestamps >100 ms.
4. **OneDrive / Windows path quirks on this exact checkout.** Content-hash sidesteps mtime entirely — the *only* OneDrive exposure is a transient read-lock (→ risk 1's one-respawn path). CRLF: if `git autocrlf` rewrites line endings on checkout, bytes change → one respawn (the on-disk file genuinely changed — harmless). *Early warning:* a respawn immediately after a `git checkout`/branch switch with no manual edit.
5. **Shared-backend eviction blast radius (inherent, accepted).** Evicting kills live browser sessions on the backend — but M2 **invents no new eviction**; it feeds a correct new signal into the *existing, version-aware-tested* path. "Code changed → fresh backend → live sessions reset" is the intended contract, identical to issue #14's version-change behavior. *Early warning:* none beyond risks 1/4 (a respawn the maintainer didn't expect).

**Overall worst case:** a transient read hiccup causes one extra respawn, or a package-root-only edit respawns the backend — both strictly-bounded, both visible in M3's logs, neither a functional regression. The fix's failure modes are *more respawns*, never *silent stale code* (which was the disease).

---

## 8. Findings closed

- **F-102 (High — `hot_reload` reloads through a stale `from`-import binding, reports success, never touches `server.py`).** Closed by **DELETION (M2-1):** the tool that "instantiates the original, pre-reload `BrowserManager` class, silently … reporting success while not actually picking up any class-level code changes" is gone; the one way to apply edits is a fresh backend.
- **F-121 (High — `hot_reload` never reloads `server.py`/`cdp_function_executor`, destroys live sessions, untested).** Closed by **DELETION (M2-1).** Its module-status sibling's purpose is fully served by **M8's `doctor`** (pid/log-path/version).
- **F-610 (High — `hot_reload` replaces singletons with empty instances, orphaning open browsers).** Closed by **DELETION (M2-1).**
- **F-206 (High — reuse keyed on `_server_version()` metadata, not source).** Closed by the **fingerprint KEY (M2-2):** a source change now makes `_find_running_server` return `None`, so `_start_backend_holding_lock` evicts+respawns via the existing path — the same treatment a version mismatch already gets.
- **F-120 (High — version METADATA frozen at 1.2.0 on an editable install; edits invisible to `_server_version`).** Closed by **M2-2:** the reuse key now reflects the source **bytes** the backend runs, not the frozen distribution version. The version gate is preserved and composes.
- **F-504 (High — edits silently do not take effect; the project's own saved memory documents the pain).** Closed by the combination: **KEY (M2-2)** makes the edit take effect on the next session, **DELETION (M2-1)** removes the false "fixed it" signal, and the **eviction LOG (M2-3)** makes the respawn visible — ending the "phantom bug" chase. Composes with **M8 `restart`** (manual escape hatch, usable immediately) and **M3's logs** (F-503 backend visibility).
- **F-765 (Medium — two wait-strategy idioms; no shared `poll_until`). DISPOSITION: LEFT UNTOUCHED (routed opportunistically, condition not met).** M2 does **not** rewrite any of the three deadline-poll loops it names (`_wait_for_server:248-256`, `_await_backend_http:316-358`, the port-release wait now inside M8's `_terminate_backend`). The fingerprint is a pure hash (no polling); the reuse-gate insertion, the `_write_server_state` signature, the deletions, and the one-line log touch **none** of those loop bodies. Per the LENS directive ("M2 += F-765 **poll_until ONLY if touching those loops** — otherwise leave it, no drive-by"), the condition is not met, so **F-765 stays open** for whoever next rewrites those loops (its natural home per `LENS_DELTA.md`). No `poll_until` helper is introduced — introducing one here would be an out-of-scope drive-by *and* risk a second wait idiom.

---

## The four lenses applied to this design (where each shaped a choice)

- **Modularity** — `_source_fingerprint` is a self-contained pure function (one constant + a hash loop), understandable in isolation with zero new coupling; the reuse decision stays entirely inside `_find_running_server`; the eviction log is one line at the spawn site. No cross-module entanglement added.
- **Deduplication** — the fingerprint **comparison lives in exactly one place** (the gate); `server.json` stays the **single** source of truth (a field is added, not a second artifact); the **existing** eviction path is reused (no second terminate/respawn). The log-reason re-read is a diagnostic, deliberately *not* a second gate. *(Shaped:* choosing to feed the existing eviction rather than add a fresh-spawn branch.)*
- **Clarity** — `SOURCE_ROOT`, `_source_fingerprint`, and the `source_fingerprint` field are self-describing; a light model reads the gate as "reuse needs version **and** fingerprint." Deleting `hot_reload` removes a name that **lied** about its behavior. *(Shaped:* the fingerprint fn sits beside `_server_version` so the two identity inputs read as a pair.)*
- **Conventions** — after M2 there is **exactly one** way code changes reach the backend (a fresh spawn: auto on staleness, manual via M8 `restart`); deleting `hot_reload` removes the *second, broken* way — the headline conventions win. The new field follows the existing `server.json` record shape; the test migration reuses M1's "stub one more helper + write one more field" idiom; the log reuses M3's `stealth.proxy` logger. **No new second-way is introduced anywhere** (which the ADDENDUM defines as a defect) — the `mtime`/env-gate/supervisor alternatives were rejected partly on exactly this ground.
