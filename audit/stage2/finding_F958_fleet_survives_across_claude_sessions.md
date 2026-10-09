# F-958 — the fleet survives across Claude sessions, and its reuse answer stopped saying it was created

**Severity:** Low (a misleading message) plus a release test for a property the owner relies on.
**Files:** `embedded/fleet_session.py` (`named_session_warning`, `reused_record`),
`embedded/profile_target.py` (`EXISTING_KEY`, `existing_fields`), one call-site edit each in
`embedded/tool_sections/browser_management.py` and `embedded/clone_storage.py` (both files sit at the
1000-LOC cap, so the logic lives in the first two and the net change there is -1 / 0),
`tests/release_gate_harness.py` (`/api/login`, `LOGIN_COOKIE`). Tests: new `tests/test_e2e_fleet_across_sessions.py` (1, real
transport), `tests/test_fleet_session.py` (+4, hermetic).
**Branch:** `test/f958-fleet-survives-across-sessions`.

---

## 1. What was measured

A separate Claude Code session (own `claude -p`, own stdio proxy, own MCP session, same installed
version so the same backend) called `spawn_browser(session="fleet", headless=False)` while the
owner's `fleet` browser was open. It got `already_running: true`, the same `instance_id`, the same
`user_data_dir` (`sessions\fleet`), and Google still read signed in. The behaviour is right. Two
things were not:

* **(a)** the answer carried `profile_selection.warning` = "Named session created — it is NOT
  auto-cleaned ...", although nothing was created. A caller who reads that goes looking for a
  duplicate session, or concludes the login it sees is new.
* **(b)** nothing kept the property above under test across two real sessions.

## 2. Cause of (a)

The warning is written onto the spawn's diagnostics dict, and `BrowserManager` stores that same
dict (`_spawn_diagnostics[instance_id]`). The reuse branch returns the stored diagnostics, so the
CREATING spawn's sentence is replayed to every later caller. Separately, the creating branch said
"created" for any explicit directory, including one that was already on disk (a session reopened
after its browser closed).

## 3. Fix (one home for the wording)

`fleet_session.named_session_warning(selection)`:

* created (the directory did not exist before this spawn): unchanged text, so the standing advice is
  kept where it applies;
* otherwise: "Named session reused, not created — it already existed and is NOT auto-cleaned; it
  persists on disk until it is deleted by hand." The advice about not creating a session is moot
  there and is dropped.

"Did it exist" is decided where the directory is decided: `resolve_profile_selection` stamps the
internal `profile_target.EXISTING_KEY` (via `existing_fields`) when the explicit directory was on disk (a walk to `<name>-N` lands on a
fresh directory, so it is correctly "created"). Like `LIVE_SEED_KEY` it is dropped in
`_public_profile_selection` (through `profile_target.INTERNAL_KEYS`), so the public field set does not change. Absent means created, so
callers that fake the resolver keep their old answer.

For the reuse answer, `fleet_session.reused_record` rewrites the warning on a COPY of the stored diagnostics
(the record belongs to the running browser; the next caller reads it too). No doc or soft golden
quotes the sentence; `tests/goldens/tool_surface.json` describes `session` in prose that does not
contain it.

## 4. The release test

`tests/test_e2e_fleet_across_sessions.py`: one isolated backend (`gate_workspace`), two independent
`fastmcp.Client`/stdio proxies of it, real headless Chrome. Session 1 creates `fleet` and signs in
through the fixture's new `GET /api/login`, a server-set **HttpOnly** cookie (page script cannot
read it, so the fixture's echo of the `Cookie` header is the oracle). Session 2 then
`spawn_browser(session="fleet")` and must get `already_running: true`, the same `instance_id` and
`user_data_dir`, no `requested_user_data_dir` / `walked_to` / `walk_reason`, no `seed_warning`, no
`headless_mismatch`, no `fleet-2`, a `warning` that is not "Named session created", and the
logged-in header on the page; after session 2's proxy exits, session 1 still reads it. The backend
pid is asserted unchanged (one backend for both). Every wait is a harness-bounded `_call` inside an
`asyncio.wait_for` budget of 270 s (< the gate's `--timeout=300`). Only the workspace's own backend
and Chrome are touched and torn down.

Marker: `integration` + `transport`, like every node that drives the installed launcher. Selected by
the gate's own selectors (`--collect-only`): `-m integration` (Linux, Windows) 1, `-m transport`
(Linux, Windows) 1, macOS `integration and not transport` 0. **macOS is not covered**: F-773
(navigation under the detached backend hangs on the hosted macOS runner) excludes every transport
node there, and this one navigates. The in-process `test_e2e_fleet_session` keeps covering macOS for
the single-process decisions.

## 5. (b) the other direction: a different backend is refused whole

`browser_reattach.held_by` -> `Refused` then `profile_target.hand_over_or_refuse` already refuse a
held `fleet` this backend does not drive, and `test_fleet_session`/`test_held_profile_handoff` pin
that at the resolver (`test_a_holder_we_cannot_reach_is_refused_by_name`, creates nothing). Not
pinned at the TOOL: that the whole `spawn_browser` call raises a `ToolError` naming the session and
the holder's reason, launches nothing, signals nobody and leaves the directory and the sessions root
as they were. `TestASiblingBackendsFleetIsRefusedWhole` adds that. A second backend identity is not
available in a gate cell, so this one is hermetic by necessity.

## 6. Evidence

* RED on the unfixed product: the new real-transport node fails at the warning assertion, with every
  earlier assertion (same instance, same directory, no walk) passing; the two hermetic warning pins
  fail the same way.
* Mutation (runtime rebinding of the loaded modules, nothing edited on disk): `reused_record`
  identity -> reuse pin fails; `named_session_warning` always "created" -> existing-session pin
  fails; resolver forgetting `EXISTING_KEY` -> existing-session pin fails;
  `_profile_hold` blind, or `hand_over_or_refuse` pretending the holder is ours -> the sibling-refusal
  pin fails (DID NOT RAISE).
