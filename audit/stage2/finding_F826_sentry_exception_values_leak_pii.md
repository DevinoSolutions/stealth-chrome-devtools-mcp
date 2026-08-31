# F-826 — Sentry exception VALUES shipped page content: OAuth tokens and email addresses

**Status: RESOLVED** on `fix/F826-targeted-pii-redaction`.
**Severity: HIGH (observed live, third-party machines included)** — this is data
belonging to people who never opted into anything, leaving their machines by
default, and some of it is a live credential.

---

## Symptom

`observability.py` ships every uncaught error to a hardcoded DSN, on by default.
The 2.0.4 scrubber (F-813) removed what a machine leaks about **itself** — the
hostname (`server_name`) and the home-directory segment of every path. It said
nothing about what a machine leaks about **the pages it drives**.

That gap matters more in this product than in most. Every tool here fails by
quoting the thing that failed, and the thing that failed is page content: a URL
an agent navigated to, a selector holding a form value, a proxy string. Observed
in the project's own Sentry:

* OAuth authorization URLs with `access_token` / `client_id` still in the query;
* email addresses, in selectors and in "element not found" messages;
* both arriving from third-party PyPI installs as well as the maintainer's box.

The memory note "0 external users" is false (see `external-users-on-pypi.md`):
at least four third-party machines report into this DSN.

## Root cause

`_scrub_event` walked every string in the event and applied exactly one rule —
`_HOME_SEGMENT_RE`, the home-directory anonymizer. Exception `value` strings,
`logentry` messages and breadcrumbs therefore passed through **verbatim** unless
they happened to contain a home path.

`include_local_variables=False` (added in the same 2.0.4 pass) already stops the
frame-locals leak, where the secret is the variable's value. It cannot help here:
this leak is in the exception's own message text, which is the part of an event
you cannot switch off without switching reporting off.

## The ruling (human, 2026-08-31)

**Targeted redaction. Not message replacement, and not widened.** An issue whose
message has been blanked is an issue nobody can act on, which is a slower way of
turning reporting off. Three shapes, each keeping the diagnostic skeleton:

1. **email addresses** → `[redacted-email]`;
2. **URLs** → keep scheme + host + path; drop query, fragment and userinfo;
3. **filesystem user paths** → keep the shape, replace the username segment.

