# F-837 — the inline/file threshold sits ABOVE the MCP client's token ceiling: a dead band too big to deliver, too small to divert

**Severity:** MEDIUM
**Found:** 2026-08-30, live stress test of v2.0.7 (recorded first as a section
of the F-834 load-test writeup; authored here as its own finding)
**Status:** FIXED — branch `fix/F822-F837-response-handling`
**Home:** `src/stealth_chrome_devtools_mcp/embedded/response_handler.py`

## Symptom

The large-response file fallback exists so an oversized tool result becomes a
`file_path` the agent can `Read` instead of an error. Live, it engaged for the
big cases and missed the middle one:

| measured payload | outcome | correct? |
|---|---|---|
| 59,734 chars | returned **INLINE**; client rejected it — *"result exceeds maximum allowed tokens"* | **NO** |
| 138.91 KB | tidy fallback dict, spilled to file | yes |
| 282.83 KB | tidy fallback dict, spilled to file | yes |

The 59,734-char response was **lost**: too big for the client to accept, too
small for the product to divert. The caller got neither the content nor a file
path — just a client-side rejection, with nothing in the backend log to say
anything went wrong (the tool counts it a success).

## Mechanism

```python
INLINE_TOKEN_CEILING = 20000            # was: ResponseHandler(max_tokens=20000)
estimated_tokens = len(json.dumps(data)) // 4
if estimated_tokens <= self.max_tokens:
    return data                          # inline
```

Two compounding errors, one visible and one hidden:

1. **The ceiling was too high.** 20,000 estimated tokens sits above the
   client's practical cap for a single tool result.
2. **The estimator is optimistic for exactly the payloads this handler
   carries.** `len // 4` is the general English-prose rule of thumb. HTML, CSS
   and JSON are punctuation-dense and get escaped inside the JSON envelope;
   they tokenize far worse. The live rejection *measures* this: 59,734 chars
   estimated at **14,933** tokens and still exceeded the client's ceiling.

So the dead band was every response between the client's real limit and the
product's belief about it.

## Choosing the new threshold (derivation, not a round number)

* **Client ceiling in play: 25,000 tokens** — Claude Code's default cap on a
  single MCP tool result.
* **Worst-case real density, derived from the failure itself:** 59,734 chars
  exceeded 25,000 real tokens ⇒ under **~2.39 chars/token**, versus the
  estimator's assumed 4. Take **2.0 chars/token** as the worst case; then
  `real ≈ 2 × estimated`.
* **Budget:** target **20,000 real** tokens, i.e. 80% of the ceiling, leaving
  headroom for the MCP envelope and for a client configured lower.
* **⇒ threshold = 20,000 / 2 = 10,000 estimated tokens ≈ 40,000 chars.**

Check against all three live data points:

| payload | estimated tokens | old (20,000) | new (10,000) |
|---|---|---|---|
| 59,734 chars | 14,933 | inline → **rejected** | **file** (1.49x over) |
| 138.91 KB | ~35,560 | file | file |
| 282.83 KB | ~70,700 | file | file |
| 4,000 chars | 1,000 | inline | inline |

The regression size clears the new threshold by 49% — not a knife-edge pass, so
estimator jitter cannot put it back in the dead band. Genuinely small responses
are untouched: a 40,000-char page-content result is already large enough that a
file path is the more useful answer.

## Why lower the ceiling rather than fix the estimator

Both would work. Lowering the ceiling was chosen because:

* `estimate_tokens` is **public API** with pinned tests and other callers'
  expectations built on `len // 4`; changing chars-per-token changes the meaning
  of the `estimated_tokens` field every fallback envelope reports.
* The chars-per-token ratio is not a constant — it depends on the content, so
  any single replacement value would be as wrong as 4, just differently. The
  ceiling is the honest place to carry the safety margin, and the derivation
  above records *why* the margin is 2x.
* No new env knob: `INLINE_TOKEN_CEILING` is a module constant with the
  derivation in its docstring, per the repo's prefer-a-constant rule.

## Tests

`tests/test_response_handler.py::TestInlineThresholdBelowClientCeiling`, using
the measured sizes as the pins:

* `test_regression_size_diverts_to_the_file_fallback` — a payload whose JSON is
  **exactly 59,734 chars** must take the file path. (RED before the fix.)
* `test_previously_diverting_sizes_still_divert` — 138.91 KB and 282.83 KB, so
  the threshold can only ever be lowered, never raised past them.
* `test_a_genuinely_small_response_stays_inline` — 4,000 chars stays inline and
  nothing is written to disk.
* `test_default_ceiling_is_the_documented_constant` — the threshold is the
  module constant, not a literal.
* `test_the_regression_size_clears_the_ceiling_with_margin` — >1.25x, pinning
  that the fix is not knife-edge.

All spills are redirected to `tmp_path`; no test touches the real clone-output
dir.

## Residual risks

* **The client ceiling is an environment fact, not a contract.** A client
  configured below 20,000 tokens for MCP output could still reject an inline
  result. The 80% budget plus the 2 chars/token worst case is the margin; there
  is no feedback channel from the client's limit into the backend, and adding an
  env knob for it was rejected (repo rule: prefer a constant; the knob would
  also be a per-client value the backend is shared across).
* **`take_screenshot` has its own, unchanged, `estimated_tokens > 20000`
  literal** (`server.py:1140`). It does not route through `handle_response`: it
  estimates base64 size from the compressed byte count. Same defect *class*, no
  measured data point, and `server.py` is at its exact LOC cap — left alone
  deliberately rather than changed on a guess.
* Historical audit docs still quote the old default
  (`audit/stage1/deepdive_lifecycle.md:180`,
  `audit/stage2/finding_F785_…:21`); they are records of what was observed at
  the time and were not rewritten.

## Related

* **F-834** — the concurrent-spawn finding whose load test surfaced this; the
  F-837 section there is superseded by this file.
* **F-822** — same module, fixed in the same change:
  `audit/stage2/finding_F822_estimate_tokens_crashes_on_cdp_objects.md`.
* **F-785** — the fallback envelope's shape and the missing truncation marker.
