# F-913 — a pydantic `ValidationError` echoes a tool RESULT into a Sentry event

**Status:** OPEN (stub — filed by F-911, not fixed there)
**Date:** 2026-09-21
**Area:** `observability.py` (`_scrub_event`), or the raise site, on
`embedded/cdp_transport.CdpReplyError`'s precedent
**Filed by:** F-911's review (M1). F-911 named it as residual 1; this is that
residual given a number, because it is the one payload leak F-911 measured and
deliberately did not close, and a numberless residual is a thing nobody picks up.
**Measured against:** mcp 1.27.1, pydantic 2.11.7, sentry-sdk 2.64.0,
CPython 3.13.11, Windows 11

---

## 1. The census lines

| Site | Kind | Level | What carries the payload |
|---|---|---|---|
| `mcp/client/streamable_http.py`:240 | `logger.exception("Error parsing SSE message")` | ERROR | `exc_info` — a `ValidationError` whose `input_value=` echoes the SSE **data** |
| `mcp/client/streamable_http.py`:394 | `logger.exception("Error parsing JSON response")` | ERROR | same, for the non-SSE response leg (`JSONRPCMessage.model_validate_json(content)`) |
| `mcp/client/streamable_http.py`:574 | `logger.exception("Error in post_writer")` | ERROR | the catch-all around the whole writer; whatever escaped, including the above shape |

All three are in the **stdio proxy**, on the leg that reads the backend's answer
back. On that leg the SSE data IS the serialised answer to a `tools/call` — the
same bytes F-908 capped at the sending end. So a `get_cookies` jar or a
`get_page_content` document is what `input_value=` is quoting.

Measured: a marker placed in the tool result reaches the formatted traceback.
pydantic truncates `input_value` in the middle for a long input, which loses
neither end — a cookie jar's first entries and its last are both rendered.

## 2. Why the three mechanisms this tree already has cannot see it

`logging_setup` holds a third party's log line down three ways, and this line
defeats all three **by construction**, which is what makes it a separate finding
rather than a wider table:

* **by LEVEL** (F-906/F-908, `PAYLOAD_LOG_FAMILIES` + `apply_payload_log_floor`).
  `mcp.client` IS in that tuple and IS floored at WARNING — measured, the logger
  is `mcp.client.streamable_http` (`logging.getLogger(__name__)` at :41), so the
  family root covers it. The floor does not help: **ERROR is above it.** Lowering
  the floor to silence ERROR would silence the SDK's real faults, which is the
  thing F-906 declined to do to nodriver.
