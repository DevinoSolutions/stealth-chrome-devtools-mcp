# prep-t8 verification addendum

Read-only verification of `prep_F808_T8_ci_and_pr.md` against worktree
`.claude/worktrees/f808` at HEAD **ad5935e** (branch `fix/F808-headed-visibility`),
2026-08-02. The prep was drafted at HEAD 58e7725; 18 commits have landed since.

Every line number below was re-read at ad5935e. Where the prep is still exact, it
says CONFIRMED; where it is stale, the correction gives the current text.

---

## ALREADY-LANDED

The prep's **entire test pre-work half is done and merged** onto this branch via
merge commit **002b348** (branch `fix/F808-t8-integration-tests`, commits
3fe6b37 → 9c539d3 → 7d08c5d → 08002f3). Task 8 must not redo any of it.

| Prep section | State at ad5935e | Where |
|---|---|---|
| Finding 1 — "INVERTED REGATE" fix to `_HEADED_NEEDS_DESKTOP` | **DONE** (3fe6b37) | `tests/test_window_sizing.py:40-65` |
| "window_sizing regate, precisely" | **DONE**, and it went further than the prep asked | same, plus a 4-row hermetic truth table at `:133-153` |
| "EnumWindows helper" (sketch) | **DONE** (9c539d3), implemented under different names | `tests/e2e_helpers.py:172` + `:201` + `:210` |
| "Integration idioms to reuse" / the integration twin | **DONE** (9c539d3 + 7d08c5d + 08002f3) | `tests/test_browser_integration.py:804-…`, class `TestHeadedSpawnIsSeenOrRefused` |
| CHANGELOG `## 2.0.4` heading (prep says "Task 7's job") | **DONE** (546731a) | `CHANGELOG.md:3` |
| CONTRIBUTING documents the release-contract regen | **DONE** | `CONTRIBUTING.md:143-152` |
| Task 10 (`browser_pid_registry.py`) | **DONE** (f437cab…977566c) | `src/…/embedded/browser_pid_registry.py`, 407 LOC |

Nothing in the prep's test half is *partially* landed. The two remaining
verification obligations the prep assigned to Task 8 are still open, both because
they need a machine this branch cannot reach:

1. **Branch A of the twin is still unexercised.** It needs one run from the
   user's interactive desktop session (agent shells here are Session 0):
   `uv run python -m pytest tests/test_browser_integration.py -k HeadedSpawnIsSeenOrRefused -m integration`
2. **`record_property` landing in `junit.xml`** is unverified on a real Windows
   CI integration cell. See the CORRECTIONS entry on `junit_family` — the local
   PytestWarning the earlier handoff reported does not apply in CI.

---

## CORRECTIONS

### C1. The cap-raise ratification is TWO markers, not one — this changes the PR's decision list

The prep's "Two decisions for the human" §1 names only `server.py` 3389→3401.
`tools/check_file_budgets.py` now carries **two** pending markers. Exact current
text (quoted verbatim, both re-read at ad5935e):

`tools/check_file_budgets.py:32-33`

```
    # padding; no-grow applies from this commit forward. CAP RAISE 3389->3401
    # PENDING HUMAN RATIFICATION in the F-808 PR.
```
(entry: `"embedded/server.py": (3401, "plan_M4ph1 + plan_M3 + plan_M10a + plan_F808")` at `:34`)

`tools/check_file_budgets.py:75-76`

```
    # (cap == actual, no padding), per the C1 discipline. CAP RAISE 966->1023
    # PENDING HUMAN RATIFICATION in the F-808 PR. No-grow applies from here.
```
(entry: `"embedded/process_cleanup.py": (1023, "plan_M11a_M15 + plan_M7 + plan_F808")` at `:77`)

A repo-wide grep for `PENDING HUMAN RATIFICATION` returns exactly these two hits
and nothing else. Both markers must die in the merge commit, and the PR body's
decision §1 must ask the human to ratify both.

Worth stating plainly in the PR, because the `process_cleanup.py` framing is
easy to misread: that file's cap went **1054 → 966** (step 10a extraction) then
**966 → 1017** (10b) then **1017 → 1023** (10c) — a **net −31 against the
pre-task cap**. The "raise" is only against the post-extraction floor. The
`server.py` raise (+12) is a genuine increase.

Both caps equal actual measured LOC, verified at ad5935e:
`server.py` = 3401, `process_cleanup.py` = 1023. No padding.

