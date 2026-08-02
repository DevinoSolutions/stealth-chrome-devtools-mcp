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
| `status` | backend state as one of three printed outcomes — `not running`, `running (responsive)`, or `running but UNRESPONSIVE … wedged` — plus pid, log path, version, browser-session root, and the two disk caps |
| `doctor` | environment check: Python, platform, browser-session root, backend, port occupant, **one line per recorded backend with its display context and whether it can show a window**, Chrome |
| `profiles` | list on-disk profiles with size, role, and in-use flag |
| `cleanup` | reclaim disk — delete idle auto-clones over the clone cap and trim idle named profiles over the browser-session cap (**dry run** unless `--apply`) |
| `stop` | stop the first recorded backend — its live browser sessions die with it; another desktop's backend keeps running and stays recorded |
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
version     : 2.0.4
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

## What is in `~/.stealth-mcp`

The state dir (`backend_registry.STATE_DIR`) is the only place the tool writes
outside your browser-session root and log dir. Nothing in it is precious — deleting
the whole directory while no backend runs costs you nothing but a cold start.

| Entry | What |
|---|---|
| `server.json` | the **backend registry**: one entry per display context, naming that backend's port, pid, version, and source fingerprint. This is what discovery reads to decide which backend to talk to |
| `server.port` | legacy, write-only — kept for a reader that no longer exists (see the `DESIGN.md` §10 ledger) |
| `singleton.lock` | the cold-start mutex; an empty file that persists between runs |
| `browser_pids.json` | the **browser-pid registry**: which browser processes are tracked, and which backend owns each one (`owner_pid`, `owner_create_time`) |
| `browser_pids.json.lock` | the sibling lock every writer of `browser_pids.json` takes so two backends cannot clobber each other's entries; empty and persistent, like `singleton.lock` |
| `logs/` | see "Where the logs are" above |

Anything else in the directory is a leftover. Nothing in `src/` writes or reads a
`.bak` file, so delete any you find.

---

## Reading `doctor`'s `contexts` block

`doctor` prints one line per **recorded** backend, ordered the way discovery
prefers them:

```
contexts    :
  backend  win-session-1  port 19222  pid 12345  version 2.0.4  responsive  (can show windows)
  backend  headless  port 19223  pid 12346  version 2.0.4  down  (headless only)
```

The liveness word is one of the same three words `_probe_backend_status` produces
(`responsive` / `wedged` / `down`), plus `no port recorded` for an entry naming no
usable port. The note in parentheses describes where that backend's windows *would*
appear, which stays true whether or not it is currently answering. With nothing
recorded the block reads `backend  (none recorded)`.

Two things it deliberately does not tell you. It does not distinguish a **proven**
desktop from an unclassifiable one: a context recorded by 2.0.3 or earlier reads
as `unverified`, which every client treats as capable, so it prints
`(can show windows)` too. And a listed backend is not necessarily one you can use
— identity (version and source fingerprint) still has to match before your session
will adopt it.

When no backend is **both** window-capable **and** responsive, `doctor` appends a
remedy line saying headed spawns will fail and to start one from a desktop session.
A recorded-but-dead desktop backend does not silence it, because in that state a
headed spawn is still refused.

**Do not diagnose display context from `validate_browser_environment_tool`.** Its
`platform_info.environment_vars.DISPLAY` is a `Settings` read of the `DISPLAY`
variable, and it is **not** the input that decided your display context. On Windows
and macOS it is `None` on a perfectly good desktop, because neither platform uses
`DISPLAY` at all — Windows context comes from the Win32 session id and macOS from
the console-owner check. Reading `DISPLAY: None` there as "this backend has no
display" is precisely the wrong turn F-808 invites. `doctor`'s `contexts` block is
the answer; that field is a Linux-only hint.

> **Windows console encoding.** CLI output contains em dashes, so redirecting it to
> a file or pipe under an OEM code page (`chcp 437`, `chcp 850`) raises
> `UnicodeEncodeError` — the default `cp1252` and any UTF-8 console are fine. Set
> `PYTHONUTF8=1` if you hit it. This is not new in 2.0.4; the `chrome :` line has
> always been exposed to it.

---

## Recovery playbooks

### Backend is `wedged` (socket open, not answering)
`restart`. It terminates the hung process and cold-starts a fresh one under the same
lock a cold start uses. `restart` reports honestly: `responsive` (good), `wedged`
(came up but still not answering — run it again or let the next session evict it), or
`down`/`none` (did not come up — check `backend-boot.log`).

