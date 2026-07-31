# F-802 — `navigate` reports `success: true` for a navigation that failed

**Status:** RESOLVED (2.0.1 stabilization, branch `fix/truthful-success-flags`).
**Severity:** MEDIUM-HIGH. A silent wrong success on the tool every session starts with.
**Surface:** `src/stealth_chrome_devtools_mcp/embedded/server.py::navigate`
→ `embedded/browser_manager.py::BrowserManager.navigate` — the payload is
assembled from a final URL that is Chrome's *error page*, and never inspected.
**Found by:** live probe against released 2.0.0 on a fresh instance while
stabilizing for 2.0.1 (sibling of F-795, same defect class).

## The behavior (2.0.0)

```jsonc
// navigate(instance_id=…, url="https://this-host-does-not-exist.invalid/")
{
  "url": "chrome-error://chromewebdata/",
  "title": "",
  "success": true
}
```

The navigation genuinely failed — DNS never resolved, Chrome committed its
network-error page — and the tool called it a success. Every Python-side step
around the failure *did* succeed, which is exactly why nothing noticed: the tab
exists, `tab.get()` returns, `window.location.href` and `document.title` read
back fine, the instance state updates. The only witness to the failure is the
scheme of the URL that was read back, and nothing looked at it.

## Why it is worth a finding

1. **It defeats the documented check.** `navigate` returns
   `{"url", "title", "success"}`; `success` is the field a caller is told to
   trust. `tests/e2e_helpers.py::navigate_and_settle` and the release-gate
   journey both assert `result["success"] is True` — against an error page that
   assertion passes.
2. **Every subsequent call is then made against the wrong document.** A caller
   that proceeds does DOM work, screenshots, extraction and script execution on
   `chrome-error://chromewebdata/`, and gets empty/absent results that look like
   product bugs rather than like a failed navigation.
3. **It contradicts `CLAUDE.md` convention 2.** A tool failure is supposed to be
   raised, not encoded in a payload whose success flag says otherwise.

`.invalid` (RFC 6761 §6.4) is a *convenient* reproduction, not the scope: a
refused connection, a failed TLS handshake and an unreachable host all commit a
`chrome-error://` page and took the same path.

## The fix

One guard, in the one guard home
(`embedded/tool_errors.py::_require_navigation_ok`), called once from the
`navigate` tool body:

```python
result = await _with_cdp_timeout(browser_manager.navigate(…), …)
return _require_navigation_ok(url, result)
```

It raises `ToolError` when — and only when — the final URL is under the
`chrome-error://` scheme, naming both the requested URL and the error-page URL:

> `Navigation to https://this-host-does-not-exist.invalid/ failed: Chrome loaded
> an error page (chrome-error://chromewebdata/). The host may not resolve, the
> connection may have been refused, or the TLS handshake may have failed.`

Three deliberate choices:

* **Only a Chrome-level failure is one.** A page answering 404/500 loaded; a
  redirect whose final URL differs from the requested one loaded; `about:blank`
  and `data:` URLs loaded. None is touched, and all four are asserted in
  `test_a_loaded_page_is_a_success_even_when_the_server_said_no` so the guard
  cannot quietly over-reach.
* **It raises AFTER the manager's bookkeeping completes**, not inside
  `BrowserManager.navigate`'s retry loop, so the tab, the instance state table
  and the navigation counter are all consistent and the instance is immediately
  reusable. (The node asserts exactly that: the same instance navigates to a
  real page and runs script after the failure — no wedge of the F-788/F-794
  shape.)
* **`go_back` / `go_forward` / `reload_page` are deliberately untouched.** They
  do not share this code path (they call `tab.back()`/`forward()`/`reload()`
  directly and return `bool`), and converting their contract is a separate,
  larger change than navigation truthfulness. Landing history-navigation
  truthfulness would be its own finding.

## Evidence

`tests/test_truthful_success_flags.py` (integration, real headless Chrome):

* `test_navigate_to_an_unresolvable_host_raises_instead_of_reporting_success` —
  the failure raises `ToolError`, the message carries both URLs, and the same
  instance is still drivable afterwards.
* `test_a_loaded_page_is_a_success_even_when_the_server_said_no` — 503, redirect,
  `data:` and `about:blank` all still report success.

Verified RED before the fix: with `src/` stashed, the first node fails and the
second passes (the guard adds truth without changing the truthful half).

## Routing

- No MQ step depends on this; found during 2.0.1 stabilization, fixed in place.
- No `--mq` id in `release-gate.yml` is bound to it.
- Sibling: **F-795** (`execute_script` reported success for a thrown script),
  fixed in the same branch and by the same lens — the second guard added to
  `tool_errors.py`.
- Related, still open: F-781 (raised errors carry no structured diagnostic),
  F-783 (the timeout path escapes the error convention), F-788 (a navigation
  *timeout* wedges the instance — a different failure mode, unchanged here).
