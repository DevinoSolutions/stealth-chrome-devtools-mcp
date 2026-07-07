# Pre-mortem — stealth-chrome-devtools-mcp @ 2267b83

**Premise (dated 2027-01):** In the same quarter this codebase caused a serious, long-lived outage *and* a badly missed deadline. This document explains how the code as it exists at SHA `2267b83` made both outcomes inevitable. Every causal claim is anchored to a schema finding with a verbatim quote from the pinned code (`findings_premortem.json`, F-501…F-509).

---

## TL;DR

- **The outage** was a total, self-perpetuating hang of every session at once, caused by the shared-singleton architecture plus a liveness check that cannot tell "listening" from "able to serve." The design routes *all* sessions through **one** detached, log-less backend process [F-502, F-503]; when that backend wedged, the proxy's liveness monitor kept seeing a healthy TCP socket and never tore down [F-501]; with no logs and no supervisor, recovery was manual, slow, and blind [F-503].
- **The missed deadline** was death by phantom debugging. The version-gated singleton silently reuses an old backend after in-place edits, so fixes appear not to work [F-504]; the only "reload" tool can't reload the file that matters [F-506]; the logic lives in one 4207-line god module [F-505] with five duplicated cloners [F-508]; and the test/release gate never exercises the real browser surface [F-507], so nothing tells you whether a change is right until a human checks by hand.

---

## Part 1 — The outage: how one stuck request took everyone down and stayed down

### 1.1 The blast radius was designed in
`singleton.py` opens with its own thesis: "only ONE HTTP server process is spawned. All sessions connect to it as lightweight stdio proxies" [F-502]. State is a single per-user directory (`~/.stealth-mcp`) with one `server.json` and one port. Every Claude Code window, every project, every concurrent automation shares the *same* backend process. There is no bulkhead: whatever happens to that one process happens to everyone.

### 1.2 The trigger: a wedged backend that still accepts connections
The backend is uvicorn + FastMCP on a single event loop. Any operation that blocks that loop or stalls the MCP session manager — a hung CDP call, a slow synchronous scan, a FastMCP session-manager stall — freezes the backend's ability to answer MCP requests while the kernel keeps the listen socket open. The codebase already *knows* this failure class: the startup readiness probe `_await_backend_http` documents that "a freshly bound uvicorn socket can answer (4xx) while FastMCP's MCP session manager is still starting," which is why startup uses a real `initialize`→HTTP-200 check [F-501].

### 1.3 Why nothing caught it
That strong probe is used **only once, at startup**. Ongoing liveness — the thing that is supposed to notice the backend has gone bad mid-session — uses `_server_is_healthy`, a bare `socket.create_connection` [F-501]. `_watch_backend_liveness` resets its failure counter on every successful *connect* and only tears down after three consecutive *connection* failures. A wedged-but-listening backend passes this probe indefinitely, so `monitor_backend` never fires, the proxy never cancels, and the client is never told to reconnect. The one condition the monitor exists to detect is exactly the one it is blind to. Meanwhile every session's `from_backend`/`to_backend` pump is parked forever forwarding to a backend that will never answer.

### 1.4 Why recovery was slow
The backend is spawned fully detached, with `stdout`, `stderr`, and `stdin` all routed to `DEVNULL`, and (on Windows) `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` [F-503]. So at the moment of the incident there were: no logs to say what wedged, no parent process watching it, no supervisor to restart it, and no user-visible indication that a hidden shared backend even exists. The runbook that quarter was, in effect, "somebody eventually realizes there's an orphan on port 19222, finds the PID, and kills it" — during which *all* sessions stayed down. A single hard-coded port with no fallback is the same class of invisible, unrecoverable failure from a different trigger: if anything foreign holds 19222, the fresh backend can't bind, dies silently into DEVNULL, and every tool call waits out the 120s `BACKEND_READY_TIMEOUT` before failing with nothing in any log [F-509].

### 1.5 The accelerant: upgrades that kill live sessions
Even without a wedge, the shared backend made routine actions dangerous. When a new session starts on a different version, `_start_backend_holding_lock` evicts the incumbent by calling `proc.terminate()`/`proc.kill()` on it so its own backend can bind [F-502]. Because that incumbent is the *shared* process other live sessions are mid-automation on, a single `uvx`/`pip` upgrade (or two projects pinned to different versions) tears the backend out from under everyone else — their browser subprocesses orphaned, their in-flight work lost. The outage didn't need bad luck; a normal upgrade could start it.

**Outage mechanism in one line:** one shared, detached, log-less backend [F-502, F-503] + a liveness check that can't see a wedged event loop [F-501] = every session hangs at once, nothing auto-recovers, and a human must blindly kill an orphan to restore service.