Eviction by "the next session" is not instant: a cold-start lock-holder retries a
**same-identity** backend for up to 60 s before it may terminate it (see "Many
sessions starting at once" below), so a wedged-but-ours backend is replaced about a
minute into the next cold start, not on its first failed probe. `restart` is the way
to un-jam it now.

### Many sessions starting at once
Expected and safe — nothing to do. One session wins the exclusive cold-start lock and
spawns the backend; every other session proxies to that one. The winner holds the lock
until the backend answers a real MCP `initialize`, **not** merely until its socket
binds, and any lock-holder gives a **same-identity** backend (version *and* source
fingerprint both match) up to 60 s of retried probes before it is allowed to evict —
so a backend that is simply busy absorbing the herd is never terminated out from under
the sessions using it. A version- or source-stale record gets no such grace and evicts
immediately (an upgrade or code edit still takes effect now), and a dead record (no
socket, no live process) skips the wait, so crash-recovery cold starts stay fast.
`tests/test_startup_herd.py` is the gate: 40 concurrent sessions, one logical backend,
all usable inside 30 s.

### Port already in use
The backend prefers `singleton.DEFAULT_PORT` (`19222`) but binds the **chosen** port:
if a **foreign** process holds `19222`, it falls back to an OS-assigned free port and
records it in `~/.stealth-mcp/server.json`. `status`/`doctor` show the actual port and
the port occupant. You do not need to free `19222` — discovery reads the recorded
port. `stop` forgets the stopped backend's own display-context entry and clears
`server.json` only once nothing else is recorded, so the next start returns to
`19222` if it is free — and stopping one backend never makes another desktop's live
backend undiscoverable.

### Orphaned browsers after a crash
If the backend died and left Chrome processes behind, `kill-orphans` reaps them. It
reaps only browsers whose **owner backend is dead**: every entry in
`browser_pids.json` carries the identity of the backend that started it, and one
belonging to a living owner is skipped. Entries it did reap are dropped from the
record by id; every other backend's entries are left exactly as they were, and the
record itself stays on disk (empty if nothing is left). Browsers tracked by 2.0.3 or
earlier carry no owner stamp, so they are orphans by construction and get reclaimed
on upgrade.

It **refuses** to run against a `responsive`/`wedged` backend (that would kill the
live backend's own browsers) — use `restart` for "backend alive but bad",
`kill-orphans` for "backend gone, browsers orphaned".

`--force` is a bigger hammer than it looks: it passes through to the reaper, so it
bypasses **both** gates — the live-backend refusal *and* the per-entry ownership
check. Under `--force`, browsers a healthy backend is actively using are killed too.
That reach is deliberate, because it is what makes `--force` work against the wedged
backend it exists for, but it means `--force` is never the casual option.

### Disk filling up
Look before you reclaim — neither of these changes anything on disk:

<!-- doc-example: runnable -->
```console
stealth-chrome-devtools profiles
stealth-chrome-devtools cleanup
```

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

### Headed spawn fails: "cannot display a window"

`spawn_browser(headless=False)` raises a `ToolError` naming a display context
(`headless`, or a desktop token) when the backend serving your session runs
somewhere a window could never be seen — a Windows service session (Session 0), or
an SSH login with no `DISPLAY`/`WAYLAND_DISPLAY`. This is deliberate: before 2.0.4
the same spawn returned `state: "ready"`, `headless: false` and a browser that was
fully driveable over CDP and permanently invisible (F-808).

Run `doctor`. It lists one line per recorded backend with its display context and
whether that context can show a window. Two outcomes:

- **A window-capable backend is listed.** Your session should already be using it —
  discovery prefers a window-capable backend, and a client that cannot prove it has
  a desktop adopts any of them. If it is not, the entry is version- or
  source-stale; `restart` or let the next cold start evict it.
- **No backend can display a window.** `doctor` says so explicitly. Start one from
  a desktop session — open a Claude Code window on the physical desktop and let it
  cold-start a backend, or run `stealth-chrome-devtools serve --http` there. Every
  other session, SSH included, then converges on it and headed spawns become
  visible on the real desktop.

If you only need automation and not a visible window, pass `headless=True`; that
path is unaffected by display context and is what CI uses.

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
