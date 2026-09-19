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
| `doctor` | environment check: Python, platform, browser-session root, backend, port occupant, **one line per recorded backend with its display context and whether it can show a window**, which of those records are **dead**, Chrome |
| `profiles` | list on-disk profiles with size, role, and in-use flag |
| `cleanup` | reclaim disk — delete idle auto-clones over the clone cap, trim idle named profiles over the browser-session cap, and **forget dead backend records** (**dry run** unless `--apply`) |
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
  recorded backend this shell could adopt. The port shown is the **chosen** port, which
  may differ from `19222` if that was taken (see "Port already in use" below).
- **Which backend is it about?** The one *this shell* would be served by — the same
  adoption order discovery uses (F-868), not whichever entry `server.json` lists first.
  `pid` and `log` name that same backend, so the four lines can never describe different
  processes. `server.json` can hold one entry per display context **and identity** — so
  two backends may share a desktop (see "Two backends on one desktop" below) — and dead
  ones are never pruned, so an `others` line appears when there are others. Each is
  named `context:port`, because two entries on one desktop are otherwise
  indistinguishable:

  ```
  others      : 2 backends recorded (win-session-2:7169, headless:19222) — run `doctor` for each one's state
  ```

  `doctor`'s `contexts :` block probes every recorded backend on its own port; that is
  the place to look when you want all of them rather than yours.
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
| `backend-boot.log` | the raw redirect of the child's stdout/stderr, opened by whoever launched it — **look here first** if the backend never reached `status`, because a crash *before* `main()` (bad import, bad env) lands here and nowhere else |
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
| `server.json` | the **backend registry**: one entry per display context *and identity* (schema v3, a list — F-886), naming that backend's port, pid, version, and source fingerprint. This is what discovery reads to decide which backend to talk to. A v2 or pre-2.0.4 record still reads; a 2.1.8-or-older client reading a v3 one sees no backends at all. **F-889 did NOT change this file** — the heartbeat lives beside it, in its own per-port sidecar (next row) |
| `heartbeat-<port>.json` | F-889: the backend on that port stamping its OWN liveness every 3 s — `heartbeat_at` (a wall-clock `time.time()`) and `heartbeat_pid` — written from its event loop. That is how a proxy tells "the backend is dead" from "I was not scheduled". A separate file per port so it has exactly one writer and can never clobber `server.json`, which is written under the cold-start lock that a 3-second heartbeat must not take. Deleted with the entry (`stop`, `cleanup --apply`). Safe to delete by hand: a missing or stale (>30 s, or a pid that is not the entry's) stamp is simply no evidence, never a fault, and the proxy falls back to exactly its 2.1.9 behaviour |
| `server.port` | legacy, write-only — kept for a reader that no longer exists (see the `DESIGN.md` §10 ledger) |
| `singleton.lock` | the cold-start mutex; an empty file that persists between runs |
| `browser_pids.json` | the **browser-pid registry**: which browser processes are tracked, and which backend owns each one (`owner_pid`, `owner_create_time`) |
| `browser_pids.json.lock` | the sibling lock every writer of `browser_pids.json` takes so two backends cannot clobber each other's entries; empty and persistent, like `singleton.lock` |
| `backend-launch/` | scratch for a Windows cold start's scheduler rung (F-867) — see below. Empty between spawns |
| `logs/` | see "Where the logs are" above |

Anything else in the directory is a leftover. Nothing in `src/` writes or reads a
`.bak` file, so delete any you find.

### How the backend is launched, and what it leaves behind (F-867)

On Windows the cold start tries three ways to create the backend **outside** the MCP
client's Job Object, and the spawning proxy's log names the one that served: grep
`proxy-<pid>.log` for `backend spawned via the … rung`. `breakaway` and `scheduler`
mean the backend outlives the session that started it. A **WARNING naming F-867**
means a fallback — `breakaway-partial` and `plain` leave the backend inside the
client's job, so it dies when that one session ends. The usual cause is a spawner
outside the logged-on console session (SSH, a service, session 0, some RDP layouts);
the plain rung there is deliberate, because the alternative is moving the backend to
a desktop nobody asked for. POSIX logs rung `posix` and never needed any of this.

The scheduler rung is visible only while it runs:

- a one-shot task `stealth-mcp-backend-<token>`, where `<token>` is 12 hex characters,
  created and deleted inside a single cold start;
- `backend-launch/`, holding `<token>.py`, `.json`, `.pid` and `.err` during a launch
  and nothing between spawns. The `.json` carries the backend's **entire
  environment**, which is why the directory is user-private; `<token>.err` is where a
  launch that produced no backend log at all explains itself.

A leftover task and spec sharing a token mean the spawner was killed mid-launch. The
next scheduler spawn sweeps them — it deletes the task by name for every spec older
than twice the 20 s pid deadline, so a live sibling spawn is never caught — or clear
them yourself with `schtasks /Delete /F /TN stealth-mcp-backend-<token>` and delete
`backend-launch/<token>.*`.

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

### "Connection closed" on every session at once (F-889)
**This should no longer happen, and if it does the cause is not the backend.**
Since F-889 a proxy never ends its own session: when it cannot reach a backend it
backs off (2 s, doubling, capped at 60 s, jittered) and keeps retrying while the
client's stdio pipe is open. So a session that is stuck reports as tool calls that
wait, not as a disconnect, and it recovers by itself the moment a backend answers —
there is nothing to restart by hand.

What to look at while it is stuck, in the spawning proxy's `proxy-<pid>.log`:

* `no backend for port N; retrying in Xs (attempt K)` — the proxy is alive and
  waiting. A climbing `attempt` with a growing delay is the designed shape; check
  `backend-boot.log` for why the backend will not start.
* `port N: K strikes, but this process is not being scheduled; deferring the
  verdict (F-889)` — **the machine is starved, the backend is probably fine.**
  Check free RAM and CPU before touching anything; this line means the watchdog
  declined to believe its own timeout.
* `backend on port N reported its own loop turning Xs ago; the probe timeouts are
  ours, not its death (F-889) [K/10]` — the backend's own heartbeat vetoed a
  condemnation. Same conclusion: look at the machine, not at the product. The
  `[K/10]` is the veto BUDGET: ten deferred strike runs, then the next line fires.
* `backend on port N is still stamping (Xs ago) but has failed K fair-time strike
  runs; asking the confirmation gate anyway (F-889 review M1)` — **this one is
  about the product, not the machine.** The backend's event loop is turning but
  its HTTP listener is not answering us, and the heartbeat has run out of
  standing. Expect a heal shortly after. Grab `backend-boot.log` and the backend's
  own log before it is replaced.
* `the client process (pid N) that started this proxy is gone; nobody is
  listening, so this proxy ends` — normal. The proxy ends on stdin EOF; this is
  the backstop for a client that died without its pipe closing, checked once a
  minute. If you see many of these at once, something killed a wave of clients.

The Sentry event for a session without a backend is `proxy: backend unreachable,
retrying` (it replaced `proxy: teardown after failed heal`, which described an exit
that no longer exists). Its `reason` is `unhealable` (this recovery failed) or
`flapping` (three deaths back to back), and it carries the first `delay`. It fires
**once per outage, not once per retry**, and is closed by exactly one
`proxy: backend reachable again` (INFO) carrying `attempts` and `outage_seconds`.
An `unreachable` with no `reachable` after it is an outage that never ended. Every
individual retry is still in the proxy's own log file.

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
the sessions using it. A version- or source-stale record gets no such grace — it is
evicted at once **unless that backend still owns a live browser**, in which case it is
spared and the new backend comes up beside it on its own port (F-886). Either way the
upgrade or code edit takes effect now, because the arriving session always gets a
fresh backend. A dead record (no socket, no live process) skips the wait, so
crash-recovery cold starts stay fast.
`tests/test_startup_herd.py` is the gate: 50 concurrent sessions, one logical backend,
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

`cleanup` also reports the `backend records:` line — how many backends `server.json`
records and how many of those are **dead** (F-880: nothing is listening on the recorded
port AND the recorded pid is not a backend of ours). Nothing else in the product ever
forgets an entry, so a machine accumulates one per display context it has ever run a
backend in; `--apply` forgets the dead ones. A **wedged** backend is never dead — it
holds its port and `restart` is its verb — and a backend recorded moments ago but still
binding its socket is not dead either, which is why the pid is the second witness.
`doctor` names the same records and marks them `(dead record)`, but never writes: it is
a read-only verb.

### Code edit didn't take effect
There is no live reload. A source edit changes the **source fingerprint**, so the next
client connection gets a fresh backend automatically. If you want it now: `restart`.
(`hot_reload`/`reload_status` were removed — a fresh backend is the one code path.)

There are now two outcomes, and `status` tells them apart. If the stale backend was
idle it is evicted and the fresh one takes its port, as before. If it still owns a
live browser it is **spared**, and the fresh backend comes up on a different port
beside it — you will see an `others` line naming the one left behind. That is not a
failure: your edit is running. The old backend goes away when the session holding its
browsers closes them, or on an explicit `stop`. See "Two backends on one desktop".

### Two backends on one desktop
`status` shows an `others` entry with the **same display context** as the one being
reported. Expected since 2.1.9, and it means exactly one thing: two clients on this
desktop are running different source bytes, so neither will adopt the other's backend
and neither is allowed to kill it while it is serving browsers (F-886). Common causes
are a `uvx @latest` session beside a `uv tool` install, or an editable checkout beside
either.

Nothing needs doing — both sessions work, each on its own backend. To collapse them
back to one, make every client on the machine run the same install, then `stop` and
let the next session cold-start. `doctor` probes every entry and will tell you which
is which; `cleanup --apply` reclaims entries whose backend is genuinely dead.

**An UPGRADE now collapses them by itself** (F-889 (d)). A backend whose recorded
version is strictly newer than the arriving client's is adopted rather than evicted,
so once one session is upgraded the rest converge onto its backend as they reconnect,
instead of the two evicting each other on every proxy start. The asymmetry only points
forward: a DOWNGRADE does not take effect until the newer backend dies (`stop` it if
you mean the rollback to apply now). Same version + different source bytes — the case
above — is unchanged, because that is a code edit and not an upgrade.

**Caveat for a mixed fleet.** The protection lives in the *arriving* client. An
install older than 2.1.9 does not have it and will still terminate a backend that is
serving, so the guarantee only holds once every install on the machine is 2.1.9 or
newer. If browsers are still closing unexpectedly, check that nothing old is left:
`uv tool list` and any pinned `uvx` version in your MCP client config.

### Headed spawn fails: "cannot display a window"

`spawn_browser(headless=False)` raises a `ToolError` naming a display context
(`headless`, or a desktop token) when the backend serving your session runs
somewhere a window could never be seen — a Windows service session (Session 0), or
an SSH login with no `DISPLAY`/`WAYLAND_DISPLAY`. This is deliberate: before 2.0.4
the same spawn returned `state: "ready"`, `headless: false` and a browser that was
fully driveable over CDP and permanently invisible (F-808).

Since 2.0.5 this is the **fallback**, not the first answer: on Windows, if a user is
logged on at the console, the spawn is handed to Task Scheduler and the OS opens the
window on that user's desktop (F-810), so you should not see this error at all. When
you do, it means delegation was unavailable (not Windows, or nobody logged on) or it
failed — the message says which.

Run `doctor`. It lists one line per recorded backend with its display context and
whether that context can show a window. Two outcomes:

- **A window-capable backend is listed.** Your session should already be using it —
  discovery prefers a window-capable backend, and a client that cannot prove it has
  a desktop adopts any of them. If it is not, the entry is version- or
  source-stale; `restart`, or let the next cold start deal with it — which evicts it
  if it is idle and spawns beside it if it is still serving browsers (F-886).
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
