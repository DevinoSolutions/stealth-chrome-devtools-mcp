# Foundational Audit — stealth-chrome-devtools-mcp

**Pinned SHA:** `2267b83d3efda03f93936db2c34ded33aaa0d701` · branch `fix/singleton-version-aware-backend` · 2026-07-02
**Baseline:** tests GREEN ×2 (402 passed, 0 flaky, coverage 40.86%/40.89% vs gate 39) · **Hallucination meter (deletion counter): 0** — every one of 79 findings carried a verbatim quote that survived re-verification.
**Method:** 10 independent finders (2 hotspot scans, 5 deep dives, change-trace, 3am-incident, onboarding, pre-mortem, prior-claim verification) → 8 fresh quote-verifiers → 2 fresh Opus adversarial reviewers (kill/downgrade mandate) → this synthesis. Priorities, strict: (1) maintainability (2) operability (3) performance.

---

## Executive summary (verdict)

The tool works and — refreshingly — its 402-test suite is genuinely green and non-flaky, so it is *safe to run*. It is not *safe to change or to operate under failure*. The whole system funnels through one detached, per-user backend that is a single point of failure, and every layer of that backend is blind: its only liveness signal is a bare TCP connect (so a wedged backend reads "healthy" forever — the one surviving **Critical**, F-301/F-501), its stdout/stderr are wired to `DEVNULL` with no file logger anywhere (so the process that matters cannot produce a log — F-303), and there is no CLI verb to stop or restart it (so recovery is a manual, undocumented PID hunt — F-302). Live code edits silently don't take effect, because backend reuse is keyed on the package *version string* and the one `hot_reload` tool skips `server.py` and orphans live browsers — this is your own saved pain, now confirmed four independent ways (F-206/F-504/F-102/F-121). Maintainability is taxed by a 4,207-line `server.py` god object holding all 96 tools plus ~700 lines of unrelated eviction logic (F-101/F-505/F-612), and by five overlapping element-cloner implementations, one of which received a hang-avoidance fix that was never propagated to its four siblings (F-142) — a live latent bug. The tests that exist protect the wrong surface: 0 of 96 tool bodies are unit-tested (F-105), so the recent `await` bug shipped silently and was patched reactively. Adversarial review killed nothing (every finding was factually correct) but downgraded 18 severities, correctly stripping a production-SRE lens off a single-user local tool. Net: a strong feature surface undermined by a backend you can't observe, can't recover, and can't hot-edit, sitting behind a god file the tests don't cover.

**Overall grade: 4/10 — Fragile.** Weighted to priorities 1–2: maintainability ~4, operability ~3.

---

## Anchored scores (2 = changes break unrelated features · 4 = fragile, tribal, tests don't protect · 6 = safe change with effort · 8 = new hire ships week one · 10 = teaches its own conventions)

| Dimension | Score | Anchor & evidence |
|---|---|---|
| **Operability** | **3** | The worst axis. Socket-only liveness (F-301, Critical), `DEVNULL` logs (F-303), no recovery CLI (F-302), silent 120s cold-start (F-183), no correlation id (F-308). The 3am-incident trace concluded the outage is **undiagnosable from the repo alone**. |
| **Architecture** | **4** | Fragile & tribal. 4,207-line god file (F-101), 5-way cloner duplication (F-140), state modeled 4–5 ways (F-207), 236-line `spawn_browser` with no seam (F-208). Mitigated by a real `section_tool` registry and deliberate singleton/version work. |
| **Testing** | **4** | Green but mis-aimed. 402 pass, 40.9% coverage, **0 flaky** (a real asset) — but 0 of 96 tool bodies unit-tested (F-105), cloner extraction untested (F-145); real bugs reach `main` (F-202). |
| **Code health** | **4** | 176 broad `except` (22 truly silent) (F-181), 4+ incompatible error shapes (F-104), but **0 bare excepts** and only 8 radon D+ blocks — not pervasive rot. |
| **Dependencies** | **4** | 34 known vulns across 9 packages (fastmcp 2 majors behind, pillow, starlette, pyjwt); no `pip-audit`/dependabot in CI. Mitigated: fully pinned + lockfiled. |
| **Docs** | **4** | README + MCP config work, but no CONTRIBUTING/runbook/release process, the documented `uv run` test cmd breaks on special-char paths (F-402/F-403), tool count disagrees in 3 places (F-108). |
| **Security** | **6** | Local single-user boundary verified: `exec()` paths exist but are operator-authored — **no page/network→exec path** (F-107/F-161 downgraded to Low). Real surface is the 34 dep vulns. No secrets found. |

