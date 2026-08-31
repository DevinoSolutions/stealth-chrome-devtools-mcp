# F-823 — half an emoji in page content crashes the whole tool return (`UnicodeEncodeError: surrogates not allowed`)

**Severity:** MEDIUM (a whole-tool crash, on ordinary pages, with no caller remedy)
**Found:** Sentry `STEALTH-CHROME-DEVTOOLS-MCP-4M`, release 2.0.6, 2026-08-30
**Status:** FIXED — branch `fix/F823-surrogate-safe-returns` (on top of `fix/F822-F837-response-handling`)
**Home:** `src/stealth_chrome_devtools_mcp/embedded/response_handler.py` (the policy) + `embedded/tool_registry.py` (the one return path it is applied on)
**Reporting tool:** `execute_script`

## Symptom

```
PydanticSerializationError: Error serializing to JSON: UnicodeEncodeError:
'utf-8' codec can't encode character '\ud83d' in position 5811:
surrogates not allowed

  fastmcp/tools/tool_manager.py  call_tool         -> await tool.run(arguments)
  fastmcp/tools/tool.py:303      run               -> _convert_to_content(result, ...)
  fastmcp/tools/tool.py:515      _convert_to_content -> default_serializer(result)
  fastmcp/tools/tool.py:57       default_serializer -> pydantic_core.to_json(data, fallback=str).decode()
```

Logged by `FastMCP.fastmcp.tools.tool_manager` under `Error calling tool
'execute_script'`. The CDP round trip had already succeeded and the page's
value was in hand; the call died while **encoding** the answer.

## Mechanism — a Python `str` can hold half a character; UTF-8 cannot

`\ud83d` is the **high half** of a UTF-16 surrogate pair — the first of the two
code units that spell `😀` (U+1F600) in UTF-16. It is not a character; it is
half of one.

Real pages hand these back constantly:

* JavaScript's `String.prototype.slice` / `substring` / `substr` index by
  UTF-16 **code unit**, so `text.slice(0, 5000)` on any string with an emoji
  near the cut lands between the halves and returns a broken tail. Every
  truncating extractor on a page does this.
* A mis-decoded byte run, or content that was never valid UTF-8 in the first
  place, arrives the same way.
* CDP itself round-trips JSON, and Python's `json` decoder recombines a
  *well-formed* `"😀"` escape pair into one U+1F600 — so what
  survives as two code points in a Python `str` is, by construction, always
  **unpaired**.

Python tolerates that: `str` stores code points and never validates the
surrogate range. UTF-8 does not tolerate it — there is no byte sequence for an
unpaired surrogate — so the encode raises, and the *entire* tool result is lost
along with the ~5,800 characters of perfectly good content preceding it.

### Why this could not be fixed in `response_handler`

`execute_script` never calls `ResponseHandler`. Neither do 86 of the other 93
tools — only 8 `handle_response` call sites exist in `server.py`. The crash is
not a large-response problem: **FastMCP UTF-8 encodes the result of every tool
call**, so the guard has to sit on the path every tool return travels.

F-822's `json_safe` does not catch it either, and the reason is precise: its
probe is `json.dumps(data, ensure_ascii=False)`, which **succeeds** on a lone
surrogate (it copies the code point into the output `str` and never encodes
anything). Serializable is not the same property as encodable.

## The fix — one string policy, applied at the one return boundary

Zero lines in `server.py`.

1. **`response_handler.surrogate_safe(data)`** — THE one repair. Walks a
   payload and rewrites **strings only**: `_LONE_SURROGATE` (`[\ud800-\udfff]`)
   → one U+FFFD REPLACEMENT CHARACTER per broken half. Returns the **same
   object** when nothing needed repairing, so a clean payload keeps its
   identity all the way through.
2. **`tool_registry._surrogate_safe_returns`** — the plumbing that puts it on
   the return of all 94 tools. `section_tool` already is the single
   registration chokepoint; it now composes
   `with_correlation_id(_surrogate_safe_returns(func))`, i.e. the repair runs
   *inside* the correlation-id context that logs the call, and `functools.wraps`
   keeps the schema FastMCP introspects (name / signature / docstring) intact.
3. **`json_safe` delegates to the same helper**, so the large-response spill
   file gets one policy, not a second implementation — and the caller-supplied
   `metadata`, which never went through the payload conversion and reaches the
   same `utf-8` file encoder, is routed through `json_safe` once and reused for
   both the on-disk copy and the returned descriptor.

### Why U+FFFD and not `errors="replace"`