* **by ARGUMENT** (F-907, the record factory's `_carries_payload` rule). The
  message is a **static string literal** and `record.args` is empty. There is no
  argument to shape. F-907's exception clause is also explicit that an exception
  is never shaped, whatever package defined it — see §4.
* **by SITE** (F-911, `payload_log_sites`). A site rule withholds `record.msg`
  and clears `record.args`. Here `record.msg` is `"Error parsing SSE message"` —
  withholding it deletes the one thing that is safe and leaves the payload
  exactly where it was, in `exc_info`. Adding
  `mcp/client/streamable_http.py` to `PAYLOAD_LOG_SITES` would therefore ship a
  rule that reads as a fix and closes nothing.

## 3. Why Sentry's own two filters do not drop it either

* **`observability._scrub_event`'s step 1** removes four things and the payload
  is none of them: the hostname, the home-directory segment of a path, email
  addresses, and a URL's userinfo/query/fragment. A JSON tool result inside a
  traceback string survives all four. (A tool result that happens to contain a
  URL loses that URL's query — incidental, not coverage.)
* **`expected_events.classify` (step 0)** recognises five classes and this event
  is none of them. The near miss is `caller-input`, which is also a pydantic
  `ValidationError` — and it is the one class decided by the **FRAMES**, not by
  the logger: `ARG_VALIDATION_FRAMES` requires `fastmcp.tools.tool run`
  ADJACENTLY above `pydantic.type_adapter validate_python`. This traceback has
  neither frame. It is raised in the stdio proxy, a process that never imports
  the FastMCP tool manager, from `mcp/client/streamable_http.py` through
  `model_validate_json`. `error-convention` needs the outermost link to be ours;
  `client-disconnect`'s message arm is matched by EQUALITY against a different
  line; `nodriver-dead-browser` requires `nodriver` in the message;
  `proactor-teardown` names a CPython callback.

So it is an unrecognised ERROR, and `LoggingIntegration(event_level=ERROR)`
ships it as a **full Sentry event** — not a breadcrumb, not a local log line.
This is the harshest sink of the four, and it is the only one of F-906/F-907/
F-908/F-911's four surfaces that is still open on this shape.

## 4. Root cause — hypothesis

A `ValidationError`'s `__str__` is a **diagnostic that quotes its own input**,
and this tree has never decided what that may say. Every rule so far reasons
about a LOG RECORD (a level, an argument, a site); this payload is inside an
EXCEPTION, and F-907 settled — deliberately, and it still stands — that *an
exception is never shaped*, because every sink that formats a traceback renders
the text anyway, so shaping the `%s` withholds nothing while costing the one
sink that formats none.

That argument is sound and it is precisely why this is a different question:
closing this leak means bounding what a validation error may quote, which is a
decision about EXCEPTIONS and not about logging. The product has made that
decision once before, for a different library, at the RAISE — `cdp_transport`'s
`CdpReplyError` (F-902), which reports the CDP method, the exception type and
the reply's field COUNT rather than nodriver's interpolation of the whole reply.
The shape of the answer is probably that one. The open question is that here the
raise is a **third party's** and we do not own it.

## 5. Candidate homes — to be decided, NOT decided here

1. **`observability._scrub_event`, as a rule about the EXCEPTION VALUE.**
   Keyed on the exception's TYPE and MODULE (`pydantic_core._pydantic_core`
   `ValidationError`), never on message text — the keying F-906 §3 and F-911 §3
   both reject — and dropping or bounding the `input_value=` span. Reaches
   Sentry only, which for this shape may be enough, since the local durable log
   is a file nobody pays for and the breadcrumb path is below ERROR. It would be
   an addition to THE one scrubber rather than a second hook, so convention 4 is
   satisfied; but it would be the first rule there that reads an exception's
   text, and whether that is one home or two needs deciding.
2. **A raise-site wrapper on `cdp_transport.CdpReplyError`'s precedent.** The
   proxy's own transport leg is `singleton.run_backend` over
   `backend_client.http_client`, so there IS a seam of ours around the SDK call.
   Whether an exception the SDK logs *and then sends downstream* (`:241`
   `read_stream_writer.send(exc)`) can be replaced without changing the SDK's own
   control flow is the thing to measure first.
3. **`pydantic`'s own knob.** Whether `ValidationError` can be asked not to echo
   `input_value` at 2.11 — and whether that is settable for a model the SDK
   owns — is unmeasured and should be checked before either of the above, since
   a library setting beats a rule of ours.

Option 1 is the cheapest and reaches the sink that matters; option 3 would beat
it if it exists. Whoever takes this should ALSO census which other exceptions in
the tree quote their input — the answer may be "a rule about a type" rather than
"a rule about this SDK".

## 6. What F-911 deliberately did not do, and why

F-911 closed the door its own three measured lines came through
(`mcp/shared/session.py`:383/:384/:430, module-level `logging` functions,
f-strings, root logger) and **stopped at the record**. It did not:

* add `mcp/client/streamable_http.py` to `PAYLOAD_LOG_SITES` — §2 above: the
  rule would withhold the safe half and leave the payload, i.e. a change that
  reads as a fix and closes nothing. A site table that covers a site it cannot
  actually clean is worse than an absent entry, because the next reader believes
  it is handled;
* lower `PAYLOAD_LOG_FLOOR` below WARNING for `mcp.client` — that silences the
  SDK's genuine faults on the one leg that reports them, which is the trade
  F-906 explicitly refused for nodriver;
* touch `exc_info` in the record factory. F-907's exception clause stands:
  an exception is a diagnostic, every formatting sink renders it anyway, and
  shaping it inside `Logger.makeRecord` would withhold nothing while breaking
  the one sink that formats none. F-911 §6.2 states the same thing about
  `session.py`:444's own `exc_info`, which it likewise leaves.

## 7. Related

* `audit/stage2/finding_F911_root_logger_tool_payload.md` — §6 residual 1, where
  this was measured and named.
* `audit/stage2/finding_F908_sse_starlette_logs_tool_result.md` — the SENDING end
  of the same answer. F-908 capped `mcp.client` at WARNING precisely because
  both ends of one round trip render it; this is the third rendering on that
  leg, above the floor.
* `audit/stage2/finding_F907_nodriver_warning_renders_page_content.md` — the
  "an exception is never shaped" ruling this finding has to reopen or work
  around.
* F-902 / `embedded/cdp_transport.py` — the precedent for bounding what an
  unreadable payload may say about itself, decided at the raise.