### C2. New hazard the prep could not know: `singleton.py` sits at exactly 1000 LOC

`src/…/embedded/singleton.py` is **1000 lines** and is NOT in the `GRANDFATHER`
dict, so it passes only because the gate tests `loc > LOC_BUDGET` (1000 > 1000 is
false). **Zero headroom.** Any single line Task 8 adds to `singleton.py` — even a
comment — fails `tools/check_file_budgets.py` and therefore the `quality` job.
Task 8 adds no src/ code by design, but this is the trap to know about if a
release-mechanics tweak reaches for that file.

### C3. `junit_family` is xunit1 in CI, not xunit2 — the PytestWarning caveat does not apply on the gate

The earlier handoff's note ("`record_property` emits a PytestWarning under
`junit_family=xunit2`") describes the **local default**. Every gate pytest lane
passes `-o junit_family=xunit1` explicitly, and
`tests/test_release_workflows.py:339` pins that it must. Confirmed at
`release-gate.yml:157, 224, 320, 488, 665`. So on CI there is no warning and the
`<properties>` land normally. Keep the prep's "do NOT change junit_family for
cosmetics" instruction — it is right, for a different reason than stated.

### C4. Helper names differ from the prep's sketch

The prep sketched `_visible_window_pids()` (private) at "~tests/e2e_helpers.py:242".
As landed:

- `tests/e2e_helpers.py:172` — `async def await_visible_window(root_pid, timeout=15.0) -> int | None`
- `tests/e2e_helpers.py:201` — `class _RECT(ctypes.Structure)`
- `tests/e2e_helpers.py:210` — `def visible_window_pids() -> set[int]` (**public**, no leading underscore)
- `tests/test_browser_integration.py:36` — module-level `from e2e_helpers import await_visible_window`
- `tests/test_browser_integration.py:789` — `_chrome_tree_pids(root_pid)` (the re-snapshot helper added in 7d08c5d)
- `tests/test_browser_integration.py:804-809` — the `sys.platform != "win32"` skipif
- `tests/test_browser_integration.py:810` — `class TestHeadedSpawnIsSeenOrRefused`
- `tests/test_browser_integration.py:832-844` — the one two-branch node; `record_property("f808_display_context", token)` at `:838`, `("f808_branch", "A")` at `:840`, `("f808_branch", "B")` at `:843`

### C5. The regate landed as a named predicate with a wiring pin (prep's finding 1 understated it)

`tests/test_window_sizing.py`:

- `:40` `def _headed_needs_desktop(platform: str, can_show_windows: bool) -> bool:`
- `:58` `return not can_show_windows or platform.startswith("linux")` — exactly the both-clauses expression the prep prescribed
- `:61-65` `_HEADED_NEEDS_DESKTOP = pytest.mark.skipif(_headed_needs_desktop(sys.platform, display_context.can_show_windows()), reason=…)`
- `:133-153` four hermetic pins, including `test_the_marker_is_wired_to_the_gate` at `:148`
- `:209` and `:230` — the two decorated headed nodes (prep said `:161` / `:181`; **both moved +48/+49 lines**)

The prep's line anchors `:43-46`, `:161`, `:181`, `:208` are all **stale**. The
headless node is now at `:208`+ — it was not touched, only shifted.

### C6. Guard anchor CONFIRMED exactly

`server.py:388-397` is still exactly the guard — `:390` is
`if not headless and not display_context.can_show_windows():`, `:392` carries the
`"...cannot display a window "` phrase branch B asserts. No correction needed.

### C7. Topology pins CONFIRMED at their stated lines

`tests/test_release_workflows.py` — every anchor in the prep's "Placement"
paragraph holds at ad5935e:

- `:113` `test_aggregate_directly_needs_every_other_job`
- `:128` `test_aggregate_checks_the_result_of_every_edge`
- `:252` `test_every_declared_required_cell_emits_its_record` (reads `release_evidence.REQUIRED_CELLS`)
- `:303` `test_every_emitting_job_uploads_what_it_emitted`
- `:327` `test_pytest_lanes_write_the_junit_the_ledger_hashes`

### C8. "ZERO workflow-file edits" — CONFIRMED, with the mechanism named

The claim holds. The mechanism the integration lane uses to pick up new tests is
a **plain marker selector over `testpaths`, with no `-k` and no file list**:

- `release-gate.yml:316-320`:
  `uv run pytest -m "${{ runner.os == 'macOS' && 'integration and not transport' || 'integration' }}" -v --tb=short --timeout=180 --junitxml=junit.xml -o junit_family=xunit1`
- `pyproject.toml:91` `testpaths = ["tests"]`; `:94` declares the `integration` marker
- `tests/test_browser_integration.py:61` `pytestmark = pytest.mark.integration` (module level)

So the new class is collected by the existing Windows/X64 cell automatically.
Matrix at `release-gate.yml:267-269` is unchanged: Linux/X64, Windows/X64,
macOS/ARM64. Xvfb block `:291-297`, `DISPLAY` env `:322`, evidence emit `:396-425`
(with `--junit "junit.xml"` at `:407`), upload `:427-433`. Nothing in
`release_evidence.REQUIRED_CELLS` changes because no job is added.

The Linux exclusion lives in the test (`:804` skipif), so the Linux cell simply
collects one fewer node — which does **not** disturb any pin, since no pin counts
collected nodes.

### C9. PR-body claims that are now stale

- **"tests/test_browser_integration.py … ~/.stealth-mcp untouched"** — the
  earlier handoff's line. Task 10 is now merged into this branch, so the
  integration lane writes the real `~/.stealth-mcp/browser_pids.json`. Do not
  carry that sentence.
- **"Every release up to 2.0.3 wrote a flat record"** — still accurate.
- **"stealth-chrome-devtools doctor names the display context of each recorded
  backend"** — accurate; the landed output shape is pinned in `RUNBOOK.md:116-117`:
  `backend  win-session-1  port 19222  pid 12345  version 2.0.4  responsive  (can show windows)`
- **The Sentry addendum's F-809 material** — unverified here (out of scope of this
  pass); it is prose about issues, not repo anchors.
- **Neither branch is pushed.** `git ls-remote --heads origin "fix/F808*"` returns
  nothing; `main == origin/main == 8674f6a`. Task 8 owns the first push. Note
  again that a branch push alone runs ZERO checks — only opening the PR runs the
  gate (`test.yml` triggers on push to main/dev and on `pull_request`).
- **29f02a0 duplicate** — CONFIRMED live: `main`'s tip 8674f6a *is* the cherry-pick
  of this branch's 29f02a0. The duplicate patch merges cleanly; do not "fix" it.

---

## VERSION-STRING CENSUS

Repo-wide Grep for `2\.0\.3` at ad5935e. **43 hits in 21 files.** The bump set is
exactly the six the team lead named — every other hit is a historical statement
that must NOT be touched.

### BUMP (6 sites, 5 files + the lock)

| File:line | Current text | Note |
|---|---|---|
| `pyproject.toml:7` | `version = "2.0.3"` | THE one version home; drives the tag and `package_verify.py` |
| `README.md:56` | `"args": ["stealth-chrome-devtools-mcp==2.0.3"]` | uvx config snippet |
| `README.md:65` | `pip install stealth-chrome-devtools-mcp==2.0.3` | |
| `RUNBOOK.md:45` | `version     : 2.0.3` | the `status` sample output |
| `RELEASE_CONTRACT.md:4` | `# Release contract — version 2.0.3` | **generated** — do not hand-edit; regenerate (see below) |
| `uv.lock:1596` | `version = "2.0.3"` | self-entry; `uv lock`/`uv sync` refreshes it |

`RUNBOOK.md:116-117` already reads `version 2.0.4` in the `doctor` sample —
**CONFIRMED**, so bumping `:45` makes the file internally consistent. `RUNBOOK.md`
also already references 2.0.4 at `:103`, `:150`, `:238`.

### KEEP — historical / migration statements (37 hits)

- `CHANGELOG.md:53, 80` — prose inside the 2.0.4 entry describing what 2.0.3 wrote.
- `CHANGELOG.md:128` — the `## 2.0.3` release heading itself.
- `DESIGN.md:118, 225, 228` — v1-record migration prose.
- `RUNBOOK.md:127, 198` — "recorded by 2.0.3 or earlier" migration prose.
- `src/…/backend_registry.py:30, 202, 305` — module docstrings about the pre-v2 format.
- `src/…/browser_pid_registry.py:28, 211` — same, for the owner-stamp migration.
- `tests/test_backend_registry.py:37, 46, 56, 61, 79, 173, 177, 362, 365, 626, 702`
- `tests/test_browser_pid_registry.py:41, 169, 184`
- `tests/test_cli_status_wedged.py:523, 525, 530`
- `tests/test_singleton_display_routing.py:255, 262, 271`
- `audit/stage2/evidence_F509_windows_herd_hang_2026-08-01.md:3, 8, 13, 67`
- `audit/stage2/finding_F806_…md:4, 12`
- `audit/stage2/finding_F808_…md:14`
- `audit/stage2/plan_F808.md:328, 333, 398, 765`