`"\ud83d".encode("utf-8", errors="replace")` yields `b"?"` — indistinguishable
from a question mark the page really contained. U+FFFD is Unicode's own "a
character was here and it did not survive" marker: the loss is **minimal** (one
code point, exactly where it happened) and **visible** (the rest of the payload
is delivered verbatim).

### Why `surrogate_safe` is deliberately narrower than `json_safe`

`json_safe` is a **type** converter and will stringify what it cannot encode.
Running that on all 94 tool returns would silently change payload shapes that
FastMCP's serializer handles natively (`datetime` → `str(dt)` instead of ISO,
`bytes`, `UUID`, `Decimal`), because `json.dumps` fails on those and
`pydantic_core.to_json` does not. `surrogate_safe` is the **encoding** guard:
it touches strings, and returns every other leaf — including objects it does
not understand — by identity. That is what makes it safe to run on everything.

### Why not repair at the source instead

There is no source. The broken half can enter through any of ~40 JS-eval
sites, CDP response bodies, cookie values, header values, or a page title —
sanitizing each would be exactly the "second way" the repo forbids, and would
still miss the next one.

## Tests (hermetic, no Chrome) — `tests/test_surrogate_safe_returns.py`

* `TestSurrogateSafePolicy` — one lone surrogate → exactly one U+FFFD; a lone
  *low* half too; surrounding text preserved; a **whole** `😀` and a whole
  U+1D11E returned **by identity**; ASCII and BMP-non-ASCII payloads returned
  by identity; nested containers and **dict keys** repaired; non-string leaves
  passed through by identity; a bare `str` return repaired; 200-deep nesting
  does not blow the stack.
* `TestExecuteScriptReturnBoundary` — the **real** `execute_script` body driven
  through the `.fn` seam (`tests/fakes.py::call_tool`) over a `FakeTab` whose
  `evaluate` returns the broken string; the result is fed to
  `pydantic_core.to_json(payload, fallback=str)` — **the exact call from the
  Sentry stack trace**, not a stand-in. Also pins the exact client-visible
  shape (`{"success": True, "result": "prefix�suffix", "error": None}`),
  emoji pass-through by identity, an ordinary result unchanged by identity, and
  that the extra wrapper did not cost the tool its name / docstring /
  signature.
* `TestFileFallbackWritePath` — the spill file is written with surrogate
  content, caller `metadata` is repaired on that path too, the inline path
  repairs, a clean payload still passes by identity, and a whole emoji survives
  the spill file byte-identically.

**RED verified before the fix** against unmodified production code, both
reproductions:

```
--- 1. tool return boundary (execute_script .fn) ---
  tool returned: {'success': True, 'result': 'prefix\ud83dsuffix', 'error': None}
  [RED] PydanticSerializationError: Error serializing to JSON: UnicodeEncodeError:
        'utf-8' codec can't encode character '\ud83d' in position 6: surrogates not allowed
--- 2. ResponseHandler spill file ---
  [RED] UnicodeEncodeError: 'utf-8' codec can't encode character '\ud83d'
        in position 501: surrogates not allowed
```

— the first line reproducing the Sentry message verbatim. Both GREEN after.

## Residual risks

* **Error paths are not covered.** The repair sits on the *return* value. A
  `ToolError` whose message carries a lone surrogate (e.g. `execute_script`'s
  `raise ToolError(str(e))` on a CDP error that quotes page text) would hit the
  same encoder. Left alone deliberately: re-raising a repaired exception means
  reconstructing it, and `InstanceNotFoundError`-style subclasses do not all
  share `ToolError`'s `__init__`. Observed frequency so far: zero.
* **Cost is one regex scan per string per tool call.** `re.search` over an
  unmatched string is a C-speed pass, and the walk short-circuits to identity
  the moment nothing changed — but a multi-megabyte `get_page_content` payload
  is now scanned once more than before.
* **A repaired `tuple`/`set` comes back as a `list`.** Only when a repair
  actually happened, and both serialize to the same JSON array.
* **The repair is lossy by design.** The broken half is genuinely
  unrecoverable — its partner was discarded upstream, before the tool ever saw
  the string.

## Related

* **F-822** — same module, the *serializability* half of the same "the payload
  must survive the transport" concern; `json_safe` now calls this repair.
  `audit/stage2/finding_F822_estimate_tokens_crashes_on_cdp_objects.md`
* **F-837** — the inline/file threshold, same base branch.
* **F-785** — declares the export encoding this repair keeps honest.
