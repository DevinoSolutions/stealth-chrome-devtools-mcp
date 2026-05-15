# Stealth Chrome DevTools MCP

Self-contained stealth Chrome DevTools MCP server with a master/copy browser profile strategy.

This repo vendors the runtime source from `stealth-browser-mcp` so installs do not depend on a separate local clone. Local tweaks live here, and the original upstream repo can still be pulled cleanly.

## Browser profile behavior

No environment variables are required.

By default, the server uses:

```text
C:\stealth-mcp-browser-sessions\master
C:\stealth-mcp-browser-sessions\master-snapshot
C:\stealth-mcp-browser-sessions\sessions
```

Behavior:

1. `spawn_browser` uses the exact master profile when it is not already in use.
2. Right before opening master, the server refreshes `master-snapshot` while master is still closed.
3. When a master browser is closed through the MCP, the server refreshes `master-snapshot` again in the background.
4. If the master profile is busy, the server creates a fresh deterministic copy from `master-snapshot` under `sessions`.
5. If launching a snapshot copy fails, the server retries with a fresh PID-suffixed copy.
6. If the deterministic copy is already busy, the server creates a fresh PID-suffixed copy such as `<session>-<hash>-<pid>`.
7. Copies are keyed by the MCP client roots when available, then workspace/cwd fallback.
8. Browser idle timeout defaults to `0`, so the MCP server does not close browsers because of idle cleanup.

## Install from GitHub

Use the package like a normal MCP server:

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

## Local development install

For local development from this checkout:

```json
{
  "mcpServers": {
    "stealth-chrome-devtools-mcp": {
      "command": "uv",
      "args": [
        "--directory",
        "C:\\Users\\amind\\OneDrive\\Desktop\\Projects\\CUSTOM MCPs\\stealth-chrome-devtools-mcp",
        "run",
        "stealth-chrome-devtools-mcp"
      ]
    }
  }
}
```

## Optional environment overrides

These are optional. Defaults are chosen for normal use.

| Variable | Default | Purpose |
| --- | --- | --- |
| `STEALTH_MCP_BROWSER_SESSION_ROOT` | `C:\stealth-mcp-browser-sessions` on Windows, `~/.stealth-mcp-browser-sessions` elsewhere | Base folder for master and clone profiles. |
| `BROWSER_MASTER_USER_DATA_DIR` | `<root>\master` | Exact master Chrome profile. |
| `BROWSER_MASTER_SNAPSHOT_DIR` | `<root>\master-snapshot` | Safe clone source refreshed while master is closed, before master opens and after MCP-managed master closes. |
| `BROWSER_PROFILE_CLONE_ROOT` | `<root>\sessions` | Folder for profile copies when master is busy. |
| `BROWSER_PROFILE_REFRESH_DAYS` | `7` | Refresh old copies from master after this many days. Use `0` to disable refresh. |
| `BROWSER_IDLE_TIMEOUT` | `0` | Browser idle cleanup timeout. `0` disables idle cleanup. |
| `BROWSER_STATE_TIMEOUT_SECONDS` | `10` | Timeout for full `get_instance_state` collection before returning compact partial state. |
| `STEALTH_CHROME_PROFILE_KEY` | unset | Force a stable clone key when client roots/cwd are not enough. |
| `STEALTH_BROWSER_DEBUG` | `false` | Enable debug logging. |

## Preparing the master profile

Start the MCP server and call `spawn_browser` without `user_data_dir`. The first browser uses the master profile at `C:\stealth-mcp-browser-sessions\master`. Sign in and configure the browser there, then close it. Future sessions use that exact master when available, or a copied profile when another client already has master open.
