# F-785 — no truncation marker, no total budget, and an undeclared export encoding

**Status:** OPEN — characterized by plan_RELEASE W15, not fixed (W15 is zero-`src/`).
**Severity:** LOW–MEDIUM. Bounds are enforced; what is missing is the *signal*
that a bound was hit, and a declaration that keeps UTF-8 true by design rather
than by accident.
**Surface:** `src/stealth_chrome_devtools_mcp/embedded/response_handler.py`,
`embedded/network_interceptor.py`, `embedded/debug_logger.py`,
`tools/canary_repro.py`.
**Found by:** plan_RELEASE §2.15 (W15), "bounded capture".

## 1. There is no inline truncation marker anywhere

§2.15 asks for "explicit truncation markers/checksums". A sweep of `src/` and
`tools/` finds **no** appended marker string — no `...`, no `[truncated]`, no
elision sentinel, and no checksum of dropped content. Boundedness is real but
signalled **structurally**, differently per surface:

| Surface | Bound | How it is signalled |
|---|---|---|
| `ResponseHandler.handle_response` | `max_tokens` (default `20000`) | the payload is **replaced** by an envelope carrying `reason = "Response too large, automatically saved to file"` and a `file_path` to the full content |
| `NetworkInterceptor._store_response` | `network_body_max_bytes`, `network_body_store_max_bytes` | `response.body` is set to `None`, metadata kept; a `debug_logger` line records it |
| `NetworkInterceptor._store_request` | `network_post_data_max_bytes`, `network_request_max_count` | same shape (`post_data = None`, FIFO eviction) |
| `DebugLogger.get_debug_view_paginated` | `MAX_ERRORS` / `MAX_WARNINGS` / `MAX_INFO` | a `summary` block with `total_*` vs `returned_*` counters |

Consequence: **a reader holding only a bounded value cannot tell it was
bounded.** A `None` body is indistinguishable from a body that was never
captured (`network_capture_bodies` is `False` by default), and no checksum of
the dropped bytes exists, so a truncated capture cannot be verified against the
original.

## 2. The total diagnostic budget is a product, not a cap

`tools/canary_repro.py` is the only bounded local diagnostic budget in the tree.
Its caps are per-field and per-list:

```python
MAX_ENTRIES = 200
MAX_VALUE_CHARS = 256
MAX_IDENTITY_KEYS = 64
```

Nothing bounds the serialized size of the record as a whole. A record at the
maxima is accepted intact and exceeds 50 KB. The effective ceiling is
`MAX_ENTRIES × MAX_VALUE_CHARS` per list, which is a consequence rather than a
declared total — so "just raise `MAX_ENTRIES`" silently raises the total too.

## 3. The JSON exports declare no encoding; UTF-8 holds by accident

Two write sites open their target with **no `encoding=`**, so the bytes are the
platform default (cp1252 on a stock Windows runner, UTF-8 on Linux/macOS):

* `embedded/network_interceptor.py` — `Path(filepath).write_text(json.dumps(data, indent=2))`
* `embedded/debug_logger.py` — `with Path(filepath).open("w") as f: json.dump(...)`

The output is nevertheless always valid UTF-8, because `json.dump`/`json.dumps`
default to `ensure_ascii=True` and escape every non-ASCII code point to `\uXXXX`.
**The property holds, but nothing declares it.** Passing `ensure_ascii=False` —
an ordinary readability change — would immediately break UTF-8 validity on
Windows only, i.e. on exactly one of the three gate OSes.

By contrast `response_handler.py` does it properly:
`open("w", encoding="utf-8")` with `ensure_ascii=False`, producing real multibyte
UTF-8. That is the shape the other two should adopt.

### 3a. The debug export silently changes format with buffer volume

Found while pinning the above. `DebugLogger.export_to_file` defaults to
`fmt="auto"`, which selects by **item count**, not by the caller's request or the
file extension:

```
total_items > _GZIP_THRESHOLD (1000)  -> "gzip-pickle"   (and rewrites .json -> .pkl.gz)
total_items > _PICKLE_THRESHOLD (100) -> "pickle"        (binary, at the .json path)
otherwise                             -> "json"
```

So `export_debug_logs(filepath="debug.json")` writes **binary pickle to a file
named `.json`** on any process that has logged more than 100 entries — which a
real session reaches quickly. A consumer that opens the result as JSON fails,
and the failure depends on how busy the process was rather than on anything the
caller did. This also means the undeclared-encoding write site above is only
reached on a lightly-logged process.

Not separately pinned as its own node — W15's encoding pin passes `fmt="json"`
explicitly and documents why — but recorded here because "the export format
depends on how much you logged" is a contract fact no document currently states.

## Pins

`tests/test_observability.py`:

* `TestBoundedCapture::test_an_oversized_payload_is_replaced_by_a_bounded_envelope`
* `TestBoundedCapture::test_a_payload_within_budget_passes_through_untouched`
* `TestBoundedCapture::test_the_spilled_bytes_are_valid_utf8`
* `TestBoundedCapture::test_the_debug_view_reports_total_versus_returned`
* `TestBoundedCapture::test_the_per_field_and_total_bundle_caps_are_enforced`
* `TestBoundedCapture::test_there_is_no_total_byte_budget_across_the_whole_record`
  (`@pytest.mark.characterization`, `route:F-785`)
* `TestBoundedCapture::test_no_diagnostic_surface_emits_an_inline_truncation_marker`
  (`@pytest.mark.characterization`, `route:F-785`)
* `TestBoundedCapture::test_the_json_exports_are_ascii_by_default_not_declared_utf8`
  (`@pytest.mark.characterization`, `route:F-785`)

Per-cap enforcement itself is **already** covered by
`tests/test_network_interceptor.py::TestBodyStoreByteCaps`,
`::TestRequestStoreCaps`, `tests/test_response_handler.py` and
`tests/test_debug_logger.py::TestBufferCaps`. W15 deliberately does not restate
them (a second way is a defect) and asserts only the signalling and totality
questions those files leave open.

## Contract limitation wording (for W5 §Limitations)

> Diagnostic capture is bounded, but no surface emits an inline truncation marker
> or a checksum of dropped content: an over-cap response is replaced by a
> file-spill envelope, an over-cap network body becomes `null`, and the debug view
> reports total-vs-returned counters. A bounded value cannot be recognised as
> bounded from the value alone. The network and debug-log JSON exports do not
> declare an output encoding; they are valid UTF-8 only because the JSON encoder
> escapes non-ASCII by default.