---

## Findings (root causes; ≤15 mains, Critical first). Full records + quotes in `audit/findings.json`.

Each main finding collapses one root cause with its N corroborating instances. 0 adversarial standoffs (nothing needed a counter-argument attached); 0 kills.

### CRITICAL

**M1 · Socket-only liveness masks a wedged backend** — `singleton.py:83` · operability · [F-301, F-501] (found independently by incident-sim and pre-mortem; F-611 related)
The only liveness signal system-wide is a raw TCP connect/close. It gates backend reuse, the sole auto-recovery watchdog, and the eviction guard. A backend whose event loop is deadlocked still completes the TCP handshake, so `status`/`doctor` print "running" and the watchdog never arms — throughout the entire incident. The one recovery test covers hard death only, never a hang. *Adversarial: attacked hardest, survived — `_await_backend_http` is an app-level probe but startup-only; the runtime paths use the weak signal.* **Fix:** application-level probe (lightweight MCP request or `/healthz` on the dispatch loop) feeding status/doctor/watchdog/eviction. Effort M.

### HIGH

**M2 · Live code edits silently don't take effect** — `singleton.py:132,263` + `server.py` hot_reload · operability/maintainability · [F-206, F-504, F-102, F-121, F-610, F-120]
Two mechanisms, one symptom (your saved memory, now quadruple-confirmed): (a) backend reuse is keyed on the installed **package version string**, frozen at last `pip install -e .`, so a same-version reconnect reuses the pre-edit backend; (b) `hot_reload` — the tool that looks like the fix — reassigns singletons through a stale `from`-import binding (reports success, does nothing), skips `server.py` entirely, and destroys live browser sessions for what it does reload. **Fix:** key reuse on a source hash/mtime (or always-fresh in dev); repair or remove `hot_reload`. Effort M. *Breaking:* changes backend-restart semantics.

**M3 · The backend is unobservable** — `singleton.py:228` · operability · [F-303, F-503, F-304, F-183, F-182, F-308, F-204]
The detached backend's stdout/stderr/stdin are wired to `subprocess.DEVNULL` at spawn; grep confirms zero `FileHandler`/`basicConfig` anywhere; the only logger is in-memory and disabled by default. So: no log file can exist for the process that matters (F-303); the debug-retrieval tools share the same hung request path, so they're unreachable exactly when needed (F-304); cold-start failure is 100% silent for 120s then tears down with no trace (F-183); and nothing carries an MCP request/correlation id (F-308). **Fix:** unconditional `RotatingFileHandler` under a documented state dir + a background flush task + a per-request correlation id. Effort M. *This is the top unlock — it makes every other failure diagnosable.*

**M4 · `server.py` is a 4,207-line god object** — `server.py` · architecture/maintainability · [F-101, F-505, F-612, F-201]
162 functions, all 96 tools behind a single `section_tool` decorator, plus ~700 lines of unrelated clone-eviction filesystem logic (F-201) — one Python import unit, so a break anywhere disables all 11 nominally-independent "sections" at once. Every tool addition edits this one file. **Fix:** split into per-section modules + a tool-registry; extract eviction logic to its own module. Effort L. *Breaking:* internal import paths move (no external consumers). *The central maintainability unlock.*