Item 3 already existed and shipped in 2.0.4 (`C:\Users\~\…`, `/home/~/…`,
`/Users/~/…`, under both PurePath flavors regardless of the reporting host's OS).
It is **not** re-implemented here and its placeholder is **not** renamed to
`[redacted]` — a second spelling of a redaction that already has one is the
"second way is a defect" lens, and renaming it would churn 20 passing goldens for
nothing. Items 1 and 2 are the new work.

## Fix

All of it inside `_scrub_event`'s own helper family in
`src/stealth_chrome_devtools_mcp/observability.py` — still THE one scrubber,
still one `before_send`, still one walk over the event.

| Piece | What it is |
|---|---|
| `_URL_RE` + `_redact_url` | `scheme://` `[userinfo@]` `host/path` `[?query][#fragment]`. Prefix kept verbatim; `userinfo@` → `[redacted]@`; query → `?[redacted-query]`; fragment → `#[redacted-fragment]` |
| `_EMAIL_RE` + `_redact_email` | address → `[redacted-email]`, but only when the domain's last label is alphabetic and ≥2 chars |
| `_redact_text` | THE per-string rule: URL, then email, then home. One home for "what a string loses" |
| `_anonymize` | unchanged walk (dict/list/tuple/str, depth-bounded); it now calls `_redact_text` instead of inlining the home rule |
| `_without_unscrubbable_text` | the degraded floor, widened — see below |

**Ordering is load-bearing exactly once**: the URL rule runs before the email
rule so a proxy URL's `user:pass@host` is already `[redacted]@host` before
anything looks for an `@`. After that the two cannot see each other's output.

**F-815 is untouched and runs first.** `_scrub_event` still drops an event whose
*entire* exception chain is our own `ToolError` family (name **and**
`stealth_chrome_devtools_mcp` module prefix). Redaction only ever runs on what
survives that drop — pinned by two new tests, one for each side of the rule.

### The degraded floor moved

The old fallback shipped the whole event minus `server_name` when the walk
itself raised. Post-F-826 that would mean shipping raw page content on the one
path where we cannot claim it was scrubbed. `_without_unscrubbable_text` now also
drops `logentry` / `breadcrumbs` / `message` / `extra` and replaces each
`exception.values[].value` with `[redacted-unscrubbable]`, while keeping the
exception `type`, `module`, `mechanism`, frames, `tags` and `release`. The event
is still **sent** — losing it is the failure the never-raises contract exists to
prevent — it just no longer carries prose nobody vouched for. (Structurally the
path is unreachable for a real event: by the time `before_send` runs, the SDK has
already serialized the event to plain JSON. It exists for hostile or hand-built
input.)

### Performance

Both new patterns carry a look-behind (`(?<![A-Za-z0-9+.\-])`,
`(?<![A-Za-z0-9._%+\-])`) and possessive runs, for the same reason the home rule
does: these run inside `before_send`, in a process that is already crashing, on
strings that can be arbitrary page content. Without the look-behind a long run of
scheme-legal or local-part-legal characters retries the scan at every offset —
quadratic. With it, only a position that actually starts a token is tried.
Measured: a 200 000-char adversarial string (`u:p@`×20 000, `a.`×20 000 + `@`,
`?`×40 000, a 40 000-char URL path) scrubs in **11 ms**. Five pathological rows
are pinned in the suite; there is no timing assertion (that would flake on a
loaded runner) — the rows *completing* is the assertion.

## Before / after

```
IN   Error: element not found for 'input[value=alice.smith@gmail.com]' on
     https://accounts.google.com/signin/oauth?client_id=99.apps.googleusercontent.com&access_token=ya29.a0ARr&state=xyz
OUT  Error: element not found for 'input[value=[redacted-email]]' on
     https://accounts.google.com/signin/oauth?[redacted-query]

IN   navigate timeout: https://app.example.com/#access_token=SECRET&expires_in=3600
OUT  navigate timeout: https://app.example.com/#[redacted-fragment]

IN   proxy http://bob:hunter2@gw.corp.example.com:3128 refused CONNECT
OUT  proxy http://[redacted]@gw.corp.example.com:3128 refused CONNECT

IN   [Errno 13] Permission denied: 'C:\Users\amind\.stealth-mcp\logs\backend.log'
OUT  [Errno 13] Permission denied: 'C:\Users\~\.stealth-mcp\logs\backend.log'
```

…and the diagnostics that must NOT move:

```
IN  = OUT   GET https://api.example.com/v1/instances/42 returned 500
IN  = OUT   /opt/homebrew/opt/python@3.11/lib/python3.11/asyncio/runners.py
IN  = OUT   sentry-sdk@2.64.0 is installed
IN  = OUT   loaded @sentry/browser
IN  = OUT   backend http://127.0.0.1:19222/mcp did not answer
```

`python@3.11` is the row that decides the email rule's shape: matching on `@`
alone would have eaten the interpreter path out of every macOS stacktrace.

## Tests

`tests/test_observability_scrubbing.py` (extended — it is the existing one home
for scrubber tests; a new file would have been a second one). Hermetic: no
network, no `sentry_init`, no real `~/.stealth-mcp`. `_scrub_event` is called
directly with dict fixtures, which is what a `before_send` is.

* **RED first**: 16 failed / 61 passed at the tests-only commit point. Every
  failure was an F-826 assertion; the 61 included the pre-existing home-path and
  never-raises pins, which stayed green throughout.
* **GREEN**: 77 passed in 0.11 s.

New coverage:

| Group | Pins |
|---|---|
| email | 5 positive rows (prose, plus-addressing + multi-label domain, two per string, sentence-final period, quoted form value) |
| email, declined | 4 rows that must NOT move (`python@3.11`, `sentry-sdk@2.64.0`, `@sentry/browser`, `postgres@localhost`) |
| URL | 6 rows (OAuth query, implicit-flow fragment, both at once, proxy userinfo, userinfo containing an address, session id + email in a query) |
| URL, declined | 3 rows with nothing secret in them; plus one row stating the anchor trade explicitly |
| combined | all three rules on one `exception.values[].value`; `logentry.params` + breadcrumb messages |
| idempotence | re-scrubbing a scrubbed event is a no-op |
| never-raises | 5 pathological rows; the hostile-`items()` event now asserts the widened floor |
| F-815 | expected `ToolError` still dropped whole; a real bug wrapped in one still shipped, now redacted |

Windows AND POSIX path flavors were already parametrized (`C:\Users\`, `C:/Users/`,
repr-escaped `C:\\Users\\`, UNC, `/home/`, `/var|usr|export/home/`, `/Users/`) and
remain green — this repo has been burned by Windows-only verification before, so
the flavors are string rules and never `os.path`.

Verified green: `test_observability_scrubbing.py` (77), `test_observability.py`,
`test_observability_toolerror_filter.py`, `test_doc_claims.py`,
`test_doc_examples.py`, `test_error_typing.py`, `test_logging_setup.py`,
`test_settings.py` — 276 + 238 across the two targeted runs. `ruff check src
tests` clean; `tools/check_file_budgets.py` and `tools/check_suppression_owners.py`
pass. `observability.py` is **573/1000 LOC**, not grandfathered.

## Residual PII shapes deliberately NOT covered

Named because the ruling is targeted, and an unstated gap reads as a claim of
completeness:

* **Free-text personal data that is not an address or a URL** — names, phone
  numbers, postal addresses, card numbers, and any other form value quoted into a
  message. Covering these needs an entity classifier, which is a different
  project and a much worse false-positive profile.
* **Identity in a URL PATH** — `https://x.example.com/u/alice` keeps `alice`.
  The path is the diagnostic; dropping it would leave `https://x.example.com`,
  which is not enough to act on.
* **`user@localhost` and other dotless-domain addresses** — declined by the
  domain-label rule, deliberately, because that is the same shape as
  `python@3.11`.
* **IP addresses and ports** — kept. `http://127.0.0.1:19222/mcp` is this
  product's own backend and reading it is half of triage.
* **Account names with spaces mid-sentence** — the pre-existing F-813 residual
  (only the first word is anonymized when no separator or quote follows). Stated
  in `observability.py`'s `_HOME_USER` comment; unchanged here.
* **Sentry `tags`** — kept whole. They are structured key/values this project
  writes itself (correlation ids), not user text.
* **A >64-character local part** is not a real address and is not special-cased.

Frame **locals** are not on this list: `include_local_variables=False` already
prevents them from being collected at all.
