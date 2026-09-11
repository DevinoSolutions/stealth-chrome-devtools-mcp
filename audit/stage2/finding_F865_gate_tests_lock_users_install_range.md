# F-865 — the gate tests `uv.lock`; users install the wheel's ranges, and the two diverge

**Status:** FIXED in this PR (release-engineering defect; observed on the live fleet 2026-09-11, mechanism confirmed with `uv`)
**Opened by:** the 2.1.2 live test — the freshly installed backend logged no F-862 reaper activity, and the reason was the library underneath it
**Source at:** `origin/main` = `fd30fba` (2.1.2)
**Severity:** HIGH for what a green gate means. Every gate cell exercised `mcp 1.27.1`; every user installing 2.1.2 on Python 3.12 got `mcp 1.30.0`, a library whose *private* session manager `session_hygiene.HygienicSessionManager` subclasses and whose 1.30 line changes session behaviour no test had seen. "Green ⇒ what ships works" was false for the one dependency the newest fix depends on most.

---

## 1. What was observed

| fact | evidence |
|---|---|
| the uv tool venv for `stealth-chrome-devtools-mcp==2.1.2` (fresh PyPI install, Python 3.12) carries `mcp 1.30.0`, `starlette 1.6.0`, `anyio 4.15.1` | `importlib.metadata` in `%APPDATA%\uv\tools\stealth-chrome-devtools-mcp` |
| the repo's `uv.lock` — what every gate cell `uv sync`s — carries `mcp 1.27.1`, `starlette 1.0.0`, `anyio 4.13.0` | `uv run python -c "importlib.metadata.version('mcp')"` at `fd30fba` |
| `uv lock --upgrade --dry-run --refresh` in the repo resolves `mcp` to **1.27.2**, not 1.30.0, even though `mcp 1.30.0` declares `requires-python >= 3.10` and the latest release is 2.2.0 | run 2026-09-11 15:1x |
| `uv lock --upgrade-package mcp==1.30.0 --dry-run` fails: *"for split python_full_version >= '3.14': mcp==1.30.0 depends on pydantic >= 2.12.0 (python >= 3.14) … your project depends on pydantic==2.11.7 … unsatisfiable"* | same run |
| the F-862 reaper's hermetic tests pass 7/7 under `mcp 1.30.0` (`uv run --isolated --with mcp==1.30.0 --with starlette==1.6.0 --with anyio==4.15.1`), and the 2.1.2 backend served 10 min without error — the divergence did not break anything **this time** | live test after the session reset |
| `mcp 1.30.0` changes what the reaper sits on: `_discard_session` is called right after a request when `transport.is_terminated` (a DELETE now unlists the session — the "listed forever" channel of F-862 is closed upstream), `session_idle_timeout` defaults to 1800 s, `max_request_body_size` defaults to 4 MiB (413 above it), `max_sessions` defaults to 10 000 (503 above it) | `mcp/server/streamable_http_manager.py` in the tool venv, lines 78-92 and 291-296 |

## 2. Mechanism

1. `[project.dependencies]` pins every DIRECT dependency exactly (`fastmcp==2.11.2`, `pydantic==2.11.7`, …), so the repo's lock and a user's install agree on those. But the source also imports packages that arrive only **transitively**: `mcp` (through `fastmcp`, which declares `mcp>=1.10.0`), `anyio`, `starlette`, `httpx` (through `mcp`), plus `requests` on a range. For those, the wheel's metadata says nothing and the installer picks.
2. `uv.lock` is a **universal** resolution: one version per package that satisfies every Python in `requires-python = ">=3.11"` (no upper bound, so 3.14 included). `mcp >= 1.28` requires `pydantic >= 2.12` on 3.14, the project pins `pydantic == 2.11.7`, so the universal resolver caps `mcp` at 1.27.x **for every Python** — including the 3.12 cells that make up most of the gate.
3. A user's installer resolves the wheel's metadata for **one** Python. On 3.12 nothing caps `mcp`, so it takes the newest that fits: 1.30.0. The gate and the user disagree by three minor versions, by construction, with no one having changed anything.
4. The `test` extra's `mcp>=1.12` did not help: it is a range, and it is not in the wheel's runtime metadata.
5. Nothing in the gate could notice: the install-smoke cells install the wheel and run a smoke journey (so they DID run on `mcp 1.30.0`), but the suite that carries the claims runs against `uv sync` = the lock.

