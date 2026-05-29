# Stealth Chrome DevTools MCP

[![Tests](https://github.com/DevinoSolutions/stealth-chrome-devtools-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/DevinoSolutions/stealth-chrome-devtools-mcp/actions/workflows/test.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-compatible-purple.svg)](https://modelcontextprotocol.io)

> Undetectable browser automation for AI agents via the Model Context Protocol.

A self-contained **stealth Chrome DevTools MCP server** with smart profile management, anti-detection stealth arg filtering, and robust process lifecycle handling. Built on [nodriver](https://github.com/AminDhouib/nodriver) (CDP-based) for full anti-bot evasion.

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

### Install from GitHub

```json
{
  "mcpServers": {
    "stealth-chrome-devtools-mcp": {
      "command": "uvx",
      "args": [
        "--refresh",
        "--from",
        "git+ssh://git@github.com/DevinoSolutions/stealth-chrome-devtools-mcp.git",
        "stealth-chrome-devtools-mcp"
      ]
    }
  }
}
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

**95 tests** covering stealth arg filtering, profile resolution, orphan recovery, and full browser integration.

## Environment Variables

All optional. Defaults work for normal use.

| Variable | Default | Purpose |
|----------|---------|---------|
| `STEALTH_MCP_BROWSER_SESSION_ROOT` | `C:\stealth-mcp-browser-sessions` (Win) / `~/.stealth-mcp-browser-sessions` (Unix) | Base folder for profiles |
| `BROWSER_MASTER_USER_DATA_DIR` | `<root>/master` | Master Chrome profile path |
| `BROWSER_MASTER_SNAPSHOT_DIR` | `<root>/master-snapshot` | Snapshot clone source |
| `BROWSER_PROFILE_CLONE_ROOT` | `<root>/sessions` | Folder for profile copies |
| `BROWSER_PROFILE_REFRESH_DAYS` | `7` | Refresh copies after N days (`0` = disable) |
| `BROWSER_IDLE_TIMEOUT` | `0` | Idle cleanup timeout (`0` = disabled) |
| `STEALTH_CHROME_PROFILE_KEY` | unset | Force a stable clone key |
| `STEALTH_BROWSER_DEBUG` | `false` | Enable debug logging |

## Preparing the Master Profile

1. Start the MCP server
2. Call `spawn_browser()` without `user_data_dir`
3. Sign in to your accounts in the browser that opens
4. Close it — future sessions use this profile or clone from it

## Requirements

- Python 3.11+
- Chrome, Chromium, or Microsoft Edge
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

## License

See [LICENSE](LICENSE).

---

Built by [Devino Solutions](https://devino.ca)