---

## Part 2 — The missed deadline: how "small" changes became multi-day slogs

### 2.1 The change that should have been quick
The scheduled work was ordinary for this project: change the behaviour of some MCP tools. Almost all tool logic lives in `embedded/server.py` — a **4207-line** god module with **96** `@section_tool` bodies behind a custom section/disable indirection over FastMCP [F-505]. Step one, "find and edit the code," is already expensive here, but the real damage was in step two: verifying the edit.

### 2.2 The structure that made verification lie to you
After editing `server.py` and reconnecting the client, the developer kept seeing the *old* behaviour. The reason is the version-gated singleton: reuse identity is the installed package **version string** (`_server_version()` → `importlib.metadata.version`), and `_find_running_server` reuses any running backend whose recorded version matches [F-504]. An in-place source edit does not change the version string, so `ensure_server_running` re-attaches to the already-running backend and proxies to the *old* code. The edit is invisible. The project's own saved memory — "server.py edits need a fresh backend process, not just a reconnect" — is a scar from exactly this.

The obvious escape hatch is a trap. The built-in `hot_reload` tool claims to "Hot reload all modules without restarting the server," but its `modules_to_reload` list omits `server.py` entirely [F-506], so the 96 tool bodies are never reloaded — and for the modules it does touch it swaps in fresh managers via reassigned globals, orphaning any live browser. And because the backend is a detached, log-less orphan [F-503], nothing in the workflow tells the developer that a stale process is the culprit. The result is hours per change lost to debugging code that isn't even running.

### 2.3 Why the estimates were wrong
The estimate assumed the normal loop: *edit → reconnect → observe → iterate.* Every link in that loop is broken here. "Observe" shows stale behaviour [F-504]; the reload tool that would fix it doesn't cover the god file [F-506]; and there is no automated signal to fall back on, because the release/coverage gate runs only `pytest -m "not integration"` [F-507] — the entire real-browser surface (spawn, CDP, navigation, all five cloners) is excluded from the 39% floor and never run before publish. So correctness could only be established by slow, manual, real-browser checking. When the change touched cloning, it had to be replicated across **five** parallel cloner implementations with mirrored method families [F-508], each verified by hand. A task scoped as a day became a week of phantom debugging and whack-a-mole, and the quarter's roadmap slipped with it.

**Deadline mechanism in one line:** the version-gated singleton hides your edits [F-504], the only reload tool can't reach the god file [F-506], the god file [F-505] and its five duplicated cloners [F-508] multiply the work, and no test gate exercises the real surface [F-507] — so verification is manual, slow, and misleading, and every estimate built on a normal edit-test loop was wrong.

---

## Part 3 — The common root

Both failures grow from the same three roots, which is why they landed in the same quarter:

1. **A shared mutable singleton with no isolation and no honest health signal** [F-501, F-502, F-503] — one process is both the single point of failure in production and the stale-state trap in development.
2. **Correctness that can only be checked by a human** — the version gate lies [F-504], the reload tool lies [F-506], and CI never runs the real surface [F-507], so neither an operator nor a developer gets an automated truth signal.
3. **Concentration and duplication** — 4207 lines in one module [F-505] and five copies of the cloner [F-508] make every change both hard to locate and wide in blast radius.

Fixing the liveness probe [F-501] and the release gate [F-507] are the highest-leverage, lowest-effort moves (they directly break the outage-detection and no-signal failure modes); de-singletoning / logging the backend [F-502, F-503] and gating reuse on real code identity [F-504] remove the shared root that feeds both stories.

---

### Finding index

| ID | Sev | Category | What |
|----|-----|----------|------|
| F-501 | Critical | operability | Liveness is a bare TCP connect; a wedged-but-listening backend never triggers teardown |
| F-502 | High | architecture | One shared per-user backend = SPOF; version-mismatch eviction kills other live sessions |
| F-503 | High | operability | Backend detached with stdout/stderr/stdin=DEVNULL, no supervisor, no logs → blind manual recovery |
| F-504 | High | operability | Reuse identity is the package version string, so in-place edits are silently ignored after reconnect |
| F-505 | High | maintainability | 4207-line god module, 96 tools behind custom section_tool indirection |
| F-506 | Medium | code_health | hot_reload omits server.py and orphans live managers — a misleading escape hatch |
| F-507 | High | testing | Publish/coverage gate runs only `-m "not integration"`; real-browser surface never gates a release |
| F-508 | Medium | architecture | Five duplicated element-cloner implementations with mirrored method families |
| F-509 | Medium | operability | Fixed backend port 19222 with no fallback; foreign holder → silent unrecoverable bind failure |