**M5 · Five overlapping element-cloners, with an un-propagated fix** — `element_cloner.py` et al. (2,223 LOC) · architecture/maintainability · [F-140, F-203, F-601, F-142]
Three of five variants independently re-implement the same "extract complete element" op with three different output schemas; ~35–40% (700–900 LOC) is removable by unifying on the CDP-native engine. The live risk (F-142): a hang-avoidance CDP rewrite landed in **1 of 5** sibling extraction methods (styles) and never reached the other four (structure/events/animations/assets still use the JS-eval path the fix exists to avoid). **Fix:** consolidate to one parameterized cloner (target×mode); *pin tests first (M6).* Effort L. *Breaking:* some of 16 tool signatures.

**M6 · Tool bodies and cloner extraction are unit-test dark** — `tests/` · testing · [F-105, F-145]
0 of 96 tool bodies have a unit test; the cloner extraction logic has none — the recent `await` bug (F-202) shipped silently and was patched reactively with a bespoke AST check. Any consolidation (M5) or decomposition (M4) is therefore flying blind. **Fix:** characterization tests around the tool dispatch + cloner outputs *before* M4/M5. Effort M. *Prerequisite for M4/M5.*

**M7 · `close_instance` teardown can freeze the whole shared backend** — `browser_manager.py` · operability · [F-180 (Crit→High), F-164]
`close_instance` wraps cleanup in `asyncio.wait_for(timeout=5.0)`, but the kill work runs **synchronously** (no `to_thread`/executor), so the timer cannot preempt it — the "5s bound" is fictional when Chrome is unresponsive. Because it's a live tool on the shared singleton backend, one bad close freezes every session. *Adversarial downgraded Crit→High: psutil waits are bounded and Windows `terminate`==immediate, so the freeze is bounded ~5s×N, not infinite.* **Fix:** offload blocking kill to a thread executor under the timeout. Effort S–M.

**M8 · No operational recovery surface** — `cli.py:219` · operability · [F-302, F-305, F-509]
The ops CLI has 5 verbs (status/profiles/cleanup/doctor/serve) — no `stop`, `restart`, or `kill-orphans`. The PID is recorded in `~/.stealth-mcp/server.json` but never surfaced by status/doctor (F-305), and the backend binds a fixed port 19222 with no fallback (F-509). Combined with M1, a correctly-diagnosed hang has no supported fix. **Fix:** add `stop`/`restart`/`kill-orphans`; print the PID. Effort M.

**M9 · Unbounded response-body store** — `network_interceptor.py` · operability/performance · [F-605]
Full response bodies are retained in unbounded dicts with no cap/eviction until instance close, and interception is **default-on** (`server.py:1397`). *Adversarial strengthened this.* The one order-of-magnitude memory risk in the audit: a long session on body-heavy sites can reach hundreds of MB→OOM. **Fix:** ring-buffer/byte-cap the body store; make capture opt-in or filtered by default. Effort M.

### MEDIUM (top cluster; full list in appendix)

**M10 · Inconsistent error handling** — code_health · [F-104, F-181] — 4+ incompatible error-response shapes across 22 `except` blocks; 176 broad handlers total, 22 truly silent (swallow the diagnosis). **Fix:** one MCP error envelope + a lint rule banning silent `except`. Effort M.

**M11 · Import-time side effects defeat testability (and can kill processes)** — architecture/testing · [F-124, F-125, F-103] — `ProcessCleanup()` reaps real orphan processes *at import*, and `conftest.py` never sets the opt-out env var; all 15 singletons construct at import with no DI/reset seam (tests work *around* globals, not through them). **Fix:** lazy init + an injectable/reset seam; set the guard in `conftest`. Effort M.