The prep's own "do NOT bump `backend_registry.py:30/202/293`" instruction is
right in spirit but one line number is off: the third hit is at **`:305`**, not
`:293`.

### Release-contract regeneration — CONFIRMED

- Tool exists: `tools/gen_release_contract.py` (46.8 KB).
- Command in `CONTRIBUTING.md:149`: `PYTHONUTF8=1 uv run python tools/gen_release_contract.py --write`
- The enforcing test is **`tests/test_release_contract.py::test_the_contract_is_regenerated_not_edited`** (`:35`),
  named in `CONTRIBUTING.md:151-152`; it runs `--check` in the unit gate on all
  three OSes.
- `RELEASE_CONTRACT.md:1-2` carries the generated-file banner.
- The gate also runs `gen_release_contract.py --check` in the `quality` job,
  pinned by `tests/test_release_workflows.py:320`.

So `RELEASE_CONTRACT.md:4` must be updated **by regeneration in the same commit as
`pyproject.toml:7`**, never by hand.

---

## PIN AUDIT

Read-only enumeration, 2026-08-02. **The team lead's premise that one entry reads
`==2.0.3` is FALSE — every pin on this machine is `==1.0.0`.** This matches the
prep's own "pin audit COMPLETE" addendum, which is accurate as written.

| File | JSON path | Line | Pin |
|---|---|---|---|
| `C:\Users\amind\.claude.json` | `mcpServers → stealth-chrome-devtools-mcp → args[0]` | **1144** | `stealth-chrome-devtools-mcp==1.0.0` |
| `C:\Users\amind\.claude\.mcp.json` | `mcpServers → stealth-chrome-devtools-mcp → args[0]` | **5** | `stealth-chrome-devtools-mcp==1.0.0` |
| `C:\Users\amind\.claude\.mcp.json` | `mcpServers → stealth-chrome-devtools-mcp-pip → args[0]` | **9** | `stealth-chrome-devtools-mcp==1.0.0` |
| repo `.mcp.json` | `_disabled_mcpServers → stealth-local` | — | source checkout, `--singleton-port 19223`; **deliberately disabled** (`_why_disabled` documents the mutual-eviction hazard). No version pin. Leave alone. |

Verified twice with different patterns (the rtk-shim false-zero landmine):

- `Grep "stealth-chrome-devtools-mcp==[0-9.]+"` over `~/.claude.json` → **1 hit, line 1144**.
- `Grep "2\.0\.[0-9]"` over `~/.claude.json` → **0 matches, whole file**. There is
  no 2.0.x pin anywhere in that file.

Other `stealth-chrome-devtools` occurrences in `~/.claude.json` are **not** pins:
`:2530` and `:3465` are per-project entries whose `mcpServers` are empty `{}`, and
`:5589-5591` is the plugin/repo path map. Nothing else to update.

**Ship-time step**: update all three `==1.0.0` pins to `==2.0.4` (or unpin). Until
then the acceptance run is invalid — the live stdio proxies are running 1.0.0, not
2.0.3, whatever the recorded backend on 19222 says.

---

## COMMIT RANGE `main..ad5935e` — 33 commits

`main` = `origin/main` = **8674f6a**. Ordered oldest → newest. (Note: a naive
`git log --oneline` here silently drops the merge commit and truncates subjects at
~72 chars — these are the full subjects, re-read via `git show -s`.)

