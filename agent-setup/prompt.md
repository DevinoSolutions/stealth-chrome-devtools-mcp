# Set up Stealth Chrome DevTools MCP for this AI development environment

These are official instructions from Stealth Chrome DevTools MCP to set up an AI
development environment for **undetectable browser automation through the Model
Context Protocol** — one shared backend, one Chrome per session, logins that
persist between sessions. All commands are validated by the project's own release
gate. Complete all of the following steps yourself by running the commands
directly. Do not ask the user to run any of these commands.

Steps:

1. Check the requirements (Python 3.11+, `uv`, a Chromium-family browser).
2. Install the pinned executable with `uv tool install`.
3. Register the MCP server in the agent you are running in (one section below
   applies to you; skip the others).
4. Verify the backend answers.
5. Print the completion message.

There are **no API keys and no accounts**. The server runs on the user's machine
and drives a local Chrome. The only thing the user does themselves is sign in to
websites inside the browser (step 6, human-only).

## 1. Requirements

- Python 3.11 or newer.
- [`uv`](https://docs.astral.sh/uv/) on `PATH`. If `uv --version` fails, install it
  first (macOS/Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`; Windows:
  `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`)
  and open a new shell.
- Google Chrome, Chromium or Microsoft Edge installed. The server finds it itself.
- For a **visible** browser window, a desktop session. Headless works from SSH, CI
  and services.

## 2. Install

Install the server as a version-pinned, standalone executable. Do not put `uvx`
in the MCP config: every agent session starts its own stdio proxy, and `uvx`
re-resolves the package on every start, which turns a fleet of sessions into a
package-resolution storm.

```bash
uv tool install stealth-chrome-devtools-mcp
```

That installs two executables:

- `stealth-chrome-devtools-mcp` — the MCP server (stdio). Path:
  `~/.local/bin/stealth-chrome-devtools-mcp` on macOS/Linux,
  `%USERPROFILE%\.local\bin\stealth-chrome-devtools-mcp.exe` on Windows.
- `stealthy` — the operator CLI (`stealthy status`, `stealthy doctor`,
  `stealthy spawn`, `stealthy ls`, …).

Resolve the server's absolute path now and use it in the config below —
`uv tool dir --bin` prints the directory. If the user already has it installed,
`uv tool install stealth-chrome-devtools-mcp` upgrades the pin to the latest
release; there is nothing else to migrate.

## 3. Register the MCP server in your agent

Use the section for the agent you are running in. Register at **user** scope so
every project gets the browser, and use the **absolute path** from step 2.

### Claude Code

macOS/Linux:

```bash
claude mcp add --scope user stealth-chrome-devtools-mcp -- ~/.local/bin/stealth-chrome-devtools-mcp
```

Windows:

```powershell
claude mcp add --scope user stealth-chrome-devtools-mcp -- "$env:USERPROFILE\.local\bin\stealth-chrome-devtools-mcp.exe"
```

Then tell the user to restart Claude Code (or run `/mcp` and reconnect) so the
new server is picked up.

### Codex

```bash
codex mcp add stealth-chrome-devtools-mcp -- ~/.local/bin/stealth-chrome-devtools-mcp
```

On Windows pass the `.exe` path from step 2 instead. This writes an
`[mcp_servers.stealth-chrome-devtools-mcp]` table to `~/.codex/config.toml`; if
`codex mcp add` is unavailable in the installed Codex version, add that table by
hand:

```toml
[mcp_servers.stealth-chrome-devtools-mcp]
command = "/Users/<user>/.local/bin/stealth-chrome-devtools-mcp"
args = []
```

### Cursor

Add the server to `~/.cursor/mcp.json` (global; create the file if it does not
exist — or `.cursor/mcp.json` inside the project for a project-only setup),
merging into any existing `mcpServers` object:

```json
{
  "mcpServers": {
    "stealth-chrome-devtools-mcp": {
      "command": "/Users/<user>/.local/bin/stealth-chrome-devtools-mcp",
      "args": []
    }
  }
}
```

On Windows the command is `C:\\Users\\<user>\\.local\\bin\\stealth-chrome-devtools-mcp.exe`
(escape the backslashes in JSON). Then tell the user to open Cursor Settings →
MCP and confirm the server shows a green status.

### Any other MCP client (Windsurf, OpenCode, Claude Desktop, VS Code, …)

Every one of them takes the same stdio entry — `command` is the absolute path
from step 2, `args` is empty:

```json
{
  "mcpServers": {
    "stealth-chrome-devtools-mcp": {
      "command": "/Users/<user>/.local/bin/stealth-chrome-devtools-mcp",
      "args": []
    }
  }
}
```

Put it in that client's MCP config file (Claude Desktop:
`claude_desktop_config.json`; Windsurf: `~/.codeium/windsurf/mcp_config.json`;
VS Code: `.vscode/mcp.json` under a `servers` key with `"type": "stdio"`).

## 4. Verify

Run the operator CLI. It talks to the backend the MCP server would use and starts
one if none is running:

```bash
stealthy status
stealthy doctor
```

Success looks like: `status` prints a `backend     :` line that is not
`not running` plus a `version     :` line, and `doctor` prints
`chrome      : <path>` naming the browser it found. If that line reads
`NOT FOUND — install Google Chrome`, tell the user to install Chrome (or
Chromium) and stop there — do not try to download a browser yourself.

Then, from inside the agent, call the MCP tool `list_instances` — an empty list
is the correct first answer — and `spawn_browser` with `headless: true`, then
`close_instance` on the id it returned. If `spawn_browser` answers with
`profile_selection` and an `instance_id`, the setup works end to end.

## 5. Completion message

Print this to the user, filled in:

```
Stealth Chrome DevTools MCP is installed and registered in <agent>.
- Server: <absolute path>  (version: the `version` line of `stealthy status`)
- Backend: <what `stealthy status` printed>
- Verified: spawn_browser (headless) → close_instance round trip OK

What you do next, once, by hand:
  Run `stealthy spawn --headed`, sign in to the websites you want your agent to
  use, then close that window. That browser profile is the shared `default`
  session; every session an agent opens afterwards is seeded from it — cookies,
  logins, Web Data — so the agent never has to log in itself. Never let an agent
  type your passwords or solve a CAPTCHA for you; that step is yours.

Then restart <agent> so it picks up the new MCP server.
```

## 6. Human-only: signing in

Do **not** sign in to any website on the user's behalf, do not enter credentials,
and do not attempt to solve a CAPTCHA. The user prepares the shared `default`
session themselves as the completion message describes.

## Resources

- README: https://github.com/DevinoSolutions/stealth-chrome-devtools-mcp#readme
- Sessions and profile handling: https://github.com/DevinoSolutions/stealth-chrome-devtools-mcp#sessions
- CLI reference: https://github.com/DevinoSolutions/stealth-chrome-devtools-mcp#cli
- Operating the backend (logs, recovery): https://github.com/DevinoSolutions/stealth-chrome-devtools-mcp/blob/main/RUNBOOK.md
- The 94 MCP tools: https://github.com/DevinoSolutions/stealth-chrome-devtools-mcp#mcp-tools
- PyPI: https://pypi.org/project/stealth-chrome-devtools-mcp/
- Issues: https://github.com/DevinoSolutions/stealth-chrome-devtools-mcp/issues
- Error reporting (on by default, how to turn it off): https://github.com/DevinoSolutions/stealth-chrome-devtools-mcp#error-reporting
