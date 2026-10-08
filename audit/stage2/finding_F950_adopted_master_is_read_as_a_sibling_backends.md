# F-950 — a browser this backend re-attached to is read as a SIBLING backend's, and is reported as `explicit`

**Severity:** High. Data-loss class for the shared (`default`) session: logins made in the
re-attached master are not carried by a session that names it, and closing the master does
not refresh the seed every later session is copied from.
**Files:** `src/stealth_chrome_devtools_mcp/embedded/browser_pid_registry.py`
(`held_by_sibling`), `src/stealth_chrome_devtools_mcp/embedded/browser_reattach.py`
(`held_by`, `_attach_one`), `src/stealth_chrome_devtools_mcp/embedded/profile_source.py` (`adopted_role`), `tests/test_adopted_master_handoff.py`,
`tests/test_e2e_adopted_master_handoff.py`.
**Seen:** the owner's machine, 2026-10-08.

---

## 1. Measured evidence

- The `default` session's Chrome is pid 38852, created 08:16:16Z. Its seed
  (`master-snapshot`) was written 08:16:11Z (`source_kind:
  default-seed-before-default-open`). `master/Default/Network/Cookies` was written after
  that, and the owner logged in to several sites in that browser today.
- Backend 84488 (v2.1.23) started 08:20:34Z. Its log at 04:21:46 local: `browser_reattach.reattach:
  Re-attached 1 browser(s)`. It adopted 38852 as instance `47b1e8d0` and rewrote
  `browser_pids.json` with that entry's `owner_pid: 84488`, ITSELF. The entry is still there,
  with `owner_pid: 84488`.
- Later spawns log `browser_reattach.held: Not adopting the holder of the requested
  profile: a live backend of ours already owns the browser holding that directory (pid
  38852); two backends driving one Chrome is the defect F-886 fixed ...`. That backend is
  84488. Two such lines in 41 spawns (09:59:47 and 11:40:09).
- The log has no `Removed stale browser instance` line, so instance `47b1e8d0` stayed in the
  manager's table for the whole run.

## 2. Cause

`held_by` (`browser_reattach.py`) raised `Refused` for any record entry whose owner is
"not reapable", which is `browser_pid_registry.is_reapable` returning False, i.e. a LIVE
owner. It never asked WHO the live owner was. A re-attach re-stamps the entry's owner to
the adopting backend (`claim_browser`), so after any restart the live owner of the shared
browser is THIS process, and the rule meant to stop two backends driving one Chrome
refused a backend its own browser, with advice to "stop that backend first".

`_attach_one` also hard-coded `profile_role: "explicit"` for every adopted browser. The
close path refreshes the seed only for role `default`
(`tool_sections/browser_management.py`, `close_instance`), so closing an adopted master
returned `seed_refreshed: null` and left the seed as stale as the last time that window was
closed.

## 3. What was NOT the cause (hypothesis disproved)

The brief's hypothesis was that an UNNAMED spawn skips the live-jar hand-off. Measured
hermetically, with a real `BrowserManager` holding a real adopted instance, a record whose
live owner is this process, and the real resolver, it does not: `adopt_held_profile(...,
reuse_ours=False)` catches the self-refusal with `_ours`, `driven_profiles` sees the adopted
instance over the lowercased record path (`profile_seed.same_dir`), and
`resolve_profile_selection` returns a clone carrying `seed_live_source` and
`handed_over_from: default`. That node passes on `origin/main` too
(`test_an_unnamed_spawn_is_handed_the_live_jar_not_told_to_stop`, kept as a pin).

What does fail is the NAMED spelling. `spawn_browser(session="default")` (or
`user_data_dir=<master>`) has `reuse_ours=True`, so the `_ours` rescue does not apply, the
self-refusal is reported as a decline, and the caller gets a walked clone instead of the
browser already open on that directory, which is what the F-888 `running` branch and the
F-931 docstring both say the answer is. The two logged lines are consistent with that
spelling; the log does not record the arguments, so this is inferred, not read off.

Not measured: whether the owner's clones actually lacked `seeded_via: "cdp-cookies"`. The
spawn answer is not logged and a successful hand-off logs nothing.

## 4. Fix

- `browser_pid_registry.held_by_sibling(entry, owner_alive)`: `is_reapable` stays THE
  ownership rule; this adds only "and the live owner is not this process" (pid compared,
  then `owner_alive` for identity so a recycled pid is not mistaken for us). `held_by`
  asks it in place of `not is_reapable`. A genuinely different live backend is still
  refused (pinned).
- `_attach_one` reports `profile_role` from `profile_source.adopted_role`: `default` when the adopted
  directory is `clone_storage.master_profile_dir()` (passed in, never re-decided), else
  `explicit`. Close then refreshes the seed, and the seed-source logic treats the adopted
  master as the default session.

## 5. Behaviour change to be aware of

A NAMED spawn onto a directory this backend already drives, in a record whose live owner is
this process, now returns the running instance (F-888's documented answer) rather than a
walked `<name>-N` clone with the jar handed over (F-915). An UNNAMED spawn is unchanged: it
still gets its own browser.

## 6. Evidence for the fix

RED on `origin/main` source (src reverted, tests kept; 6 failed / 7 passed):
`held_by` raised `Refused: a live backend of ours already owns the browser holding that
directory ... Stop that backend first` for an entry this process owns; `assert 'explicit' ==
'default'` for an adopted master; `assert [] == ['after-default-close']` for the close
refresh; a named spawn onto the adopted master returned `declined="a live backend of ours
..."`. GREEN with the change: 13 passed. Targeted neighbours (reattach, pid registry,
cookie hand-off, held-profile, profile seed, clone storage, seed refresh, and every file
that greps `held_by`, `driven_profiles` or `profile_role`): 770 passed, 36 integration
deselected.

The real-Chrome node `tests/test_e2e_adopted_master_handoff.py` (a session, a persistent and a
`__Host-` cookie in a master, re-attach, unnamed clone, asserted by the fixture server's
echo of the `Cookie` header) is collected but was not run here.

The helper lives in `profile_source` rather than `browser_reattach` because
`browser_reattach.py` sits exactly at its 1000-line budget and caps ratchet down only.
