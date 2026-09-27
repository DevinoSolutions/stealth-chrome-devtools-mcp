# F-933 — a failed `navigate` listed three guesses while Chrome had said which

**Severity:** Low for users, Medium for this repo's CI. The refusal is truthful,
but it names no cause, so a recurring Windows gate failure could not be
diagnosed from its own message.
**Files:** `embedded/navigation_milestone.py`, `embedded/tool_errors.py`,
`embedded/browser_manager.py` (one line).
**Depends on:** F-802 (the `chrome-error://` detector), F-833 (its one entry
point), F-881/F-882 (the milestone wait that receives `Page.navigate`'s answer).

---

## 1. Symptom

`integration (Windows/X64)` went red on 3 of the last 6 runs, always in
`tests/test_e2e_fleet.py::test_a_fleet_of_six_browsers_answers_truthfully_about_every_page`.
In each run, a `navigate` to the fixture server landed on Chrome's error page:
`/cov/form.html` in run 36268702476 attempt 1 (PR #169's gate) and
`/cov/slow_load.html?ms=1200` in run 36280896091 attempt 1 (the v2.1.15
publish). Byte-identical trees passed on rerun. The message was:

```
Navigation to http://127.0.0.1:<port>/cov/form.html failed: Chrome loaded an
error page (chrome-error://chromewebdata/). The host may not resolve, the
connection may have been refused, or the TLS handshake may have failed.
```

A loopback URL rules out DNS and TLS. That leaves "refused" as a guess, and
nothing in the message confirms it or rules out something else.

## 2. Measurement

At 13bfc45, against real headless Chrome on this host (Windows 11
10.0.26200), I navigated to a loopback port that is bound and never listening:

```
Navigation to http://127.0.0.1:49883/ failed: Chrome loaded an error page
(chrome-error://chromewebdata/). The host may not resolve, the connection may
have been refused, or the TLS handshake may have failed.
```

After the fix, the same navigation ends
`Chrome's reason: net::ERR_CONNECTION_REFUSED.` (the new node in
`tests/test_truthful_success_flags.py`).

## 3. Root cause

`navigation_milestone.navigate` unpacks `Page.navigate`'s answer as
`frame_id, loader_id, error_text`. It used `error_text` for one thing only, the
comparison with `net::ERR_ABORTED`. Any other `errorText` means Chrome commits
its error page under our loader and fires `load`, so the wait ended normally and
the text was dropped. By the time F-802's detector (`_require_navigation_ok`)
read the landing, all it had was the URL `chrome-error://chromewebdata/`, which
is the same for every network failure.

## 4. Fix

- `Progress.error_text` keeps Chrome's reason. It is set for every `errorText`
  except `net::ERR_ABORTED`. Our document never commits under that one, and the
  page the tab shows afterwards is someone else's (F-882 §2e), so the abort's
  text describes no page the caller can land on. The abort keeps its own named
  refusal (`_aborted_error`).
- `navigation_milestone.answer(url, title, progress)` builds
  `BrowserManager.navigate`'s payload. It is `{url, title, success}` as before,
  plus `error_text` only when Chrome gave a reason. The one call replaced one
  line, so `browser_manager.py` stays at its 1447-line cap.
- `_require_navigation_ok`, still the one detector, quotes the reason:
  `… error page (chrome-error://chromewebdata/). Chrome's reason: net::ERR_….`
  It keeps the three likely causes only when nothing said which. That covers
  `go_back`, `go_forward` and `reload_page`, which never see a `Page.navigate`
  answer. Neither does `new_tab`: nodriver opens the tab with
  `Target.createTarget(url)`, which returns no `errorText`.
- `Progress.describe()` appends `Chrome's reason: …`, so a navigate timeout and
  the failed-attempt warning carry it too.

## 5. Tests

RED at 13bfc45: `5 failed, 94 passed` over `test_navigate_milestone.py`,
`test_tool_errors.py`, `test_navigation_truthfulness.py` and the two
real-Chrome nodes below. GREEN after the fix: `109 passed` over the same
selection, plus the rest of `test_truthful_success_flags.py` and all of
`test_e2e_navigation_truthfulness.py`. The latter holds the real-Chrome
download and pre-emption nodes, which exercise the `net::ERR_ABORTED`
exclusion.

- `tests/test_navigate_milestone.py`: the answer carries `error_text` for
  `net::ERR_CONNECTION_REFUSED` (RED). A timeout after Chrome gave a reason
  names it (RED). An ordinary landing answers with exactly its three keys, which
  is green on both sides because it guards the other half. The pre-existing
  `test_an_abort_whose_page_took_our_place_is_followed_not_called_a_download`
  asserts exact equality under `net::ERR_ABORTED`, so it pins the exclusion.
- `tests/test_tool_errors.py`: the refusal quotes the reason and drops the
  guesses (RED).
- `tests/test_truthful_success_flags.py`, real Chrome: a bound, never-listening
  loopback port gives exactly `net::ERR_CONNECTION_REFUSED` (RED), and the
  `.invalid` host's refusal now carries some `net::ERR_*` code (RED; the
  resolver decides which one).
- `tests/fakes.py`: `FakeTab` now models what Chrome does with any `errorText`
  other than the abort. `location.href` reads `chrome-error://chromewebdata/`
  while `target.url` keeps the requested URL.
- `tests/test_navigation_truthfulness.py::test_all_five_speak_with_one_voice`
  is unchanged and still green. Without a reason, all five tools say the same
  sentence.

## 6. Residuals and cost

1. The fleet flake's cause is still unproven. This finding only makes its next
   occurrence name Chrome's code. F-934 separately raises the fixture server's
   accept backlog, which the refused-connection hypothesis points at.
2. `error_text` could reach the wire on a SUCCESS answer only if Chrome's error
   page were replaced before the landing read, for example by Chrome's own
   network-error auto-reload. The answer would still be true: Chrome gave a
   reason for the URL that was asked for, and the tab has since moved. I have
   not observed this.
3. `new_tab` and the history moves could learn the reason too, by sending
   `Page.navigate` themselves or by listening for it. Not done: that changes how
   those tools move a tab, which is outside this fix.
4. Cost: one dataclass field, one `if` per navigation, and no extra CDP
   round trip.
