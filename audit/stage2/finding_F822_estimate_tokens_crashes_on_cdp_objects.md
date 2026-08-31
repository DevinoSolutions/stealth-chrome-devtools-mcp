# F-822 — the large-response path crashes on a raw CDP object (`TypeError: Object of type ExceptionDetails is not JSON serializable`)

**Severity:** MEDIUM (a whole-tool crash, but only on pages whose JS throws)
**Found:** Sentry, release 2.0.x
**Status:** FIXED — branch `fix/F822-F837-response-handling`
**Home:** `src/stealth_chrome_devtools_mcp/embedded/response_handler.py`
**Reporting tool:** `get_page_content`

## Symptom

`get_page_content` raised, from inside the size-estimation step that is
supposed to *protect* the caller:

```
TypeError: Object of type ExceptionDetails is not JSON serializable
  response_handler.py  estimate_tokens  ->  json.dumps(data, ensure_ascii=False)
```

The CDP work had already succeeded. The HTML was in hand. The call died while
measuring how big the answer was.

## Mechanism — the same nodriver behavior as F-795, one guard short

nodriver's `Tab.evaluate` does **not** raise when the evaluated JS throws; it
**returns** the CDP record in the value's place:

```python
# nodriver/core/tab.py — Tab.evaluate
remote_object, errors = await self.send(cdp.runtime.evaluate(...))
if errors:
    return errors                      # <- cdp.runtime.ExceptionDetails
if remote_object:
    if return_by_value:
        if remote_object.value:
            return remote_object.value
    ...
return remote_object                   # <- cdp.runtime.RemoteObject
```

Note the **second** leak: the trailing `return remote_object` is taken whenever
the value is *falsy* — an empty `document.body.innerText` is enough — so a
`RemoteObject` dataclass can escape even with no exception at all.

F-795 already found this and installed the one converter,
`tool_errors._require_js_value`, at the eval boundary — but only in
`DOMHandler.execute_script` (dom_handler.py:726, :750), because that is where
the *user's* script runs. `DOMHandler.get_page_content` calls `tab.evaluate`
**three** more times, unguarded:

```python
content = {
    "html":  await tab.get_content(),
    "text":  await tab.evaluate("document.body.innerText"),   # can throw
    "url":   await tab.evaluate("window.location.href"),
    "title": await tab.evaluate("document.title"),
}
```

On any page where `document.body` is null (a bare XML/JSON/PDF document, a page
caught mid-navigation, a CSP-blocked eval), `content["text"]` is an
`ExceptionDetails` dataclass. `get_page_content` hands that dict straight to
`response_handler.handle_response`, whose first act is `json.dumps` — and
`json` cannot encode a dataclass. Crash.

Had estimation survived, the *next* step would have failed anyway: the MCP
layer has to serialize the tool result too. So the defect is not only "the
estimator is fragile", it is "**a raw CDP object reaches a tool result at
all**".

## The fix — one conversion at the transport boundary

Two changes, both in `response_handler.py`, zero lines in `server.py`:

1. **`json_safe(data)`** — THE one home for making a tool payload
   JSON-serializable. Returns `data` *unchanged* when it is already pure JSON
   data (so the identity contract the suite pins for ordinary payloads still
   holds), and otherwise a converted copy. `handle_response` applies it once,
   before the size estimate, which covers **both** exits — inline and spilled —
   and all six `handle_response` call sites in `server.py`
   (`get_page_content`, `list_network_requests`, `extract_element_assets`,
   `extract_related_files`, `expand_children`, `clone_element_complete`) at
   once. Conversion prefers the object's **own** `to_json()`, so a converted
   `ExceptionDetails` still carries its real `text` / `exception` /
   `className`; then `dataclasses.asdict`; then `str(value)`. Depth-guarded at
   24, never raises.
2. **`estimate_tokens` and the spill `json.dump` take `default=str`.**
   Defence in depth: measuring a payload's size, or writing it to a file, must
   never be able to fail the tool that produced it — even for a caller that
   reaches `estimate_tokens` directly (it is public API) or passes a foreign
   object in `metadata`.

### Why not `_require_js_value` (i.e. raise) here?

Deliberate, and the two guards are not a second way to do one thing:

* `tool_errors._require_js_value` is the **error convention** for the eval
  escape hatch (F-795). There, a throwing script *is* the failure, and
  `success: true` with the exception hidden in the value was the bug.
* `json_safe` is the **transport** guard for payloads whose foreign object is
  incidental. A page whose `document.body.innerText` threw still has real HTML,
  a real URL and a real title to return; raising would convert a
  partially-successful content fetch into a total failure and lose them.

The caller loses nothing: the converted record is right there under `text`,
readable, with the browser's own message.

## Tests (hermetic, no Chrome)

`tests/test_response_handler.py`:

* `TestNonSerializablePayloads` — estimation survives a real `ExceptionDetails`
  and a real `RemoteObject`; the inline payload carries no foreign object; the
  record's own fields survive conversion; a clean payload still passes through
  **by identity**; the spill file is valid JSON; an object with no JSON form at
  all degrades to its string form instead of crashing.
* `TestGetPageContentDoesNotLeakCdpObjects` — the **real**
  `dom_handler.get_page_content` body driven through the `.fn` seam with a tab
  whose `evaluate` returns the CDP record; the tool's result is plain,
  `json.dumps`-able data.

Every CDP record in these tests is built by **nodriver's own**
`ExceptionDetails.from_json` / `RemoteObject.from_json`, not by a hand-rolled
double — a fake is free to encode the very bug it is meant to catch (see the
`FakeTab.send` bare-except lesson).

RED verified before the fix: 7 of these nodes failed, the first with the exact
Sentry text `TypeError: Object of type ExceptionDetails is not JSON
serializable`, raised through the real tool body.

## Residual risks

* **The leak is fixed at the boundary, not at the source.** `dom_handler`'s
  three unguarded `evaluate` calls still *produce* the CDP record; the payload
  is only sanitized when it passes through `handle_response`. Every tool that
  currently returns raw `tab.evaluate` output without going through either
  guard would still leak. The census: `dom_handler.py` lines 539/551/867
  discard the result, 713/717/745 are the `_require_js_value`-guarded
  `execute_script` path, and 773/778/779 are `get_page_content` (fixed here).
  Nothing else in `dom_handler` returns raw evaluate output to a caller.
* `contains_foreign_object` in the tests is a test-side check; production has no
  assertion that a tool result is plain — the guarantee comes from the single
  boundary, not from a tripwire.

## Related

* **F-795** — the same nodriver return-instead-of-raise behavior, the *other*
  guard (`_require_js_value`), on the `execute_script` path.
* **F-837** — the inline-vs-file threshold, same module, fixed in the same
  change: `audit/stage2/finding_F837_inline_threshold_above_client_ceiling.md`.
* **F-785** — documents the fallback envelope this path returns.
