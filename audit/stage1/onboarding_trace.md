# Stage 1 — Docs-Only Onboarding Trace

Role-played: brand-new engineer, day one, Windows 11, this exact checkout
(branch `fix/singleton-version-aware-backend`, short SHA 2267b83).
Constraint: **README.md only**, plus files it explicitly links (`src/stealth_chrome_devtools_mcp/embedded/server.py`
for "See all tools →", and `LICENSE`). No CONTRIBUTING.md, no `docs/` folder, no INSTALL.md exist or are
referenced — confirmed via a recursive `*.md` search of the repo (excluding `.venv`): only `README.md`,
`CODEBASE_AUDIT.md`, `HIGH_VALUE_CHANGES.md`, `MCP_TOOL_TEST_RESULTS.md`, and `audit/` notes exist, none linked
from README and none consulted for this trace.

Known env quirk under test: this checkout's absolute path contains a bare `&` and spaces —
`C:\Users\amind\OneDrive\Desktop\Projects\CUSTOM MCPs & PRODUCTIVITY\stealth-chrome-devtools-mcp`.

---

## Step 1 — Install + Run (~12 min)

**1.1 Read README.md end-to-end (255 lines).** *(~3 min)*
Doc graph extracted: exactly two local links in the whole file — `[See all tools →](src/stealth_chrome_devtools_mcp/embedded/server.py)`
and `[LICENSE](LICENSE)`. No link to a contributing guide, install guide, or docs folder anywhere.
**Verdict:** no `git clone` / repo-acquisition instructions exist anywhere in the document (checked every line).

**1.2 Environment precheck.** *(~1 min)*
```
> uv --version; python --version
uv 0.9.29 (1f1321d84 2026-02-03)
Python 3.14.2
```
**Verdict:** OK, `uv` present.

**1.3 "Local Development" MCP config (README.md:64-78) is JSON for an MCP client, not a runnable shell
command — the doc gives no other way to sanity-check it.** Translated it by hand into the terminal command a
new hire would naturally type, substituting this checkout's real path, **unquoted** exactly as the placeholder
`/path/to/stealth-chrome-devtools-mcp` implies (no quoting hint given). *(~2 min)*
```
> uv --directory C:\Users\amind\OneDrive\Desktop\Projects\CUSTOM MCPs & PRODUCTIVITY\stealth-chrome-devtools-mcp run stealth-chrome-devtools-mcp --help

Id    Name    PSJobTypeName   State    HasMoreData
1     Job1    BackgroundJob   Running  True
PRODUCTIVITY\stealth-chrome-devtools-mcp: The module 'PRODUCTIVITY' could not be loaded.
For more information, run 'Import-Module PRODUCTIVITY'.
```
**Verdict: FAIL.** PowerShell 7's trailing `&` was parsed as the background-job operator: it silently launched
`Job1` against the truncated, nonexistent path `...CUSTOM MCPs`, then tried to run `PRODUCTIVITY\stealth-chrome-devtools-mcp`
as a module-qualified command, producing an error about a nonexistent PowerShell module — zero connection to the
real cause (an unquoted path). Confirmed the stray job self-terminated when its ephemeral parent `pwsh.exe`
process exited (`Get-Job` returned empty afterward); no manual cleanup was needed for this part. → **F-402**.

**1.4 Retried with the path double-quoted (my own fix — not documented anywhere).** *(~3 min incl. cleanup)*
```
> uv --directory "C:\...\CUSTOM MCPs & PRODUCTIVITY\stealth-chrome-devtools-mcp" run stealth-chrome-devtools-mcp --help
(no stdout/stderr; did not return — tool auto-backgrounded it after its timeout)
```
Inspected the live process tree: `uv.exe` → `stealth-chrome-devtools-mcp.exe --help` → `python.exe` → further
descendants (grandchildren spawned after start, confirming the server actually began real startup rather than
printing usage and exiting). Force-killed the whole tree: `taskkill /PID 44940 /T /F` → 8 processes terminated.
Verified (via `Get-CimInstance Win32_Process` filtered on `--directory`/`--help`) that this did **not** touch the
10+ unrelated, pre-existing `uvx stealth-chrome-devtools-mcp==1.0.0` production processes already running on this
machine.
**Verdict: FAIL as a smoke test.** No crash, but no confirmation output either — it silently began real server
startup and hung. A new hire has no documented way to tell "it's working, just blocking on stdio by design" from
"it's broken." → **F-405**.

**1.5 CLI section (README.md:214-228) — ran literally as printed.** *(~1 min)*
```
> stealth-chrome-devtools status
stealth-chrome-devtools: The term 'stealth-chrome-devtools' is not recognized as a name of a cmdlet,
function, script file, or executable program.
```
**Verdict: FAIL** as literally documented — the CLI block never distinguishes the pip-installed scenario (script
on PATH) from the local-checkout scenario the "Local Development" section two headings earlier was about.

