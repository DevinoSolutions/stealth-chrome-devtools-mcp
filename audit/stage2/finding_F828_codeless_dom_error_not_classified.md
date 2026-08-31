# F-828 — the stale-document classifier misses Chromium's other message, "DOM Error while querying"

**Status: FIXED** on `fix/F824-F828-classifier-reach` (2026-08-31), opened by the
2.0.8 Sentry triage of the 2.0.7 fleet.
**Severity: MEDIUM** — bounded and non-corrupting, but the calling tool fails
outright (`wait_for_element`, `query_elements`) with a message no caller can act
on, for a condition the product already knows how to recover from.

Sentry: `STEALTH-CHROME-DEVTOOLS-MCP-3F` (6 events, 2026-08-12 → 2026-08-30,
live on 2.0.7) and `STEALTH-CHROME-DEVTOOLS-MCP-23` (4 events, 2026-08-03 →
2026-08-16) — the same message, grouped apart because they arrive on different
stacks.

---

## What is proven

3F, on 2.0.7:

```
Error calling tool 'wait_for_element'
ProtocolException: DOM Error while querying

  dom_handler.py, line 646, in wait_for_element
                  element = await resolve_element(tab, selector)
  element_resolution.py, line 111, in _resolve_with_recovery
              return await resolve()
  nodriver\core\tab.py, line 570, in query_selector
              node_id = await self.send(cdp.dom.query_selector(doc.node_id, selector))
```

The failure happens *inside* the recovery loop and is not recovered: the frame
that catches it decided the error was not a race and re-raised it.

23 is the same message on `query_elements`/`select_all`, and its log line
records the code: `DOMHandler.query_elements: DOM Error while querying [code:
-32000]`. 3F's carries no code at all. Both shapes are real, from the same
message.

---

## Root cause

The classifier matched exactly one CDP message:

```python
# element_resolution.py, before
_STALE_NODE_MARKER = "Could not find node with given id"

def _is_stale_node_error(exc: ProtocolException) -> bool:
    return _STALE_NODE_MARKER in str(exc)
```

That is the message Chromium returns when `DOM.querySelector[All]` is handed a
nodeId the document has invalidated (`AssertNode` fails). But Blink has a
*second* reply for a query it could not complete: when the query reaches the
renderer and fails there, `InspectorDOMAgent` answers with the blanket
`ServerError("DOM Error while querying")`. Same call, same tool-visible
outcome, different string — and the string was the whole match.

**On the "code-less" part.** The task framed this as a variant arriving without
`-32000`, and 3F does: `ProtocolException.__str__` appends `[code: N]` only when
the CDP error object carried a `code` key (`connection.py:36-66`), and 3F's did
not, while 23's did. That is a real difference in the *string*, so any attempt
to key the classifier on the code would have matched only half the events. It
is not, however, what made the classifier miss them — the message was. The fix
therefore matches on message text only, and the pin
`test_the_codeless_dom_error_is_the_shape_sentry_reports` records both string
shapes from the library's own constructor so a future reader does not have to
rediscover which half of the string is stable.

---

## The fix

The one classifier, widened by one marker — no second classification site:

```python
_STALE_NODE_MARKERS = (
    "Could not find node with given id",
    "DOM Error while querying",
)

def _is_stale_node_error(exc: ProtocolException) -> bool:
    message = str(exc)
    return any(marker in message for marker in _STALE_NODE_MARKERS)
```

Everything downstream is unchanged: same `recoverable_race` description, same
`_MAX_RESOLVES` bound, same `_SETTLE_SECONDS` backoff, same "the original
`ProtocolException` surfaces after the final attempt" ending.

---

## Pins

In `tests/test_element_resolution.py` (hermetic, exceptions built from
nodriver's own `ProtocolException` with real CDP error objects):

| test | asserts |
|---|---|
| `test_the_codeless_dom_error_is_the_shape_sentry_reports` | the library contract: `[code: -32000]` is appended only when the error object carried a code |
| `test_resolve_element_recovers_from_a_codeless_dom_error` | the exact 3F shape → recovered on the retry |
| `test_resolve_elements_recovers_from_a_dom_error_carrying_the_code` | the 23 shape on the `select_all` path |
| `test_dom_error_recovery_is_bounded_exactly_like_the_node_id_one` | 3 attempts, then the original `ProtocolException` |
| `test_an_unrelated_runtime_error_is_never_retried` | a fatal error still surfaces on attempt 1 |

The existing `test_resolve_element_propagates_non_stale_error_immediately` and
the two `KeyError` scoping pins are untouched and still green: widening the
marker tuple did not widen anything else.

---

## Known trade — an invalid selector now costs ~0.15 s more

`ServerError("DOM Error while querying")` is Blink's blanket answer for *any*
exception raised while running the query, which includes a selector Chromium
rejects as invalid syntax. Those calls are now re-resolved up to three times
before failing. The cost is bounded by construction (`_SETTLE_SECONDS * attempt`
= 0.05 s + 0.10 s) and the outcome is unchanged: the original
`ProtocolException` reaches the caller with the same text, ~150 ms later.

That trade is deliberate, and it is the right way round for this product:
a stale document is a *transient, common* failure of a correct call, whereas an
invalid selector is a *permanent* failure that the caller repeats at most once.
Making a permanent failure 150 ms slower is cheaper than leaving a transient one
unrecovered. It could only be avoided by distinguishing the two, and Chromium
does not let us — `DummyExceptionStateForTesting` discards the underlying Blink
exception before the message is built, so both reach us as the same string.

Watch: if `wait_for_element`-with-a-bad-selector ever shows up as a latency
complaint, the fix is at the caller (validate the selector before the poll
loop), not by narrowing this marker.
