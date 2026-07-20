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

### The `uv run` caveat (important on this checkout)

If your checkout path contains spaces or an `&` — the dev checkout lives under
`…/CUSTOM MCPs & PRODUCTIVITY/…` — **`uv run` may fail to resolve** its environment.
That is not a bug in this package; it is `uv`'s resolver tripping on the special
characters in the path. Two working responses:

1. Invoke the **venv Python directly** (works everywhere, and is what the commands in
   this repo's docs use):
   ```
   .venv\Scripts\python.exe -m pytest -m "not integration" -q
   ```
2. Or check the repo out to a path without spaces/`&` (e.g. `C:\src\stealth-…`), where
   `uv run` works. CI checks out to a clean path, which is why CI uses `uv run`.

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
- **`ty` runs with `--exit-zero-on-warning`.** There is a tolerated baseline of typing
  warnings on pre-typing modules; *new* modules must be error-free.

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

## Where the naming rule lives

The canonical **verb taxonomy** — the one tool-naming rule new tools follow (`list_*`,
`get_*`, `create_*`/`spawn_*`, `execute_*`/`call_*`, `extract_*`/`clone_*`,
`set_*`/`modify_*`/`clear_*`, `discover_*`/`inspect_*`) — is the module docstring of
`embedded/tool_registry.py`. Follow it when adding a tool; do not restate it elsewhere
(one home per rule).
