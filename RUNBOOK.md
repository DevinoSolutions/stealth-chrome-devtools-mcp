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
| `heartbeat-<port>.json` | F-889: the backend on that port stamping its OWN liveness every 3 s — `heartbeat_at` (a wall-clock `time.time()`) and `heartbeat_pid` — written from its event loop. That is how a proxy tells "the backend is dead" from "I was not scheduled". A separate file per port so it has exactly one writer and can never clobber `server.json`, which is written under the cold-start lock that a 3-second heartbeat must not take. Deleted with the entry, by both doors out of the record: `stop` and `cleanup --apply` forget it, and a cold start that supersedes an old entry of ours unlinks that port's file as it records the new one. What is NOT tidied is a backend still running that nothing has recorded — it keeps stamping and re-creates its own file about 3 s after a `cleanup --apply`; that is a backend to `stop`, not a file to delete. Safe to delete by hand: a missing or stale (>30 s, or a pid that is not the entry's) stamp is simply no evidence, never a fault, and the proxy falls back to exactly its 2.1.9 behaviour |
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
  The pid named is the first ancestor that is NOT a launcher of ours — the walk
  steps over the venv `python` trampoline, `uv`/`uvx` and the console-script
  redirector, all of which live exactly as long as the proxy does — so expect it
  to be the MCP client (`claude.exe`, `node`) or the shell that ran it, never
  `uv.exe`. No such line ever appearing is the designed behaviour when the walk
  cannot settle: unknown presence never ends a session.

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
If the backend died and left Chrome processes behind, `kill-orphans` reaps them.
**Since F-888 it does not reap all of them.** Two gates decide, per entry, and a
browser has to pass both:

1. **Its owner backend must be dead.** Every entry in `browser_pids.json` carries
   the identity of the backend that started it, and one belonging to a living owner
   is skipped. Browsers tracked by 2.0.3 or earlier carry no owner stamp, so they
   are orphans by construction and get reclaimed on upgrade.
2. **It must not be on a persistent profile.** A browser a caller named with
   `spawn_browser(session=…)` is left RUNNING and left TRACKED, because that
   is the login this tool exists not to lose — it is re-attached to instead, and
   the next section is how. Disposable auto-clones are reaped exactly as before.

Entries actually reaped are dropped from the record by id; every other backend's
entries — and every spared one — are left exactly as they were, because the record
is the only thing that still names a spared browser. The record itself stays on
disk (empty if nothing is left).

