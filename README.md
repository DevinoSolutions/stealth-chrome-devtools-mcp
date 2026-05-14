# Stealth Chrome DevTools MCP

Profile-aware MCP wrapper for `stealth-browser-mcp`.

It keeps the upstream browser automation server update-friendly while adding the shared browser-session behavior we want across Claude Code, Codex, and other MCP clients.

## Behavior

- Uses the exact master profile when it is free.
- Uses a deterministic per-project copy when the master profile is already in use.
- Refreshes session copies from master after 7 days by default.
- Respects explicit `user_data_dir` values passed by a tool caller.
- Proxies all upstream `stealth-browser-mcp` tools unchanged except for injecting `user_data_dir` into `spawn_browser`.

Default paths:

```text
Master profile:   C:\browser-sessions\master
Session copies:   C:\browser-sessions\sessions
Upstream server:  C:\Users\amind\stealth-browser-mcp
```

## Initialize The Master Profile

Run `spawn_browser` once with:

```text
user_data_dir=C:\browser-sessions\master
```

Log in to the services you need, then close the browser. Future sessions inherit those logins from the master profile.

## Install From Git

Typical MCP config after the repo is pushed:

```json
{
  "mcpServers": {
    "stealth-chrome-devtools": {
      "command": "uvx",
      "args": [
        "--from",
        "git+ssh://git@github.com/DevinoSolutions/stealth-chrome-devtools-mcp.git",
        "stealth-chrome-devtools-mcp"
      ],
      "env": {
        "STEALTH_BROWSER_MCP_ROOT": "C:\\Users\\amind\\stealth-browser-mcp",
        "BROWSER_IDLE_TIMEOUT": "0",
        "BROWSER_MASTER_USER_DATA_DIR": "C:\\browser-sessions\\master",
        "BROWSER_PROFILE_CLONE_ROOT": "C:\\browser-sessions\\sessions",
        "BROWSER_PROFILE_REFRESH_DAYS": "7"
      }
    }
  }
}
```

Local development config:

```json
{
  "mcpServers": {
    "stealth-chrome-devtools": {
      "command": "C:\\Users\\amind\\stealth-browser-mcp\\venv\\Scripts\\python.exe",
      "args": [
        "C:\\Users\\amind\\OneDrive\\Desktop\\Projects\\CUSTOM MCPs\\stealth-chrome-devtools-mcp\\src\\stealth_chrome_devtools_mcp\\server.py"
      ],
      "env": {
        "STEALTH_BROWSER_MCP_ROOT": "C:\\Users\\amind\\stealth-browser-mcp",
        "BROWSER_IDLE_TIMEOUT": "0"
      }
    }
  }
}
```

## Configuration

Environment variables:

```text
BROWSER_MASTER_USER_DATA_DIR      Master Chrome profile path
BROWSER_PROFILE_CLONE_ROOT        Directory for per-project profile copies
BROWSER_PROFILE_REFRESH_DAYS      Copy refresh age in days
BROWSER_IDLE_TIMEOUT              Passed through to upstream stealth-browser-mcp
STEALTH_BROWSER_MCP_ROOT          Upstream stealth-browser-mcp checkout
STEALTH_BROWSER_MCP_PYTHON        Optional explicit Python executable for upstream
STEALTH_BROWSER_MCP_SERVER        Optional explicit upstream server.py path
```

Equivalent CLI flags are available:

```powershell
stealth-chrome-devtools-mcp `
  --upstream-root C:\Users\amind\stealth-browser-mcp `
  --master-profile C:\browser-sessions\master `
  --sessions-dir C:\browser-sessions\sessions
```

## Why This Exists

Chrome profiles cannot be shared by multiple live Chrome processes. Claude Code previously solved this with a `PreToolUse` hook. This MCP server moves that policy into a normal MCP entry point so every client can use the same behavior without client-specific hooks.
