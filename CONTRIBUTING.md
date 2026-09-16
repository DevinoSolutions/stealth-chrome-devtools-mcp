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
oracle on one where it did, with no flake either way. The longest consecutive run is
printed so which case a run hit is readable from the output. Below the limit the CPU
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

**The one open defect this module pins (`xfail(strict=True)`): an eviction closes
another session's browser, silently.** When a backend is replaced **on the same
port** — the F-829 source-fingerprint eviction, and by extension `restart` — the
cold-start lock calls `singleton._clear_stale_backend` → `_terminate_backend` on the
running backend, and afterwards the browser that backend owned is gone — measured six
times with no exception, on the pid captured at spawn. (Which of the two candidate
mechanisms kills it was not isolated: dying with the terminated backend, or being
reaped as unowned by the replacement's orphan recovery, since `browser_pid_registry`
stamps the *backend* as owner.) The other
session is never told: its log carries no condemnation, no heal and no teardown —
only transient strikes that reset themselves (usually one `probe failed 1/3`; once
`2/3`). The proxy's **fast** death witness is port-only: `backend_watchdog.watch_liveness`
probes with `singleton._backend_http_ready`, which asks the port and not the identity,
and the replacement binds the SAME port and answers it — so the three strikes that
would open the confirmation phase never accumulate, and the confirmation that *is*
identity-scoped (`_same_identity_backend_ready`) is never reached; the
streamable-HTTP bridge is per-request, so nothing "breaks" for
`_confirm_bridge_verdict` either. What actually died is the MCP **session**, and
nothing watches that, which makes `proxy_selfheal`'s entire recovery unreachable on
the most common way a backend goes away. The client-visible half *varies* (5 of 6 runs the
loser answered `Session terminated` forever; 1 of 6 it kept answering over a backend
that no longer had its browser), so the node asserts the half that did not vary — the
browser — and prints the rest.

The proposed universal rule, for whoever fixes it: make the watchdog's per-tick check
**session-scoped as well as port-scoped**. The backend already hands each proxy an
`mcp-session-id`; a proxy that gets an invalid-session answer for its OWN id knows its
backend is gone even though the port answers. Feed that into the existing
`watch_liveness` verdict and the existing heal path fires, so an evicted session
re-bridges instead of bricking. A second, independent rule removes the churn rather
than the symptom: give the backend record an **order** (installed version, then a
monotonic stamp written at record time) and let only a strictly-newer client evict —
`backend_registry.fingerprint_mismatch` answers "these digests differ", never "mine is
newer", so today nothing makes an eviction ping-pong impossible; an older or
equal-but-different client should adopt the running backend, or exit naming both
digests and the upgrade that reconciles them. The convergence node is written to
hold under either rule (at most one wave; the fleet ends on exactly one live recorded
backend); the stronger property "a newer install must take effect" is the identity
gate's own contract (`test_singleton_version_aware`) and belongs beside whichever
rule the fix chooses, not in this fleet, which has no "newer" side — only a
different one.

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
