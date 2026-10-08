# CLAUDE.md — navigation map for agents

You are placing a change in `stealth-chrome-devtools-mcp`. This file (with [`NAVMAP.md`](./NAVMAP.md)) is your map:
**where** things live, **what** each term means, and the **four conventions** a change
here must follow. It is name-only on purpose — you should be able to route a change to
the right file from this map *without reading function bodies*. The *why* behind the
architecture is in [`DESIGN.md`](./DESIGN.md); how to operate the backend is in
[`RUNBOOK.md`](./RUNBOOK.md); how to build/test/ship is in
[`CONTRIBUTING.md`](./CONTRIBUTING.md).

> This is a **local, single-user tool, 0 external users**. Priorities:
> maintainability, operability, performance.

---

## The four conventions (non-negotiable)

1. **One import form.** Always `from stealth_chrome_devtools_mcp.embedded.X import Y`
   (absolute-from-package; relative imports are banned). **No module under `embedded/`
   imports `server`** — it causes double tool-registration under runpy; pass
   `browser_manager` as an argument instead. See [DESIGN §8](./DESIGN.md#8-the-one-import-convention).
2. **One error convention.** Tools **raise** `tool_errors.ToolError` /
   `InstanceNotFoundError` on failure; success helpers return values. Do not add a
   `{"success": False}` dict — except to join a named KEEP contract
   ([DESIGN §9](./DESIGN.md#9-the-one-error-convention)).
3. **One cloner engine.** All DOM extraction lives in
   `embedded/cdp_element_cloner.py` (`CDPElementCloner`). The file/progressive cloners
   are thin adapters; never add extraction anywhere else, and never resurrect the
   deleted engines ([DESIGN §5](./DESIGN.md#5-the-cloner-subsystem-one-engine-deliberate-per-aspect-transport)).
4. **Golden discipline + "a second way is a defect."** Two-tier goldens: HARD
   invariants never bend; SOFT goldens update *deliberately*, in the same PR that
   changes a schema, with justification (see `CONTRIBUTING.md`). And the binding lens:
   **a change that introduces a second way to do something already done is a defect** —
   prefer extending the one home over adding a parallel path.

---

## Navigation map

The map of the tree (every module, what it owns, where a change goes) is in
[`NAVMAP.md`](./NAVMAP.md). It is ~290K chars, so it is kept out of this auto-loaded
file: `grep -n "<module or term>" NAVMAP.md` and read only the matching entries.
A change that adds, moves or renames a module updates its entry there.

---

## Glossary

One meaning per term. Where a term is irreducibly overloaded, each sense gets a
distinct qualified name; the bare word is retired from ambiguous surfaces.

| Term | THE one meaning | Not to be confused with |
|---|---|---|
| **backend** | the shared `python -m … --transport http` process running FastMCP + 97 tools, one per display context | the stdio proxy; "the server" (ambiguous — avoid); the pre-2.0.4 "exactly one process per machine" reading ([DESIGN §2.7](./DESIGN.md#27-display-context-where-a-window-launched-here-would-be-seen)) |
| **stdio proxy** | the short-lived per-Claude-Code-session process bridging stdio ↔ the backend's HTTP | the backend |
| **MCP session** | FastMCP's `mcp-session-id` handshake token (created by `initialize`, discarded by the liveness probe) | a browser session; a Claude Code session |
| **Claude Code session** | one client connection = one stdio proxy instance | an MCP session; a browser session |
| **browser session / named session** | a `spawn_browser(session=…)` profile-backed browser instance. **The parameter is `session` since F-896** and it takes a NAME, never a path; `user_data_dir` is the DEPRECATED alias that resolves to the same request through `profile_seed.profile_request` and is the only one that still accepts a path. This row said `session_name=` until F-894 and taught agents a keyword that raised | any of the above; a "session root" |
| **`default` session** | `profile_seed.DEFAULT_SESSION` — the shared profile an unnamed spawn lands on, the one a human logs in to, and the one every new session is copied from. `session="default"` opens it; it is `<root>/master` on disk and `profile_role: "default"` in the answer (F-896) | its **seed** (below), the copyable form; a session OF YOUR OWN called `default`, which is refused because one word may not name two profiles |
| **`fleet` session** | `fleet_session.FLEET_SESSION` — the shared, signed-in browser several agents work from (F-952): an ordinary NAMED session, so persistent, re-attached by F-888 (restored FIRST; a failed attach to it leaves the browser running instead of reaping it) and spared by every reap but `kill-orphans --force`. Not a reserved role — that would be a second way to say "named". Asking for it while it runs in THIS backend returns that instance with `already_running: true`, never a `fleet-2` and never a clone. `STEALTH_MCP_SEED_SESSION=fleet` makes it the default source for new clones/sessions | the **`default` session** (the shared PROFILE a human logs in to — `Roots.shared`; "shared" means that and only that, which is why this one is named `fleet`); a clone, which is disposable |
| **session lock** | `session_lease`: an ADVISORY lease on a session NAME (`acquire_session_lock` / `release_session_lock` / `get_session_lock_status`), with an owner label, a lease of 1-3600 s that expires on its own, and a refusal that names the holder and expiry. Advisory because a tool call carries no caller identity; in memory, cleared by a backend restart | an enforced mutex; the `get_debug_lock_status` tool (debug logger); `profile_lock` (is a directory held by a process); the `singleton.lock` file |
| **seed** | the profile a new session is COPIED from at creation — `<root>/master-snapshot` by default, reported as `seeded_from: "default"` because to a caller it IS the `default` session in copyable form (F-895, renamed F-896), or since F-897 any session the caller named with `seed_from` / `--from`, reported by that session's own name. Recorded per session as `seeded_from` / `seeded_at` in its clone marker | the `default` session itself (the seed's own source, refreshed into it); a *reserved name* (below), which is what a caller may not call a session |
| **`seed_from` / `--from`** | `spawn_browser(session=NEW, seed_from=SOURCE)`: create NEW as a copy of the SOURCE session instead of of `default` (F-897). A NAME, through the same gate `session` passes — including F-896's empty rules, shared rather than copied: `""` is NOT GIVEN and `"   "` raises with the one `_names_nothing` sentence. CREATION-only, so an existing target RAISES. Unset means `default`, which is today's behaviour on the same code path. **A RUNNING source is allowed since F-898 when THIS backend drives it** — see *cookie hand-off* below; a NAMED one held by a browser we do not drive is still REFUSED BY NAME, because a copy of a live profile silently drops whatever Chrome has locked and nothing can say afterwards what was lost. **`default` — and therefore an unset `seed_from` — has its OWN three outcomes** (F-898 review M1): the copy always comes from the SEED, the live jar is handed over when we drive the shared browser, a `default` held by a Chrome we do not drive is copied and never refused, and the one refusal is no-seed-yet + `default`-open | a *re-seed* or *promote*, which do not exist — a login done in one session still never reaches another unless the caller asks (study §3.D) |
| **cookie hand-off** | `cookie_handoff.hand_off`: after a new session is created from the file copy, the SOURCE browser's cookie jar is read with `Storage.getCookies` and written into the TARGET with `Storage.setCookies`, over the CDP connections this backend already holds to both (F-898). It is what makes `seed_from` work while the source is still open, and the answer says which happened — `seeded_via: "cdp-cookies"` with counts, or `"copy"` with a `cookie_handoff_error`. It carries **the WHOLE jar and cookies ONLY**: every cookie in the source browser for every site (there is no origin filter — `carry_origins=` is named as a future opt-in and deliberately not built), and no localStorage, sessionStorage, IndexedDB, service worker or Cache Storage, all measured | a *re-seed* (the source is untouched and keeps running); the FILE copy, which still runs and is what carries everything on disk — the hand-off adds the one thing a copy of a live profile provably cannot carry |
| **reserved profile name** | `profile_seed.RESERVED_NAMES` — `master` and `master-snapshot`: the MECHANISM's own directory names, refused outright because anchoring them under the clone root hands back a DIFFERENT profile under the name the caller used (F-894). `default` was a third until F-896 gave it a meaning; it is still reserved in the sense that it names exactly one directory, so any spelling that would CREATE a second one under that word (`sessions/default`) is refused | the `default` session, which is openable; the shared profile by absolute PATH, which is allowed and selects the `default` role |
| **browser-session root** | the on-disk `STEALTH_MCP_BROWSER_SESSION_ROOT` dir holding profiles/clones | a browser session (this is the *storage* for them) |
| **instance / instance_id** | one live browser managed by `BrowserManager`, keyed by `instance_id` | a browser session (an instance is the *runtime*; a session is the *named profile*) |
| **profile** | a Chrome user-data-dir (the `default` session's, or a per-session copy) | a session (which *selects* a profile) |
| **profile clone** | a copy-on-spawn profile derived from the seed | the **element clone** (DOM extraction) — always qualify |
| **persistent profile** | `browser_pid_registry.on_persistent_profile`: a profile the caller NAMED (`spawn_browser(session=…)`), so the directory outlives its browser AND, since F-888, the browser outlives its backend — never reaped at shutdown or startup recovery, re-attached instead | a **profile clone** (disposable, dies with its browser, still reaped); a *protected backend* (F-886's question, about a BACKEND's port); `kill-orphans --force`, which takes it anyway |
| **re-attach / adoption (of a browser)** | `browser_reattach.run`: a new backend connecting over CDP to a persistent-profile browser a dead backend left running, under its RECORDED instance id | **adoption (of a backend)** below — which recorded BACKEND a client reuses. Two different questions; always qualify |
| **in-memory storage** | the deliberately non-durable `InMemoryStorage` cross-check (M15 rename of `persistent_storage`) | durable disk state (there is none for instances) |
| **clone storage** | `clone_storage.py`: the on-disk profile/clone quota + GC subsystem | in-memory storage; the cloner *engine* |
| **cloner engine** | `CDPElementCloner`: the one canonical DOM-extraction engine (post-M5b) | clone storage (disk); a profile clone |
| **display context** | `display_context()`'s token for the desktop a window launched HERE would appear on ([DESIGN §2.7](./DESIGN.md#27-display-context-where-a-window-launched-here-would-be-seen)) | the `headless=` spawn argument (a caller's request); the `DISPLAY` env var (one Linux-only input) |
| **adoption** | which recorded backend a client reuses — `adoption_candidates`, asymmetric by design ([DESIGN §2.7](./DESIGN.md#27-display-context-where-a-window-launched-here-would-be-seen)) | reuse *identity* (`singleton._same_identity_backend_ready`) — adoption picks WHICH record to test, identity decides whether it passes |
| **backend registry** | `backend_registry.py` + `server.json`: **which backend to talk to**, one entry per (display context, identity) since F-886 | the **browser-pid registry** (below); `server.port` (`PORT_FILE`), write-only legacy; `singleton.lock` |
| **protected backend** | `backend_eviction.protected`: one of ours, running, of an identity we would NOT adopt, still owning a live browser — so never terminated and never bound over (F-886) | *adoptable* (identity matches, so we'd reuse it); *alive* (`backend_liveness`'s question); an idle stale backend, which IS evicted |
| **browser-pid registry** | `browser_pid_registry.py` + `browser_pids.json`: **which browsers are tracked and by whom** | the **backend registry** (above); in-memory storage; clone storage |

---

## Tool count = 97 (derived, never typed)

The authoritative count is the live registry:
`sum(len(v) for v in SECTION_TOOLS.values())` == **97** across 12 sections.
`SECTION_TOOLS` is filled by **`server.py`'s binding loop over `SECTION_MODULES`** — the
loop walks each section module's `TOOLS` tuple, applies `section_tool(SECTION)` and binds
the result into `server`'s namespace — so the count derives from what the twelve modules
export, not from anything typed. The CLI's `--list-sections` and description string derive
their numbers from `SECTION_TOOLS`, and a test asserts the printed total equals the
registry count — so no hand-maintained number can drift. If you add a function to a
section module's `TOOLS` tuple (or remove one), the count updates itself; update the `97`
in the docs to match (the count-assertion test will remind you).