It **refuses** to run against a `responsive`/`wedged` backend (that would kill the
live backend's own browsers) — use `restart` for "backend alive but bad",
`kill-orphans` for "backend gone, browsers orphaned".

`--force` is a bigger hammer than it looks: it passes through to the reaper, so it
bypasses **all three** gates — the live-backend refusal, the per-entry ownership
check, *and* the persistent-profile spare. Under `--force`, browsers a healthy
backend is actively using are killed too, and so is a human's logged-in Chrome on a
named profile. That reach is deliberate, because it is what makes `--force` work
against the wedged backend it exists for, but it means `--force` is never the casual
option — and it is now the one verb that can still lose a login.

### Recover a stranded login

A browser on a **named session** — one a caller asked for with
`spawn_browser(session=…)` — is no longer killed when its backend goes away
(F-888). `stop`, `restart`, a proxy heal and a crash all now end with that Chrome
still running, and **two paths re-attach to it over CDP**:

1. **A new backend adopts what the record names, at its own startup.** Start a
   session and the login is already in `list_instances`, under its original
   `instance_id`. Give it a moment — the pass runs off the first-serve path so it
   cannot delay the backend answering.
2. **Spawning onto the profile re-attaches to whatever holds it.**
   `spawn_browser(session="<the same name>")` returns the RUNNING
   browser — same renderer, same open page, `spawn_diagnostics.reattached: true`
   — instead of walking to a sibling directory.

Path 2 is the one that matters when the record has lost the browser, which is the
normal outcome of a backend dying and being replaced: the successor rewrites
`browser_pids.json` without it, so there is no entry left for path 1 to walk.
Measured on the real incident — the stranded Seller Central Chrome had **no entry
at all** and **no `DevToolsActivePort` file**; its port was recovered from
`--remote-debugging-port=` on the process command line. **So the general recipe is
one call: spawn with the same `session`.**

From a shell that is one command, and since F-891 you do not need an MCP client
to make it:

```console
stealthy spawn --session seller-central --headed
```

It talks to the backend this shell would be served by (the one `status` reports),
starts one if none is running, and prints `REATTACHED : yes` with the holder's pid
when it got the browser that was already open rather than a new one. That command
is what replaced the hand-written 40-line stdio client this recipe needed on
2026-09-19 — `stealthy call spawn_browser --arg user_data_dir=…` is the same call
without the sugar.

> **What "starts one if none is running" can cost, and when to pass
> `--no-start`.** A backend that ANSWERS is always used as it is, whatever build
> it came from — so running `stealthy` out of a dev checkout beside a released
> backend drives that backend and never replaces it. But when nothing answers,
> the CLI takes the *proxy's* cold start, deliberately and unforked, and that
> path can evict: a **wedged** backend of a different build on this desktop
> owning no live browser is terminated and replaced. Anything still holding a
> browser is spared (F-886). If you are diagnosing a wedged backend and want it
> left exactly as it is, add `--no-start` — the command then exits 3 instead.

**Once you have it back, you can branch off it without closing it** (F-898).
A recovered session is a session this backend drives, so it is a legal `--from`
source while its window stays open:

```console
stealthy spawn --session seller-central --headed        # recover it
stealthy spawn --session seller-staging --from seller-central
```

The second call copies the profile for everything the copier can read and hands
the COOKIES over CDP out of the running browser, which is the half a file copy
of a live profile loses entirely. The first window is untouched. Cookies only —
see "Start a new session from an existing one" below for what does not come
across, and close the source first if you need `localStorage`.

Two cases still need a hand.

**The owner backend is still ALIVE (wedged, or just unreachable because every
proxy that could talk to it has died).** Adoption refuses a browser whose owner is
a live backend of ours, and it must — two backends driving one Chrome is what
F-886 exists to prevent. You will see the refusal rather than guess at it: the
spawn succeeds onto a different directory and its answer carries
`spawn_diagnostics.reattach_declined` naming the live owner, and the browser you
were reaching for is left running and untouched. Stop that backend first, then
start a session:

```console
stealth-chrome-devtools status          # read the port off the summary
stealth-chrome-devtools stop --port <that backend's port>
```

`stop` terminates the backend and leaves its persistent-profile browsers running;
the next backend adopts them. (On Windows this also works against a 2.1.9 backend
that is running today, because `stop` is `TerminateProcess`, which runs no
handler, so 2.1.9's kill-everything shutdown path never fires. On POSIX a 2.1.9
backend still kills them on the way out — install this release **before** the
stop.)

**The browser cannot be re-attached at all** (Chrome is wedged, or nothing in the
ladder names a port). On the spawn path this never kills it — the spawn just
proceeds normally and you get a *different* directory, reported in
`spawn_diagnostics.profile_selection.walked_to`, and
`spawn_diagnostics.reattach_declined` says which of the three refusals it was, so
"a browser is there and we could not get in" is never confused with "that
directory was free". If that happens, the old Chrome is still running and can be
closed by hand. On the *startup recovery* path a
recorded browser that cannot be reached is reaped exactly as 2.1.9 reaped it, but
the **profile directory is spared** either way, so the on-disk cookies survive for
a fresh spawn — you may just have to log in again if the session cookies are gone.

Things worth knowing:

- **A re-attached browser is the one that was already running, so it carries the
  state the dead backend gave it and not the arguments you just passed.**
  `headless`, `user_agent`, viewport, `proxy`, `browser_args`, `timezone_id` and
  `extra_headers` describe a LAUNCH and cannot be applied to a running browser;
  the ones you passed come back in `spawn_diagnostics.ignored_spawn_args` rather
  than failing the call. `block_resources` IS applied. `spawn_diagnostics.
  not_restored` names what the dead backend held that nobody can read back.
- **If that browser was spawned behind an authenticated `proxy=`, its egress is
  dead** — the forwarder lived inside the backend that died. You get it back with
  `spawn_diagnostics.dead_egress_proxy` set and a WARNING in the log; page loads
  will fail at a closed local port. Read what you need off it, then close it and
  spawn fresh with the same `proxy=` and `session`.
- `kill-orphans --force` takes persistent-profile browsers too; that is what
  `--force` means, and it is now the only verb that still can.
- A profile directory under the session root is **never** reclaimed by the
  storage cap or `cleanup --apply`, whether or not it carries a clone marker —
  only disposable auto-clones are, and a named profile is never one. A named
  directory can be *trimmed* of regenerable caches, never removed.

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

`profiles` also prints, under each session, the seed it was copied from and when —
plus `SEED CHANGED SINCE` when that seed has taken a login write since (F-895). A
session created before 2.1.11 has no such record and reads `seeded from unknown`;
that is the truth, not a fault, and nothing back-fills it. The `default` row and
the `default-seed` row carry no seed line because they ARE the seed.

### Start a new session from an existing one

```console
stealthy spawn --session work2 --from work
```

`--from` (the tool's `seed_from=`) copies a NEW session from an existing one
instead of from `default`, so it starts already logged in. It applies at
CREATION only — for a session that already exists it is an error naming where
that session actually came from, never a silent no-op and never a re-seed over
a login somebody typed by hand.

**The source may be open, if this backend is driving it** (F-898) — which it is
if `stealthy ls` lists it. A file copy of a live profile carries ZERO cookies
(measured: the SQLite jar is held open and skipped, and nothing can say
afterwards what was lost), so the COOKIES are handed over the two browsers' CDP
connections instead, after the new one launches. The spawn reports it:

```
seeded     : seeded from work at 2026-09-21 14:02
cookies    : 14 handed over from the running source
```

That hand-off carries **cookies only** — every kind, including the session
cookies a file copy can never carry, and every site the source is logged into.
It does NOT carry `localStorage`, `sessionStorage`, IndexedDB, Cache Storage,
service workers or saved passwords. If a site keeps its token in `localStorage`,
close the source first so the file copy can read it. A hand-off that fails does
not fail the spawn — the session works minus the cookies, and the line reads
`cookies : NOT carried (<type> from <CDP method>)`; the reason is shape only,
because a cookie name identifies on its own.

**A source open in a browser this backend does NOT drive is still refused by
name** — another backend's, or a Chrome someone started by hand:

```
seed_from='work' is open in a browser this backend does not drive, so its
cookies cannot be handed over: … Close the 'work' session first (`stealthy
close <instance>`), or spawn it through this backend (`stealthy spawn --session
work`) and seed from it while it runs, or seed from 'default', …
```

`stealthy ls` names the instance to close.

**`--from default` has three outcomes and is worth knowing separately**, because
`default` is the session a human logs in to and — since a named session's
browser survives its backend (F-888) — its window is normally still open. It is
also the default for an unset `--from`, so `stealthy spawn --session NAME` takes
the same three paths.

| `default`'s window | what happens |
|---|---|
| open, driven by this backend (`stealthy ls` lists it) | the seed is copied **and** the live jar is handed over — `cookies : N handed over from the running source`. This is the common case and it is what F-898 added |
| open in a Chrome we do not drive | the seed is copied and nothing is refused. The seed is only as fresh as the last close, which is what `SEED CHANGED SINCE` on the `seeded` line means |
| closed | the seed is copied, exactly as in 2.1.12 |

The copy always comes from the seed (`master-snapshot`) and never from the live
`default` directory — that is what makes it safe — and while `default` is open
the seed is not refreshed, which is why the hand-off matters: it puts the
current jar on top of a copy that may be days old.

The one refusal here is a machine with **no seed yet AND `default` open**:

```
the 'default' session has no copyable form yet and its browser is open, so
there is nothing safe to seed from: … Close the 'default' session once
(`stealthy close <instance>`); the seed every later session is copied from is
written when it closes, …
```

Nothing is created on disk when that happens. Closing the `default` window once
writes the seed and the refusal is gone for good, open or closed.

**One-time job if you have a session directory named `master` or
`master-snapshot`.** Those two names are reserved (F-894): `spawn_browser` used
to anchor a bare name under `sessions/`, so `user_data_dir="master"` silently
opened `sessions/master` — a copy of the seed — instead of the shared profile.
Asking for one now raises `profile request rejected: 'master' is a reserved
profile name …`. Your directory is **not** touched, is still listed by
`profiles`, and stays reachable two ways: open it by its **absolute path** with
`user_data_dir`, or rename it to a name that is not reserved and use that with
`session=`. To use the shared profile itself, pass `session="default"` — or no
session at all.

**And if you have one named `default`:** F-894 reserved that word too, as a
placeholder; since F-896 it MEANS the shared profile, so `session="default"`
opens `<root>/master` and never `sessions/default`. An existing
`sessions/default` directory is untouched and still listed, and is reachable by
its absolute path through `user_data_dir`; asking for it by NAME is refused,
because one word may not name two profiles.

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
