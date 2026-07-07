# Stage 2 Plan — M14 CODIFY (Stage-4 documentation deliverable + its validation)

> **Status: APPROVED by human 2026-07-03 (approve as-is)** with THREE gate rulings: (1) F-108 = **option (a), derive both printed counts from SECTION_TOOLS** (the one planned code touch, own commit); (2) stale docs = **RETIRE to audit/history/**; (3) **F-741 OVERTURNED → the CLI `status` code-field renames are pulled INTO M14** (not Ph2/opportunistic) — **Amendment A1 below adds this second fenced code touch** (glossary-conformant field/label renames + consumer sweep + pins).
> Plan 12 of 12. Executes **LAST** (Stage 4), after all 11 code plans have landed
> (M3+A1, M1, M8+A1, M2, M7, M11a+M15, M9, M6, M12a, M4-Ph1+A1, M5b).
> Pinned SHA at authoring: `2267b83d3efda03f93936db2c34ded33aaa0d701` (HEAD == pinned).
> Branch: `fix/singleton-version-aware-backend`.
> Context: **LOCAL SINGLE-USER tool, 0 external users.** Priorities: (1) maintainability,
> (2) operability, (3) performance. Four lenses BINDING: modularity · deduplication ·
> clarity · conventions ("a fix that introduces a second way of doing something is a defect").

## The one fact that shapes this whole plan

**This plan is documentation, with exactly one code touch (F-108).** Every other line
it writes is prose that *describes the post-fix tree the 11 code plans define* — so its
single failure mode is **docs that lie about the tree**. That failure mode is precisely
the finding-family M14 closes (F-401..407: "docs don't let a new engineer run/test/ship";
F-403: "the documented test command actually FAILS"). Therefore the plan is built around
one discipline: **every runnable command and every named symbol in the docs is verified
against the actual tree by the Stage-4 executor before the docs are accepted** (§4), and
**counts are derived, not typed** (F-108, §2.6) so they cannot drift again.

Because M14 runs at Stage 4, its content inventory is not speculative: it is the union of
the `§approach` / `§8 findings-closed` / `resolved_decisions` of the 11 approved plans and
the binding `cross_review_notes` in `audit/state.json`. Those sections ARE the source of
truth for what the docs must say. The executor re-reads them (and the tree) at Stage 4.

---

## 1. Scope

### 1.1 Doc files CREATED (4 primary + 1 index touch)

| File | Audience | Purpose (one line) |
|---|---|---|
| `CLAUDE.md` (repo root) | **agents** (incl. lighter models) | name-only navigation map of the post-fix tree + glossary (F-741) + the 4 conventions an agent must follow + tool-count=94 provenance |
| `DESIGN.md` (repo root) | maintainer + agents | the *why* behind the architecture: two-surfaces-one-backend, liveness/port/fingerprint invariants, the cloner transport table + events rationale, the KEEP contracts, the import/error/one-engine conventions + the **known-debt ledger** |
| `RUNBOOK.md` (repo root) | operator (the maintainer at 3am) | CLI verbs, log locations, wedged-backend recovery, port-fallback behavior, orphan reaping, the manual MCP smoke path, crash/hang first-response |
| `CONTRIBUTING.md` (repo root) | contributor | clone→install(editable)→test with **the venv python** (documents the uv-run failure F-403/F-404); the real quality gate (pytest + coverage 39 CI, no linter — stated honestly); branch/PR/checkpoint conventions; golden discipline; the four lenses as review criteria; where the verb taxonomy lives |

Placement decision (glossary): the glossary lives in **CLAUDE.md** (agent-facing navigation is
where "what does *session* mean" is asked), and DESIGN.md links to it rather than duplicating
it (dedup lens — one home per concept). See §2.2.

### 1.2 Doc files EDITED

- **`README.md`** — repair, not rewrite (§2.3, §3-S6): add a "Clone & Setup" subsection (F-401),
  fix the CLI/test blocks so the documented commands actually run on a checkout (F-403/F-404),
  reconcile the tool count to **94** and point at the derivation (F-108), remove/annotate the
  `uv run` test command that fails on this path, add pointers to the 4 new docs.

### 1.3 Stale-doc sweep (retire or repair — §2.4, §3-S7)

- **`MCP_TOOL_TEST_RESULTS.md`** — references deleted tools `hot_reload`/`reload_status`
  (lines 159-160, deleted by M2), claims "97 tools", and carries a 2026-05-20 bug log whose
  items are now fixed (e.g. B1 `get_instance_state` → fixed by M4/F-746). **Decision: RETIRE**
  (§2.4) — move to `audit/history/` or delete; it is a point-in-time test log, not a maintained
  doc, and repairing it would resurrect a *second* tool inventory competing with CLAUDE.md's
  derived count (conventions defect).
- **`HIGH_VALUE_CHANGES.md`** — a pre-audit improvement plan, now superseded by this entire
  pipeline. **Decision: RETIRE** to `audit/history/` (it is the *prior* roadmap; the audit's
  `REPORT.md` + these plans are the current one). Non-blocking; flagged for the human.
- **`CODEBASE_AUDIT.md`** (untracked, root) — out of audit scope per exclusions; left as-is.

### 1.4 The code touches — **[A1] now TWO, each its own revertible commit**

M14 makes two small, independent code changes; everything else is docs. Each is fenced to its own
checkpoint commit so either can be reverted alone.

**Touch 1 (F-108) — `--list-sections` + CLI description derive from `SECTION_TOOLS`.**
`src/stealth_chrome_devtools_mcp/embedded/server.py` — **derive the two printed tool counts
from the live `SECTION_TOOLS` registry instead of hardcoding them** (full argument in §2.6):
- `:4085` `ArgumentParser(description="Stealth Browser MCP Server with 90 tools")` → description
  built from `sum(len(v) for v in SECTION_TOOLS.values())`.
- the `--list-sections` branch (`:4145`) → print `len(names)` per section from `SECTION_TOOLS`
  and the summed total, instead of the hand-typed per-section numbers that summed to 99.

Post-M4 `SECTION_TOOLS` lives in `embedded/tool_registry.py`; the `__main__` block imports it
(it already must, to register tools). This alters only two **printed strings** (the counts).

**Touch 2 (F-741) — glossary-conformant renames of the colliding CLI `status` labels — [A1],
human-ordered at the gate.** `src/stealth_chrome_devtools_mcp/cli.py` (post-M1+M8 `_cmd_status` /
`_cmd_doctor` / `_cmd_cleanup`): rename the print labels that say bare "session" where they mean
*named browser-profile directories on disk*, so the CLI output stops colliding with "MCP session."
Full old→new table, the env-var decision, and the consumer sweep are in **Amendment A1**; this is
the second executable change and is flagged explicitly at the gate.

### 1.5 Explicit OUT of scope (stated so Stage 4 does not creep)

- **No renames — [A1] EXCEPT the human-ordered F-741 CLI `status` label renames (Touch 2, Amendment
  A1).** The F-760/F-743/F-744 *tool* renames remain Ph2 (documented as *pending debt*, not done);
  the only renames M14 performs are the CLI display labels the human pulled forward at the gate.
- **No behavior changes.** M14 touches no tool body, no CLI verb behavior, no lifecycle path. Touch 1
  changes two printed count strings; Touch 2 changes CLI display labels (and, per the §A1 decision,
  possibly one internal helper/env name) — neither alters what any command *does*.
- **No new tooling.** No ruff/black/mypy/pre-commit is introduced. F-406 is answered *honestly*:
  document the gate that EXISTS (pytest + coverage 39 in CI). Inventing a linter would be a
  new convention with no enforcement — the opposite of the lens directive.
- **Debt is RECORDED, not fixed.** F-740, F-702, F-703, F-603, F-106, F-606, F-765, M4-Ph2, M1
  probe-body dedup, M11b DI seam → ledger entries in DESIGN.md (§2.5). M14 fixes none of them.
  (The broader F-741 "proxy" overload — `proxy_forwarder`/`proxy_utils` egress-proxy vs the stdio
  proxy — is pinned by the **glossary**, not renamed; only the CLI `status` "session" collision is
  renamed. See §A1.6.)
- **No verb-taxonomy re-derivation.** The canonical taxonomy lives in `tool_registry.py`'s
  docstring (M4 ruling); CONTRIBUTING **references** it, does not copy it (dedup lens).

### 1.5 Explicit OUT of scope (stated so Stage 4 does not creep)

- **No renames.** F-760/F-743/F-744 tool renames are Ph2 (documented as *pending debt*, not done).
- **No behavior changes.** M14 touches no tool body, no CLI verb behavior, no default. The F-108
  change alters only two **printed strings** (the counts), not which tools exist.
- **No new tooling.** No ruff/black/mypy/pre-commit is introduced. F-406 is answered *honestly*:
  document the gate that EXISTS (pytest + coverage 39 in CI). Inventing a linter would be a
  new convention with no enforcement — the opposite of the lens directive.
- **Debt is RECORDED, not fixed.** F-740, F-702, F-703, F-603, F-106, F-606, F-765, M4-Ph2, M1
  probe-body dedup, M11b DI seam → ledger entries in DESIGN.md (§2.5). M14 fixes none of them.
- **No verb-taxonomy re-derivation.** The canonical taxonomy lives in `tool_registry.py`'s
  docstring (M4 ruling); CONTRIBUTING **references** it, does not copy it (dedup lens).

---

## 2. Approach + rejected alternatives

### 2.1 Doc architecture — why these 4 files (+ README) and not one big doc

**CHOSEN — four audience-scoped root docs + a repaired README, each with a single reader in mind.**

The finding family splits cleanly by *who is blocked*: the **agent** placing a change (needs a
navigation map + glossary + conventions → `CLAUDE.md`), the **maintainer** asking *why is it built
this way* (needs invariants + rationale + debt → `DESIGN.md`), the **operator** recovering a wedged
backend (needs verbs + log paths + recovery steps → `RUNBOOK.md`), and the **contributor** shipping
a change (needs install/test/branch/PR/golden discipline → `CONTRIBUTING.md`). One file per audience
means each is short enough to be read in full by its reader — the same "understandable in isolation"
property the modularity lens demands of code, applied to docs. README stays the front door and
*links* to the four (no content duplicated across them — dedup lens).

Root-level placement (not `docs/`): a single-maintainer repo with 4 docs does not need a `docs/`
tree; root files are found by `ls` and by GitHub's auto-render, and `CLAUDE.md` at root is the
convention the agent harness already looks for.

**Rejected — one mega-doc (`DEVELOPMENT.md`).** A single 1500-line file forces every reader to
scroll past three-quarters of content aimed at someone else; the operator at 3am does not want the
glossary and the contributor's golden discipline between them and the recovery steps. Fails the
"readable in isolation" bar and makes the name-only-navigation check (§5.2) harder, because the
agent must locate the right *section* rather than the right *file*.

**Rejected — a GitHub wiki.** Off-tree docs cannot be verified by the Stage-4 executor against the
tree (they are not in the checkout), drift silently, and are invisible to a cold agent given "only
the new docs + a clean checkout" (§5.1). The whole point of this finding-family is *docs that travel
with the code and are checkable*; a wiki defeats that.

**Rejected — in-code docstrings only (no top-level docs).** Docstrings answer "what does this
function do" but not "where do I start" or "why two surfaces" or "how do I recover a wedged backend."
The onboarding audit (F-401..407) failed on exactly the cross-cutting questions no single docstring
owns. Docstrings stay (M4 sharpened the exec-family ones), but they are not a substitute.

### 2.2 Glossary placement + the F-741 collision (one meaning per term)

**CHOSEN — the glossary lives in `CLAUDE.md`; DESIGN.md and RUNBOOK.md link to it.**

F-741's core harm: "session" means ≥3 unrelated things and two of them **collide in the CLI's own
`status` output**. The fix is to **pin exactly one meaning per term** and use that term consistently;
where a term is irreducibly overloaded, give each sense a *distinct* qualified name and retire the
bare word from ambiguous surfaces. The pinned glossary (agent-facing, so it sits with the navigation
map):

| Term | THE one meaning (pinned) | Not to be confused with |
|---|---|---|
| **backend** | the single shared detached `python -m … --transport http` process that runs FastMCP + all 94 tools | the stdio proxy (below); "the server" (ambiguous — avoid) |
| **stdio proxy** | the short-lived per-Claude-Code-session process bridging stdio↔the backend's HTTP | the backend |
| **MCP session** | FastMCP's `mcp-session-id` handshake token (created by `initialize`, thrown away by the liveness probe) | a browser session; a Claude Code session |
| **Claude Code session** | one client connection = one stdio proxy instance | an MCP session; a browser session |
| **browser session / named session** | a `spawn_browser(session_name=…)` profile-backed browser instance | any of the above; a "session root" |
| **session root** | the on-disk `STEALTH_MCP_BROWSER_SESSION_ROOT` dir holding profiles/clones | a browser session (the *storage* for them) |
| **instance / instance_id** | one live browser managed by `BrowserManager`, keyed by `instance_id` | a browser session (an instance is the *runtime*; a session is the *named profile*) |
| **profile** | a Chrome user-data-dir (master, or a per-session clone) | a session (which *selects* a profile) |
| **clone** | a copy-on-spawn profile derived from the master snapshot | the element **clone** (DOM extraction) — DISTINCT sense, always qualify as "element clone" vs "profile clone" |
| **in-memory storage** | the deliberately non-durable `InMemoryStorage` cross-check (post-M15 rename of `persistent_storage`) | durable disk state (there is none for instances) |
| **clone storage** | `clone_storage.py`: the on-disk profile/clone quota+GC subsystem | in-memory storage; the cloner *engine* |
| **cloner engine** | `CDPElementCloner`: the one canonical DOM-extraction engine (post-M5b) | clone storage (disk); a profile clone |

The **CLI `status` collision** (F-741's two-senses-collide case) is resolved by **renaming the
colliding CLI print labels themselves** so the output reads glossary-conformant with no bare
"session" — **[A1] the human overturned the doc-only resolution at the gate; the renames are now IN
M14, not Ph2.** The RUNBOOK's "reading `status`" section then documents the *renamed* output (simpler
and more honest than disambiguation-labeling around a still-ambiguous string). Full rename table,
consumer sweep, the env-var decision, and pins are in **Amendment A1** (the second fenced code touch).
The glossary above is unchanged — it already pins the terms; the CLI now *follows* it.

**Rejected — a standalone `GLOSSARY.md`.** A fifth root file for ~12 terms is over-structured, and
splitting the glossary from the navigation map means an agent placing a change reads the map in
CLAUDE.md but must open a second file to learn what "instance" means. Co-locating them is the dedup-
and clarity-lens choice.

### 2.3 README repair vs rewrite

**CHOSEN — surgical repair.** The README's structure (Demos → Features → Quick Start → How It Works
→ Usage → MCP Tools → Testing → Env → CLI → Requirements → License) is sound and its "How It Works"
sections (profile strategy, stealth arg filtering, orphan recovery) are accurate. Repair only the
three things that are **wrong or missing** and that the findings name:
1. **Add "Clone & Setup"** above "Local Development" (F-401): the `git clone <url>` + `uv sync` (or
   the venv install) steps, since no repo URL appears anywhere today.
2. **Fix the Testing + CLI blocks** (F-403/F-404): replace the bare `uv run pytest` / bare
   `stealth-chrome-devtools status` (which fail on a local checkout — see §2.7) with the
   venv-python forms that work, and add the one-line note about the `&`/space path hazard, with a
   pointer to CONTRIBUTING.md and RUNBOOK.md for the full story.
3. **Reconcile the tool count** (F-108): change any hardcoded count to **94** and add "(derived from
   `SECTION_TOOLS`; run `--list-sections`)" so the number has a checkable provenance.

A rewrite would churn accurate content and risk introducing new drift; the findings are specific and
local.

### 2.4 Stale-doc sweep — retire vs repair

**Ruling: retire point-in-time artifacts; repair only living docs.** `MCP_TOOL_TEST_RESULTS.md`
(97-tool inventory + deleted-tool rows + a dated bug log) and `HIGH_VALUE_CHANGES.md` (the *prior*
roadmap) are historical snapshots. Repairing the tool inventory in `MCP_TOOL_TEST_RESULTS.md` would
create a **fourth** hand-maintained tool list competing with the derived count — exactly the F-108
disease. So they move to `audit/history/` (preserved, out of the front-door path) rather than being
kept current. README is the only *living* doc that gets repaired (§2.3). The human gate confirms the
retire-vs-keep call (§9 open question 1).

### 2.5 The known-debt ledger — record, do not fix (DESIGN.md § "Known debt")

The ledger is a **DESIGN.md section**, not a task list, and each entry quotes the disposition already
recorded in `findings.json` / `state.json` so the record is traceable, not re-derived:

- **F-740** (`singleton.py` is really `backend_lifecycle` + `stdio_proxy`) — *"defer:known-debt
  (singleton.py split backend_lifecycle/stdio_proxy → wave 4 post-M2; sequence-critical, four plans
  edit this file serially)."* Final severity High.
- **F-702** (`BrowserManager` 6-concern split) — *"defer:known-debt (BrowserManager 6-concern split
  → M4-Ph2 era; record in DESIGN.md at M14)."*
- **F-703** (`DebugLogger` hides a serialization engine) — *"defer:known-debt (DebugLogger
  serialization engine → wave-4 cleanup; record in DESIGN.md at M14)."*
- **F-603** cross-module timeout preamble (the `_with_cdp_timeout` idiom repeated across modules) —
  server.py half landed in M4; the cross-module consolidation remains debt.
- **F-106** deeper `spawn_browser` decomposition beyond the M13 seam — remains debt.
- **M4-Ph2** — full per-section `server.py` split + the deferred renames **F-760** (verb taxonomy),
  **F-743** (exec-family), **F-744 remainder**, and the **`extract_element_styles` / `extract_element_styles_cdp`
  twin-tool merge** (both route to the same engine CDP method post-M5b — natural Ph2 merge/rename).
- **F-606** (hook `matches()` eval re-parse, no compile cache) — **OPEN and unrouted** after M12a.
- **F-765** (`poll_until` helper) — **OPEN**; only landed if a plan touched those loops (M2 did not).
- **M1 probe-body dedup** — `_backend_http_ready` deliberately COPIES ~10 lines of
  `_await_backend_http`'s `initialize` body to stay disjoint from M3-owned code; the canonical home
  (one shared probe body in `singleton.py`) is a future cleanup finding.
- **M11b DI seam** — the general factory/DI seam for all 15 import-time singletons (F-125 remainder)
  is DEFERRED; M11a removed only `process_cleanup`'s import-time side effects.

**NOTE — what does NOT appear:** **M10b / F-104** is absent because it was **DELIVERED** via M4-Ph1
Amendment A1 (the full 22-site error-envelope sweep; F-104 CLOSED). Recording it as debt would be a
lie the executor must catch. (state.json: *"F-104 remainder [SUPERSEDED by A1: F-104 CLOSED, M10b
DELIVERED — debt line removed]."*)

### 2.6 F-108 — the derive-vs-hardcode decision (the one code-touch question)

**Both options, argued:**

- **(a) Derive the printout from `SECTION_TOOLS` at runtime** — one source of truth. `SECTION_TOOLS`
  is a real, already-populated `defaultdict(list)` (`server.py:51`, filled by the `@section_tool`
  decorator at `:1215`); it is proven populated-and-iterable by `__main__` time because
  `apply_disabled_sections` iterates it (`:1220-1228`). *(Orchestrator touch-up: `--list-sections`
  itself does NOT iterate it today — it prints pure hardcoded strings at `:4146-4156` summing to 99;
  that hardcoding is exactly what option (a) replaces.)* The counts become
  `len(SECTION_TOOLS[section])` per section and `sum(len(v) for v in SECTION_TOOLS.values())` for the
  total, and the CLI `description` string is built the same way. Cost: ~6 lines changed inside the
  `__main__` block, no behavior change beyond the printed numbers now being correct-by-construction.
- **(b) Docs-only reconciliation** — fix the three hand-typed numbers (90 / 96→94 / 99→94) by hand,
  leave the code hardcoded. Cost: zero code change. But it **accepts a fourth hand-maintained count**
  (README + CLI description + `--list-sections` breakdown + the docs) that will re-diverge the next
  time a tool is added — the *exact* recurrence F-108 is about ("nobody can keep the tool count
  straight").

**RECOMMENDATION: (a).** The four lenses decide it. Deduplication: (a) collapses three
hand-maintained numbers into one derived source; (b) adds a fourth. Conventions: a derived count is
"one way to know how many tools exist," followed everywhere; (b) leaves the drift mechanism in place.
The change is tiny, mechanical, and inside the very branch that already reads `SECTION_TOOLS` — it
carries essentially no blast radius (it prints numbers; nothing consumes them programmatically).
This is the single code touch in an otherwise docs-only plan, and it is **flagged explicitly** at the
gate so the human can veto it down to (b) if they want M14 to be pure docs. If vetoed to (b), the
documented count is still **94** and the ledger records "F-108 code-derivation deferred" — but the
lens-preferred answer is (a).

**Authoritative count = 94.** Derivation: 96 `@section_tool` decorators at the pre-fix HEAD, minus
`hot_reload` and `reload_status` deleted by M2 = **94** (state.json note 24: *"Tool surface is 94
post-M2"*; note 26: *"tool-count reconciliation (90/96/99→94) is M14/F-108"*). M12a and M5b add/remove
zero tools (notes 27, 29). The Stage-4 executor asserts this equals the registry-derived count (§5.4).

### 2.7 The uv-run truth (F-403/F-404) — document, do not "fix"

F-403 is a **real finding about this checkout**, and the plan documents it truthfully rather than
pretending it away: **`uv run` is broken in a path containing `&` and spaces** (this repo lives under
`…/CUSTOM MCPs & PRODUCTIVITY/…`). The documented, working commands throughout the new docs use the
**venv python** directly:

```
.venv\Scripts\python.exe -m pytest -m "not integration" -q        # the test command that WORKS
.venv\Scripts\python.exe -m stealth_chrome_devtools_mcp --help    # invoke the CLI without uv/PATH
```

CONTRIBUTING states the root cause (special-char path defeats uv's resolver), notes CI uses `uv run`
because the CI checkout path is clean, and recommends against `&`-bearing folder names for local
checkouts. This is the honest F-403/F-404 closure: the tool is not broken, the *documented invocation*
was, and now the docs show the invocation that runs.

---

## 3. Sequencing — doc-by-doc, each independently landable; one checkpoint commit each

Docs are order-independent for correctness (no doc *executes*), so the sequence is chosen for
**verifiability**: build the fact-dense reference docs first (so later docs can link to them), do the
one code touch in isolation, then run the validation last. Each step is one checkpoint commit; every
step leaves the repo shippable (docs never break the build or the suite).

- **S1 — `DESIGN.md`.** The invariants + rationale + KEEP contracts + transport table + known-debt
  ledger. Densest doc; everything else references it. (Content inventory: §2.5 + §6 below.)
- **S2 — `CLAUDE.md`.** Navigation map of the post-fix tree + glossary + the 4 conventions +
  tool-count=94 provenance. Links into DESIGN.md.
- **S3 — `RUNBOOK.md`.** CLI verbs, log locations, recovery playbooks, manual MCP smoke path,
  reading-`status` — **[A1] documents the RENAMED status output (§A1.2), not disambiguation labeling.**
  Links into DESIGN.md + CLAUDE.md glossary.
- **S4 — `CONTRIBUTING.md`.** clone→install→test (venv python), the real gate, branch/PR/checkpoint
  conventions, golden discipline (two-tier), the four lenses as review criteria, verb-taxonomy pointer.
- **S5 — F-108 code touch (Touch 1).** Derive the two counts from `SECTION_TOOLS` (server.py `__main__`,
  post-M4 importing from `tool_registry.py`). Add/extend a tiny test asserting the printed total ==
  registry-derived total (§5.4). Independently revertible (its own commit) so the human can drop it
  to option (b) without touching the docs.
- **[A1] S5b — F-741 CLI `status` label rename (Touch 2).** Apply the §A1.2 old→new label renames in
  `cli.py` (`_cmd_status`/`_cmd_doctor`/`_cmd_cleanup`) and, per the §A1.3 env-var decision, the one
  backing helper/env if chosen; add the §A1.5 output-shape pin; update the `test_status_runs` assertion
  (§A1.4). **Own commit, independently revertible.** *Placement justification:* it sits with S5 (both
  code touches grouped, both before README + validation) rather than before S3. Authoring the RUNBOOK
  (S3) against the *target* renamed output is safe because no doc executes — S8's doc-claim smoke
  (§5.3) runs the actual post-S5b CLI and asserts the documented labels appear verbatim, so if S5b were
  ever skipped the RUNBOOK claim would fail loudly rather than drift silently. Landing it after the
  RUNBOOK (not before) also keeps the two code commits adjacent for a clean single-touch revert of
  either.
- **S6 — README repair.** Clone & Setup, test/CLI command fixes, count→94, links to the 4 docs;
  **[A1] the README CLI/env sections reflect the renamed labels + the §A1.3 env-var decision.**
- **S7 — stale-doc sweep.** Retire `MCP_TOOL_TEST_RESULTS.md` + `HIGH_VALUE_CHANGES.md` to
  `audit/history/`; confirm no remaining root `*.md` references deleted tools or wrong counts.
- **S8 — validation (the 4-part protocol, §5).** Doc-claim smoke (every command run, every symbol
  checked), tool-count assertion, name-only-navigation check, cold-agent onboarding re-run. This is
  the acceptance gate; it lands last and its output is the evidence the docs don't lie.

---

## 4. Doc-claim accuracy protocol — how the Stage-4 executor verifies every claim

The failure mode is docs that lie; this section is the defense. **Before the docs are accepted, the
Stage-4 executor runs a scripted check that fails loudly on any false claim.** Three claim classes:

1. **Runnable commands** — every fenced command block in the 4 docs + README that is meant to run is
   **executed verbatim** on the post-fix tree (in the real checkout, `&`-and-spaces path included, to
   prove the venv-python forms actually work here). A non-zero exit that the doc did not predict =
   failure. (The one command documented as *failing* — bare `uv run pytest` — is asserted to fail, so
   the F-403 claim is itself verified true.)
2. **Named symbols / files** — every module, class, function, env var, CLI verb, and log-file name in
   the navigation map and RUNBOOK is checked to **exist** in the tree: a scripted grep/`python -c
   "import …"` over the post-fix `src/` + `pyproject.toml [project.scripts]` + the `SECTION_TOOLS`
   keys. Any name the docs invent but the tree lacks = failure. This is the check that catches "the
   docs describe a `backend_lifecycle.py` that M14's own ledger says was NOT split yet."
3. **Derived numbers** — the tool count (94) is asserted **== the registry-derived count** (§5.4), not
   trusted as typed.

The executor script is committed under `tests/` (or `audit/stage4/`) so the check is repeatable and
becomes the regression guard for future doc drift. Concretely (illustrative, executor finalizes):

```python
# doc_claims_check: fail if a named symbol is absent or a documented command misbehaves
import importlib, re, subprocess, sys, pathlib
# 2) symbols: every `module`/`ClassName`/`env_VAR` fenced as inline code in the nav map must resolve
# 1) commands: run each ```bash block; assert exit code matches the doc's stated expectation
# 3) counts: sum(len(v) for v in SECTION_TOOLS.values()) == 94 == the number printed in every doc
```

---

## 5. Test strategy — the 4-part validation protocol, concretized

Mirrors the Stage-1 onboarding audit that produced F-401..407, so the before/after is directly
comparable (the audit's verdict was: *can wire the MCP config, but cannot run tests via the documented
command; ~6 tribal steps; ~25 min to green only via an undocumented `.venv` workaround*).

### 5.1 Cold-agent onboarding re-run (the headline acceptance test)

**Setup.** A **fresh agent** (no conversation context, no audit access) is given **only**: (i) the 4
new docs + repaired README from a clean checkout, and (ii) the checkout itself. It must complete this
exact checklist unaided, and each step is pass/fail:

1. **Locate the source & clone it** — from README's "Clone & Setup" alone, state where the source
   lives and run the clone. *Pass:* clone command found and runnable. *(Was F-401: impossible.)*
2. **Install editable** — run the documented install. *Pass:* import succeeds
   (`python -c "import stealth_chrome_devtools_mcp"`).
3. **Run the unit suite green** — using the command the docs give. *Pass:* the documented command
   exits 0 with the suite green. *(Was F-403/F-404: the documented command hung/failed.)* The agent
   must NOT need to discover the `.venv` workaround on its own — the docs must hand it over.
4. **Start the MCP server** — from RUNBOOK. *Pass:* server reaches ready (log line / port bound).
5. **Run the manual MCP smoke path** — from RUNBOOK (F-405). *Pass:* one documented request gets one
   documented response, no silent hang. *(Was F-405: the stdio entrypoint hung silently, no smoke
   path existed.)*
6. **Locate 3 named subsystems by doc navigation alone** — e.g. "where does backend liveness get
   decided?" (`singleton.py` app-probe), "where is the response-body cap enforced?"
   (`network_interceptor._store_response`), "which module owns the one cloner engine?"
   (`cdp_element_cloner.py`). *Pass:* correct file named **without** reading bodies, using CLAUDE.md's
   map + glossary.

**Overall pass criterion:** all 6 green with **zero tribal-knowledge steps** (nothing the agent had to
guess that the docs didn't state). Time-to-green recorded for the before/after comparison, but the
binary gate is "6/6 unaided."

### 5.2 Name-only-navigation check (the clarity-lens acceptance target)

A **lighter model** is given the docs + the tree's *names* (file list, symbol names, glossary) and **5
representative change-requests**, and must route each to the **right file(s)** *without reading
function bodies*. The 5 tasks (chosen to span the subsystems the code plans reshaped):

| # | Change request | Correct target (must be named without reading bodies) |
|---|---|---|
| 1 | "Add a field to the complete-element clone schema" | `embedded/cdp_element_cloner.py` (the one engine) + the structure aspect; NOT the 4 deleted cloners |
| 2 | "Change the body-store cap default" | `embedded/network_interceptor.py` — `STEALTH_MCP_NETWORK_BODY_STORE_MAX_BYTES` (128 MiB) at `_store_response` |
| 3 | "Add a new `stealth-chrome-devtools` CLI verb" | `cli.py` (front-end) delegating to a `singleton.py` primitive — the two-surfaces story |
| 4 | "Make a tool raise a not-found error consistently" | `embedded/tool_errors.py` — `_require_tab`/`InstanceNotFoundError` (the one error convention) |
| 5 | "Change how a stale-source backend gets rebuilt" | `embedded/singleton.py` — `_source_fingerprint` + the reuse key, NOT `hot_reload` (deleted) |

**Pass criterion:** ≥4/5 routed to the exact file **and** the model cites the glossary/nav-map line
(not a body) as its evidence. A miss that lands in a *deleted* module (e.g. task 1 → `element_cloner.py`,
task 5 → `hot_reload`) is a hard fail — it means the docs let a stale mental model survive.

### 5.3 Doc-claim smoke (every runnable command executed verbatim)

Exactly §4 class 1+2, run by the Stage-4 executor as the S8 gate: every fenced command runs, every
named symbol resolves, on the actual post-fix tree in the actual `&`-and-spaces path. Output attached
to the checkpoint. This is what makes "the docs were verified against the tree" a fact, not a claim.

### 5.4 Tool-count assertion

`sum(len(v) for v in SECTION_TOOLS.values())` (from the post-M4 `tool_registry.py`) **== 94 == the
number printed by `--list-sections` == the number in every doc.** Committed as a tiny test so a future
tool addition that forgets to update a doc count is caught by CI (and, with F-108 option (a), the CLI
numbers can't drift at all because they're derived). If the human vetoes F-108 to option (b), this
assertion still guards the docs' `94`.

---

## 6. DESIGN.md content inventory (the binding rulings this doc MUST carry)

So the Stage-4 executor can verify completeness, DESIGN.md must state each of the following (each is a
BINDING ruling from a plan's `resolved_decisions` / `cross_review_notes`):

- **Two surfaces, one backend** (F-700/F-109): the **MCP tool surface** (94 tools over HTTP/stdio) and
  the **ops CLI** (`stealth-chrome-devtools` verbs) are two front-ends over the one shared backend
  process. The verb taxonomy's canonical copy lives in `tool_registry.py`'s docstring (F-760) —
  DESIGN references it, does not duplicate it.
- **Liveness semantics** (M1): the only correct liveness signal is a real MCP `initialize`→200
  app-probe, not a bare TCP connect; states are **responsive / wedged / down** (+ none); a wedged
  backend un-jams the *existing* eviction/respawn/orphan-reap machine (M1 added no new kill code).
- **The port invariant** (M8-A1/M15): the backend port is the **CHOSEN** port, possibly ≠ 19222; it
  flows via `server.json` `{port, version, pid, source_fingerprint}`; a foreign occupant of the
  target port forces an OS-assigned fallback (`_select_backend_port` → `_free_port`); `stop` resets to
  `DEFAULT_PORT`; **never re-hardcode 19222.** Discovery/reuse read the recorded port.
- **Source-fingerprint reuse + always-fresh dev backend** (M2): reuse is gated on a SHA-256 over the
  package `*.py` source (composed with the version key), so an in-place edit evicts+respawns via the
  existing path; `hot_reload`/`reload_status` are **deleted** — the one way code reaches the backend
  is a fresh backend; an **empty fingerprint never matches** (fail-closed at record time).
- **The cloner transport table + the EVENTS rationale** (M5b — load-bearing per state.json note 29):
  styles + structure = **CDP**; events + animations + assets + related_files = **JS-eval**, inside the
  ONE canonical `CDPElementCloner` engine. The events rationale MUST be stated so a future all-CDP
  "purification" cannot silently regress it: **CDP `DOMDebugger.getEventListeners` sees only
  `addEventListener`-registered listeners — it misses inline `on*` handlers and framework/synthetic
  handlers (React attaches one delegated root listener; per-element handlers are invisible).** The
  transport is pinned by a path-assertion test.
- **Hook semantics = FIRST-MATCH-BY-PRIORITY, not a chain** (M12a): the CDP `Fetch.RequestPaused`
  disposition is terminal (exactly one per paused request), so the highest-priority matching hook wins
  and lower-priority matches are shadowed; a runtime WARNING names winner + shadowed hooks. This is the
  domain's semantics, not a bug.
- **The ONE import convention** (M4-STEP0): absolute-from-package imports everywhere
  (`from stealth_chrome_devtools_mcp.embedded.X import Y`); an embedded module must **NEVER import
  `server`** (double-registration under `runpy` `__main__`); there is exactly **one** sanctioned
  `sys.path` shim (the compatibility bridge); `server.py` stays loaded via
  `runpy.run_path(run_name="__main__")` from the top-level shim.
- **The ONE error convention** (M4-A1): **raise `ToolError` / `InstanceNotFoundError`** (helpers return
  values on success) — NOT a `{"success": bool}` dict on every tool. The **named KEEP contracts** (the
  dict/value IS the contract, converting would be a defect): the result-envelope success dicts
  (`execute_script`, `create_python_binding` success paths — G5 pin), the diagnostic dict
  (`validate_browser_environment_tool`), the input-validation value-returns (`expand_children`,
  `clone_element_to_file` bad-arg returns), and the deliberate fallbacks
  (`query_elements` loop-resilience, `get_response_content` base64-alt, `get_instance_state` F-746
  blessed-partial, `clear_debug_view` bool, `export_debug_logs` guidance-string).
- **clone_storage (disk quota/GC) vs the cloner ENGINE (extraction)** boundary (M4-C1/M5b):
  `clone_storage.py` is the on-disk profile/clone quota + GC subsystem (server.py-side storage);
  `CDPElementCloner` is the DOM-extraction engine. M5b must never converge a cloner into `clone_storage`;
  they are distinct concerns that happen to share the word "clone" (glossary pins both).
- **Network capture off-by-default + caps** (M9): body capture is opt-in
  (`STEALTH_MCP_NETWORK_CAPTURE_BODIES`, default False); always-on byte caps — per-body 5 MiB
  (`…BODY_MAX_BYTES`), total-store 128 MiB (`…BODY_STORE_MAX_BYTES`) with FIFO eviction; env parsed via
  the canonical `env_utils` (not hand-rolled `os.getenv` — a second parser would be a conventions
  defect).
- **`activate()`-at-serve-boundary process cleanup** (M11a): `ProcessCleanup.__init__` is
  side-effect-free; recovery + handler-install move behind `activate()`, called once in
  `app_lifespan` startup; mere import (tests, the ops CLI's read-only commands, the stdio proxy) does
  zero reaping. A public `recover_orphans()` seam backs `kill-orphans`.
- **`in_memory_storage` semantics** (M15): the store is **deliberately non-durable** (cleared on every
  graceful shutdown, only ever a secondary cross-check in `list_instances`); the M15 rename from
  `persistent_storage` fixes the misnomer — durability was rejected (would resurrect stale instances).
  `BrowserInstance` is serialized whole via `model_dump(mode="json")` (no more silent field-drop).
- **Executor-offloaded teardown** (M7): `close_instance` runs the blocking kill via `asyncio.to_thread`
  under a real `wait_for` with the `_instances` lock released across it, so one wedged close can no
  longer freeze every other session's dispatch (the `wait_for(5.0)` bound is now real).
- **The observability spine** (M3): file logging on both fronts — `backend-<pid>.log` (in-process
  `RotatingFileHandler`) + `backend-boot.log` (raw `Popen` redirect for pre-`main()` crashes) +
  per-proxy `proxy-<pid>.log`, all under `logging_setup.resolve_log_dir()`; a per-MCP-request
  correlation id stamped at the `section_tool` chokepoint.
- **The known-debt ledger** (§2.5).

RUNBOOK content inventory: CLI verbs (`status`/`doctor`/`stop`/`restart`/`kill-orphans` + `serve`); log
locations (`logging_setup.resolve_log_dir()`, `backend-<pid>.log`, `backend-boot.log`, `proxy-<pid>.log`
— the F-503 log-path half M8 surfaced); wedged-backend recovery (`doctor` → `wedged` → `restart`);
port-fallback behavior (foreign occupant → fallback port, read from `server.json`); orphan reaping
(`kill-orphans`); the manual MCP smoke path (F-405 — the exact request/response); crash/hang
first-response steps; reading `status` — **[A1] documents the RENAMED output (§A1.2): the disk-cap
lines now read glossary-conformant ("browser-session root" / "browser-session cap"), so the operator
is never misled into thinking `status`'s "session" is their MCP session; a one-line note explains the
cap trims named browser-profile directories, not connection/backend behavior — the exact
`why_it_matters` harm F-741 names**.

CONTRIBUTING content inventory: clone→install(editable)→test with **the venv python** (the uv-run
special-char failure F-403/F-404 documented with its cause); the real gate — **pytest + coverage
`--cov-fail-under=39` in CI only** (pyproject deliberately keeps coverage out of `addopts`); **no
linter/type-checker/formatter is configured** — stated honestly (F-406), do not invent one;
branch/PR/checkpoint-commit conventions (from this pipeline's own fix-branch discipline: one checkpoint
commit per independently-verifiable step, suite green at each); golden discipline — **two-tier: HARD
invariants never bend, SOFT goldens update deliberately in the PR that changes a schema, with
justification** (M6/M5b); the four lenses as review criteria incl. "a second way of doing something is
a defect"; where the verb taxonomy lives (`tool_registry.py` docstring).

CLAUDE.md content inventory: the name-only navigation map of the post-fix tree (§7); the glossary
(§2.2); tool count = **94** + provenance (`SECTION_TOOLS`, `--list-sections`); the conventions an agent
must follow (import form, error convention, one-engine rule, golden discipline, "a second way is a
defect").

---

## 7. The post-fix navigation map (CLAUDE.md core — asserted symbol-by-symbol at S8)

The map names the modules of the tree **as the 11 plans leave it**. Every name below is checked to
exist at Stage 4 (§4 class 2). Deltas from HEAD that the map must reflect:

- **NEW modules:** `embedded/logging_setup.py` (M3), `embedded/env_utils.py` (M11a),
  `embedded/tool_registry.py` (M4: `SECTION_TOOLS` + `section_tool` + verb taxonomy docstring),
  `embedded/tool_errors.py` (M4: `ToolError`/`InstanceNotFoundError`/`_require_tab`/`_require_browser`),
  `embedded/clone_storage.py` (M4: the 47-helper profile/clone quota+GC subsystem).
- **RENAMED:** `embedded/persistent_storage.py` → `embedded/in_memory_storage.py`
  (singleton `persistent_storage` → `in_memory_storage`; class `InMemoryStorage`) (M15).
- **DELETED:** `embedded/response_stage_hooks.py` + its test (M12a); the `hot_reload`/`reload_status`
  tools (M2, tools not a module).
- **CLONER end-state (M5b):** `cdp_element_cloner.py` is the ONE canonical engine; the twin/overlapping
  variants (`element_cloner.py` and the redundant siblings) are consolidated away; `FileBasedElementCloner`
  class name is **KEPT** (protects two `output_dir` tripwire tests — do NOT "clean up" the misleading
  filename `test_element_cloner_output_dir.py`).
- **UNCHANGED core the map still points at:** `singleton.py` (backend lifecycle + stdio proxy — still
  one file; F-740 split is *deferred debt*, and the map must say so rather than describe a split that
  didn't happen), `browser_manager.py`, `network_interceptor.py`, `dynamic_hook_system.py`,
  `proxy_forwarder.py`, `cli.py`, `server.py` (still the tool-body host post-Ph1; full per-section
  split is Ph2 debt).

The map is the clarity-lens acceptance target: a lighter model must place a change using it **without
reading bodies** (§5.2). Where the map would tempt a reader toward a *deleted* module, it says so
explicitly ("`element_cloner.py` — REMOVED, use `cdp_element_cloner.py`"; "`hot_reload` — REMOVED, edits
apply via a fresh backend").

---

## 8. Rollback + checkpoints

**Trivial — this is documentation.** Every step (S1–S8) is its own checkpoint commit on the fix
branch. Reverting any doc is `git revert <sha>` with **zero** runtime effect (no doc executes; the
build and suite are untouched by S1–S4, S6, S7). The **only** step with any runtime surface is **S5**
(the F-108 code touch); it is a **separate, independently-revertible commit**, so if it regresses
anything (it prints numbers — it won't) or the human prefers option (b), reverting S5 alone drops the
plan to docs-only while every documented `94` still stands. No migration, no data, no config change to
roll back.

---

## 9. Risk

- **Docs drift from the tree (the core risk).** *Mitigation:* the doc-claim accuracy protocol (§4) —
  every command executed, every symbol resolved, every count derived — runs as the S8 gate and is
  committed as a repeatable check; the F-108 one-source rule (§2.6) removes the count-drift mechanism
  entirely; the navigation map is asserted symbol-by-symbol so a doc describing a not-yet-done split
  (F-740) fails the check.
- **Scope-creep into code fixes.** The plan touches a lot of *findings*, and the temptation is to "just
  fix" a small one while documenting it. *Mitigation:* the debt-ledger discipline (§2.5, §1.5) —
  everything except the **two human-sanctioned touches** (F-108 Touch 1, F-741 Touch 2) is RECORDED,
  and each touch is fenced to its own commit. The executor rejects any diff outside the doc files +
  the F-108 `__main__` lines + the §A1.2/§A1.3 `cli.py` (and possibly one `clone_storage`/env) lines.
- **[A1] The F-741 rename's blast radius.** The renamed labels are **print-only display strings**
  (`_cmd_status`/`_cmd_doctor`/`_cmd_cleanup`), so changing them is behavior-neutral. The one place
  with real reach is the **env var / backing helper** (`STEALTH_MCP_SESSION_STORAGE_CAP_GB`,
  `_session_storage_cap_bytes`) — a user-facing config contract. *Mitigation:* the §A1.1 consumer sweep
  enumerates every reader (code + tests + README); the §A1.3 decision keeps the env var name stable by
  default (renaming a documented knob silently breaks the maintainer's shell config for zero external
  benefit at 0 users), so the default-path rename is display-only and its worst case is a cosmetic
  label revert. The §A1.5 pin prevents a future edit from silently reintroducing a bare "session"
  label, and `test_status_runs` is updated in the same commit so the suite stays green.
- **Describing the intended tree vs the actual tree.** Because M14 plans against the *post-fix* tree
  before the executor has the tree in hand, a plan claim could describe an intended-but-not-landed
  state. *Mitigation:* the executor plans nothing — it *reads* the tree at Stage 4 and every named
  symbol must exist; the map explicitly encodes what did NOT change (singleton not split, server.py
  not fully split, F-104 delivered not deferred) so the actual/intended gap is closed at authoring.
- **Cold-agent / lighter-model checks are subjective.** *Mitigation:* both are scripted to binary
  pass criteria (§5.1 6/6 unaided; §5.2 ≥4/5 with glossary-cited evidence and hard-fail on
  deleted-module misses), mirroring the Stage-1 onboarding audit so before/after is comparable.

---

## 10. Findings closed

**Closed by this plan:**
- **F-401** (no git-clone/checkout step) — README "Clone & Setup" (§2.3, S6).
- **F-402** (MCP-config snippet unverifiable) — the config snippet's commands are executed in the
  doc-claim smoke (§4/§5.3); the local-checkout vs pip-installed distinction is documented.
- **F-403** (`uv run pytest` FAILS on this special-char path) — documented truthfully with the working
  venv-python command and the root cause; the *failing* command is asserted-to-fail so the claim is
  verified (§2.7, §5.3).
- **F-404** (documented CLI verb not recognized — install/PATH gap) — README/CONTRIBUTING give the
  `python -m …` invocation that resolves without uv/PATH (§2.7).
- **F-405** (MCP stdio entrypoint has no manual smoke path — hangs silently) — RUNBOOK's manual MCP
  smoke path, exercised in the cold-agent re-run (§5.1 step 5).
- **F-406** (zero quality-gate docs) — CONTRIBUTING documents the gate that EXISTS (pytest + coverage
  39 CI) and states honestly that no linter/type-checker/formatter is configured (§1.5, §6).
- **F-407** (no contributing/branch/PR/release conventions) — CONTRIBUTING (§6).
- **F-306** (no RUNBOOK/ops doc) — RUNBOOK.md (§1.1, §6).
- **F-108** (three hand-maintained tool counts disagree; true count 94) — counts derived from
  `SECTION_TOOLS` (option (a), approved at gate, §2.6) + the count-assertion test (§5.4); every doc
  says 94.
- **F-741** (glossary — "session" means ≥3 things, two collide in `status`) — **[A1] FULLY closed,
  code + docs (not doc-only).** The glossary pins one meaning per term (§2.2), AND — per the human's
  gate overturn — the colliding CLI `status`/`doctor`/`cleanup` labels are **renamed** glossary-
  conformant (Amendment A1, Touch 2), with the output-shape pin (§A1.5) guaranteeing the collision
  cannot silently return. The remaining "proxy" overload is closed by the glossary (pinned, not
  renamed — §1.5, §A1.6), which is the finding's own recommended remedy for that half.

**Recorded as debt (their disposition IS the closure — quoted in the DESIGN.md ledger, §2.5):**
- **F-740** (singleton.py → backend_lifecycle + stdio_proxy split, wave 4).
- **F-702** (BrowserManager 6-concern split, M4-Ph2 era).
- **F-703** (DebugLogger serialization engine, wave-4 cleanup).

**Explicitly noted as NOT-in-the-ledger:** **M10b / F-104** does **not** appear — it was **DELIVERED**
via M4-Ph1 Amendment A1 (F-104 CLOSED). Recording it would be a false claim (§2.5).

---

## Amendment A1 — F-741 CLI status field renames (human-ordered at gate 2026-07-03)

> **Status: APPROVED by human 2026-07-04** (the amendment; base plan approved 2026-07-03). Gate ruling on the §A1.3 open question: **Option X-HARD — rename the env var + CLI flag outright, NO back-compat alias** (supersedes the planner-recommended Option Y; ruling folded below, marked [GATE RULING 2026-07-04]).
> The human OVERTURNED §2.2's doc-only resolution of the F-741 CLI `status` collision and ordered the
> **code-field renames pulled INTO M14**. This amendment adds M14's **second fenced code touch**
> (Touch 2): glossary-conformant renames of the colliding `status` output, a consumer sweep, the
> env-var decision, and an output-shape pin. Base tree = **post-all-11-plans** — every anchor below is
> re-anchored by SYMBOL on the *final* `cli.py` shape (post-M1 rewrite + M8 pid/log extension + M4
> storage-helper move to `clone_storage.py`), NOT the HEAD line numbers. All old→new tables are stated
> at HEAD for identification and MUST be re-located by symbol at Stage 4.

### A1.1 The exact collision at HEAD + full consumer sweep

**The finding's own quote** (F-741, verified verbatim) names two senses colliding *directly in the
`status` output*, backed by these HEAD sites (`cli.py`):

| # | HEAD site | Prints (verbatim) | Backing symbol | Sense |
|---|---|---|---|---|
| 1 | `_cmd_status:103` | `session root: {root}  (exists: …)` | `server._default_session_root()` | disk dir of browser profiles |
| 2 | `_cmd_status:105` | `session cap : {…}  [STEALTH_MCP_SESSION_STORAGE_CAP_GB]` | `server._session_storage_cap_bytes()` | disk cap on named profiles |
| 3 | `_cmd_doctor:172` | `session root: {root}  (exists: …)` | `server._default_session_root()` | (same as #1) |
| 4 | `_cmd_cleanup:137` | `caps        : clone {…} \| session {…}` | `session_cap` local | disk cap on named profiles |
| 5 | `_cmd_cleanup:132/157` | (no label; identifiers) | `server._named_profiles_over_session_cap`, `session_cap` | internal |
| — | `build_parser:236` | `status` help `"…session root, and storage caps"` | (help string) | doc-string echo of #1/#2 |
| — | `build_parser:254` | `doctor` help `"…platform, session root, backend…"` | (help string) | doc-string echo of #3 |
| — | `build_parser:242` | `cleanup` help `"…over the session cap …"` | (help string) | doc-string echo of #4 |
| — | `build_parser:246-247` | `--session-cap-gb` flag + help | `dest="session_cap_gb"` | CLI arg (see §A1.3) |

The colliding word is **"session"** — sense (2) here is *a named browser-profile-clone directory on
disk*, which reads identically to the *MCP/Claude-Code session* sense the `singleton.py` docstring uses
one screen away. (`singleton.py`'s docstring "multi-session environments" and `process_cleanup.py:361`
"pre-date this server session" are the other two senses; those are pinned by the **glossary**, not
renamed — §A1.6.)

**Consumer sweep — everything that reads the colliding labels/keys (grepped across `src/` + `tests/` +
docs, excluding `.venv/`/`element_clones/`/root scratch scripts):**

*Print-only labels (display strings — rename is behavior-neutral):*
- `cli.py:103,105,137,172` (the four printed lines above) and the three `build_parser` help strings
  (`:236,242,254`).

*The backing helper + env var (the ONE contract-bearing surface):*
- `server._session_storage_cap_bytes()` — **defined** `server.py:583`; **post-M4 it moves to
  `clone_storage.py`** (M4-C1 moves all 47 storage helpers; `_session_storage_cap_bytes` is one — the
  Stage-4 anchor is `clone_storage.session_storage_cap_bytes` per M4's underscore-drop naming, called
  from `cli.py`).
- `STEALTH_MCP_SESSION_STORAGE_CAP_GB` — read at `server.py:588` (`parse_float_env(...)`, post-M4 in
  `clone_storage.py`); **documented in `README.md:110` and `README.md:207`** (env table). This is the
  user-facing knob (§A1.3).
- `_named_profiles_over_session_cap` — `server.py:661` (def), called `:711`, `cli.py:132`; post-M4 in
  `clone_storage.py`. Internal identifier.

*Tests that pin the colliding surface (must stay green / be updated — see §A1.4):*
- `tests/test_cli.py:57-59` `test_status_runs` — **asserts `"session root" in out.lower()`** (the one
  hard assertion on the label; M8's plan explicitly keeps this green — plan_M8 §Step-3 verify).
- `tests/test_clone_trash_recovery.py:107,148`, `tests/test_sweep_deferred_cleanup.py:80` — call
  `_named_profiles_over_session_cap` / pass `session_cap=` **kwargs** (identifier-level, not the label).
- The `tmp_session_root` **conftest fixture** (`tests/conftest.py:39`) + its ~40 call sites across
  `test_profile_resolution.py`, `test_browser_integration.py`, etc. — this fixture name uses "session
  root" in the **glossary-valid** sense (the browser-profile disk root); **NOT renamed** (see §A1.2
  "what stays").

*Docs that show the colliding output (updated by the base plan's doc steps, now against renamed text):*
- `README.md:221` (`status` comment "session root + caps"), `README.md:231` (cleanup prose "session
  cap"), `README.md:110,207` (the env-var rows). Updated in S6.
- `audit/stage1/incident_trace.md` / `onboarding_trace.md` — these are **frozen Stage-1 audit
  evidence** (the before-snapshot); **NOT edited** (rewriting audit evidence would corrupt the
  before/after comparison — they are historical, like the retired docs).

### A1.2 The rename (old → new) — display labels + one internal helper; the fixture stays

The glossary (§2.2) is the authority: the disk dir holding browser profiles is the **"browser-session
root"** (a `spawn_browser(session_name=…)` profile lives under it); its cap is the **"browser-session
cap."** Qualifying bare "session" with "browser-" is the minimal change that makes each label say which
of the three senses it means.

| Old (HEAD) | New | Kind | Site (re-anchor by symbol) |
|---|---|---|---|
| `session root: {root}` | `browser-session root: {root}` | print label | `_cmd_status`, `_cmd_doctor` |
| `session cap : {…}` | `browser-session cap : {…}` | print label | `_cmd_status` |
| `caps : clone {…} \| session {…}` | `caps : clone {…} \| browser-session {…}` | print label | `_cmd_cleanup` |
| `status` help "…session root, and storage caps" | "…browser-session root, and storage caps" | help string | `build_parser` |
| `doctor` help "…platform, session root, backend…" | "…platform, browser-session root, backend…" | help string | `build_parser` |
| `cleanup` help "…over the session cap …" | "…over the browser-session cap …" | help string | `build_parser` |
| `_session_storage_cap_bytes` (→ M4 `session_storage_cap_bytes`) | `browser_session_storage_cap_bytes` | internal fn | `clone_storage.py` def + `cli.py` call |
| `_named_profiles_over_session_cap` (→ M4 `named_profiles_over_session_cap`) | *unchanged* | internal fn | already says "named_profiles"; "session_cap" arg is local — leave (renaming ripples 3 test kwargs for zero clarity gain) |

**What deliberately STAYS (rename would be wrong or net-negative):**
- **`STEALTH_MCP_SESSION_STORAGE_CAP_GB`** — the env var. **Kept as-is** (see §A1.3 for the full
  argument): it is a user-facing config contract, and the display-label rename + a one-line RUNBOOK/env
  note already removes the operator's "does this affect my MCP session?" confusion without breaking a
  documented knob.
- **`tmp_session_root` fixture** and `_default_session_root` / `STEALTH_MCP_BROWSER_SESSION_ROOT` — these
  already use "session" in the **glossary-blessed "browser-session root"** sense; the env var
  `STEALTH_MCP_BROWSER_SESSION_ROOT` even carries the "BROWSER_SESSION" qualifier already. Renaming the
  fixture would churn ~40 test call sites for no clarity gain (the collision is the bare "session"
  *label* in `status`, not the well-qualified root symbol).
- **`--session-cap-gb` CLI flag** — kept (see §A1.3): renaming a CLI flag is a harder break than an env
  var (muscle-memory + any wrapper scripts), and its `cleanup` help string is already reworded to
  "browser-session cap" so the flag's meaning is unambiguous at point of use.

### A1.3 The env-var / CLI-flag decision (argued both ways; recommend KEEP-NAME + relabel)

**The tension:** the human's overturn wants the *code fields* renamed, not just labels. `STEALTH_MCP_
SESSION_STORAGE_CAP_GB` and `--session-cap-gb` are the surfaces where "field" and "user contract" meet.

- **Option X — rename the env var too** (`STEALTH_MCP_BROWSER_SESSION_STORAGE_CAP_GB`) and the flag
  (`--browser-session-cap-gb`). *For:* maximal glossary-conformance, the "field" is renamed literally.
  *Against:* it **silently breaks the maintainer's existing shell/MCP-config** (a set
  `STEALTH_MCP_SESSION_STORAGE_CAP_GB` stops being read → the cap silently reverts to the 20 GB
  default), and it breaks any wrapper invoking `cleanup --session-cap-gb`. For a **documented** knob at
  **0 external users but a real single maintainer**, this is a config-break for cosmetics — the exact
  "user-specific breakage for a cosmetic win" the project's own working style rejects.
- **Option Y (RECOMMENDED) — keep the env var and flag names; rename the display labels + the internal
  helper; add a one-line meaning note.** *For:* the operator-facing confusion F-741 names ("they could
  tune `STEALTH_MCP_SESSION_STORAGE_CAP_GB` expecting it to affect connection/backend behavior") is
  cured by the **relabeled output** ("browser-session cap") + a RUNBOOK/env line stating the cap trims
  *named browser-profile directories*, not MCP/backend behavior. The one genuinely mis-scoped
  *identifier* (`_session_storage_cap_bytes`) IS renamed. No contract breaks. *Against:* the env var
  string still contains bare "session" — but it is now unambiguous **in context** (its value is printed
  next to "browser-session cap", and the env table row explains it).

**Recommendation: Option Y.** It satisfies the overturn's intent (the colliding *labels* and the
mis-scoped *helper* are renamed; the collision is gone from what the operator reads) without a silent
config break. **Flagged for the human** as an A1 open question: if they want the literal env-var/flag
rename (Option X) despite the config break, add a **back-compat alias** — read the new name first, fall
back to the old with a one-time deprecation `warning` — so no config silently breaks; that alias is
itself a *second* way to spell the knob (a mild conventions cost) and is why Y-without-alias is the
lean default. (Either way the RUNBOOK documents the final name.)

**[GATE RULING 2026-07-04] — the human chose Option X-HARD: rename outright, NO alias.**
- `STEALTH_MCP_SESSION_STORAGE_CAP_GB` → **`STEALTH_MCP_BROWSER_SESSION_STORAGE_CAP_GB`** (the
  `parse_float_env` read in `clone_storage.py` post-M4, plus its docstring mention).
- `--session-cap-gb` → **`--browser-session-cap-gb`** (`build_parser`; `dest` becomes
  `browser_session_cap_gb`; the one `args.session_cap_gb` consumer in `_cmd_cleanup` updates with it).
- **No fallback read of the old name** (clean cutover; a lingering old var is simply ignored and the
  cap reverts to the 20 GB default). Mitigations, all mandatory: (i) the README env-table row and the
  RUNBOOK env note document the NEW name with a one-line migration notice ("previously
  `STEALTH_MCP_SESSION_STORAGE_CAP_GB` — update your shell/MCP config"); (ii) the §A1.5 pin is
  EXTENDED to assert the new env-var name appears in the `status` cap line (`[STEALTH_MCP_BROWSER_
  SESSION_STORAGE_CAP_GB]`), so the rename is test-guarded end to end; (iii) the S6 README edit and
  this rename land in the SAME S5b-adjacent checkpoint window so no committed state documents the old
  name against renamed code. Rationale for X-hard over X-alias: family-consistency with the sibling
  `STEALTH_MCP_BROWSER_SESSION_ROOT` (which already carries the qualifier) and zero second-way spelling
  period; the single maintainer updates their own config once, guided by the docs this very plan ships.
  This supersedes the Option-Y bullets in §A1.2's "what deliberately STAYS" list (env var + flag move to
  the rename set) and the Option-Y phrasing in §A1.5/§A1.7/§A1.8.

### A1.4 Predecessor interaction + the test updates (base = post-all-11-plans)

**Predecessor shifts M14/A1 must re-anchor over (by symbol, not line):**
- **M1** rewrote `_cmd_status`/`_cmd_doctor` to call `_probe_backend_status()` and print
  `responsive/wedged/down` — the `backend :` line is M1's. A1 does **not** touch that line; it renames
  only the `session root`/`session cap` lines that sit below it. Re-anchor on the `session root:` /
  `session cap :` print substrings **inside** the M1-rewritten functions.
- **M8** added the **pid** + **log dir/`backend-<pid>.log`** lines to both `status` and `doctor`
  (Step-3) and left `test_status_runs` asserting `"session root"`. A1's label rename lands on the
  M8-final output; **the `test_status_runs` assertion string changes** `"session root"` →
  `"browser-session root"` in the same commit (§A1.5).
- **M4-C1** moved the 47 storage helpers (incl. `_session_storage_cap_bytes` →
  `session_storage_cap_bytes`) into `clone_storage.py` and repointed `cli.py`'s calls to
  `clone_storage.*`. A1's helper rename (`session_storage_cap_bytes` → `browser_session_storage_cap_bytes`)
  applies **in `clone_storage.py`** and updates the single `cli.py` call site. Re-anchor by the function
  name, not the HEAD `server.py` line.
- **M15** (F-762) nested some state under `STATE_DIR`; unrelated to the session-cap labels. No shift.

**Test updates (same commit as the rename, so the suite is green at the checkpoint):**
- `test_cli.py::test_status_runs` — assertion `"session root"` → `"browser-session root"` (lower-cased,
  matching the new label). This is the only existing test asserting the label text.
- `test_clone_trash_recovery.py` / `test_sweep_deferred_cleanup.py` — **no change** (they pass
  `session_cap=` as a kwarg to `named_profiles_over_session_cap`, whose signature is unchanged per §A1.2).
- No fixture rename → no change to the ~40 `tmp_session_root` call sites.

### A1.5 The output-shape pin (so a future edit can't reintroduce bare "session")

Add to `test_cli.py` (per the base plan's pure-filesystem CLI-test convention, `tmp_session_root`
fixture, `capsys`): a `test_status_labels_are_glossary_conformant` that runs `_cmd_status` (and
`_cmd_doctor`) under the isolated root and asserts on the captured stdout:
- the **new** labels are present: `"browser-session root"` in the output;
- the **bare** collision is absent from the cap/root display lines: no line matching
  `^\s*session (root|cap)\b` (i.e. "session" as a standalone leading label) — the regex allows
  "browser-session" but fails a reverted bare "session root"/"session cap".

This is the tripwire that makes the F-741 closure durable: a later edit that types `session cap` again
fails the pin. (It asserts on **display text**, deliberately not on the env-var string, consistent with
the §A1.3 keep-the-knob decision.)

### A1.6 The "proxy" overload half — glossary, not rename (recorded here for completeness)

F-741 also names a "proxy" overload: `proxy_forwarder.py`/`proxy_utils.py` (outbound browser egress
proxy) vs `singleton.py`'s `run_stdio_proxy`/`_proxy_streams` (the inbound stdio↔HTTP bridge). The
human's overturn was specifically about the **CLI `status` "session" collision**; the proxy overload is
**closed by the glossary** (§2.2 pins "stdio proxy" as the bridge; DESIGN names the egress proxy as the
browser's outbound proxy), which is the finding's *own* stated remedy ("a short glossary … pinning one
meaning per term would prevent recurrence"). No proxy renames in M14 — recorded so the executor does not
expect them.

### A1.7 Sequencing, rollback, checkpoint

- **Slots at S5b** (§3), immediately after the F-108 touch (S5), before README (S6) and validation
  (S8). One checkpoint commit: `M14-S5b: rename F-741 status labels + helper + pin`.
- **Independently revertible** — `git revert <S5b>` restores the old labels with zero effect on any
  other commit (the two code touches are adjacent and separable; docs are separate commits).
- **Rollback trivial:** display-string + one-identifier change + one test edit + one new test; no
  migration, no data, no config change (the env var is untouched under the recommended Option Y).

### A1.8 Findings closed by A1

- **F-741 — FULLY CLOSED (code + docs).** Glossary pins one meaning per term (base plan §2.2); the
  colliding CLI `status`/`doctor`/`cleanup` **labels are renamed** glossary-conformant and the one
  mis-scoped **helper** (`session_storage_cap_bytes` → `browser_session_storage_cap_bytes`) is renamed;
  the **output-shape pin** (§A1.5) makes the closure durable; the **env var is deliberately kept**
  (§A1.3) to avoid a silent config break, with its meaning documented in the RUNBOOK/env table. The
  "proxy" half is closed by the glossary (§A1.6). This upgrades the base plan's doc-only F-741 closure
  to the code+docs closure the human ordered.
