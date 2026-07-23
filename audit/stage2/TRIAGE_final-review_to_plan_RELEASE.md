# Triage — final arch review → plan_RELEASE

**Source:** 7-Opus whole-system review, 2026-07-20, on PR #39 branch `audit/fixes-2026-07-02-m14` @ f0554ef.
**Purpose:** input to the `plan_RELEASE` PLAN/triage phase. Every review finding is sorted into
Tier A (pre-release code FIX — real defect, ship-blocking-ish, own scoped chunk — **NOT** bolted onto PR #39),
Tier B (release-program scope — maps onto existing W-items), or Tier C (doc/cosmetic, rides a docs touch).

**Plan of record (the "recommended approach"):**
1. Merge PR #39 **as-is** (docs + 2 touches; reviewed scope stays clean) — satisfies the plan_RELEASE HARD-GATE prereq (M14+A1 → main). *User merge gate.*
2. Greenlight `plan_RELEASE`; its PLAN phase ingests this triage. *New chunk — explicit human confirmation required.*
3. Run an early Tier-A FIX wave (the silent/correctness bugs that undermine "green gate == reality" and block honest soak/perf measurement) **before** the big W-items, then the W-items with **cross-OS CI first**.

Conventions all PASS (no `server` imports, no relative imports, one cloner engine, no tombstone imports, tool-count=94 derived, debt ledger honest) — no architectural rework needed.

---

## Tier A — pre-release code FIX candidates (ranked)

| # | Defect | Loc | Sev | Fix shape | Verify via |
|---|---|---|---|---|---|
| A1 | **Selector-atomicity VIOLATION + silent `[]`.** `query_elements`/`get_page_content` call `tab.select_all` directly, bypassing element_resolution; broad `except` swallows -32000 into empty list → tool lies under DOM churn. Breaks the HARD invariant PR #35 established. | dom_handler.py:88, :725 | **MED (top)** | Route both `select_all` calls through element_resolution's recovery; stop swallowing -32000 (retry or surface). Check dom_handler budget headroom. | B6 |
| A2 | Multi-session cold-start port divergence behind a foreign port squatter → lock losers poll a dead port ~120s. | singleton.py:639-681 | MED | Losers re-read `server.json` after acquiring/losing the lock instead of committing to their own `_free_port`; or serialize port choice under the lock. | B-crossOS |
| A3 | Network capture: request **count** unbounded + request `post_data` bytes uncapped (only response bodies capped) → long-session memory leak. Hits "fast/lean". | network_interceptor.py:163-165 | MED | FIFO count cap + per-`post_data` byte cap mirroring the §6 response-body store cap; update DESIGN §6 to say metadata/count bounding. | B5 |
| A4 | `create_python_binding` is a success-returning **no-op** (no `Runtime.bindingCalled` handler; `call_python_from_js` is dead). | cdp_function_executor.py:754-793 | MED | Either wire `Runtime.bindingCalled` → `call_python_from_js`, or remove/mark-unsupported. Shipping a lying success is worse than absent. | new pin |
| A5 | `extract_element_styles` raises `NameError` on legal `include_css_rules=False, include_pseudo=True` → silent styles failure. | cdp_element_cloner.py:552 | MED | Fetch `matched_styles` whenever any of css_rules/pseudo/inheritance is requested (or guard the reference). | new pin (param matrix) |
| A6 | JS extractors embed selector as raw `"$SELECTOR$"` → quoted-attribute selectors (`input[name="email"]`) break structure/events/animations/assets while CDP styles path succeeds (asymmetric). | js/extract_structure.js:12, extract_events.js:19, extract_animations.js:11, extract_assets.js:97 | MED | `JSON.stringify`-encode the selector at substitution time. | new pin |
| A7 | `close_instance` Phase-2 `await tab.close()` unbounded (no `wait_for`) → hangs on a wedged renderer, defeating the bounded-teardown contract. | browser_manager.py:863-865 | MED | Wrap in `asyncio.wait_for` like the sibling `browser.close()`/`disconnect()`. **browser_manager at 1532 no-grow cap** — keep net-neutral or offset. | B-crossOS (renderer-hang) |
| A8 | Windows hard-kill (`TerminateProcess`) on evict/restart/stop bypasses `app_lifespan` cleanup → transient orphan window (POSIX shuts down gracefully; behavior diverges by OS). | singleton.py:280-288; browser_manager kill tiers; process_cleanup | MED (Win) | Judgment call: attempt graceful Windows shutdown signal, or accept + document + rely on next-boot reaper. Triage decides fix-vs-accept. | B-crossOS |
| A9 | Windows `msvcrt.locking` on a **read-only** pid-file handle may fail (swallowed) → orphan recovery sees zero tracked PIDs. | process_cleanup.py (~228, _file_lock) | MED (Win) | Open the pid-file handle with write access for locking; verify on real Windows. | B-crossOS |
| A10 | TOCTOU free-port race (pick :0, close, rebind later). Low prob, known flake source; compounds A2. | singleton.py:650-655; proxy_forwarder.py:22-29 | LOW | Bind-with-retry on the actual listener rather than pre-picking a port. | B-crossOS |
| A11 | Naive vs aware datetime: `BrowserInstance.created_at/last_activity` naive, idle reaper subtracts aware UTC → latent `TypeError` (currently caught → "reaped nothing"). | models.py:27-28 | LOW | `default_factory=lambda: datetime.now(timezone.utc)`. | existing reaper pins |
| A12 | Proxy credentials not percent-decoded → proxies with special-char passwords fail auth (HTTP + SOCKS). | proxy_forwarder.py:231-232,359-366; proxy_utils.py:53-54 | LOW | `urllib.parse.unquote` username/password before use. | B3 (proxy suite) |

---

## Tier B — release-program scope (map onto existing plan_RELEASE W-items)

| # | Item | Maps to | Note |
|---|---|---|---|
| B1 | **Cross-OS CI matrix** — add `windows-latest`+`macos-latest` to the unit job; stand up a macOS integration lane. **Highest leverage: until Win/mac are gated, nothing else buys cross-OS confidence.** | §7 3-OS gating (RESOLVED) | Near-zero effort for the unit matrix axis; integration lanes are the real work. |
| B2 | **Harden publish gate** — `publish.yml` depends on full `test.yml` (integration + quality/budgets) + **install-smoke** (`uv build` → clean-env install → launch → MCP handshake) on all 3 OSes. Today publish is ubuntu-unit-only, no cov floor, no lint/budget, no install. | install-smoke hard-blocks publish (planned) | `test_packaging.py` only *builds*; never installs/launches. |
| B3 | **Fault-injection + coverage** for thin-net modules: proxy_forwarder (20%), cdp_function_executor (28%), dom_handler (33%), browser_manager (41%) — auth-failure/tunnel-drop, malformed-arg/CDP-error, spawn-failure. | W10 resilience + W7 corpus | Absorbs A12's proxy suite. |
| B4 | **Perf/resource budgets** — assertion-backed (spawn latency, per-tool wall-clock, backend RSS ceiling). None exist today → a 2× slowdown ships green. | W9 perf/resource budgets | |
| B5 | **Wire the soak/leak lane** — adopt `tests/stress_memory_leak.py` (never collected — filename isn't `test_*`) into a scheduled nightly soak with an RSS-growth budget. Also the verification vehicle for A3. | W6 soak lane | |
| B6 | **DOM-churn stress lane** — drive `query_elements`/`get_page_content` against continuous `DOM.documentUpdated`, assert non-empty when elements exist. A naive "no-exception" soak passes while A1 silently lies — this lane is what actually catches A1. | W10 resilience | Pair with A1 fix. |
| B7 | **Golden-discipline hardening** — `load_or_capture_golden` auto-captures on missing file → a deleted golden silently regenerates & passes. Make missing-golden a hard fail; prune trivial change-detector goldens. | W7 corpus taxonomy | |

---

## Tier C — doc/cosmetic (ride a docs touch; some are one-line follow-ups)

- C1 DESIGN §9 KEEP list: add the hook-interface envelope family (`dynamic_hook_ai_interface.py` ~20 success/error dicts — behaviorally KEEP, currently undocumented).
- C2 CLAUDE.md nav map: add the 8th CLI verb `profiles` (cli.py:450).
- C3 `extract_styles.js` + `comprehensive_element_extractor.js` are **dead** (styles is CDP-only; the latter is tombstone residue). Delete + fix CLAUDE.md:84 ("7 extraction scripts"), or annotate "kept, not wired." Removing tidies the one-cloner-engine surface.
- C4 logging_setup.py:84,88 comment says "96 tools" → 94.
- C5 cdp_element_cloner.py:989 docstring claims selector forwarded to `related_files` but it isn't (page-scoped; harmless — doc accuracy only).
- C6 progressive `available_data` advertises `"html"` (no `expand_html`) and `fonts` is served by `expand_animations` — cosmetic mismatch.

---

## Suggested plan_RELEASE ordering
1. **Phase 0** — re-verify prereq chain (M14+A1 now ancestor of main after merge).
2. **Early FIX wave** — A1, A3, A5, A6, A4, A7 (cheap, high-signal; A1/A3 block honest soak/perf measurement). Each its own commit under the normal gate discipline; A7 must respect the browser_manager no-grow cap.
3. **W-items** — B1 (cross-OS CI) FIRST, then B2, then B3/B4/B5/B6/B7. Cross-OS code fixes A2/A8/A9/A10 land alongside B1/B-crossOS (their verification only exists once Win/mac are gated).
4. **Doc touches** — Tier C rides the relevant W-item's docs edit.
