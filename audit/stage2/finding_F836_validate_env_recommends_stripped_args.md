# F-836 (contradiction, LOW) — the environment validator recommended what the stealth filter strips

**Status:** FIXED on `fix/F821-F825-F836-small-fixes`.
**Source:** the F-835/F-836/F-837 block in
`finding_F834_concurrent_spawn_retry_profile_collision.md`.

## What was wrong

`validate_browser_environment_tool` returned

```json
"recommended_args": ["--no-sandbox", "--disable-setuid-sandbox",
                     "--disable-dev-shm-usage", "--disable-gpu", "--single-process"]
```

(as root, or in a container), while `filter_stealth_args` — the stealth filter
`browser_manager` runs over every caller-supplied `browser_args` — blocks
**every one of those five**. An operator who followed the advice saw
`Stripped 4 detectable arg(s)` in the debug log and no effect. One tool
recommended exactly what another discarded.

The advice was also redundant: `merge_browser_args` appends
`get_required_sandbox_args()` **after** `filter_stealth_args`, so the spawn path
already applies those flags itself. The caller never needed to pass them.

## Fix (`platform_utils.py` only — filter and validator already share this file)

`_caller_safe_sandbox_args()` splits the platform's required args into
`(recommend, automatic)` by running them **through the filter** rather than
restating a second list beside it:

* `recommended_args` now carries only what a caller may actually pass (empty in
  practice, since all five required flags are blocked);
* the blocked ones move into `recommendations` as a sentence that tells the
  operator the truth — `spawn_browser` adds them itself, and passing them by
  hand gets them stripped because they are detectable.

Deriving the recommendation from the filter is what stops the two drifting
again: adding a flag to `_stealth_blocked_args()` automatically removes it from
the recommendation. **The launch policy is untouched** — only the advice changed.

On a normal desktop (not root, not a container) `get_required_sandbox_args()` is
empty, so the tool's output is byte-identical to before.

## Tests

`tests/test_stealth_args.py::TestRecommendedArgsAgreeWithTheStealthFilter`, with
a fixture forcing the root/container precondition (without it the contradiction
is invisible and the pin would be vacuous — hence the guard test):

* no recommended arg is one `filter_stealth_args` strips (cross-check, not a
  literal list);
* the operator is still told which args the environment needs and that they are
  applied automatically;
* `merge_browser_args` still supplies every required arg — the fix did not
  weaken the sandbox handling.

RED evidence (pre-fix):
`AssertionError: recommends args the stealth filter strips: ['--no-sandbox
stripped: …', '--disable-setuid-sandbox stripped: …', '--disable-dev-shm-usage
stripped: …', '--disable-gpu stripped: …', '--single-process stripped: …']`