**M12 · Self-inflicted unsandboxed code-exec + hook gaps** — code_health/security(local) · [F-107↓, F-161↓, F-160, F-163, F-166] — `create_python_binding` execs with real builtins; the hook restricted-builtins sandbox is bypassable. *Adversarial confirmed no page→exec path, so downgraded from RCE to a local robustness/maintainability issue.* Also F-163: `_process_request_hooks` only ever runs `matching_hooks[0]` despite docs promising a "priority chain." **Fix:** document the trust boundary; fix the hook-chain/doc mismatch. Effort S–M.

**M13 · God methods block isolated testing** — architecture/testing · [F-208, F-106] — `spawn_browser` is one ~236-line method with no seam (every tool's most fundamental op, untestable without mocking all of nodriver); `_resolve_profile_selection` is D-grade on stringly-typed state. **Fix:** extract a pipeline of small private methods. Effort M. *Enabled by M4.*

**M14 · Docs don't let a new engineer run/test/ship** — docs · [F-401, F-402, F-403↓, F-404, F-405, F-406, F-407↓, F-306↓] — no git-clone step, the documented `uv run` test cmd fails on special-char paths, the MCP entrypoint hangs silently with no smoke path, and there is no CONTRIBUTING/branch/release/runbook doc anywhere. **Fix:** delivered by the Codify stage (CLAUDE.md/DESIGN.md/RUNBOOK.md/CONTRIBUTING). Effort S–M.

**M15 · State duplicated 4–5 ways; `persistent_storage` is an in-memory misnomer** — code_health · [F-207, F-122] — instance state lives in ≥4 parallel shapes; `InMemoryStorage.store_instance` hand-builds a hardcoded 6-field subset that silently drops every other field (incl. any new option), and the store is wiped on the same shutdown that would need it. **Fix:** serialize the `BrowserInstance` model directly; rename or make it actually durable. Effort S.

---

## Empirical results (the audit's "does it actually hurt?" tests)

- **Change-trace.** *Small* (add a `locale` spawn option) = 3 files, blocked by the 236-line seam-less `spawn_browser` (M13). *Medium* (a `download_response_body` tool) = 3 files, but 3 disagreeing "write artifact to disk" precedents, none canonical. *Cross-cutting* (per-session log correlation) = **16 files / up to 287 `debug_logger` call sites**, one global logger, zero `contextvars` — i.e. M3 is load-bearing for a whole class of future work.
- **3am incident.** Verdict: **could not diagnose or recover from the repo alone.** `status`/`doctor` report false-healthy (M1); there is no log file (M3); recovery is a manual PID hunt (M8). The *one* thing that is well-built — `process_cleanup` orphan-Chrome matching — only fires on a fresh backend start, which M1 prevents.
- **Onboarding (docs-only, day one).** Can wire the MCP config, but **cannot run tests via the documented command** on a normal Windows checkout (M14); ~6 tribal-knowledge steps; ~25 min to green, only via an undocumented `.venv` workaround.

## Performance (priority 3 — intentionally thin; order-of-magnitude only)

Only one order-of-magnitude item: **M9** (unbounded body store → OOM on long sessions). Minor: `eval()` re-parses each hook `custom_condition` per network event with no compile cache (F-606). Everything else is within normal factors and deliberately out of scope under the stated priorities.

---

## Roadmap (leverage-ordered; lead with the unlocks)

**Do these three first — they make everything else tractable:**
1. **M3 Observability spine** (file logging + correlation id + crash-flush). *No deps.* Effort M. Unlocks diagnosing 1,2,7,8 and the cross-cutting change class.
2. **M1 App-level liveness** (replace the socket probe across status/doctor/watchdog/eviction). *Pairs with M3.* Effort M. Unlocks real auto-recovery and makes M7/M8 verifiable.
3. **M4 Decompose `server.py`** (per-section modules + tool registry; extract eviction). Effort L. *Do M6 first.* Unlocks safe change everywhere; prerequisite for M5/M13.

**Then, in dependency order:**
4. **M6** characterization tests *(gate before M4/M5 land)* → 5. **M2** source-hash reuse key + fix/remove `hot_reload` → 6. **M7** offload teardown → 7. **M8** recovery CLI → 8. **M5** cloner consolidation *(after M6)* → 9. **M9** bound the body store → 10. **M10–M13, M15** code-health/testability → 11. **M14** docs (the Codify stage).

**Breaking-change flags:** M2 (backend-restart semantics), M4 (internal import paths — no external consumers found), M5 (some of 16 cloner tool signatures). All acceptable per the "breaking changes allowed" mandate; none has an external consumer we could identify.

**Dependencies:** M6 → M4/M5 · M3 → M1 (logs verify the liveness fix) · M4 → M5/M13.

---

## What's good — preserve, don't "fix"

- **The test suite is green and non-flaky** (402 passed ×2, ±0.03pp coverage). A real asset; the problem is *coverage aim*, not health.
- **`process_cleanup` orphan-Chrome killing is well-engineered** — matches on exact `--user-data-dir` + `create_time`, so it will **not** kill the user's other Chrome windows (verified negative result). Preserve this logic; only add a standalone trigger (M8).
- **The version-aware backend genuinely fixes cross-version staleness** (issue #14, 15+ tests). It's scoped correctly; M2 is about the *source-edit* case it was never meant to cover, not a regression.
- **Deliberate, verified non-issues (don't re-flag):** no page→exec RCE path (data-flow verified); no shared-state race (asyncio cooperative scheduling); `exec()` is operator-authored and local. These are design records, not debt.
- **Deps are fully pinned + lockfiled**; the vuln count is a patch-cadence gap, not sloppiness.

---

## Appendices

- **A. Overflow findings (ranked, not in the 15):** F-141 (cloner to-file boilerplate ×8), F-143 (dead dual-schema fallback), F-144 (per-call vs singleton cloner), F-162 (regex py→js corrupts True/False/None literals), F-165 (dup header loop), F-166 (broken "log" hook template), F-184 (element_clones spill dir no quota), F-108 (tool count 90/96/99 disagree), F-109 (private-attr reach-ins), F-126 (Windows terminate bypasses graceful), F-602/603/604 (ad-hoc getenv, timeout preamble ×49, sys.path fragility), F-606/607/608/609 (eval no compile cache, unlocked pid file, PID-trust kill path, buffer-outside-lock), F-611 (list_instances no CDP round-trip), F-613 (import strategy inconsistency). Full records in `audit/findings.json`.
- **B. Unverified / low-confidence:** F-307 (untimed logger lock deadlock — confidence 0.55; real but needs DEBUG=true + undrained pipe, unreachable on the DEVNULL prod path).
- **C. Downgraded from High/Critical (adversarial, with rebuttals in `audit/stage1/adversarial/`):** F-403 Crit→Low (machine-specific path, not a repo defect), F-202 High→Low (already fixed at HEAD), F-507 High→Low (integration CI *does* run per-PR), F-107/F-161 High→Low (operator-authored exec), F-306/F-402/F-407 High→Low (production-SRE lens on a single-user tool), + 11 High→Medium.
- **D. Prior-audit reconciliation (`audit/prior/claims_verdicts.json`):** 27 claims → 13 confirmed, 7 already-fixed, 5 refuted, 2 unverifiable. Notably the prior #1 Critical **P-016 "CDP hangs forever" is refuted** (timeout wrapper at 59 sites) — the real residual hang is the narrower M7/M1.
- **E. Hallucination meter: deletion counter = 0 / 79.** Every finding's quote verified verbatim (75) or paraphrased-but-true (4). No fabricated evidence.
- **F. Machine-generated ledger:** `audit/findings.json` (79 findings, final severities, adversarial verdicts) — diff this against the next audit.

---

## ⛔ Human triage gate

Mark each main finding **M1–M15** (and any appendix item you want promoted) as **fix / defer / reject**. Only fix-marked items proceed to Stage 2 planning (one Opus plan per item, each individually approved by you before any code). Nothing is edited until then.
