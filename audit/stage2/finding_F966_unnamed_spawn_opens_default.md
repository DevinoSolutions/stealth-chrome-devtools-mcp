# F-966 — an unnamed `spawn_browser()` opens the shared `default` browser, and `close_instance` closes it for every chat

**Severity:** High (data availability). One chat closed the browser every other chat's copies are
seeded from, so every later copy lost its live seed (cookie hand-off, F-898, needs the source
open). Seen 2026-10-12: chat SafeMeet called `spawn_browser()` with no session twice on backend
2.1.27; both answers were `profile_role: "default"` on `<root>/master`; it then `close_instance`d
both.
**Files:** `embedded/clone_storage.py` (`resolve_profile_selection` loses `force_clone`;
`_clone_seed`'s last branch; `_copy_profile_tree`; `_refresh_master_snapshot_if_safe`),
`embedded/profile_source.py` (`empty_seed`, `NO_SEED_WARNING`), `embedded/profile_seed.py`
(`seed_label`, `NO_SEED`, `provenance`), `embedded/browser_reattach.py` (`_ours` and `reuse_ours`
deleted), `embedded/browser_health.py` (`shared_session_name`, `require_shared_session_named`, the
heal passes the session), `embedded/tool_sections/browser_management.py` (`spawn_browser`,
`close_instance(session=)`), `cli_call.py` (`stealthy close --session`),
`embedded/profile_target.py` and `embedded/fleet_session.py` (message text),
`tests/goldens/tool_surface.json`, `README.md`, `RUNBOOK.md`, `DESIGN.md` (new 2.9), `CLAUDE.md`
(glossary), `NAVMAP.md`, `CHANGELOG.md`.
Tests: `tests/test_unnamed_spawn_never_opens_default.py` (new, 25), `tests/test_e2e_unnamed_spawn_is_a_clone.py`
(new, real Chrome), `tests/test_browser_health.py` (+3), `tests/test_browser_exit_relaunch.py` (+1),
`tests/test_stealthy_cli.py` (+1), and the F-834 / F-914 / F-952 / F-931 pins listed in 5.
**Branch:** `fix/f966-unnamed-spawn-never-opens-default`, from `origin/main` (6301127, release 2.1.31).

---

## 1. What happened

`spawn_browser()` with nothing named answered the shared profile: `profile_role: "default"`,
`user_data_dir` `<root>/master`. The caller treated it as a browser of its own and closed it. The
shared browser is a persistent profile that humans sign in to and that new sessions are copied from;
closing it ended the live jar the next copy would have been handed, and the next unnamed spawn
reopened it cold.

## 2. Root cause

Two independent holes that compose into the incident.

1. **Resolution.** `resolve_profile_selection` landed an unnamed spawn on `master` when nobody held
   it (F-834: "an unnamed spawn lands on the shared session"), and F-931 added an adoption path so
   that `spawn_browser()` re-attached a `default` browser a dead backend had left running. The
   shared session is therefore reachable without naming it, by any chat that just wants a browser.
2. **Close.** `close_instance(instance_id)` closed whatever it was given. Nothing distinguished a
   disposable copy from the browser that other chats depend on.

## 3. Fix

**Spawn (owner ruling, no setting).** An unnamed spawn is always a disposable auto-clone.

* Master free: copied from the seed (`master-snapshot`), or from master at rest
  (`live-default-fallback`) when there is no snapshot. `STEALTH_MCP_SEED_SESSION` (F-952) keeps
  its rule.
* Master held by a browser this backend drives: the existing hand-off, unchanged.
* Master held by one it does not drive: the existing refusal by name (F-914), unchanged. Its text
  now names `stealthy close <instance> --session NAME`.
* **Brand-new install** (no seed, no master): a fresh empty clone, marked `seeded_from: "none"`,
  with a `seed_warning` that names `spawn_browser(session="default")` as the way to sign in once
  and `close_instance(..., session="default")` as the way to end that. Master is neither created
  nor opened, no empty seed is published, and `provenance` does not call an empty clone "changed".
  The alternative (open master for the first spawn) is exactly the incident on a clean machine, and
  the old `RuntimeError` left a new user with no browser at all; an empty clone with a warning gives
  them a working browser and the one call they need.
* Only `session="default"` or master's own path opens master. `resolve_profile_selection` loses
  `force_clone`: with no named directory there is nothing to force.
* F-931's unnamed re-attach is removed (`adopt_held_profile` is asked only about a directory the
  caller named; `_ours` and `reuse_ours` are deleted). A stranded `default` browser is the
  resolver's to copy from or refuse, jar and all, like a running one.