## 3. Fix (this PR)

- `pyproject.toml` `[project.dependencies]`: `mcp==1.27.1`, `anyio==4.13.0`, `starlette==1.0.0`, `httpx==0.28.1` added; `requests>=2.32.0,<4` → `requests==2.34.2`. All at the versions `uv.lock` already resolved, so the gate's behaviour is unchanged and the wheel now installs exactly it. `mcp>=1.12` leaves the `test` extra (the runtime pin covers it). `uv lock` re-run: manifest only, no version moves.
- New `tools/check_pinned_imports.py` — **THE one home for "is what the source imports pinned at what the lock resolved"**: walks every `import` under `src/` (AST; stdlib and first-party excluded), maps module → distribution through `importlib.metadata.packages_distributions`, and fails when a distribution is not an exact `==` pin in `[project.dependencies]` or its pin disagrees with `uv.lock`. Distributions declared in an `[project.optional-dependencies]` extra are the named exceptions (`py2js`). Wired into `.husky/pre-commit` and the release gate's `quality` cell next to the other `check_*` tools.
- CI installs with `uv sync --locked` (release gate, all eight sync steps, and the canary), so a `pyproject.toml` that moved without `uv lock` fails instead of silently re-locking on the runner.
- `tests/test_pinned_imports.py`: the real tree has no floating import; an unpinned import bites; a range is not a pin; a pin that disagrees with the lock bites and names both versions; the locked pin passes; stdlib/first-party are not third-party; an optional-extra import is allowed; extras and case do not hide a pin. RED (tool absent, then the real tree failing on `anyio`, `httpx`, `mcp`, `requests`, `starlette`) → GREEN.
- `CONTRIBUTING.md` gate rules gain the paragraph; `CHANGELOG.md` `### Fixed — … (F-865)`.

## 4. What this deliberately does NOT do

- **It does not move any version.** 2.1.3 ships `mcp 1.27.1` — the version every gate cell ran and the version F-862 was measured on — to users who were getting 1.30.0. Bumping to 1.30 (or 2.x) is its own change: raise the pin, run `uv lock`, and let the gate test it. Note that reaching `mcp >= 1.28` under the current universal lock needs `pydantic >= 2.12` (or an upper bound on `requires-python`), which is a decision, not a chore.
- **It does not pin every transitive package**, only the ones the source imports. A package the source never names (pydantic-core, httpcore, …) can still drift between the lock and an install; that drift cannot break an import the source makes, and pinning the whole closure in `[project.dependencies]` would turn the wheel into a lockfile. If a future drift in an un-imported package bites, the lever is `[tool.uv] constraint-dependencies` plus a generated pin, not a hand-maintained list.
- **It does not add a lock-freshness gate** (`uv lock --upgrade --dry-run` must be empty). That would redden every PR the day any upstream releases; the property we want is "the gate tested what ships", which pins give, not "we ship the newest".

## 5. Follow-ups

- A deliberate `mcp` bump PR: `mcp 1.27.1 → 1.30.x` (or 2.x) with `pydantic 2.12`, re-measuring F-862 (1.30 forgets a DELETE'd session itself; the reaper keeps the abandoned channel) and re-reading the new defaults (4 MiB body cap vs `MAX_USER_SCRIPT_BYTES`; 30-min idle timeout vs a proxy's standing GET stream; 10 000 sessions vs the fleet's probe churn, F-864).
- The install-smoke cells run the built wheel on a fresh resolution; with the pins in place they now run the SAME versions as the suite, so the evidence they carry finally describes the same software.