```
a1b3075  F-808 step 1: display_context, the one home for "can a window be seen here"
8a78561  F-808 step 1b: quality fixes for display_context
efed9d0  F-808 step 2: move the backend record out of singleton.py
663da1a  F-808 step 2b: quality fixes for backend_registry
7989dee  F-808 step 3: server.json records one backend per display context, v1 records still readable
32b3185  F-808 step 3b: a v2 write supersedes the v1 entry on its port
62d4813  F-808 step 3c: quality fixes for the registry
85f7fe6  F-808 step 4: discovery prefers a window-capable backend
09433a0  F-808 step 4b: an unverified entry is never a port conflict
f02334c  F-808 step 4c: a proven-capable client adopts only its own desktop
4e2ede3  F-808 step 4d: restart terminates only the port it is about to bind
2b22fe1  F-808 step 5: a headed spawn with no displayable desktop raises instead of returning a ghost
d209b46  F-808 step 5b: pin the context token; state the cap raise honestly
29f02a0  Test runs no longer ship injected failures to the real Sentry
58e7725  F-808 step 5c: the doomed_spawn fixture is all tripwires, as documented
172e014  F-808 step 6: doctor reports a backend per display context
60e48da  F-808 step 6: say that own_or_first_port's own-context branch no longer decides anything
69e48ad  F-808 step 6b: a recorded desktop backend only silences the remedy while it is alive
d84323e  F-808 step 6c: quality-review fixes — falsifiable remedy asserts, hermetic doctor tests
f437cab  F-808 step 10a: give browser_pids.json one home, and make every write merge
38aa897  F-808 step 10b: a backend may only reap browsers nobody living owns
3fe6b37  F-808 step 8-pre: gate F-804's headed nodes on display capability, not platform
f4e58f3  F-808 step 10c: keep kill-orphans --force meaning what it says
22529ad  F-808 step 10d: size the record lock for the critical section it now guards
9c539d3  F-808 step 8-pre: ask the machine whether a headed spawn is really visible
7d08c5d  F-808 step 8-pre: review fixups — the kill net must survive a failing close
08002f3  F-808 step 8-pre: correct the double-exec comment — the second exec is unconditional
3aea184  F-808 step 10e: stage-2 review fixes — contain the ownership check's raise
977566c  F-808 step 10f: name the normalizer test class after the function it tests
002b348  Merge F-808 test pre-work: capability regate, EnumWindows helper, integration twin
546731a  F-808 step 7: the docs describe a backend keyed by where a window can be seen
4a36ef5  F-808 step 7b: spec-review accuracy fixes
ad5935e  F-808 step 7c: precision leftovers
```

Narrative shape for the PR: steps 1–4 (the registry and routing), step 5 (the
guard), step 6 (doctor), step 7 (docs), step 8-pre (tests), step 10 (the
browser-pid registry / fratricide fix). 29f02a0 is the Sentry-mute duplicate.
There is no step 8 or 9 commit — Task 8 proper is the release commit itself.

---

## STALE BRANCH `fix/F808-t7-docs`

- The ref **still exists**, tip **41d7d9a** — `F-808 docs: hand the version strings back to Task 8; close three routed gaps`.
- **Content-wise fully subsumed by ad5935e — re-verified.** `git diff ad5935e 41d7d9a`
  over the doc paths only *removes* prose (RUNBOOK's three-outcome `status` row,
  the `.bak` leftover paragraph, the doctor liveness-vocabulary paragraph, the
  F-804 1024x768 provenance note, and the step-8-pre row in the F-808 finding's
  commit table). ad5935e is strictly the richer text. Nothing on 41d7d9a is
  missing from ad5935e.
- **Task 8 deletion caveat**: the branch is **checked out in a worktree** at
  `.claude/worktrees/f808-t7docs`. `git branch -D fix/F808-t7-docs` will refuse
  until `git worktree remove .claude/worktrees/f808-t7docs` runs first. Same for
  `fix/F808-t8-integration-tests` (tip 08002f3, worktree
  `.claude/worktrees/f808-t8tests`) — that branch is genuinely merged (002b348)
  and its worktree is likewise still checked out.

---

## SUMMARY FOR THE TASK 8 IMPLEMENTER

What is left of Task 8 after this verification:

1. Bump the six version strings (regenerating `RELEASE_CONTRACT.md`, not editing it).
2. Retire **both** ratification markers in `tools/check_file_budgets.py` (`:32-33`
   and `:75-76`) to the completed-ruling idiom, in the merge commit.
3. Push the branch and open the PR (nothing is on origin yet; a push alone gates nothing).
4. Refresh the PR narrative against the 33-commit range above, dropping the
   "~/.stealth-mcp untouched" line and adding the two-marker decision.
5. At ship time, move all three `==1.0.0` pins, then run the desktop acceptance.
6. Clean up the two stale worktrees before deleting their branches.

No workflow file needs editing. No new job, no new required cell, no
`REQUIRED_CELLS` change.
