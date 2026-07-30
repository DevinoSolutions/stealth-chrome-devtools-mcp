# HANDOFF — Execution context & current goals (offloaded 2026-07-16)

**Purpose of this file.** A self-contained briefing so any fresh agent can pick up
the `stealth-chrome-devtools-mcp` release-hardening effort without the originating
conversation. It captures *verified state at HEAD*, the *goal*, the *hard gate*,
the *ordered path*, and the *binding constraints*. **Re-verify every fact before
acting — this is context, not proof.** Where a fact is load-bearing, the
re-verification command is given.

---

## 0. One-paragraph orientation

We are hardening this MCP so that "the automated gate is green" becomes a faithful
stand-in for "a real user on Linux, Windows, or macOS gets software that works
well, is fast/lean, and fails safe." The *plan* for that (`plan_RELEASE.md`,
workstreams **W1–W16**) is written, adversarially reviewed, remediated, and
approved-in-principle. **None of it is implemented yet, and by the plan's own rule
it may not begin** until three earlier FIX workstreams land through the human
merge-gate: **M4-Ph1+A1 → M5b → M14+A1**. Only **M4-Ph1** is in motion (a WIP draft
PR, 5 of 7 chunks done). The immediate, gate-legal task is to finish **M4-Ph1's C4
and C5 chunks**. Everything downstream is queued behind that.

---

## 1. The product (what we're gating)

A stealth (nodriver/CDP-based) Chrome DevTools MCP server exposing **94 tools**
over stdio via a `fastmcp` `Client`. Headline promise = *undetectable browser
automation*. Console entrypoint: `stealth-chrome-devtools-mcp = "…server:main"`
(a second CLI surface `stealth-chrome-devtools = "…cli:main"` also ships).

### Honest confidence scorecard today (the reason this effort exists)
| Claim we want to make | Reality at HEAD |
|---|---|
| Works on Linux, **Windows, macOS** | CI is **Ubuntu-only**. Zero Windows/macOS CI. |
| Undetectable (the headline) | **Zero runtime stealth tests.** Only `test_stealth_args.py` (launch-flag sanitizer). |
| All 94 tools work | `get_cookies` broken (known, documented); ~9 other bugs pinned as characterization, routed, unfixed. |
| Installing the published artifact works | **Never tested** (CI tests the source tree only). |
| Great performance | **Zero** perf/latency/resource tests. |
| Fails safe | **Zero** fault-injection/resilience tests. |
| Green gate == manual QA pass | ~22 of 113 manual-QA steps have live automated coverage (~19%). |

`plan_RELEASE.md` W1–W16 is designed to close **every** row above.

---

## 2. Authoritative current state (verified 2026-07-16)

Re-verify: `git branch --show-current && git rev-parse --short HEAD && git status --short`

- **Execution HEAD**: `a1267db` on `agent/e2e9-plan-remediation`, working tree clean.
- **`origin/main`**: `49126ce` — **IS** an ancestor of HEAD
  (`git merge-base --is-ancestor origin/main HEAD` → true).
