# F-965 — `navigate(referrer=...)` blocks the page it opens and every later navigation in that tab

**Severity:** High. A chat that passed a referrer could not open the site it wanted, and then could
not open any site in that tab. Seen 2026-10-12 09:49Z (postify chat, `app.usepostify.com/signup`
blocked twice) and 2026-10-01 (uprank chat, even `example.com` blocked).
**Files:** `embedded/navigation_milestone.py` (`navigate(..., referrer=None)`),
`embedded/browser_manager.py` (`BrowserManager.navigate`: the `set_extra_http_headers` call
deleted, `referrer` passed through),
`embedded/tool_sections/browser_management.py` (the `navigate` docstring),
`tests/goldens/tool_surface.json` (regenerated, see §5), `NAVMAP.md`, `CHANGELOG.md`.
Tests: `tests/test_extra_headers_cdp.py` (site 2 rewritten, 2), `tests/test_navigate_milestone.py`
(+1), `tests/test_e2e_extra_headers.py` (+3, real Chrome).
**Branch:** `fix/f965-referrer-blocks-tab`, from `origin/main` (d66ef8f, release 2.1.30).

---

## 1. What happened

`navigate(url, referrer=R)` answered a Chrome error page (`net::ERR_BLOCKED_BY_CLIENT`) for the
target. Every later `navigate` in the same tab, with no referrer at all, was blocked the same way.

## 2. Root cause

`BrowserManager.navigate` applied the referrer with
`Network.setExtraHTTPHeaders({"Referer": R})` and then sent `Page.navigate(url)`. Three consequences,
all measured on Chrome 154:

1. Chrome's network stack cancels a request whose `Referer` header is a full URL that the referrer
   policy would not send to that target (default `strict-origin-when-cross-origin`: cross-origin
   gets the origin only, https to http gets nothing), with `ERR_BLOCKED_BY_CLIENT`. Header
   `https://verify.uprank.test/rel61-walk` to wikipedia: blocked. `https://usepostify.com/pricing`
   to wikipedia: blocked. The origin-only `https://verify.uprank.test/` to wikipedia: loads. A
   target controlled by a service worker escapes the check, which is why it looked specific to
   some domains.
2. Extra headers are per tab and sticky: after one `navigate(referrer=...)` every later `navigate`
   in the tab carries the same header and is blocked the same way.
3. `setExtraHTTPHeaders` REPLACES the set, so a referrer navigation silently dropped what
   `spawn_browser(extra_headers=...)` / `modify_headers` had installed.

The existing e2e `test_navigate_with_a_referrer_applies_it` passes a SAME-origin referrer, the one
case Chrome allows, so it never saw any of this.

## 3. Fix

The referrer is `Page.navigate`'s own parameter: `cdp.page.navigate(url, referrer=R,
referrer_policy=ReferrerPolicy.UNSAFE_URL)`, sent from `navigation_milestone.navigate`, the one
navigation home (the new `referrer` argument is threaded from `BrowserManager.navigate`). The
header call is gone, so there is no second way to apply a referrer. `unsafeUrl` is deliberate: the
caller named an exact referrer, so it is delivered as given, for this one navigation. Without a
referrer the frame carries neither field. Because it is a parameter of each `Page.navigate`, the
stale-tab recovery retry carries it too. `tab_binding.navigate_callers_tab` only passes `referrer`
through, so it needed no change; no other site applied a referrer.

## 4. Tests

* `test_navigate_referrer_rides_the_page_navigate_frame`: one `Page.navigate` carrying `referrer` and
  `referrerPolicy: unsafeUrl`, and no `Network.setExtraHTTPHeaders`.
* `test_navigate_without_a_referrer_sends_no_referrer_and_no_header_command`.
* `test_the_recovery_retry_still_carries_the_referrer`: both sends of the stale-tab retry.
* e2e: a cross-origin referrer loads and `document.referrer` equals it exactly; a plain navigate
  afterwards loads and carries no referrer; `extra_headers` installed at spawn still reach the
  server after a referrer navigation. The same-origin test stays.

## 5. Evidence

* RED (the unfixed `src/` restored over the fixed worktree with `git stash push -- src`,
  `__pycache__` cleared before and after): the unit frame test and the recovery-retry test fail
  (`KeyError: 'referrer'` on the frame params); the three new e2e nodes fail with
  `net::ERR_BLOCKED_BY_CLIENT`; the same-origin e2e and the no-referrer unit test pass on both
  sides. The stickiness and extra-header nodes fail already at their first (referrer) navigation,
  since that is where main blocks; their later assertions are what stay pinned after the fix.
* GREEN: the same three files, 48 passed (real Chrome 154 for the e2e file).
* `tests/goldens/tool_surface.json` regenerated deliberately (`dump_tool_surface.py --write`): only
  the `navigate` description changed (the `referrer` line now says it is sent as given, for this
  navigation only, and leaves the tab's extra headers alone). The tool count stays 97.

## 6. Rollout

A backend change only; no setting, no new tool. A tab that already carries a sticky `Referer` header
from an earlier backend is unaffected until its browser is closed.
