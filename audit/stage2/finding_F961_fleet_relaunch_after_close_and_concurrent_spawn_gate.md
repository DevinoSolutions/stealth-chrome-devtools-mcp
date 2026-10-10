# F-961 — the shared `fleet` browser must come back after it is closed, and two chats asking at once must get ONE browser

**Severity:** High (data-integrity class for the signed-in profile). Several chats share one `fleet`
browser for sign-in steps. An accidental `close_instance`, a closed window or a crash must cost the
others one `spawn_browser` call, never their login. Two chats asking for a just-closed `fleet` at the
same moment launched two browsers on one signed-in profile. Owner requirement 2026-10-10.
**Files:** `embedded/directory_gate.py` (new), `embedded/browser_reattach.py` (the per-directory lock
moved out), `embedded/tool_sections/browser_management.py` (hold the gate across a named spawn),
`embedded/tool_errors.py` (`instance_not_found`, `remember_departed`), `embedded/fleet_session.py`
(`relaunch_hint`, `annotate_diagnostics`), `embedded/browser_manager.py` (two one-line calls, one raise
site), `embedded/server.py` (three raise sites), `CHANGELOG.md`, `NAVMAP.md`.
Tests: `tests/test_fleet_relaunch.py` (new, 23, hermetic), `tests/test_e2e_fleet_relaunch.py` (new,
real Chrome).
**Branch:** `fix/f961-fleet-relaunch-after-close`, from `origin/main` (a3bd392, release 2.1.27).

---

## 1. The five paths: before, change, pin

| # | Path | Before (evidence) | Change | Pin |
|---|------|-------------------|--------|-----|
| 1 | `close_instance` by chat A, `spawn_browser(session="fleet")` by chat B | Already correct: `held_by` finds no live holder, the resolver lands on the existing explicit directory (no walk, no copy), `reuse_answer(False, …)` adds nothing. Pinned, not changed. | none | `TestAnotherChatClosedIt` (hermetic, real resolver/gate/adopt path); real-Chrome journey step 1 |
| 2 | Chrome dies / window closed without `close_instance` | Already correct: `list_instances` and `driven_profiles` discard a dead browser, `held_by` reads the process table, and `profile_lock` only counts a `SingletonLock` whose pid lives (Windows: process scan alone). Pinned, not changed. | none | `TestTheBrowserDiedOnItsOwn` (dead instance in the manager; stale `lockfile` / dead-pid `SingletonLock` / dangling `SingletonSocket` through the real `profile_hold` and resolver); journey step 2 (kill by PID) |
| 3 | A chat holding the OLD `instance_id` | Bare `Instance not found: <id>` from six raise sites. | `tool_errors.instance_not_found(id)` is the one builder; it appends `fleet_session.relaunch_hint` for an id remembered as a NAMED session's browser. `remember_departed` is called where an instance leaves `BrowserManager` (`close_instance` Phase 1, dead-browser discard). Still `InstanceNotFoundError` (one error convention). Clones, the shared profile and paths keep the bare message (a chat wanting a browser calls `spawn_browser()`, never asks for `default`). | `TestTheOldInstanceIdSaysHowToGetBack` (both departure routes, through the `navigate` tool; bounded memory; no-hint cases) |
| 4 | Concurrent `spawn_browser(session="fleet")` after a close | DEFECT. `adopt_held_profile` held a per-directory lock for the question "does a browser hold this?" only; the launch happened after it was released, so every concurrent spawn read "nothing holds it" and launched. | `directory_gate`: `spawn_browser` takes the directory's gate before its `try` and releases it in `finally`, so the second spawn waits for the launch and then reads the first's browser (`already_running: true`). The gate is reentrant per context, so the inner take by `adopt_held_profile` does not deadlock. A spawn naming no profile holds nothing. | `TestTwoChatsAskAtOnce`, `TestTheGate`; journey step 4 |
| 5 | Advisory session lock held at close time | Already correct: the lease is keyed on the session NAME in `session_lease`, independent of any browser. | none | `TestAnAdvisoryLockDoesNotBlockTheRelaunch`: relaunch succeeds; `get_session_lock_status` afterwards still shows `locked: true, holder: chat-a`; the next asker's `already_running` answer carries `session_lock.holder`. Note a fresh relaunch's own answer carries no `session_lock` block (as for any non-reused spawn); the status tool is the reader. |

## 2. Root cause of path 4

`browser_reattach._directory_lock` was taken only inside `adopt_held_profile`. Asked one at a time that
is enough; asked together, N spawns all returned an empty `Held()` before any launch finished, then
each ran the resolver and `BrowserManager.spawn_browser`. The F-931 `_spawns_in_flight` count does not
help a NAMED spawn (`reuse_ours=True` skips `_ours`).

## 3. Footprint decisions (caps never raised)

`browser_manager.py` is grandfathered at exactly 1454 and `browser_reattach.py` / `browser_management.py`
at 1000, so the new logic lives elsewhere:

* the per-directory lock moved from `browser_reattach` (-30 lines) to the new `directory_gate`;
  `holding_gate=` was avoided altogether by making the gate reentrant per context (a `ContextVar`);
* the departure memory lives in `tool_errors` (already imported by `browser_manager`, so no new
  import), and `instance_not_found` takes only the id (no manager parameter), which also removes a
  line at each raise site;
* `spawn_browser` needed 4 statements fewer (ruff PLR0915, 50 max, was at 50) so its
  profile-selection diagnostics block moved verbatim to `fleet_session.annotate_diagnostics`
  (-33 lines in `browser_management.py`). Final sizes: `browser_manager` 1454/1454,
  `browser_reattach` 971, `browser_management` 970.

Rebase note for the F-962 lane (per-caller tab binding): `tool_errors._require_tab` /
`_require_browser` changed only in their raise line (`instance_not_found(instance_id)`);
`browser_manager.py` has two added lines and one shortened raise; `browser_management.py` has the
`gate =` line, a `finally:` and the diagnostics call replacing the moved block.

## 4. RED evidence

New hermetic tests run against `git archive origin/main src` (PYTHONPATH pointed at the export,
`tool_errors.__file__` verified to be in it): 10 failed, 6 errors (the `directory_gate` fixture, module
absent), 7 passed. The failures are the real ones: `launched ['i-1', 'i-2']` (and 3 launches for three
chats) for path 4, and `'spawn_browser(session="fleet")' in 'Instance not found: i-old'` for path 3.
The 7 that pass on origin are the paths already correct (1, 2, 5) plus failed-launch and
different-session independence. GREEN with the fix: 23 passed.

## 5. Rollout

Ships with the next release; no migration. Backend needs a fresh process (`server.py` edits).
