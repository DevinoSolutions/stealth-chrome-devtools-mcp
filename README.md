# Stealth Chrome DevTools MCP

[![PyPI](https://img.shields.io/pypi/v/stealth-chrome-devtools-mcp?color=blue&label=pypi)](https://pypi.org/project/stealth-chrome-devtools-mcp/)
[![Tests](https://github.com/DevinoSolutions/stealth-chrome-devtools-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/DevinoSolutions/stealth-chrome-devtools-mcp/actions/workflows/test.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-compatible-purple.svg)](https://modelcontextprotocol.io)

> Undetectable browser automation for AI agents via the Model Context Protocol —
> one shared backend built for 50+ concurrent Claude Code sessions.

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
- **Named sessions** — a session keeps its cookies and logins; new ones are seeded from `default`
- **Stealth arg filtering** — automatically strips 30+ detectable Chrome flags (Puppeteer/Playwright signatures, automation markers)
- **Multi-instance support** — spawn and manage multiple browsers simultaneously
- **Built for fleets of Claude Code sessions** — a session costs a thin stdio proxy
  (≈ 60 MB resident), not a browser: every session shares one backend per desktop,
  and a Chrome exists only where a session spawned one. Measured with 62 sessions
  attached at once; simultaneous cold start is scale-tested at 50 concurrent
  sessions, all usable in seconds against one backend — see
  [Built for fleets](#built-for-fleets-50-claude-code-sessions-one-backend)
- **A busy session re-attaches, and only then suffixes** — spawning with a `session` a live browser still holds joins THAT browser; `github-session` becomes `github-session-2` only when the re-attach declines
- **Orphan recovery** — safely cleans up leaked browser processes without killing live ones
- **Session persistence** — a new session carries the cookies, logins and Web Data of `default`
- **Zero idle timeout** — browsers stay alive until explicitly closed
- **Full CDP access** — DOM manipulation, network interception, JavaScript execution, screenshots

## Installation

### The right way — `uv tool install` (persistent, fleet-safe)

```bash
uv tool install stealth-chrome-devtools-mcp==2.1.12
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
50) turns startup into a package-resolution storm. A `uv tool install` gives
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
      "args": ["stealth-chrome-devtools-mcp==2.1.12"]
    }
  }
}
```

Or via pip (`pip install stealth-chrome-devtools-mcp==2.1.12`), then use the
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

### Built for fleets: 50+ Claude Code sessions, one backend

The usual MCP browser server runs one server process and one Chrome per client
session. That is fine for one session and ruinous for fifty: every session pays
for a whole browser before it has done anything. This server is shaped
differently. A Claude Code session runs only a thin stdio proxy. The browsers
live in one shared backend per desktop, and a Chrome exists only where a session
asked for one.

Measured on a Windows 11 workstation (2026-09-11) with 62 Claude Code sessions
attached at once:

| | Measured |
|---|---|
| Claude Code sessions attached to the shared backends | 62 |
| Resident memory per session (its stdio proxy process tree) | ≈ 60 MB, ≈ 3.7 GB across all 62 |
| The same 62 sessions if each ran its own Chrome (≈ 750 MB per browser) | ≈ 46 GB |
| Backends on the machine | 3 — one per desktop context, plus headless |
| Live Chrome instances | 5 — only the ones sessions had spawned |
| Cold start: 50 sessions at once, every one usable (`initialize` + `tools/list`) | 7.3 s (`tests/test_startup_herd.py`) |
| A 51st session joining the warm backend | 1.0 s |

What that buys a fleet:

- **Memory scales with the browsers you use, not the sessions you open.** An
  idle session holds a proxy, not a Chrome. Sixty sessions with five browsers
  between them pay for five browsers and sixty proxies, not sixty browsers.
- **One cold start per backend.** The first session boots the backend under a file
  lock; every other session converges on it and is usable in seconds. A session
  that arrives later joins in about a second.
- **Nothing is left behind.** Orphaned Chrome processes are reaped without
  killing live ones, and a spawn that fails after Chrome launched cleans up its
  own browser.
- **Startup is not a package-resolution storm.** With `uv tool install` every
  proxy is a pinned executable, so fifty sessions starting together do not
  re-resolve the package fifty times.

The per-session and per-browser figures above are the ones to plan capacity
around. The backend's own footprint depends on what the sessions do with it
(captured network bodies, stored element clones, live tabs), so it is not quoted
as a constant.

### Sessions

A **session** is a named, persistent Chrome profile: `spawn_browser(session="acme")`,
or `stealthy spawn --session acme`. Ask for the same name again and you get the
same cookies and the same logins — and if a browser is still open on it, that
browser, not a new one.

**`default`** is the session you get when you name none. It is where a human
logs in, and every new session starts as a copy of it. `session="default"` opens
it explicitly. It is reserved: you cannot create a session of your own by that
name (nor by `master` or `master-snapshot`, which are the mechanism's own
directories).

```
C:\stealth-mcp-browser-sessions\
  master/              # the `default` session — your logins, cookies, extensions
  master-snapshot/     # its seed: a safe copy, refreshed while `default` is closed
  sessions/            # your named sessions, and disposable copies
    acme/
    acme-2/            # auto-suffixed when a browser already holds `acme`
```

The directory names are historical and are what you will see in a path; the
words you type and the words the tool answers with are `default` and the seed.

1. An unnamed spawn opens `default` when it is free
2. Before opening it, the server refreshes its seed
3. When `default` is busy, a disposable copy is made from the seed
4. Copies carry all cookies, logins, and session data
5. A stale seed is auto-refreshed when auth files change

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
# The shared default session (what you get when you name none)
spawn_browser()

# A named session, with login persistence
spawn_browser(session="github-session")

# Same name while a browser still holds it → re-attached to THAT browser
spawn_browser(session="github-session")

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
| `BROWSER_MASTER_USER_DATA_DIR` | `<root>/master` | the `default` session's directory (name is historical) |
| `BROWSER_MASTER_SNAPSHOT_DIR` | `<root>/master-snapshot` | its seed: what a new session is copied from |
| `BROWSER_PROFILE_CLONE_ROOT` | `<root>/sessions` | Folder for profile copies |
| `BROWSER_PROFILE_REFRESH_DAYS` | `7` | **Currently inert.** Its only reader was deleted as dead code in F-892 — nothing refreshes a copy after N days, and nothing has since the setting was introduced. The field is kept so an existing `.env` naming it still loads; see `audit/stage2/finding_F892_*.md` §6. |
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

Installs a **`stealthy`** command. It both operates the backend and drives its
tools, so a shell can do anything an AI client can. (`stealth-chrome-devtools` is
the same command under its older name — one CLI, two names, not two tools.)

### Drive the browser from a shell

Every one of these talks to the **backend your shell would be served by** — the
same one `status` reports — and starts one if none is running (`--no-start` makes
that an error instead: a live backend is always used as-is, whatever build it is,
but starting one can evict a *wedged* backend of another build). Output is a
table on a terminal and JSON in a pipe:

```console
stealthy ls                                          # browser instances
stealthy spawn --session seller-central --headed     # recover a stranded login
stealthy spawn --headed                              # whatever spawn_browser() picks
stealthy nav e364 https://example.com --wait load    # ids resolve by prefix
stealthy call get_cookies --arg instance_id=e364c31b --arg domain=example.com
stealthy tools --section browser-management
```

`call` reaches **any** tool the backend serves — there is no per-tool mirror
here, so a newly added tool is callable the day it ships. `--arg key=value`
values are JSON when they parse (`headless=false`, `viewport_width=1200`,
`browser_args=["--x"]`) and plain strings when they do not; `--json '<object>'`
passes the whole arguments object at once.

`spawn --session <name>` is the **stranded-login recovery**: if a browser is
already holding that session it is re-attached to over CDP — same window, same
open page — and the answer says `REATTACHED`. See RUNBOOK, *Recover a stranded
login*. A session is a NAME; to open a directory by PATH use
`stealthy call spawn_browser --arg user_data_dir=<path>`. A `spawn` with no
`--session` is whatever `spawn_browser()` itself selects. Either way the
answer prints the `profile_selection` the backend actually made — role and
directory — so you can see what you got rather than infer it.

#### Exit codes are a closed set

Every failure there is becomes one of these, and errors are one line on stderr —
a script never gets a raw traceback paired with Python's own exit 1:

| Code | Means |
|---|---|
| `0` | it worked |
| `1` | the tool answered and said **no** — the round trip succeeded |
| `2` | usage: a bad flag, a bad `--arg`, an ambiguous id, no verb at all (argparse's own code, so its refusals and ours are indistinguishable to a script) |
| `3` | no backend: nothing running and `--no-start`, or the transport failed, so nothing on the backend ever saw the request |
| `70` | a bug in the CLI itself (`EX_SOFTWARE`) — never 1, which would blame the tool |
| `130` | `Ctrl-C` |
| `141` | the **reader** went away: `128 + SIGPIPE`, so `stealthy ls \| head -1` under `set -o pipefail` looks exactly like `ls \| head -1`. No message — your `head` did what you asked |

`--traceback` adds the full stack **after** that one line; it never replaces it
and never changes the code.

### Operate the backend

These four only read and preview — they change nothing, and the test suite runs
them on every commit, so they are known to work:

<!-- doc-example: runnable -->
```console
stealthy status
stealthy profiles
stealthy cleanup
stealthy cleanup --browser-session-cap-gb 12
```

`status` reports whether the backend is up plus the browser-session root and both
caps; `profiles` lists profiles with size / role / in-use; `cleanup` previews the
reclaimable disk (dry run), and `--browser-session-cap-gb` previews it at a
tighter cap.

These are not auto-executed — `--apply` deletes, `serve` does not return, and
`doctor` needs Chrome installed:

```console
stealthy cleanup --apply               # actually reclaim
stealthy doctor                        # check Chrome / environment
stealthy serve --http --port 19222     # start the server
```

`cleanup` deletes idle auto-clones over the clone cap and trims idle named
profiles down to their session state — **logins kept** — over the browser-session cap. It
is a **dry run unless you pass `--apply`**, never touches in-use profiles, and
uses the same selectors as the automatic sweep, so the preview matches `--apply`.

## Preparing the `default` session

1. Start the MCP server
2. Call `spawn_browser()` with no `session` — or, from a shell,
   `stealthy spawn --headed`
3. Sign in to your accounts in the browser that opens
4. Close it — new sessions are seeded from it, and it is reused directly

That reaches `default` **only while `default` is free**; once a browser holds
it, the same call gets a disposable copy of its seed instead. The
`profile_selection` in the answer (`profile_role` + the directory) says which of
the two you got — `default` or `clone`.

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
