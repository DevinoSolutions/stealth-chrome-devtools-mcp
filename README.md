# Stealth Chrome DevTools MCP

[![PyPI](https://img.shields.io/pypi/v/stealth-chrome-devtools-mcp?color=blue&label=pypi)](https://pypi.org/project/stealth-chrome-devtools-mcp/)
[![Tests](https://github.com/DevinoSolutions/stealth-chrome-devtools-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/DevinoSolutions/stealth-chrome-devtools-mcp/actions/workflows/test.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-compatible-purple.svg)](https://modelcontextprotocol.io)

> Undetectable browser automation for AI agents via the Model Context Protocol.

A self-contained **stealth Chrome DevTools MCP server** with smart profile management, anti-detection stealth arg filtering, and robust process lifecycle handling. Built on [nodriver](https://github.com/AminDhouib/nodriver) (CDP-based) for full anti-bot evasion.

---

## Demos

### Cloudflare Turnstile Bypass

https://github.com/user-attachments/assets/c4de61ae-6878-4fff-9bfd-65cdd4fadc2f

[Watch on YouTube](https://www.youtube.com/watch?v=dx2ksEI056U)

### Persistent Login Sessions

https://github.com/user-attachments/assets/f81fc0c2-9233-48cd-8a9d-2577b1d33d57

[Watch on YouTube](https://www.youtube.com/watch?v=8w4ejfhTsLo)

---

## Key Features

- **Undetectable by anti-bot systems** — Cloudflare, DataDome, PerimeterX, etc.
- **Smart profile management** — master/snapshot/clone strategy preserves logins across sessions
- **Stealth arg filtering** — automatically strips 30+ detectable Chrome flags (Puppeteer/Playwright signatures, automation markers)
- **Multi-instance support** — spawn and manage multiple browsers simultaneously
- **A shared backend across sessions** — every client session proxies to a shared
  backend process rather than starting its own, one per desktop so a headed spawn
  lands on a real screen; simultaneous cold start is scale-tested at 40 concurrent
  sessions, all usable in seconds against one backend
- **Auto-suffix busy profiles** — `github-session` auto-becomes `github-session-2` when occupied
- **Orphan recovery** — safely cleans up leaked browser processes without killing live ones
- **Session persistence** — cloned profiles carry cookies, logins, and Web Data from master
- **Zero idle timeout** — browsers stay alive until explicitly closed
- **Full CDP access** — DOM manipulation, network interception, JavaScript execution, screenshots

## Installation

### The right way — `uv tool install` (persistent, fleet-safe)

```bash
uv tool install stealth-chrome-devtools-mcp==2.0.8
```

This installs a version-pinned executable at `~/.local/bin/stealth-chrome-devtools-mcp`
(Windows: `%USERPROFILE%\.local\bin\stealth-chrome-devtools-mcp.exe`). Point your MCP
config (`claude_desktop_config.json`, `~/.claude.json`, etc.) at it:

```json
{
  "mcpServers": {
    "stealth-chrome-devtools-mcp": {
      "command": "C:\\Users\\<you>\\.local\\bin\\stealth-chrome-devtools-mcp.exe",
      "args": []
    }
  }
}
```

Claude Code one-liner (use the `.exe` path above on Windows):

```bash
claude mcp add --scope user stealth-chrome-devtools-mcp -- ~/.local/bin/stealth-chrome-devtools-mcp
```

**Why not `uvx` in the config?** It works, but `uvx` re-resolves the package on
**every client session start**. Each Claude Code session launches its own stdio
proxy, so a fleet of concurrent sessions (the shared backend is scale-tested at
40) turns startup into a package-resolution storm. A `uv tool install` gives
every proxy an instant, pinned executable — the shared backend, profile
handling, and per-session browser isolation behave identically.

To upgrade later: `uv tool install stealth-chrome-devtools-mcp==<new-version>`
(or `uv tool upgrade stealth-chrome-devtools-mcp` to track the latest release).

### Alternatives

Zero-install trial (fine for a first look, not for fleets):

```json
{
  "mcpServers": {
    "stealth-chrome-devtools-mcp": {
      "command": "uvx",
      "args": ["stealth-chrome-devtools-mcp==2.0.8"]
    }
  }
}
```

Or via pip (`pip install stealth-chrome-devtools-mcp==2.0.8`), then use the
`stealth-chrome-devtools-mcp` console script from that environment as the
`command`.

Crashes are reported to the maintainers by default, with your username and
machine name scrubbed out. See [Error Reporting](#error-reporting) for what a
report contains and how to turn it off.

### Local Development

```json
{
  "mcpServers": {
    "stealth-chrome-devtools-mcp": {
      "command": "uv",
      "args": [
        "--directory", "/path/to/stealth-chrome-devtools-mcp",
        "run", "stealth-chrome-devtools-mcp"
      ]
    }
  }
}
```

## How It Works

### Browser Profile Strategy

```
C:\stealth-mcp-browser-sessions\
  master/              # Your primary Chrome profile (logins, cookies, extensions)
  master-snapshot/     # Safe copy refreshed while master is closed
  sessions/            # Cloned profiles for concurrent use
    github-session/
    github-session-2/  # Auto-suffixed when github-session is busy
```

1. `spawn_browser()` uses the master profile when available
2. Before opening master, the server refreshes `master-snapshot`
3. When master is busy, a clone is created from the snapshot
4. Clones carry all cookies, logins, and session data
5. Stale snapshots are auto-refreshed when auth files change

Clones exclude regenerable Chrome caches, so each is a few MB rather than
multiple GB. Disposable auto-clones are deleted on close, and a storage cap
(`STEALTH_MCP_CLONE_STORAGE_CAP_GB`, default 10 GB) reclaims the oldest **idle**
clones if any ever leak — so `sessions/` stays bounded. Cap eviction is
**recoverable**: an evicted clone is moved into `sessions/.trash/` and only
purged after a retention window (`STEALTH_MCP_CLONE_TRASH_RETENTION_HOURS`,
default 24 h), so a mistaken eviction can be restored rather than lost.

Named profiles you create explicitly (e.g. `github-session`) persist and are
never deleted. But even a "persistent" profile is ~98% regenerable (caches plus
Chrome's multi-GB on-device AI model). So when `sessions/` exceeds
`STEALTH_MCP_BROWSER_SESSION_STORAGE_CAP_GB` (default 20 GB), the largest **idle** named
profiles are trimmed of those regenerable dirs while **every login is
preserved** — Chrome rebuilds them on next launch. In-use profiles are never
touched.

> **Shared-machine note:** the browser-session root defaults to `C:\stealth-mcp-browser-sessions`
> (drive root), which holds your logged-in cookies and session data. On a
> single-user machine this is fine. On a **shared multi-user** Windows box, other
> local users may be able to read it — point `STEALTH_MCP_BROWSER_SESSION_ROOT`
> at a location inside your user profile (e.g. `%LOCALAPPDATA%\stealth-mcp`) so
> the OS user ACLs protect it.

### Stealth Arg Filtering

The server automatically strips Chrome flags that would compromise stealth:

| Category | Examples | Why Stripped |
|----------|----------|-------------|
| Automation signals | `--enable-automation`, `--test-type` | Sets `navigator.webdriver=true` |
| Fingerprint leaks | `--disable-gpu`, `--disable-webgl` | Detectable via WebGL/canvas probes |
| Puppeteer defaults | `--disable-backgrounding-occluded-windows` | Bot signature fingerprint |
| Playwright defaults | `--password-store=basic`, `--use-mock-keychain` | Bot signature fingerprint |

Stripped args are reported in `spawn_diagnostics.stealth_args_stripped`.

### Orphan Recovery

On server restart, the process cleanup system:

- Reaps only browsers whose **owner backend is dead** — every tracked browser records
  which backend started it, so two backends running side by side never reap each
  other's browsers
- Keeps `create_time` tracking as a second net: never kills a process that started
  **after** the current server session began
- Safely handles `psutil.AccessDenied` on Windows elevated processes

### Headed Browsing and Where the Window Opens

A headed browser appears on the desktop of whichever process **launched** it, not
of whichever session asked. Because sessions share a backend, a backend that was
first started from an SSH login or a Windows service session cannot show a window
to anyone — including the sessions running on the physical desktop.

So the backend is keyed by **display context**: one per desktop, plus one for a
headless context. Discovery prefers a backend that can show a window, which means
an SSH-driven `spawn_browser(headless=False)` automatically uses the desktop
backend and its window opens on the real screen. Where no such backend exists, the
spawn **raises** instead of handing back an invisible browser; run
`stealth-chrome-devtools doctor` to see which contexts have a backend. Headless
spawns work from anywhere.

## Usage Examples

```python
# Spawn with default master profile
spawn_browser()

# Named session with login persistence
spawn_browser(user_data_dir="github-session")

# Same name while first is open → auto-suffixes to github-session-2
spawn_browser(user_data_dir="github-session")

# Headless with stealth (bad args auto-stripped)
spawn_browser(headless=True, browser_args=["--enable-automation"])
# → stealth_args_stripped: ["--enable-automation stripped: sets navigator.webdriver=true"]
```

## MCP Tools

| Tool | Description |
|------|-------------|
| `spawn_browser` | Launch a new stealth browser instance |
| `navigate` | Navigate to a URL |
| `take_screenshot` | Capture page screenshot |
| `execute_script` | Run JavaScript in page context |
| `query_elements` | Find DOM elements by CSS selector |
| `click_element` | Click on an element |
| `type_text` | Type text into an input |
| `get_page_content` | Get page HTML content |
| `list_instances` | List all active browser instances |
| `close_instance` | Close a specific browser |
| `list_network_requests` | View intercepted network traffic |
| `get_cookies` / `set_cookie` | Manage browser cookies |

**94 tools** across 11 sections — the count is derived from the live tool registry,
never hand-maintained. [See the full navigation map →](CLAUDE.md).

That is what the server **serves**, which is not the same as what the release
gate **proves**. At the release SHA in the evidence ledger, 3 of those 94 are
release-qualified: asserted end-to-end over the real stdio transport a client
actually speaks. The rest are driven against real Chrome by the E2E suite but
through an in-process seam, so they are `served-unqualified` at the wire — tested,
not proved there. [`RELEASE_CONTRACT.md`](RELEASE_CONTRACT.md) lists the state of
each tool and is the only source for those numbers.

## Testing

```bash
# Unit tests only (no Chrome needed)
uv run pytest -m "not integration"

# All tests (needs Chrome installed)
uv run pytest

# Verbose with short tracebacks
uv run pytest -v --tb=short
```

> If your checkout path contains spaces or an `&`, `uv run pytest` fails with
> `Failed to canonicalize script path` — use the venv Python directly:
> `.venv\Scripts\python.exe -m pytest -m "not integration"`. See
> [CONTRIBUTING.md](CONTRIBUTING.md) for the full test/gate workflow.

A comprehensive suite covers stealth arg filtering, profile resolution, orphan recovery, storage-cap sweeps, the ops CLI, and full browser integration.

## Environment Variables

All optional. Defaults work for normal use. Set them in your shell, or in
`~/.stealth-mcp/.env` — every key is documented in [`.env.example`](./.env.example).

A `.env` in your **project** directory is deliberately ignored. The backend is a
shared process launched with whatever folder your MCP client had open, so
reading the project's `.env` meant reading someone else's application config —
which crashed the server outright on an ordinary `DATABASE_URL` and silently
adopted that app's `PORT`, `DEBUG`, and `SENTRY_DSN` as the server's own.

| Variable | Default | Purpose |
|----------|---------|---------|
| `STEALTH_MCP_BROWSER_SESSION_ROOT` | `C:\stealth-mcp-browser-sessions` (Win) / `~/.stealth-mcp-browser-sessions` (Unix) | Base folder for profiles |
| `BROWSER_MASTER_USER_DATA_DIR` | `<root>/master` | Master Chrome profile path |
| `BROWSER_MASTER_SNAPSHOT_DIR` | `<root>/master-snapshot` | Snapshot clone source |
| `BROWSER_PROFILE_CLONE_ROOT` | `<root>/sessions` | Folder for profile copies |
| `BROWSER_PROFILE_REFRESH_DAYS` | `7` | Refresh copies after N days (`0` = disable) |
| `STEALTH_MCP_CLONE_STORAGE_CAP_GB` | `10` | Cap on total auto-clone storage; oldest **idle** clones are reclaimed when exceeded (`0` = disable). Named profiles and in-use clones are never touched. |
| `STEALTH_MCP_BROWSER_SESSION_STORAGE_CAP_GB` | `20` | Cap on total `sessions/` storage; when exceeded, the largest **idle** named profiles are trimmed of regenerable cache/model dirs — logins kept (`0` = disable). *(Renamed from `STEALTH_MCP_SESSION_STORAGE_CAP_GB`; update your config — the old name is no longer read.)* |
| `STEALTH_MCP_CLONE_TRASH_RETENTION_HOURS` | `24` | How long a cap-evicted clone stays recoverable in `sessions/.trash/` before purge (`0` = purge on next sweep). |
| `STEALTH_MCP_CLONE_OUTPUT_DIR` | `~/.stealth-mcp/element_clones` | Where screenshots, large-response spills, and element-clone files are written. Kept in a per-user dir (never inside the installed package) so a read-only `site-packages` can't break captures. |
| `BROWSER_IDLE_TIMEOUT` | `0` | Idle cleanup timeout (`0` = disabled) |
| `STEALTH_CHROME_PROFILE_KEY` | unset | Force a stable clone key |
| `STEALTH_MCP_CLIENT_ROOTS_TIMEOUT_SECONDS` | `5` | Deadline for the `roots/list` request the auto-clone path sends to the MCP client to name a clone. MCP `roots` is optional, so a client may never answer; on expiry the clone name falls back to `CODEX_WORKSPACE`/`CLAUDE_PROJECT_DIR`/`PWD`/cwd (`0` = never ask). |
| `STEALTH_BROWSER_DEBUG` | `false` | Enable debug logging |
| `STEALTH_MCP_NO_ERROR_REPORTING` | `false` | Set to `true` to disable [error reporting](#error-reporting) |

## CLI

Installs a `stealth-chrome-devtools` ops command for managing the server and its
disk usage. (This is for *ops* — to drive a browser, use the MCP server or its
HTTP backend.)

These four only read and preview — they change nothing, and the test suite runs
them on every commit, so they are known to work:

<!-- doc-example: runnable -->
```console
stealth-chrome-devtools status
stealth-chrome-devtools profiles
stealth-chrome-devtools cleanup
stealth-chrome-devtools cleanup --browser-session-cap-gb 12
```

`status` reports whether the backend is up plus the browser-session root and both
caps; `profiles` lists profiles with size / role / in-use; `cleanup` previews the
reclaimable disk (dry run), and `--browser-session-cap-gb` previews it at a
tighter cap.

These are not auto-executed — `--apply` deletes, `serve` does not return, and
`doctor` needs Chrome installed:

```console
stealth-chrome-devtools cleanup --apply               # actually reclaim
stealth-chrome-devtools doctor                        # check Chrome / environment
stealth-chrome-devtools serve --http --port 19222     # start the server
```

`cleanup` deletes idle auto-clones over the clone cap and trims idle named
profiles down to their session state — **logins kept** — over the browser-session cap. It
is a **dry run unless you pass `--apply`**, never touches in-use profiles, and
uses the same selectors as the automatic sweep, so the preview matches `--apply`.

## Preparing the Master Profile

1. Start the MCP server
2. Call `spawn_browser()` without `user_data_dir`
3. Sign in to your accounts in the browser that opens
4. Close it — future sessions use this profile or clone from it

## Requirements

- Python 3.11+
- Chrome, Chromium, or Microsoft Edge
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- A desktop session for **headed** browsing (headless works from SSH, CI, and services)

## Error Reporting

Crashes and errors are reported to [Sentry](https://sentry.io) **by default**, so
that a failure you hit is a failure we can see and fix. There is nothing to
install and nothing to configure: the SDK ships with the package and the
destination is built in.

**What a report contains.** The exception type and message, the stack trace, the
package version, and the platform. Three things are kept out of it:

- your **machine name** (Sentry's `server_name`) is dropped entirely;
- your **username** is removed from every path, so a stack frame reads
  `C:\Users\~\...`, `/home/~/...` or `/Users/~/...` instead of your home
  directory;
- **local variables are not captured at all.** The Sentry SDK sends them by
  default; we turn that off, because a local in this tool can hold a proxy
  password, an `Authorization` or `Cookie` header, or a script you asked it to
  run — secrets that no path rule could rescue.

That is universal — it runs on every install, ours included, and there is no way
to opt back into sending those fields. What it deliberately leaves alone is the
part that makes a report useful: the error type, the module path after the home
segment, the source line that failed, and the release it came from.

An error message still quotes whatever the failing call was working with — a URL
you navigated to, a file you asked for. If that is not a trade you want to make,
turn reporting off.

To turn it off, set one variable in your shell or in `~/.stealth-mcp/.env`:

```bash
STEALTH_MCP_NO_ERROR_REPORTING=true
```

Earlier releases read `SENTRY_DSN` from the environment. They no longer do —
that variable belongs to *your* application, and a shared backend launched from
your project folder was picking it up. See
[Environment Variables](#environment-variables) for why this tool ignores your
project's `.env` entirely.

## Development setup

```bash
git clone https://github.com/DevinoSolutions/stealth-chrome-devtools-mcp
cd stealth-chrome-devtools-mcp
uv sync --extra dev --extra test   # install linters + test deps
npm install                        # arm husky pre-commit/pre-push hooks
```

The six quality gates run automatically on every commit:
ruff format, ruff check, ty check, vulture, suppression-owner check, file-budget check.
Unit tests run on pre-push.

## Documentation

- **[CLAUDE.md](CLAUDE.md)** — navigation map of the source tree + glossary + conventions
- **[DESIGN.md](DESIGN.md)** — architecture invariants and the *why* behind them
- **[RUNBOOK.md](RUNBOOK.md)** — operating the backend: verbs, logs, recovery, MCP smoke path
- **[CONTRIBUTING.md](CONTRIBUTING.md)** — clone → install → test, the quality gate, conventions

## License

See [LICENSE](LICENSE).

---

Built by [Devino Solutions](https://devino.ca)
