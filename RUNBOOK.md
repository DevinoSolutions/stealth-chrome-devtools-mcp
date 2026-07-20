# RUNBOOK — operating the backend

For the maintainer at 3am. This is how you inspect, recover, and reclaim disk. Every
command below is real and runnable; the *why* is in [`DESIGN.md`](./DESIGN.md), the
term definitions are in the [`CLAUDE.md` glossary](./CLAUDE.md#glossary).

Two console scripts are installed:

- `stealth-chrome-devtools` — the **ops CLI** (this document).
- `stealth-chrome-devtools-mcp` — the **MCP server** entrypoint (what a client wires up).

If your checkout folder has spaces or an `&` in its path (as the dev checkout does),
`uv run` may not resolve — invoke the venv Python directly. All commands below also
work as `.venv\Scripts\python.exe -m stealth_chrome_devtools_mcp.cli <verb>` if the
console script is not on PATH. See [`CONTRIBUTING.md`](./CONTRIBUTING.md) for the
`uv run` caveat.

---

## The verbs

| Verb | What it does |
|---|---|
| `status` | backend state (`responsive`/`wedged`/`down`/not running), pid, log path, version, browser-session root, and the two disk caps |
| `doctor` | environment check: Python, platform, browser-session root, backend, port occupant, Chrome |
| `profiles` | list on-disk profiles with size, role, and in-use flag |
| `cleanup` | reclaim disk — delete idle auto-clones over the clone cap and trim idle named profiles over the browser-session cap (**dry run** unless `--apply`) |
| `stop` | terminate the shared backend (kills all live browser sessions) |
| `restart` | terminate + fresh cold-start spawn (the recovery for a **wedged** backend) |
| `kill-orphans` | reap browser processes orphaned by a dead backend (refuses against a live backend unless `--force`) |
| `serve` | start the MCP server yourself (stdio by default, or `--http`) |

`stop`, `restart`, and `kill-orphans` are thin front-ends over `singleton` /
`process_cleanup` primitives — they add **no** kill logic of their own; the matching
and teardown live in the backend and are reused from the eviction path.

---

## Reading `status`

```
backend     : running (responsive) on port 19222
pid         : 12345
log         : C:\Users\you\.stealth-mcp\logs\backend-12345.log
version     : 1.2.0
browser-session root: C:\stealth-mcp-browser-sessions  (exists: True)
clone cap   : 10.0 GB  [STEALTH_MCP_CLONE_STORAGE_CAP_GB]
browser-session cap : 20.0 GB  [STEALTH_MCP_BROWSER_SESSION_STORAGE_CAP_GB]
```

- **`backend`** is the real liveness state (`singleton._probe_backend_status`):
  `responsive` = answers a real MCP `initialize`; `wedged` = socket open but not
  answering (→ `restart`); `down` = recorded but nothing there; "not running" = no
  recorded backend. The port shown is the **chosen** port, which may differ from
  `19222` if that was taken (see "Port already in use" below).
- **`browser-session root`** and **`browser-session cap`** are about **disk** — the
  directory holding named browser-session profiles/clones and the cap that trims idle
  ones. They are named "browser-session" deliberately: this cap trims *named
  browser-profile directories on disk*; it does **not** affect your MCP/Claude-Code
  session or backend behavior. The cap knob is
  `STEALTH_MCP_BROWSER_SESSION_STORAGE_CAP_GB` (default 20 GB; `0` disables the trim).
  The separate `clone cap` (`STEALTH_MCP_CLONE_STORAGE_CAP_GB`) bounds throwaway
  auto-clones.

> Migration note: the browser-session cap env var was previously
> `STEALTH_MCP_SESSION_STORAGE_CAP_GB`. If you set that in your shell/MCP config,
> rename it to `STEALTH_MCP_BROWSER_SESSION_STORAGE_CAP_GB` — the old name is no longer
> read, and the cap silently reverts to the 20 GB default if it is left behind.

---

## Where the logs are

All under `logging_setup.resolve_log_dir()` — `~/.stealth-mcp/logs` unless
`STEALTH_MCP_LOG_DIR` overrides it (`status`/`doctor` print the exact path):

| File | What |
|---|---|
| `backend-<pid>.log` | the running backend's own rotating log (5 MB × 3) |
| `backend-boot.log` | the parent's raw redirect of the child's stdout/stderr — **look here first** if the backend never reached `status`, because a crash *before* `main()` (bad import, bad env) lands here and nowhere else |
| `proxy-<pid>.log` | one per stdio proxy (a client connection) |

Each backend log line carries a `[correlation_id]` tying one MCP request's lines
together.

---

## Recovery playbooks

### Backend is `wedged` (socket open, not answering)
`restart`. It terminates the hung process and cold-starts a fresh one under the same
lock a cold start uses. `restart` reports honestly: `responsive` (good), `wedged`
(came up but still not answering — run it again or let the next session evict it), or
`down`/`none` (did not come up — check `backend-boot.log`).

### Port already in use
The backend prefers `singleton.DEFAULT_PORT` (`19222`) but binds the **chosen** port:
if a **foreign** process holds `19222`, it falls back to an OS-assigned free port and
records it in `~/.stealth-mcp/server.json`. `status`/`doctor` show the actual port and
the port occupant. You do not need to free `19222` — discovery reads the recorded
port. `stop` clears `server.json`, so the next start returns to `19222` if it is free.

### Orphaned browsers after a crash
If the backend died and left Chrome processes behind, `kill-orphans` reaps them (and
clears the pid-tracking file). It **refuses** to run against a `responsive`/`wedged`
backend (that would kill the live backend's own browsers) — use `restart` for "backend
alive but bad", `kill-orphans` for "backend gone, browsers orphaned". `--force`
overrides the guard.

### Disk filling up
`cleanup` (dry run) shows what it would delete/trim; `cleanup --apply` reclaims. It
deletes idle auto-clones over the clone cap and trims regenerable data from idle named
profiles over the browser-session cap — **logins are kept**. Override caps for one run
with `--clone-cap-gb` / `--browser-session-cap-gb` (`0` disables a cap). `profiles`
lists what is on disk first.

### Code edit didn't take effect
There is no live reload. A source edit changes the **source fingerprint**, so the next
client connection evicts the stale backend and spawns a fresh one automatically. If you
want it now: `restart`. (`hot_reload`/`reload_status` were removed — a fresh backend is
the one code path.)

---

## Manual MCP smoke path

To confirm the server is actually answering the MCP protocol (not just holding a port
— the failure that used to hang silently):

1. Ensure a backend is up — either let a client connect, or start one yourself:
   `stealth-chrome-devtools serve --http` (or `.venv\Scripts\python.exe -m
   stealth_chrome_devtools_mcp --transport http`).
2. `stealth-chrome-devtools status` → **`backend : running (responsive) on port <port>`**.

Step 2 is the smoke: `status` performs a **real MCP `initialize` handshake** against
the backend over HTTP (`singleton._backend_http_ready`) and only prints `responsive`
when it gets a 200 back — one request, one response, no silent hang. If it prints
`wedged`, the process is up but not answering → `restart`. `doctor` runs the same probe
plus the environment checks.

---

## Crash / hang — first response

1. `stealth-chrome-devtools status` — is it `responsive`, `wedged`, `down`, or not
   running?
2. `wedged` → `restart`. Not running / `down` → read `backend-boot.log` (pre-`main()`
   crash) then `backend-<pid>.log`.
3. Still bad → `stop`, confirm no orphaned Chrome (`kill-orphans`), then let a client
   reconnect (auto-spawn) or `serve` one manually and re-check `status`.
4. `doctor` if you suspect the environment (no Chrome, wrong Python, port taken).