* Heal and relaunch of the `default` instance still open master explicitly: they relaunch with the
  recorded `BrowserOptions`, never through the resolver (pinned, see 4).

**Close.** `close_instance(instance_id, session=None)`. If the instance is the `default` or `fleet`
session's browser and `session` is not exactly that name, a `ToolError` names the session, says other
chats depend on it, gives the exact call, and leaves the browser alone. Any other instance closes as
before and `session` is ignored (rejecting it would break a caller that always sends one).

* **One classifier.** `browser_health.shared_session_name(user_data_dir, auto_clone=)` is the F-964
  relaunch classifier made to answer "which", and `close_instance` reads it from the profile
  selection the spawn recorded. That record survives a re-attach (F-888) and, unlike
  `BrowserManager.get_instance`, does not discard a dead browser as a side effect (which would
  start F-964's relaunch). It lives in `browser_health` rather than the tool module because
  `tool_sections/browser_management.py` is under its 1000-LOC cap and `browser_manager.py` is
  grandfathered at exactly its cap.
* The in-process heal (`BrowserHealth.heal`) passes the session name, so a heal still closes and
  relaunches the browser it owns.
* `stealthy close --session NAME` passes it through.

Tool count stays 97; `tool_surface.json` is regenerated deliberately (the `close_instance`
description and `session` schema, and the `spawn_browser` description).

## 4. Tests

Hermetic, in `tests/test_unnamed_spawn_never_opens_default.py`: the resolver (clone while master is
free; no snapshot copies master at rest; `session="default"` still opens master; concurrent
selections get distinct clones; first install is an empty clone with the warning, twice; `default` on
a first install opens master), the spawn tool end to end through `call_tool` (the owner's exact
call, a stranded holder, three concurrent unnamed spawns), and the close guard (refused unnamed,
refused for the other shared name, allowed named, ordinary instances unchanged, `session` ignored
for them, unknown id still the manager's answer, a clone sitting on the fleet directory is not the
fleet). `test_browser_health.py::TestTheHealNamesTheSharedSession` and
`test_browser_exit_relaunch.py::test_the_default_relaunch_names_master_and_never_asks_the_resolver`
pin that heal and relaunch name the session and never ask the resolver. `test_stealthy_cli.py` pins
the `--session` passthrough. Real Chrome: `tests/test_e2e_unnamed_spawn_is_a_clone.py` (isolated
session root, every spawn closed in `finally`).

## 5. Goldens and pins changed

* `tests/goldens/tool_surface.json` (SOFT): `close_instance` gains `session`; the `spawn_browser`
  description no longer says an unnamed spawn lands on `default`.
* F-834 characterization pins (SOFT) in `test_profile_resolution.py`, `test_concurrent_spawn_collision.py`,
  `test_held_profile_handoff.py`, `test_adopted_master_handoff.py`, `test_live_master_seed.py`:
  they asserted "unnamed lands on master" or the `RuntimeError` for no seed; they now assert the
  clone and the empty-clone-plus-warning outcome. The F-914 refusal and F-898 hand-off pins are
  unchanged in meaning. Renamed where the old name asserted the old behaviour:
  `test_no_user_data_dir_uses_master` -> `test_the_default_session_uses_master`,
  `test_no_master_no_snapshot_raises` -> `test_no_master_no_snapshot_starts_an_empty_clone`,
  `test_concurrent_unnamed_selections_all_still_pick_master` -> `..._default_selections_...`
  (the master race is now only reachable by naming `default`), and F-931's
  `test_both_spellings_*` / `test_a_stranded_holder_is_still_re_attached` became
  "the named spelling asks the re-attach", "an unnamed spawn never asks the re-attach" and
  `test_a_stranded_holder_is_no_longer_re_attached`.
* `test_fleet_session.py` (F-952): the nonexistent `STEALTH_MCP_SEED_SESSION` node assumed the unnamed
  spawn opened master so the setting was never read; it is a clone now, the setting is read, and the
  guarantee (the spawn still succeeds, with a `seed_warning`) is what the node now asserts.
* `test_session_vocabulary.py` (F-896): `test_default_is_the_same_answer_as_naming_no_session` pinned
  the opposite of the ruling and is now `test_default_is_not_the_answer_to_naming_no_session`.
* `test_wire_semantics.py::test_a_second_unnamed_spawn_is_bounded_when_roots_list_is_never_answered`
  (F-790, transport lane, found by CI): its control spawn named nothing and relied on landing on the
  shared profile, which asks no `roots/list`. A spawn naming nothing is a clone now and asks, so the
  control is `session="default"` (same premise: one instance already holding the shared profile, no
  round trip); the second, auto-clone spawn is unchanged and `_close` takes the session for it.
* `test_e2e_seed_from_running_source.py::test_b_...` (found by CI on a root with no master): opening
  `default` on a root with no master used to publish an EMPTY seed, which the node leaned on. It
  publishes none now (see 3), so the node starts from a master that exists, the state its docstring
  already describes.
* E2E and integration tests that closed the `default` or `fleet` browser without `session`
  (`test_e2e_fleet_*`, `test_e2e_two_sessions_own_tabs`, `test_e2e_browser_health`,
  `test_e2e_seed_from_running_source`, `test_e2e_adopted_master_handoff`,
  `test_e2e_google_rotation_guard`, `test_seed_refresh_after_close`, `test_browser_integration`)
  now pass `session`. Their `allow_fail=True` closes would otherwise have leaked a browser silently.

## 5a. Evidence

Method: an OUT-OF-TREE `git archive` export of each tag/ref under
`%TEMP%/f966-export/<ref>/`, with this branch's `tests/` and `pyproject.toml` copied in, run from
that directory with `PYTHONPATH=<export>/src` and the branch's venv; `__pycache__` cleared before
each run and the loaded `stealth_chrome_devtools_mcp.__file__` (and `e2e_helpers`' `server.py`)
printed after each run and confirmed to resolve into the export. No `git stash`, no shared worktree
mutated. (A first attempt that rebound only the package left `e2e_helpers` loading `server.py`
from the branch; it was discarded and redone as above.)

| Selection | v2.1.27 | origin/main (6301127) | this branch |
|---|---|---|---|
| `test_unnamed_spawn_never_opens_default.py` + `test_close_passes_the_session_it_is_told_to` | 19 failed, 7 passed | 19 failed, 7 passed | 26 passed |
| + `TestTheHealNamesTheSharedSession` + `test_the_default_relaunch_names_master_and_never_asks_the_resolver` | modules do not exist (`browser_health` landed in 2.1.30) | 21 failed, 9 passed (30 selected) | 30 passed |
| `test_e2e_unnamed_spawn_is_a_clone.py` (real Chrome, tmp root) | FAILED: `profile_role == "default"`, wanted `"clone"` | FAILED: same | passed |

The 7 / 9 that pass on the old side are the controls that must hold on both sides:
`session="default"` (name and path) still opens master, an ordinary instance (clone, named session)
closes as before, an unknown id is still the manager's answer, a clone sitting on the fleet
directory is not the fleet, and the relaunch names master and never asks the resolver. The
`TestTheHealNamesTheSharedSession` nodes for `default` and `fleet` are RED on main (the closer is
called with no session), the one for an ordinary session is a control.
After the fix the whole unit lane (`-m "not integration"`) was 4317 passed, 1 skipped, 1 failed;
the one failure (`test_seed_refresh_atomicity::test_a_missing_source_still_creates_an_empty_target`,
a SOFT golden: the empty first-install clone now carries its marker) was updated and passes.

## 6. Rollout

A backend change; no setting. A chat that called `close_instance` on a `default` or `fleet` browser
without `session` now gets the refusal naming the exact call. A caller that relied on an unnamed
spawn opening `default` (to sign in, for instance) must say `session="default"`.