- **Remote branches (authoritative — `git ls-remote --heads origin`): exactly 4.**
  - `agent/e2e9-plan-remediation` `a1267db`  (PR #37 head)
  - `agent/e2e9-release-plan`      `06b0a06`  (PR #36 head; PR #37 base)
  - `audit/fixes-2026-07-02-m4ph1` `1112dd1`  (PR #35 head — M4-Ph1 WIP)
  - `main`                          `49126ce`
- **PR stack** (all draft, unmerged, held at human merge-gate):
  - **#35** `audit/fixes-2026-07-02-m4ph1` → `main` — *M4-Ph1+A1 server.py decomposition [WIP — C3b of 7 done; C4/C5 remain]*
  - **#36** `agent/e2e9-release-plan` → `main` — *E2E-9 release-gate plan + manual QA protocol*
  - **#37** `agent/e2e9-plan-remediation` → `agent/e2e9-release-plan` — *harden E2E-9 plan* (stacked on #36)
- **Local `main` pointer anomaly (hygiene flag):** in the originating session local
  `main` pointed at `06b0a06` (inside the e2e9 stack), **not** `origin/main`
  (`49126ce`). Verify `git rev-parse main` and, if it is not `49126ce`, reset it —
  but this is a human call, do not silently rewrite the stack.

### Plan artifacts
- `audit/stage2/plan_RELEASE.md` — status: **"REMEDIATED AFTER INDEPENDENT
  COVERAGE VALIDATION; REVALIDATION REQUIRED BEFORE IMPLEMENTATION."** Workstreams
  **W1–W16**. Zero implemented. Phase 0 must be re-run at the exact execution HEAD
  and return GO / GO-with-changes before RELEASE-1 begins.
- `tests/MANUAL_QA_PROTOCOL.md` — **MQ-1…113 live baseline.** MQ-114…162 are
  *reserved future ownership* (referenced by W7/W9/W10/W12–W16) and are **NOT
  current coverage**; each must be appended atomically (MQ step + its live test +
  parity update + evidence-ledger update in one commit).
- Prerequisite plans present as *documents only*: `plan_M4ph1.md`, `plan_M5b.md`,
  `plan_M14.md`. **A plan doc is not landed code.**

---

## 3. The HARD GATE (why implementation is blocked)

`plan_RELEASE.md` §Position: *"executes AFTER the audit FIX pipeline lands
(M4-Ph1+A1 → M5b → M14+A1) through the human merge-gate … record the three
prerequisite merge SHAs and prove each is an ancestor of the execution base with
`git merge-base --is-ancestor <sha> HEAD`. A matching file or branch name is not
proof. If any ancestor check fails, STOP and raise the sequencing item."*

**Current gate result = BLOCKED / NO-GO for RELEASE-1:**
- **M4-Ph1+A1**: branch exists (`1112dd1`), PR #35 **open/draft, mergedAt=null**,
  **not** an ancestor of HEAD, **not** merged to `origin/main`. C4/C5 incomplete.
- **M5b**: **no branch at all** — plan doc only.
- **M14+A1**: **no branch at all** — plan doc only.
- Therefore no authoritative landing SHAs exist; the ancestry proof cannot pass;
  a Phase-0 re-run "at that HEAD" is unreachable.

**PLAN GO ≠ implementation GO.** Do not infer prerequisite landing from branch
names, bookkeeping commits, plan text, partial-green CI, or PR descriptions. Do not
choose substitute prerequisite SHAs. If stack topology conflicts with a valid
execution base, STOP and ask the human.

---

## 4. Ordered execution path (the "current goals")

> Sequential dependency chain — each step builds on the prior having landed.

1. **M4-Ph1 C4 → C5** *(IN MOTION — the only step startable now)*. Finish the
   server.py decomposition on `audit/fixes-2026-07-02-m4ph1`. Governed by
   `plan_M4ph1.md`. See §6 for the exact scope. → human merge-gate on PR #35.
2. **M5b** — build from scratch on the M4-landed base (branch to be created).
   Governed by `plan_M5b.md` (cloner consolidation; M5a folded in).
3. **M14+A1** — build from scratch on the M5b-landed base. Governed by
   `plan_M14.md` (Ph2 error-envelope debt + remaining decomposition).
4. **Re-run plan_RELEASE Phase 0** at the execution HEAD that now contains all
   three landed SHAs (prove ancestry for each). Obtain explicit human **Phase-1
   clearance**.
5. **Implement W1–W16**, one workstream per stacked draft PR, unmerged, human
   merge-gate on each. This is the largest remaining chunk (3-OS CI matrix,
   runtime stealth probe, artifact smoke test, perf budgets, fault injection,
   security/redaction, transport/concurrency, upgrade/migration, i18n/PWA
   fixtures, and the MQ parity gate).

---

## 5. Binding constraints (do not violate)

**Global (all plans):**
- `--no-verify` is **BANNED** (never skip hooks; if a hook fails, fix the cause).
- In the **main checkout** use `.venv\Scripts\python.exe` directly (uv is broken in
  this OneDrive `&`+spaces path). In a **clean worktree** uv works:
  `uv sync --extra test --extra dev`, then `uv run python -m pytest`.
- Commit messages end with: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- **No embedded module may `import server`** (runpy double-registration hazard).
- `ADDENDUM_LENSES.md` binds: **no second way of doing something** (a fix that
  introduces a second convention is itself a defect).
- **M6-pinned error-message bytes are preserved** verbatim at pinned sites.
- **No new runtime dependencies** (test-extras only, and only when unavoidable).
- **Human retains all merge gates. Never merge a PR or mark one ready-for-review.**

**Plan-specific — READ THIS, it flips per plan:**
- **plan_RELEASE / plan_E2E: ZERO `src/` production edits.** Any product bug the
  suite hits is pinned `@pytest.mark.characterization` + F-id docstring, routed,
  and **never fixed** in these plans. Product bugs known-pinned: E8-1…E8-4, E7-1,
  E7-6, F-181, F-165, close-tab flake, `get_cookies`.
- **plan_M4ph1 / plan_M5b / plan_M14: `src/` edits are REQUIRED** — these are the
  decomposition/consolidation FIX plans; editing `src/` is their job, governed by
  their own plan docs. Do **not** apply the RELEASE "zero src/ edits" rule here.
- `server.py` grandfather LOC cap stays **3389** (never pad the cap; ratchet down
  when code leaves the file).

**Release-claim integrity (for W1–W16 later):** characterization / skip / xfail /
fixture-only / schema-only / informational-live-site / padded-budget results
**cannot** satisfy a release claim. Refused cheats: redefine words, snapshot the
adversary, assert nothing, gate-on-fixture-advertise-live, score-chase, silence the
caveat.

---

## 6. Immediate task detail — M4-Ph1 C4 & C5

Source of truth: `audit/stage2/plan_M4ph1.md`. Branch `audit/fixes-2026-07-02-m4ph1`
@ `1112dd1` already contains STEP 0 → C1 → C2 → C3a → C3b. **Recommended order:
C4 then C5.**

- **C4 — spawn_browser sub-method pipeline (M13/F-208).** In
  `src/…/embedded/browser_manager.py`, extract `spawn_browser` (approx lines
  **311–546**) into a private-method pipeline **in place** (same statements, same
  order, just grouped into named methods). **Disjoint from M7's `close_instance`**
  (starts :605 — no overlap). RED-first: extend `tests/test_bug_prone_tools.py`
  with sub-method seam assertions; keep M6's existing `spawn_browser` `.fn`-seam
  pins green. No golden JSON changes (cloner untouched — a golden diff = STOP).
- **C5 — `_with_cdp_timeout` canonical (F-164 server half).** Make it the single
  CDP-timeout wrapper for server.py call sites (M7 already closed the cfe half).
  RED-first: extend `tests/test_cdp_timeout.py` to pin it as the one wrapper.
- **Green bar:** unit suite `pytest -m "not integration"` (~703–705) stays green;
  the **94-tool tripwire** must stay green (a drop = a tool accidentally lost);
  **G5/G6/G7 characterization pins** (deliberate result-dict/fallback contracts)
  must stay green — a red there means a wrong conversion, **STOP**. Full suite
  (755 collected / 754 passed incl. ~15-min integration) if feasible.
- **Landing:** commit C4 and C5 as separate checkpoints, push to the branch. Do
  **not** mark PR #35 ready, do **not** merge. Report SHAs + before/after test
  counts + whether C4/C5 close Ph1's headline.

---

## 7. Re-verification quick-reference

```bash
# gate proof
git ls-remote --heads origin                                   # expect 4 branches
git merge-base --is-ancestor 1112dd1 HEAD && echo ANCESTOR || echo BLOCKED
gh pr view 35 --json state,isDraft,mergedAt,headRefName

# plan/protocol shape
grep -c "REVALIDATION REQUIRED" audit/stage2/plan_RELEASE.md
grep -oE "MQ-[0-9]+" tests/MANUAL_QA_PROTOCOL.md | sort -t- -k2 -n | tail -1
```

## 8. What NOT to do
- Do not begin any plan_RELEASE W-workstream (gate-blocked).
- Do not start M5b/M14 before M4-Ph1 lands (chain is sequential; their bases don't
  exist yet).
- Do not merge, mark-ready, rebase, or retarget any PR without explicit human say-so.
- Do not pick substitute prerequisite SHAs or infer landing from names/CI/text.
- Do not switch the main checkout off `agent/e2e9-plan-remediation` — use a
  dedicated worktree for M4-Ph1 work.
