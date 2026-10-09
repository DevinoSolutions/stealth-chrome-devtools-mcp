# F-953 — `test_network_debugging_flow` reads a response body once, and the body can lag the response record

**Severity:** Low. Test-only fix; one product behaviour is recorded as a residual.
A flake on a loaded Linux runner failed the publish gate for 2.1.24 and cost a re-run.
**Files:** `tests/test_e2e_data_tools.py` (`_poll_response_body`, new;
`test_network_debugging_flow`). Read, unchanged:
`embedded/tool_sections/network_debugging.py` (`get_response_content`),
`embedded/network_interceptor.py` (`_on_response`, `get_response_body`).
**Seen:** publish run 37798968161, attempt 3, 2.1.24, `gate / integration (Linux/X64)`.
The runner was slow that attempt: Chrome took 41.9 s to answer `/json/version`
(`ms_to_json_version`) and logged "Network service crashed or was terminated,
restarting service". The failing line:

```
tests/test_e2e_data_tools.py:162: in test_network_debugging_flow
    reflected = json.loads(echo_body)
E   TypeError: the JSON object must be str, bytes or bytearray, not NoneType
```

Earlier flakes of the same test ("fetch to /api/json was not captured") are F-935 /
F-936, a different window (the request record) and fixed there.

---

## 1. Cause (read from the code; the window itself is NOT measured)

The test has two eventual-consistency waits and covered one of them.

1. `_find_request` / `_poll_response_details` poll until the request and the
   response RECORD exist. `_on_response` stores the record only after it has awaited
   its own `Network.getResponseBody` (capture on), and it runs on
   `Network.responseReceived`, that is when the HEADERS arrive, not on
   `loadingFinished`.
2. `get_response_content` then issues a SECOND, live `Network.getResponseBody`, once.
   `NetworkInterceptor.get_response_body` turns EVERY failure into `None` (the
   `except Exception` branch logs at debug and returns), and the tool maps that
   `None` through `if body:`. "Chrome refused: the body has not finished arriving",
   "evicted", and "empty body" are indistinguishable to the caller.

The `/api/json` read had `assert body is not None`; the `/api/echo` read had no
assertion and fed `None` straight into `json.loads`, which is why the report is a
`TypeError` and not an assertion message. A runner that takes 40 s to start Chrome
can also be slow to finish delivering a POST reply, so the record can exist while the
body is still refused.

What is NOT established: which of the three `None` causes it was on that run. The
log carries no CDP error text, because the swallow logs at debug. The leading
hypothesis is "not finished arriving". Eviction is unlikely at this size and age.

## 2. Fix (test-only)

`_poll_response_body(iid, rid, timeout=10.0)` re-reads `get_response_content` every
0.25 s until it is non-None, the same deadline/interval shape as
`_poll_response_details`. Both reads in the test use it, and each asserts with a
message before parsing, so a real miss now reads as "response body never became
available" and not as a `TypeError`.

## 3. Evidence

- **The failure is reproducible by emulation, and the fix closes it.** Mutation by a
  `-p` plugin that rebinds `NetworkInterceptor.get_response_body` so the first three
  live reads per request id return `None` (no source file modified):
  - the pre-fix test (`git show HEAD:tests/test_e2e_data_tools.py` copied to a
    scratch file): 1 failed (`assert None ...`);
  - the fixed test, same plugin: 1 passed.
- Unmutated, the fixed test: 1 passed (16.9 s, real headless Chrome, sandbox profile).
- `ruff check` and `ruff format --check` on the file: clean.

## 4. Residual (product, not changed here)

`get_response_content` cannot tell a caller "not yet" from "never". A caller that
polls, as the test now does, works; one that reads once gets `None` for both. A
distinct answer (for example raising `ToolError` with Chrome's own message for the
refusal, and `None` only for a genuinely empty body) is a behaviour change to a
documented return type and is not part of this finding.
