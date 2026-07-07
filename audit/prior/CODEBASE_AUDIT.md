# Codebase Audit — Gotchas, Performance, Maintainability

Handoff doc from a full-codebase sweep (4 parallel read-only auditors over ~20
modules, findings then verified against the code by the orchestrator). Written
so a fresh session can pick up without re-deriving anything.

**Branch:** `fix/singleton-version-aware-backend` (PR #19).
**Last committed + pushed + green:** `9778218` (clone-output-dir per-user fix).
**Date:** 2026-07-02.

Threat model reminder: single-user **local** tool; dominant risk is reliability
(damaging the user's own data / automations), not external attackers. The
machine also runs ~283 unrelated Chrome automations — **never touch a Chrome
this tool did not spawn** is a hard constraint.

---

## 0. UNCOMMITTED work-in-progress currently on disk

One confirmed bug was fixed during the sweep but **not yet committed**:

- **NEW file** `tests/test_server_call_conventions.py` — 2 static AST tests
  (both passing).
- **EDITED** `src/stealth_chrome_devtools_mcp/embedded/server.py` — removed a
  bogus `await` in front of `response_handler.handle_response(...)` at 3 return
  sites (added a one-line comment above each). See finding **B1** below.
- Full unit suite after the edit: **402 passed, 24 deselected**.
- **Not committed, not pushed.** Decide whether to fold this into the next
  commit or keep separate.

Verification commands (Windows; note the env quirks):
- Unit tests: `./.venv/Scripts/python.exe -m pytest -m "not integration" -q -p no:cacheprovider`
  (system `python` is 3.14 and lacks deps — must use the venv interpreter).
- `uv` must be run as `rtk proxy uv ...` (bare `uv run` fails "Failed to
  canonicalize script path" under the rtk hook).
- Git raw output: `rtk proxy git ...` (the rtk hook otherwise masks output).
- Commit trailer required: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- **Do not run real-browser tests locally** (the "don't disturb other Chromes"
  constraint) — keep new tests hermetic / no-browser, or AST/static.

> Line numbers below were accurate at audit time but **will drift** as edits
> land — search by symbol, not line.

---

## A. FALSE ALARMS — verified NOT real (do not re-chase)

The auditors flagged these with high confidence; direct code reading disproved
or substantially downgraded each. Recorded so they don't get re-proposed.

- **A1 — "substring `chrome` name-match kills foreign Chromes."** FALSE. The
  actual kill gate (`_get_browser_pids_for_profile`, `process_cleanup.py:311`)
  requires an **exact `--user-data-dir` equality** against the tool's own clone
  dirs. `_is_browser_process_name` (substring match, `:95`) is only a cheap
  prefilter to decide which processes to read `cmdline` for. A foreign Chrome on
  a different profile dir never matches. **The guarantee holds.**
- **A2 — "network data never cleaned up on instance close."** FALSE.
  `clear_instance_data()` (`network_interceptor.py:594`) is called from
  `close_instance` (`server.py:1471`) and purges the instance's request/response
  entries. (Intra-session growth within *one long-lived* instance is still real
  — see C1.)
- **A3 — "fire-and-forget storage sweep swallows exceptions."** DOWNGRADED.
  `_spawn_background_sweep` (`server.py:763`) doesn't check `task.result()`, but
  `_run_storage_sweep` (`:755`) wraps its body in try/except and logs, so
  exceptions don't vanish. Adding a result-checking done-callback is cheap
  defense but low priority.
- **A4 — "`time.sleep(0.15)` in cleanup stalls the event loop."** FALSE for the
  loop: `_cleanup_profile_dir` (`process_cleanup.py:449`) runs via
  `asyncio.to_thread`, so the sleep blocks a worker thread, not the loop.
- **A5 — "1-second PID-reuse tolerance too loose."** FALSE. The 1.0s window
  (`process_cleanup.py:398`) compares a stored vs actual `create_time` for the
  *same* PID; a recycled PID belonging to a different process differs by far
  more than 1s. Fine as-is.

---

## B. REAL — small, safe, behavior-preserving (fix via TDD)

- **B1 — `await` on a sync method broke 3 tools. [FIXED, uncommitted]**
  `response_handler.handle_response` is **synchronous** (returns dict/list), but
  three tools did `return await response_handler.handle_response(...)`:
  `extract_element_assets`, `extract_related_files`, `discover_object_methods`.
  `await` on a dict raises `TypeError: object dict can't be used in 'await'
  expression` — the CDP work completed, then the return line threw **every
  time**. The other 5 call sites are correct.
  *Fix applied:* dropped `await` at all three sites; added
  `tests/test_server_call_conventions.py` (AST guard: no `await` may wrap
  `handle_response`; plus a non-vacuity check that call sites exist). RED→GREEN
  confirmed. **Confidence: CONFIRMED (verified + tested).**

- **B2 — Event-loop stall: profile copy not offloaded.**
  `server.py:1114` calls `_copy_profile_tree(source, explicit, clone_root, ...)`
  **synchronously inside async** `_resolve_profile_selection`. It does
  `os.walk` + file copies (hundreds of MB possible) → freezes the entire shared
  backend for seconds during an explicit-profile spawn. Fix: wrap in
  `await asyncio.to_thread(_copy_profile_tree, ...)`. Also has **no try/except**
  — a failed/partial copy returns a profile path pointing at corrupt/empty data
  and the caller reports success (`:1116`). Consider wrapping + error return.
  *TDD note:* test behaviorally by monkeypatching `_copy_profile_tree` to block
  and asserting a concurrent coroutine still makes progress. **CONFIRMED.**

- **B3 — `"timestamp": "now"` literal.**
  `comprehensive_element_cloner.py:328` sets `"timestamp": "now"` (a literal
  string, not a real time) in every `extract_complete_element` result. All
  clones report the same fake timestamp. Fix: `datetime.now(timezone.utc)
  .strftime("%Y-%m-%dT%H:%M:%SZ")` (repo convention). *TDD note:* browser-coupled
  — extract the timestamp into a tiny pure helper and test that, or assert the
  format on a constructed result dict. **CONFIRMED.**

- **B4 — Naive `datetime.now()` in cloner/file metadata.**
  `file_based_element_cloner.py` (~lines 70, 177, 239, 300, 360, 420, 482, 538)
  and `response_handler.py` (:89, :97) use naive `datetime.now()` / `.isoformat()`.
  Repo convention is timezone-aware UTC with `Z` (see the marker-format guard
  test added earlier this session). Consistency + correct ordering. **CONFIRMED.**

- **B5 — Redundant `mkdir` per response (minor).**
  `response_handler.handle_response` re-`mkdir`s the clone dir on every spill
  though `__init__` already did. Negligible for a local tool; fold in if
  touching the file anyway. **PLAUSIBLE / low.**

- **B6 — Dead assignments.** `proxy_utils.py` has `netloc = host` immediately
  overwritten by `netloc = f"{host}:{port}"` at ~:64, :102(-ish in
  redact_launch_arg), and the paired block. Harmless but confusing; delete the
  dead line. **CONFIRMED (cosmetic).**

- **B7 — `debug_logger` import-time-in-function.** `import time` appears inside
  `__init__`/methods (`debug_logger.py:43, 317, 357`) instead of module top.
  Cosmetic. **CONFIRMED (cosmetic).**

- **B8 — `debug_logger` buffer reassignment possibly outside lock.**
  `clear`/exception path (~`debug_logger.py:289`) reassigns `self._errors = []`
  etc. — verify it's under `self._lock`, since buffers are touched from
  `asyncio.to_thread` worker threads. Low severity (debug only). **PLAUSIBLE —
  needs a look.**

---

## C. REAL — but a behavior / semantics DECISION (get user sign-off)

- **C1 — Unbounded body store in a long-lived instance (biggest memory risk).**
  `network_interceptor._responses` / `_requests` (`network_interceptor.py:20-23`)
  store every captured request and **response body** for the life of an
  instance. `_on_response` (`:159`) fetches + decodes bodies via
  `get_response_body` and keeps them. A multi-hour heavy-traffic session on one
  instance can reach hundreds of MB (e.g. 10k responses × ~50KB ≈ 500MB).
  Cross-instance cleanup on close is fine (A2); the gap is *within* a session.
  **Decision needed:** cap (LRU/ring, e.g. last N requests or a byte budget),
  or fetch bodies on demand instead of storing, or make body capture opt-in.
  Also confirm whether interception is default-on or explicitly enabled (changes
  severity). **CONFIRMED (mechanism); fix is a behavior change.**

- **C2 — `eval()` per network event for custom hook conditions.**
  `dynamic_hook_system.py:123` `eval(condition_code, namespace)` runs on every
  matching event; the condition string is not pre-compiled (the hook *function*
  IS compiled once at `:68`, only the `matches()` custom-condition path re-evals).
  Precompile with `compile(...)` at hook registration and `eval` the code object.
  Niche (only hooks using `custom_condition`), but a clean, low-risk opt.
  **CONFIRMED.**

- **C3 — pid-file lock is non-blocking and proceeds unlocked on contention.**
  `_file_lock` (`process_cleanup.py:214`) uses `LK_NBLCK` / `LOCK_NB` and, on
  `OSError`, `yield`s anyway (runs the critical section **without** the lock).
  Under the version-gated **singleton** there's normally one writer, so real
  contention is rare — but the load→modify→save sequence
  (`_recover_orphaned_processes` / `track` / `untrack`) is not atomic across
  processes. A naive "blocking lock with timeout" fix could introduce a *hang*,
  so this needs thought (atomic temp-file rename is probably safer than blocking
  locks). **CONFIRMED (low real-world probability under singleton).**

- **C4 — Thinnest spot in the kill-safety story.**
  Non-recovery path of `_kill_processes_for_metadata` (`process_cleanup.py:412`):
  `if not pids_to_kill and isinstance(fallback_pid, int): pids_to_kill =
  {fallback_pid}` — kills the bare stored PID with **no** create_time/identity
  re-check (unlike the recovery path at `:390`). Only reached when no live
  process currently uses the profile dir, and normal close happens moments after
  use, so PID recycling in that window is unlikely — but it's the one place the
  "never touch foreign Chromes" guarantee is thinner than it could be. Consider
  applying the same create_time guard the recovery path uses. **CONFIRMED (low
  probability).**

---

## D. ARCHITECTURAL — large, review-heavy (needs sign-off; pairs with the
##     already-deferred god-module split of `embedded/server.py`)

- **D1 — Cloner sprawl (~850–1300 LOC removable).** Four overlapping modules:
  `element_cloner.py` (base, 7 `extract_*` methods), `comprehensive_element_cloner.py`
  (extends base), `file_based_element_cloner.py` (wraps BOTH base + comprehensive,
  adds 7 near-identical `extract_*_to_file` wrappers ≈ 70 lines each ≈ 490 lines
  of boilerplate), `progressive_element_cloner.py` (alt strategy). Also ~13/15/5
  triple-quoted JS templates duplicated across them with slight drift (a fix to
  one won't reach the others). Plan: parametrize the `extract_*_to_file` wrappers
  into one `_extract_and_save(method, key, metadata)`; fold `comprehensive` into
  the base as a strategy; extract shared JS into `resources/*.js` loaded at
  runtime; confirm `progressive` is still used or deprecate it. **CONFIRMED
  duplication; PLAUSIBLE on exact merge shape (watch import cycles —
  file_based imports both base and its extension).**

- **D2 — Config scattered across 13 `getenv` sites** (`server.py:55-257` region)
  with defaults set at each parse site, plus duplicated state-dir resolution
  (`_master_profile_dir()`/`_clone_root_dir()`/`_master_snapshot_dir()` repeated
  in 4+ functions). Centralize into a single `Config` dataclass built once at
  startup + a `_load_profile_dirs()` helper returning a dataclass. Cuts the
  maintenance surface materially and removes stale-default risk. **CONFIRMED.**

- **D3 — 96-tool boilerplate.** `_with_cdp_timeout(...)` is repeated at ~59 call
  sites and most tools repeat the same instance-lookup/validation preamble.
  Extract a `@require_instance` / `@cdp_timeout` decorator so tools become thin
  handlers. Best done together with the god-module split so boundaries move once.
  **CONFIRMED (counts approximate).**

- **D4 — Import-time side effects.** Several modules do work at import:
  `singleton.py:51` `STATE_DIR.mkdir(...)`; `cli.py:35`
  `os.environ.setdefault("STEALTH_MCP_NO_AUTO_RECOVERY", "1")`; `server.py`
  top-level `sys.path.insert`; the module-global `ResponseHandler()` /
  `FileBasedElementCloner()` / `ProcessCleanup()` singletons. Mostly benign and
  partly intentional, but they make importing the package non-inert (fails
  loudly if a dir is read-only, mutates global env). Consider moving into an
  explicit init/`main()`. Pairs with the bare-import migration (#6, deferred).
  **CONFIRMED.**

- **D5 — `sys.path` anchor inconsistency.** `comprehensive_element_cloner.py:23`
  appends `Path(__file__).parent` (embedded/) while `file_based_element_cloner.py:15`
  appends `.parent.parent` (package root); both then do bare `from element_cloner
  import ...`. Works today via conftest/entrypoint but is fragile. Fold into the
  bare-import migration. **CONFIRMED.**

---

## E. Duplicated dispatch logic (maintainability, medium)

- Header dict→CDP `HeaderEntry` conversion is duplicated across
  `response_stage_hooks.py` (~:38, :52, :101, :117) and `dynamic_hook_system.py`
  (~:307-320). Extract one `_headers_to_cdp_entries(headers)` helper.
- `hook_learning_system.get_request_object_documentation()` hardcodes field docs
  that duplicate the `RequestInfo` dataclass — generate from
  `RequestInfo.__dataclass_fields__` instead so docs can't drift.
- `network_interceptor._on_response` body-capture `except Exception: pass`
  (`:171`) silently drops body-capture failures with no debug log — add a
  `log_debug` so missing bodies are expl\ainable. (The `ConnectionError/RuntimeError`
  branch above it already logs.)

---

## Suggested execution order (for the next session)

1. **Safe bug batch (TDD, one `fix:` commit):** B1 (already done, uncommitted) +
   B2 (`to_thread` the copy) + B3 (`"now"` timestamp) + B4 (naive datetimes).
   No behavior change; all hermetic/AST-testable except B2 (behavioral test via
   a blocking monkeypatch).
2. **Then decide C1** (interceptor memory cap) — needs a cap value / opt-in call.
3. **Architectural (D1–D5)** only with explicit sign-off; best sequenced with the
   god-module split so module boundaries move once.

Nothing in A/B changes the security posture; C3/C4 are low-probability hardening
of an already-sound "don't touch foreign Chromes" guarantee (gated by exact
`--user-data-dir` match, per A1).