**1.6 Guessed the missing `uv run` prefix (basic uv knowledge, not stated in the CLI section).** *(~2 min)*
```
> uv run stealth-chrome-devtools status
[stderr] ...fastmcp\server\auth\providers\jwt.py:10: AuthlibDeprecationWarning: authlib.jose module is deprecated...
backend     : not running
version     : 1.2.0
session root: C:\stealth-mcp-browser-sessions  (exists: True)
clone cap   : 10.0 GB  [STEALTH_MCP_CLONE_STORAGE_CAP_GB]
session cap : 20.0 GB  [STEALTH_MCP_SESSION_STORAGE_CAP_GB]

> uv run stealth-chrome-devtools doctor
python      : 3.12.12
platform    : Windows-11-10.0.26200-SP0
session root: C:\stealth-mcp-browser-sessions  (exists: True)
backend     : not running
chrome      : C:\Program Files\Google\Chrome\Application\chrome.exe
```
**Verdict: PASS**, once the undocumented `uv run` prefix is guessed — this is the *only* command in the entire
trace that gave a clean, human-readable "yes, your local checkout works" signal.

---

## Step 2 — Run the tests (~6 min)

**2.1 Documented unit-test command, exactly as written (README.md:183-184).** *(~2 min, incl. a retry without
output piping to rule out a harness artifact)*
```
> uv run pytest -m "not integration"
Failed to canonicalize script path
(exit code 1)
```
Reproduced identically three ways: with output piped, without piping, with `uv run pytest --version`, and with
an explicit **quoted** `--directory "<path>"` flag instead of relying on cwd. Always the same one-line failure,
no stack trace, no further detail.
**Verdict: FAIL.** The literal documented command does not run on this checkout, period.

**2.2 Isolated the cause by bypassing `uv run` entirely.** *(~2 min incl. ~62s test run)*
```
> .\.venv\Scripts\python.exe -m pytest -m "not integration" -q
........................................................................ [ 17%]
........................................................................ [ 35%]
........................................................................ [ 53%]
........................................................................ [ 71%]
........................................................................ [ 89%]
..........................................                               [100%]
402 passed, 24 deselected, 1 warning in 61.91s (0:01:01)
```
**Verdict: PASS.** Proves the code, the venv, and the tests themselves are completely healthy — the failure in
2.1 is 100% a `uv run` script-path-canonicalization problem specific to this checkout's directory name, not a
test or environment problem. Note `uv run stealth-chrome-devtools-mcp` and `uv run stealth-chrome-devtools
status/doctor` (Step 1.4/1.6) did **not** hit this error, so it's specific to how `uv run` resolves the `pytest`
console-script shim, not to every `uv run` invocation in this directory. → **F-403** (Critical).

*(Did not run the full `uv run pytest` / Chrome-requiring integration suite: the docs' own unit-only command is
the recommended safe first step, Chrome-driving tests are slow and risk leaving orphaned browser processes if
cut off, and the `uv run` breakage above already had to be routed around via the direct-venv invocation for
even the cheap subset. This is a scope judgment call, noted here rather than silently skipped.)*

---

## Step 3 — Ship a one-line change, dry run (no files edited) (~6 min)

Candidate trivial change (not applied): fix one of the two doc gaps just found — e.g. add the missing `uv run`
prefix to the CLI block, or quote the `--directory` placeholder in the Local Development JSON example.

| Sub-step | Documented in README? | Evidence |
|---|---|---|
| 3.1 Branch naming | **No** | Not mentioned anywhere; would have to guess (e.g. mirror the current branch's `fix/...` pattern) from outside knowledge/convention, not from any doc. |
| 3.2 Run tests before pushing | Yes, but **broken as written** on this checkout | Step 2 above — would additionally need the undocumented `.venv\Scripts\python.exe -m pytest` workaround. |
| 3.3 Lint / format / type-check | **No** | Zero mention of ruff/black/mypy/pre-commit anywhere in README; no CONTRIBUTING.md or docs/ folder exists in the repo to cover it either (confirmed by recursive `*.md` search). |
| 3.4 Open a PR / get reviewed / CI gate | **No** | No PR checklist, no reviewer expectations. Only artifact is a passing-tests badge image linking to a GitHub Actions URL — no actionable local instructions. |
| 3.5 Release / publish to PyPI | **No** | Package is clearly live on PyPI (badge + `pip install stealth-chrome-devtools-mcp` in Quick Start) but no RELEASING.md, version-bump location, or tag convention is documented anywhere. |

**Verdict:** of five steps needed to ship a one-line change, only one (3.2, testing) has any doc coverage at
all — and that one is broken as written on this exact checkout. The other four are 100% tribal knowledge.

---

## Totals

- **Rough elapsed time:** ~25 minutes of hands-on attempts to reach "tests confirmed passing," the majority of
  it spent diagnosing undocumented workarounds rather than doing productive setup.
- **Tribal-knowledge / guessed steps:** 6 — (1.3→1.4) quoting the `--directory` path, (1.5→1.6) the `uv run`
  CLI prefix, (2.1→2.2) the `.venv\Scripts\python.exe -m pytest` fallback, plus (3.1) branch naming, (3.3)
  lint/format tooling, (3.4)/(3.5) PR & release process.
- **Commands that worked exactly as documented, first try, no guessing:** 1 of 7 attempted (`uv --version` /
  `python --version` environment precheck is arguably the only fully clean one; even the CLI and test commands
  that eventually worked required an undocumented fix first).
- **Findings filed:** 7 (F-401…F-407) — see `findings_onboarding.json`.
