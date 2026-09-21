# CONTRIBUTING

How to clone, install, test, and land a change. Architecture rationale is in
[`DESIGN.md`](./DESIGN.md); the file map + conventions are in
[`CLAUDE.md`](./CLAUDE.md).

> Local, single-user tool, 0 external users. Priorities: maintainability, operability,
> performance. Every change is reviewed against four lenses — **modularity ·
> deduplication · clarity · conventions** — and the sharp one: *a fix that introduces a
> second way of doing something is a defect.*

---

## Clone & install

Requires **Python ≥ 3.11** and (for integration tests) **Google Chrome**.

```bash
git clone https://github.com/DevinoSolutions/stealth-chrome-devtools-mcp
cd stealth-chrome-devtools-mcp
uv sync --extra test --extra dev        # creates .venv with test + dev tooling
```

`uv sync` installs the package editable with the `test` (pytest, pytest-asyncio,
pytest-timeout, pytest-cov) and `dev` (ruff, ty, vulture) extras. Verify:

```bash
.venv\Scripts\python.exe -c "import stealth_chrome_devtools_mcp; print('ok')"
```

### The `uv run pytest` caveat (on a spaces/`&` checkout path)

The dev checkout lives under `…/CUSTOM MCPs & PRODUCTIVITY/…`. On such a path,
**`uv run pytest` fails** with `Failed to canonicalize script path` — `uv` cannot
resolve the `pytest` entry-point script through the special characters. (`uv run
python …` and `uv run stealth-chrome-devtools …` still work — it is specifically the
`pytest` console-script path that trips.) That is not a bug in this package. Two
working responses:

1. Invoke the **venv Python directly** (works everywhere, and is what the commands in
   this repo's docs use):
   ```
   .venv\Scripts\python.exe -m pytest -m "not integration" -q
   ```
2. Or check the repo out to a path without spaces/`&` (e.g. `C:\src\stealth-…`), where
   `uv run pytest` works too. CI checks out to a clean path, which is why CI uses
   `uv run`.

Prefer a clean checkout path for local work; where you can't, the `.venv\Scripts\python.exe`
forms below are the ones that run.

---

## Run the tests

```bash
# unit suite (fast; the pre-push gate) — WORKS on any path:
.venv\Scripts\python.exe -m pytest -m "not integration" -q

# one file / one test while iterating:
.venv\Scripts\python.exe -m pytest tests/test_cli.py -q
.venv\Scripts\python.exe -m pytest tests/test_cli.py::TestCli::test_status_runs -q

# integration suite (spawns real Chrome; slower; needs Chrome installed):
.venv\Scripts\python.exe -m pytest -m integration -q
```

Markers (`pyproject.toml`): **`integration`** (spawns real browsers), **`characterization`**
(pins *current* observable behavior — quirks/known bugs included — so an intended change
surfaces as a failing test you update deliberately). The default pre-push run is
`-m "not integration"`.

`integration` means **real browsers**, not "slow" or "not hermetic". The unmarked (unit)
lane therefore also holds a small number of tests that shell out to the installed
`stealth-chrome-devtools` console script as a real subprocess against a throwaway `HOME`
(`tests/test_doc_examples.py`, `tests/test_cli_backend_records_e2e.py`) — no Chrome, no
mock, and they run on every push. Use `release_gate_harness._isolated_env` +
`resolve_launcher` for those: `backend_registry.STATE_DIR` is `Path.home()/".stealth-mcp"`
with no env override, so redirecting the child's `HOME`/`USERPROFILE` *before* it starts
is the only way a test can touch a backend record without touching yours.

### Test isolation: the three roots, and what fences each (F-903)

A test run can reach three directories that are not its own. Know which one you
are near before you write a fixture.

| Root | What lives there | Fenced by |
|---|---|---|
| clone / large-response output | screenshots, clone artifacts | `STEALTH_MCP_CLONE_OUTPUT_DIR`, set in `tests/conftest.py` at import |
| **browser-session root** | the `default`/`master` profile **a human is logged into**, and every named session copied from it | `tests/operator_fence.py` — env **forced**, plus a read+write tripwire |
| **backend state dir** (`~/.stealth-mcp`) | `server.json`, the lock, heartbeats, `browser_pids.json`, logs — **and the live backends they name** | `tests/operator_fence.py` — ten rebound globals, plus a write tripwire and a kill guard |

The last two are one module because they share one tripwire. **You get all of it
for free — do not re-implement any of it**:

1. **The state-dir redirect.** Ten module globals across five modules are
   re-pointed at a per-process tmp root. Ten because `singleton`,
   `process_cleanup` and `response_handler` each FROM-import the path — a
   `setattr` on `backend_registry.STATE_DIR` alone reaches *none* of them — and
   because pydantic copied its value into `Settings.model_config["env_file"]` at
   class creation. If you add a global derived from the state dir, add it to
   `operator_fence.STATE_DIR_BINDINGS`; `tests/test_operator_fence.py` measures
   the package and will fail until you do.
2. **The session root is FORCED, not `setdefault`-ed** — along with the three
   derived names (`BROWSER_MASTER_USER_DATA_DIR`, `BROWSER_PROFILE_CLONE_ROOT`,
   `BROWSER_MASTER_SNAPSHOT_DIR`), which are cleared so they derive from it.
   `setdefault` could not tell the release gate redirecting the suite from the
   operator's own root arriving in an inherited environment; on Windows the
   product default is the hardcoded `C:\stealth-mcp-browser-sessions`, and test
   directories (`e2e-warmup`, `ci-warmup`, `ci-cycle-*`) are still sitting in
   the real one beside 87 real sessions.
3. **The tripwire.** A write under the real state dir, or a **read or write**
   under the real session root, raises a **`BaseException`**
   (`operator_fence.RealStateDirWrite` / `RealSessionRootAccess`) — the product
   is fail-open by design (`backend_registry` is a never-raise cache,
   `proxy_selfheal` never raises), so an `Exception` would be swallowed at the
   first handler and your node would go green over a real write. If you see one,
   a path escaped a redirect; **fix the path, never the guard.**
   It wraps **every door the product goes through**, which is not the same as
   every filesystem primitive and is not advertised as one: `shutil.copy2`'s
   Win32 fast path, `Path.glob`/`rglob` (their `scandir` is bound inside `glob`
   at import), `os.chmod`/`link`/`symlink`, `sqlite3`, any subprocess and any fd
   opened before the fence installed all reach a designated root without
   raising. The **redirect** is what covers those; the tripwire is the backstop
   for what the redirect misses.
4. **A kill guard.** `psutil.Process.terminate`/`kill`/`send_signal` and
   `os.kill` refuse a pid the operator's real `server.json` names — the
   primitives, so it sees the number the OS is about to act on rather than an
   argument some caller happened to be passed.

   A collection-time fence hit reads as `Interrupted: 1 error during collection`
   with **0 tests run and exit code 2**, not as a failing node. If a selection
   that used to run reports no tests at all, read the error above the summary
   before assuming your `-k` is wrong.

Two asymmetries, both deliberate. **Reads of the real state dir are allowed**
(`release_gate_harness._reserved_ports()` must read the real `server.json` so an
isolated backend never binds a live backend's port) while **reads of the real
session root are not** (nothing in the harness reads a profile, and copying one
is how a test would take the operator's logged-in cookies into a clone). And
`HOME` is deliberately **not** redirected in the pytest process — it would break
`_reserved_ports()` and would not fence the session root on Windows anyway.
Child processes still redirect `HOME`/`USERPROFILE`; that is the paragraph
above, a different mechanism for a different process.

**Keep writing per-file `isolated_state` fixtures.** The fence makes the
operator's directories unreachable; it does not give each node a clean record.
Two nodes in one file that both write `server.json` still need `tmp_path`
between them. The two answer different questions and the suite needs both.

**Never move a module in `sys.modules` by hand — use `tests/module_cache.py`.**
A module's identity lives in TWO places: the `sys.modules` mapping and the
attribute its parent package carries (`import a.b` writes both). Move one half
and the other is left naming the wrong object, which is **not local to your
file**: pytest resolves a dotted `monkeypatch.setattr("a.b.c.d", …)` target by
`__import__` plus a `getattr` walk, so the next file in the lane that patches
through that attribute fails — green in every single-file run, red only under
the full alphabetical order. Both directions have now been measured here: a
popped PARENT left `tests/test_python_exec_timeout.py` with `module
'stealth_chrome_devtools_mcp' has no attribute 'embedded'`, and a popped CHILD
restored into the mapping alone orphaned
`embedded.file_based_element_cloner`. `module_cache.bind` / `absent` /
`pristine_package` are the one home for the rule (move the pair, never one
half); `tests/test_package_entrypoints.py::TestTheImportTreeSurvivesTheseNodes`
pins it for this file and everything sorted before it.

**No module body in `src/stealth_chrome_devtools_mcp/` may CALL anything.**
Importing a module must only define things, so that a `pkgutil.walk_packages`
sweep, a doc generator, an import linter or an IDE can walk the package without
running the product. This is a rule because it was once broken (F-904):
`__main__.py` called `main()` at module level, so importing it started a stdio
proxy and cold-started a backend — which is how F-903 reproduced itself while
being investigated. It has the `if __name__ == "__main__":` guard now.
`tests/test_package_entrypoints.py::TestNoModuleBodyDoesWork`
enforces the general rule by AST and carries the single allowance
(`tool_runtime`'s `cdp_transport.install()`); adding a second means writing down
why. There is no exclusion list to add a module to — fix the module instead.

**`--help` and `--list-sections` used to cold-start a backend** (F-905, fixed on
this branch). `server.main` parses with `add_help=False` + `parse_known_args` —
deliberately, because it decides one thing from three flags and every other
argument belongs to `embedded/server.py`'s full parser — so both were *unknown*
to it and the default `--transport stdio` carried them into
`ensure_server_running`. They take the `runpy` branch now, which is the one that
can answer them. `server._ANSWER_AND_EXIT` is the set and the rule is "does
`build_arg_parser()` print and exit on it" — `--minimal`/`--debug`/
`--xpool-safe` are outside it, because they configure a backend that then
serves. **If you add a printing flag to the backend's parser, add it to that
set**; nothing derives it, because the shim may not import the backend's parser.
`tests/test_package_entrypoints.py::TestAskingAQuestionStartsNothing`
parametrises every member.

Coverage is **intentionally not** in `addopts` (it would slow every single-file TDD run
and trip `--cov-fail-under` on partial runs). CI turns it on explicitly.

---

## The real quality gate (what CI enforces)

CI (`.github/workflows/test.yml`) is the source of truth. It runs three jobs; a change
must pass all three. Locally you can run each with the venv Python:

**1. Unit tests + coverage** (`ubuntu`, Python 3.11 / 3.12 / 3.13)
```
pytest -m "not integration"  … --cov-fail-under=55
```

**2. Lint & type check & budgets** — this repo **does** have a lint/type/dead-code gate
(don't believe older docs that say "no linter"):
```
ruff format --check                                   # formatting (line-length 88)
ruff check                                            # curated ruleset (see pyproject [tool.ruff.lint])
ty check --exit-zero-on-warning src/stealth_chrome_devtools_mcp/   # types (a baseline of warnings is tolerated; new code must be clean)
vulture src/stealth_chrome_devtools_mcp/ tools/vulture_allowlist.py   # dead code (min_confidence 80)
python tools/check_suppression_owners.py              # every lint suppression must be owner-tagged
python tools/check_file_budgets.py                    # grandfathered files may not grow past their recorded LOC
python tools/check_pinned_imports.py                  # every third-party import is pinned at the uv.lock version (F-865)
```

**3. Integration tests** (`ubuntu` + `google-chrome-stable` + `Xvfb`)
```
pytest -m integration … --timeout=120
```

Locally, substitute `.venv\Scripts\python.exe -m ruff …` / `-m pytest …` etc. for the
bare tool names (or `uv run …` on a clean checkout path).

### Gate rules worth knowing before you fight them

- **Env access has one home.** `os.getenv` / `os.environ` are **banned** (ruff
  banned-api). Add a typed field to `Settings` in `settings.py` instead
  ([DESIGN §4](./DESIGN.md#4-environment-configuration-has-one-home)).
- **Relative imports are banned**; use `from stealth_chrome_devtools_mcp.embedded.X
  import Y`. No `embedded/` module imports `server`.
- **File budgets never grow.** `tools/check_file_budgets.py` grandfathers a few large
  files at their *exact* current LOC — you may not push them over. Never pad a cap;
  shrink the file or move code out.
- **Every suppression is owner-tagged.** A `# noqa` / per-file-ignore must carry an
  owner tag (a plan id or `PERMANENT(reason)` / `DEBT(finding)`), enforced by
  `tools/check_suppression_owners.py`.
- **What the source imports is pinned at what the lock resolved.** Every direct
  dependency is an exact `==` pin, and so is every third-party module the source
  imports even when it arrives transitively (`mcp`, `anyio`, `starlette`, `httpx`).
  `uv.lock` is a universal resolution — one version per package for every Python we
  support — while a user's installer re-resolves the wheel's metadata for one Python and
  can land on a newer transitive version than any gate cell ran (F-865: the gate ran
  `mcp 1.27.1`, `uv tool install ==2.1.2` on 3.12 got `1.30.0`). `tools/check_pinned_imports.py`
  fails when an imported distribution is unpinned or its pin disagrees with `uv.lock`;
  CI installs with `uv sync --locked`, so a lock that lags `pyproject.toml` fails too. To
  move a dependency, change the pin and run `uv lock` in the same commit.
- **`ty` runs with `--exit-zero-on-warning`.** There is a tolerated baseline of typing
  warnings on pre-typing modules; *new* modules must be error-free.

### The canary is not a gate

`.github/workflows/canary.yml` (plan_RELEASE W6) runs on a schedule and on manual
dispatch. It exists so a human can *look* at drift; it has no authority:

- Its **deterministic** half calls the same reusable gate a PR calls, against the
  local fixture. A failure there is red and real.
- Its **live-observation** half drives the approved live detectors and is
  **informational only** — it is structurally incapable of failing the run, and a
  result from it may never be quoted as evidence for or against a release claim.
- There is **no notification of any kind**, by design. Nothing pages anyone; a red
  scheduled run is discovered whenever someone next looks.
- It is not a promise of permanent coverage, and not a commitment to detect or
  repair drift within any interval.

`tests/test_release_workflows.py` pins those properties, so widening them means
deleting a test on purpose rather than by accident.

---

## The release contract is generated

`RELEASE_CONTRACT.md` is output, never hand-edited — every count in it derives from
the live tool registry, the claim ledger, and the evidence aggregate. Regenerate it
in the **same commit** as whatever changed those:

    PYTHONUTF8=1 uv run python tools/gen_release_contract.py --write

`tests/test_release_contract.py::test_the_contract_is_regenerated_not_edited` runs
`--check` in the unit gate on all three OSes, so drift is a red test.

---

## CHANGELOG placement (F-923)

An entry under a heading for a release that does not contain it is a false
statement about a shipped artefact. It has happened: between 2.1.7 and 2.1.8,
four entries reached `main` under `## 2.1.7` and stayed there across five
merged PRs. The `v2.1.7` tag ships two sections; at the worst point that
heading held six, and none of the four extra ones was in the release.

**It happens on a CLEAN merge, which is why it is easy to miss.** A release
commit renames `## Unreleased` to `## <version>` and adds no replacement — so a
branch that appended its section inside that block merges afterwards with the
surrounding context unchanged, and git places the section by context, under the
RELEASE heading. Nothing conflicts, so no resolver runs.

`tests/test_doc_claims.py::TestChangelogIntegrity` now fails on three shapes of
this — a queue below a shipped heading, a second queue, and release headings
that are duplicated, out of order or malformed — plus a `pyproject.toml` bump
that moved without its heading. **It cannot catch the shape above**, because a
file whose entries were absorbed into the shipped section and whose queue is
gone is byte-indistinguishable from a legitimate release commit. That one needs
a base ref, so it is this procedure:

### After every merge of `main` into a branch

    git diff origin/main --numstat -- CHANGELOG.md

Insertions, and **0 deletions**. A nonzero right-hand column means `main` has
content your branch does not — its blocks were overwritten rather than appended
to. Run it on *every* merge, not only conflicting ones; the conflicting merges
are the safe case, because a human reads those.

**It is a CLOBBER check and it cannot see PLACEMENT.** Your own block is not in
`origin/main`, so wherever the merge puts it — under `## Unreleased` or under a
shipped release heading — it is purely an INSERTION and the deletion count
stays 0. Measured on the F-913 lane's own 2.1.13 merge: the misplaced file
reported `57  0` and the corrected one `58  0`. So run it for the clobber, and
check placement the other two ways — `TestChangelogIntegrity` catches the two
shapes a rule can see, and for the third (your block absorbed into the release
section with no queue left) the only control is reading the file, which is what
the next section is for.

**The word _after_ is load-bearing.** `git diff origin/main` is not symmetric:
deletions are lines `origin/main` has that your branch lacks. Run it *before*
merging and a perfectly healthy branch reports every entry `main` has gained
since the merge-base as a deletion. This is not hypothetical — as of 2026-09-21
`fix/F916-…`, `fix/F919-…` and `fix/F921-…` each report `132` deletions, which
are F-903/F-904/F-905's blocks, and all three branches are fine: they simply
have not merged `main`. That reading was taken for a data-loss incident once
already. The discriminator:

    git log --oneline origin/main..HEAD

No merge of `main` on the branch means the deletions are `main`'s lead, not
your loss. Do not "restore" them — merge `main` and re-run the `--numstat`.

### Merging `main` after a release has landed

`main` will lead with `## <version>` and carry **no `## Unreleased` at all**.
Do not put your entry under the release heading:

1. create a new `## Unreleased` heading **above** the release heading;
2. put your block under that;
3. check: exactly one `## Unreleased`, it is the first `## ` heading, the
   release heading is immediately below with its contents untouched, and the
   `--numstat` above shows 0 deletions.

Cheapest option of all: **do not merge `main` while a release lane is in
flight.** Merging before it lands just means doing it twice.

### Before a release bump

Diff the `### ` headings under the previous release heading against the tag:

    git show v<prev>:CHANGELOG.md

Anything extra belongs back under `## Unreleased`.

---

## Golden discipline (two-tier)

Schema/shape tests compare against goldens in `tests/goldens/`. Two tiers:

- **HARD invariants** never bend — a change that breaks one is a real regression, fix
  the code.
- **SOFT goldens** update **deliberately**, in the **same PR** that changes the schema,
  **with justification** in the PR/commit. A golden diff must never be an accident. The
  `characterization` marker flags tests that pin current behavior *on purpose* so an
  intended change shows up as a failure to update, not a silent pass.

Never regenerate goldens blindly to make a suite green — that erases the signal the
golden exists to give.

---

## Lifecycle resilience: the invariants `test_e2e_lifecycle_resilience.py` guards

Two operator symptoms — *"the MCP server went to `CONNECTION_CLOSED` mid-session"* and
*"my browsers closed on their own"* — have the same shape in the code: something
decided the shared backend was no longer the backend. Five machines can decide it, and
each has its own finding: the proxy watchdog's condemnation (F-820),
`proxy_selfheal`'s heal/teardown (F-838, F-843), a source-fingerprint eviction
(F-829), orphan reaping (`process_cleanup` + `browser_pid_registry`'s owner stamps),
and MCP session hygiene (F-862).

`tests/test_e2e_lifecycle_resilience.py` drives a REAL fleet — the installed console
launcher over stdio JSON-RPC, a detached backend on an isolated `HOME` and an
OS-assigned port, real headless Chrome — applies one stress per node, and asserts the
same four things every time:

1. the backend pid recorded in the isolated `server.json` is **unchanged**;
2. every browser spawned before the stress is **alive AND usable** — pid running *and*
   a CDP round trip (`get_active_tab` + `execute_script`) answers, which is what
   separates "the process is still there" from "the browser still works";
3. every `tools/call` on a surviving proxy got **exactly one response frame, not an
   error** (`_call` and `assert_wire_healthy` count the frames per request id), and no
   proxy's stdout reached **EOF** (an EOF *is* the client's `CONNECTION_CLOSED`);
4. **zero lifecycle incidents** in the proxy/backend logs written during the stress.

The mixed-fingerprint fleet (S5) is the one deliberate exception: it churns a backend
by construction, so it runs in its own workspace, checks browsers on the pid captured
at spawn only (the winner rewrites `browser_pids.json`, so the registry cannot be its
oracle), and is exempt from rule 4 — the eviction line *is* its measurement. Its
fixture first proves, with the product's own `singleton._source_fingerprint` over a
repointed `SOURCE_ROOT`, that the two roots the two proxies import really carry
different digests, so a copy that failed to move the digest cannot pass as a
same-source fleet.

**Never assert a strike count; assert the implication.** `os.cpu_count()*2`
normal-priority busy loops made probes miss only *sometimes* on a 32-core box — 0
strikes in five runs, 6 in one (where answered calls also fell from 76-80 to 28),
while the *unstressed* 60 s soak logged 2 in another — and nothing condemned in any
of them. A count or a floor over 0,0,0,0,0,6,0 is a coin flip, so what every node
asserts (`assert_strikes_concluded_correctly`, inside the shared incident check) is:
**whenever a FULL strike run is reached on a port, the confirmation phase must have
run for that port and answered `was busy, not dead`** — never silence, never
`confirmed unusable`. That is exactly as strong as `watch_liveness`'s own branch,
which logs precisely one of those two at `consecutive == failures_before_teardown`,
so it is vacuous on a run where the load did not bite and a real end-to-end F-820
oracle on one where it did. Two details are what keep it from flaking, and each
without the other is wrong in a different direction: the key is **(log file, port)**,
never the port — every proxy here shares one backend, so a port-only key lets a
sibling's verdict close another proxy's open run — and a full run that is the **last
watchdog line its proxy wrote is PENDING**, because `watch_liveness` then awaits a
confirmation that may legitimately take `REUSE_PATIENCE_SECONDS` (60 s, 10 s per
attempt) while logging nothing, and demanding its verdict would fail a correct
product. The longest consecutive run is printed so which case a run hit is readable
from the output. **The oracle has not yet fired on any real run** — seven runs, the
limit never reached — so its pass and fail paths are exercised hermetically instead,
on synthetic log lines, by
`test_the_strike_implication_is_per_proxy_and_waits_for_a_pending_verdict`. Below the limit the CPU
node is deliberately silent about the confirmation phase, because the product never
entered it — `test_watchdog_busy_vs_dead` and `test_singleton_starvation_patience`
remain the nodes that enter it deliberately rather than when the box happens to be
slow. The node also does not reproduce the 100 %-CPU condemnation recorded in the
team memory. On F-856's reading of that incident the proxy process itself had to be
starved, not merely the machine kept busy — an inference from that design, not
something this branch measured. A stronger stress would have to starve the proxy
process, which is a different node and a different budget.

**If you change a lifecycle log line, that module is what breaks.** The incident
oracle is the product's own text, because `observability.capture_lifecycle` is a no-op
under the suite's `STEALTH_MCP_NO_ERROR_REPORTING=1` and only the piggybacked log line
survives. `LIFECYCLE_INCIDENTS` maps each kind to its substring, and
`test_lifecycle_incident_patterns_match_the_product_strings` asserts each substring
still occurs in the module that emits it — so a rename turns *that* node red instead
of leaving six stress nodes matching nothing. A watchdog STRIKE
(`probe failed n/3`) is deliberately **not** an incident: F-820 exists precisely so
strikes alone never condemn, and a node that failed on one would re-assert the defect.
Strikes are counted and printed.

**If you add a periodic reaper, re-derive the idle window.** The idle node out-waits
the longest periodic period in the tree, computed from
`session_hygiene.ABANDONED_AFTER_SECONDS + SWEEP_INTERVAL_SECONDS` and asserting that
`Settings.browser_idle_timeout` still defaults to `0`. A new reaper with a longer
period must be added to that derivation, never left implicit.

**The rule that keeps a session's browsers alive (F-886).** When a backend was
replaced **on the same port**, the browser that backend owned used to die. It was NOT
killed with its backend: measured at 0.25 s resolution, it outlived the terminated
backend by **4.43 s** and was then reaped by the REPLACEMENT's orphan recovery
(`process_cleanup.recovery: Killed 1 orphaned browser processes`), because
`browser_pid_registry` stamps the *backend* as owner and an owner we just killed is
indistinguishable from one that crashed last week. The surviving session was never
told either: both of the proxy's death witnesses are PORT-scoped and the replacement
binds the same port, so `backend_watchdog.watch_liveness` kept getting an answer, the
per-request bridge never "broke" for `_confirm_bridge_verdict`, and what actually died
was the MCP **session**, which nothing watches.

So `embedded/backend_eviction.py` is now **the one home for whether a backend may be
terminated at all**, and its rule is: a backend that is one of ours, running, of an
identity we would NOT adopt, and still owning at least one **live browser** is
PROTECTED — never terminated, never bound over. The arriving client spawns its own on
a fresh port and `server.json` (schema v3, a list) records both. `singleton` asks at
the bind site (`_select_backend_port`) and again at the kill site
(`_clear_stale_backend`).

**If you touch eviction, these are the constraints.** An IDLE stale backend must stay
evictable — that is the issue-#14 upgrade flow (edit source, get a fresh backend), and
protecting every live backend would accumulate one per source edit with nothing in the
tree able to reclaim it. A backend of OUR OWN identity is never protected, which is
what keeps `restart` landing on and replacing its own wedged backend. `stop` and
`restart` call `backend_eviction.terminate` directly and ungated, because an operator
asking IS the authority the rule otherwise supplies. And an unreadable
`browser_pids.json` must resolve toward EVICTING, because refusing on one would brick
every cold start on the machine.

**Still open, and NOT yet safe to do — read this before you wire it up.** The one
residual F-886 leaves is that a session evicted while holding NO browser is bricked
silently. The obvious fix is to make the proxy's liveness question **session-scoped as
well as port-scoped**: `mcp.client.streamable_http` synthesises
`{"code": 32600, "message": "Session terminated"}` at exactly one site, reached only
from a 404 to a POST carrying a session id, and the bridge already sees that frame —
so the signal is clean. **The consequence is not.** A browser-less session is not
protected, so the replacement binds its port and supersedes its record entry; when it
heals it finds only the replacement's entry, refuses it on identity, cold-starts
against it — and the replacement, equally browser-less, is not protected either. It
evicts back. That is the same unbounded eviction war one level down. Closing this
needs one of two real decisions first: widen the protection from "owns a live browser"
to "has a live client session" (which stops being a LOCAL read, and the rule has to be
askable from a cold-start path where there is no backend to ask), or let a heal ADOPT
the replacement rather than cold-start against it (which means running a session
against source it did not start — what issue #14 exists to prevent).

What is deliberately NOT wanted either way is an **ordered** eviction (version, then a
record-time stamp): with two clients at the same version and different source bytes —
the measured case — the arriving one is always later, so the order permits exactly the
eviction that does the harm, and where it does bite it still kills the older session's
browsers. See `audit/stage2/finding_F886_eviction_kills_sibling_browsers.md` §3 and §6
residual 1.

**Isolated workspaces never bind a port the developer is using.** The harness's
`_pick_free_port` refuses the product's default singleton port and every port the
developer's REAL `~/.stealth-mcp/server.json` records, and retries; the ephemeral
range covers both, and a throwaway backend squatting a live backend's recorded port
while that backend was down would be adopted by the developer's next real proxy and
then die at workspace teardown. `tests/test_release_gate_harness_ports.py` pins the
pick without a socket or a real home.

---

## Branch / PR / commit conventions

- Work on a branch; **do not** commit to `main` directly.
- **One checkpoint commit per independently-verifiable step**, and **the suite is green
  at every checkpoint** — so any commit is a safe revert point and the history reads as
  a sequence of provable steps (this is the discipline the audit fix-branches follow).
- Keep unrelated changes out of a commit; a code touch and a docs touch are separate
  commits so either can be reverted alone.
- `--no-verify` is not used; if a hook or gate fails, fix the cause.
- Open one PR per logical change; the human reviews and merges (merge gates are
  per-PR).

---

## Adding a tool

A tool is a **plain `async def` in a section module**, plus an entry in that module's
`TOOLS` tuple. That is the whole mechanism:

1. Write the function in `src/stealth_chrome_devtools_mcp/embedded/tool_sections/<section>.py`
   — the module whose `SECTION` matches the section you want it in. Resolve every
   singleton and knob as `rt.<name>` (`tool_runtime` is the one patchable home, resolved
   at call time); raise `ToolError` on failure (DESIGN §9).
2. Add it to that module's `TOOLS` tuple, in the surface order you want.

That is all. **Do not** apply `@section_tool` (or any `mcp.*` decorator) in a section
module, and do not import `mcp`, `registry` or `server` there — `server.py`'s binding
loop over `SECTION_MODULES` applies the decorator and binds the name, once per execution
of `server.py`'s module body, and that is what keeps the `runpy` `__main__` load serving
a full app. DESIGN §8 has the why; `tests/test_tool_sections_contract.py` enforces it by
AST, and `tests/test_tool_module_reload.py` catches the failure the count tripwire cannot
see. A whole new *section* additionally means a new module and an entry in
`tool_sections/__init__.py`'s `SECTION_MODULES` (and `tests/source_scan.py`'s floor
moves with it).

The tool count is derived from `SECTION_TOOLS`, so adding to a `TOOLS` tuple updates it
by itself — but the served surface is pinned by the HARD golden
`tests/goldens/tool_surface.json`, so a new tool means a deliberate regeneration
(`PYTHONUTF8=1 python tools/dump_tool_surface.py --write`) with a justification, and the
`94` in the root docs moves in the same PR.

The canonical **verb taxonomy** — the one tool-naming rule new tools follow (`list_*`,
`get_*`, `create_*`/`spawn_*`, `execute_*`/`call_*`, `extract_*`/`clone_*`,
`set_*`/`modify_*`/`clear_*`, `discover_*`/`inspect_*`) — is the module docstring of
`embedded/tool_registry.py`. Follow it; do not restate it elsewhere (one home per rule).
