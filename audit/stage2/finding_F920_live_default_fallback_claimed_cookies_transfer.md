# F-920 — the `live-default-fallback` comment claimed cookies transfer from a profile Chrome holds open

**Status: FOLDED into F-914/F-915 and fixed there.** This stub exists so the
number is not claimed in prose alone. An F-number cited in code, tests and the
CHANGELOG with no file behind it is exactly the cross-lane collision shape that
filed F-922 twice in one week, and `audit/stage2/` is the only registry there
is. Census at the time of writing (`git branch -a`, `git log --all --oneline`):
no other branch or commit claims F-920, so the number is this lane's.

## 1. The claim

`clone_storage.resolve_profile_selection`'s `live-default-fallback` arm — the
shared-session branch reached when there is no seed yet — carried a comment
asserting that a copy taken while Chrome holds the profile open still carries
the cookies, "transfer successfully even while Chrome has it open".

## 2. Why it is false

`profile_copy.copy_file`'s own docstring says the opposite, and it is the
measured behaviour the whole of F-897/F-898 rests on: a file Chrome holds open
is retried briefly and then **SKIPPED**, so the SQLite jar does not come across
at all — twice over for the double pass — and nothing afterwards can enumerate
which file mattered. A comment promising the opposite is the kind that stops a
later reader asking, which is how the substitution survived as long as it did.

## 3. Where it was fixed

In F-914/F-915, because it is the same branch rather than a separate defect:

- The arm is now reachable with the shared session OPEN only after
  `profile_target.hand_over_or_refuse` has allowed it — i.e. only when THIS
  backend drives the holder — so the jar arrives over CDP
  (`cookie_handoff.hand_off`) rather than being hoped for from the file copy.
- With the shared session CLOSED it is a copy of a directory at rest, which is
  what 2.1.11's first run always was and where the original comment would have
  been true.
- The comment now says that, instead of the claim it made.

## 4. Where the citations live

`CHANGELOG.md` (the F-914/F-915 block), `audit/stage2/finding_F914_unnamed_spawn_substitutes_seed_clone.md` §5,
`src/stealth_chrome_devtools_mcp/embedded/clone_storage.py` (the
`live-default-fallback` comment) and `tests/test_held_profile_handoff.py`
(module docstring, and the node covering that branch).

## 5. Residual

None of its own. The rule it now depends on is F-914/F-915's, and that rule's
own residuals are named in those two findings.
