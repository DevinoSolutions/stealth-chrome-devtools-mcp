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
- **Auto-suffix busy profiles** — `github-session` auto-becomes `github-session-2` when occupied
- **Orphan recovery** — safely cleans up leaked browser processes without killing live ones
- **Session persistence** — cloned profiles carry cookies, logins, and Web Data from master
- **Zero idle timeout** — browsers stay alive until explicitly closed
- **Full CDP access** — DOM manipulation, network interception, JavaScript execution, screenshots

## Quick Start

Add to your MCP config (`claude_desktop_config.json`, `.claude/settings.json`, etc.):

```json
{
  "mcpServers": {
    "stealth-chrome-devtools-mcp": {
      "command": "uvx",
      "args": ["stealth-chrome-devtools-mcp"]
    }
  }
}
```

Or install via pip:

```bash
pip install stealth-chrome-devtools-mcp
```

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
`STEALTH_MCP_SESSION_STORAGE_CAP_GB` (default 20 GB), the largest **idle** named
profiles are trimmed of those regenerable dirs while **every login is
preserved** — Chrome rebuilds them on next launch. In-use profiles are never
touched.

> **Shared-machine note:** the session root defaults to `C:\stealth-mcp-browser-sessions`
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

- Identifies browser processes from previous sessions via `create_time` tracking
- Only kills processes started **before** the current server session
- Never kills browsers spawned during the current run
- Safely handles `psutil.AccessDenied` on Windows elevated processes

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

[See all tools →](src/stealth_chrome_devtools_mcp/embedded/server.py)

## Testing

```bash
# Unit tests only (no Chrome needed)
uv run pytest -m "not integration"

# All tests (needs Chrome installed)
uv run pytest

# Verbose with short tracebacks
uv run pytest -v --tb=short
```

**256 tests** covering stealth arg filtering, profile resolution, orphan recovery, storage-cap sweeps, the ops CLI, and full browser integration.

## Environment Variables

All optional. Defaults work for normal use.

| Variable | Default | Purpose |
|----------|---------|---------|
| `STEALTH_MCP_BROWSER_SESSION_ROOT` | `C:\stealth-mcp-browser-sessions` (Win) / `~/.stealth-mcp-browser-sessions` (Unix) | Base folder for profiles |
| `BROWSER_MASTER_USER_DATA_DIR` | `<root>/master` | Master Chrome profile path |
| `BROWSER_MASTER_SNAPSHOT_DIR` | `<root>/master-snapshot` | Snapshot clone source |
| `BROWSER_PROFILE_CLONE_ROOT` | `<root>/sessions` | Folder for profile copies |
| `BROWSER_PROFILE_REFRESH_DAYS` | `7` | Refresh copies after N days (`0` = disable) |
| `STEALTH_MCP_CLONE_STORAGE_CAP_GB` | `10` | Cap on total auto-clone storage; oldest **idle** clones are reclaimed when exceeded (`0` = disable). Named profiles and in-use clones are never touched. |
| `STEALTH_MCP_SESSION_STORAGE_CAP_GB` | `20` | Cap on total `sessions/` storage; when exceeded, the largest **idle** named profiles are trimmed of regenerable cache/model dirs — logins kept (`0` = disable). |
| `STEALTH_MCP_CLONE_TRASH_RETENTION_HOURS` | `24` | How long a cap-evicted clone stays recoverable in `sessions/.trash/` before purge (`0` = purge on next sweep). |
| `STEALTH_MCP_CLONE_OUTPUT_DIR` | `~/.stealth-mcp/element_clones` | Where screenshots, large-response spills, and element-clone files are written. Kept in a per-user dir (never inside the installed package) so a read-only `site-packages` can't break captures. |
| `BROWSER_IDLE_TIMEOUT` | `0` | Idle cleanup timeout (`0` = disabled) |
| `STEALTH_CHROME_PROFILE_KEY` | unset | Force a stable clone key |
| `STEALTH_BROWSER_DEBUG` | `false` | Enable debug logging |

## CLI

Installs a `stealth-chrome-devtools` ops command for managing the server and its
disk usage. (This is for *ops* — to drive a browser, use the MCP server or its
HTTP backend.)

```bash
stealth-chrome-devtools status       # backend running? session root + caps
stealth-chrome-devtools profiles     # list profiles with size / role / in-use
stealth-chrome-devtools cleanup      # preview reclaimable disk (DRY RUN)
stealth-chrome-devtools cleanup --apply               # actually reclaim
stealth-chrome-devtools cleanup --session-cap-gb 12   # preview at a tighter cap
stealth-chrome-devtools doctor       # check Chrome / environment
stealth-chrome-devtools serve --http --port 19222     # start the server
```

`cleanup` deletes idle auto-clones over the clone cap and trims idle named
profiles down to their session state — **logins kept** — over the session cap. It
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

## Error Reporting (opt-in)

Error reporting via [Sentry](https://sentry.io) is available but **off by default**.
No data is collected unless you explicitly enable it.

To opt in (helps us diagnose issues when you need support):

```bash
pip install stealth-chrome-devtools-mcp[sentry]

# Add to your .env or export in your shell:
SENTRY_DSN=https://3206541bdab9246f00d7099e692e2ee2@sentry.devino.ca/34
```

To disable, simply unset `SENTRY_DSN` or remove it from your `.env`.

## Development setup

```bash
uv sync --extra dev --extra test   # install linters + test deps
npm install                        # arm husky pre-commit/pre-push hooks
```

The six quality gates run automatically on every commit:
ruff format, ruff check, ty check, vulture, suppression-owner check, file-budget check.
Unit tests run on pre-push.

## License

See [LICENSE](LICENSE).

---

Built by [Devino Solutions](https://devino.ca)
