# F-913 — a pydantic `ValidationError` echoes a tool RESULT into a Sentry event

**Status:** fixed
**Date:** 2026-09-21
**Area:** `embedded/payload_log_sites.py` (the site tables) + `embedded/logging_setup.py`
(the one record factory)
**Filed by:** F-911's review (M1). F-911 named it as residual 1; this is that
residual given a number, because it is the one payload leak F-911 measured and
deliberately did not close, and a numberless residual is a thing nobody picks up.
**Measured against:** mcp 1.27.1, pydantic 2.11.7 / pydantic-core 2.33.2,
sentry-sdk 2.64.0, CPython 3.13.11, Windows 11

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

All three line numbers verified unchanged at HEAD against the installed SDK.

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

Driven at HEAD: `classify()` answers `None` for the real event (pinned,
`TestTheLeakIsReal::test_expected_events_does_not_recognise_it`).

So it is an unrecognised ERROR, and `LoggingIntegration(event_level=ERROR)`
ships it as a **full Sentry event** — not a breadcrumb, not a local log line.
This is the harshest sink of the four, and it is the only one of F-906/F-907/
F-908/F-911's four surfaces that is still open on this shape.

## 4. Root cause

A `ValidationError`'s `__str__` is a **diagnostic that quotes its own input**,
and this tree had never decided what that may say. Every rule so far reasons
about a LOG RECORD (a level, an argument, a site); this payload is inside an
EXCEPTION, and F-907 settled — deliberately, and it still stands — that *an
exception is never shaped*, because every sink that formats a traceback renders
the text anyway, so shaping the `%s` withholds nothing while costing the one
sink that formats none.

That argument is sound and it is precisely why this is a different question.
F-907's clause is about an exception that quotes NOBODY, where the text is pure
diagnostic; here the text IS the payload. Closing this leak means bounding what
a validation error may quote, and the product has made that decision once
before, for a different library, at the RAISE — `cdp_transport`'s
`CdpReplyError` (F-902), which reports the CDP method, the exception type and
the reply's field COUNT rather than nodriver's interpolation of the whole reply.
The shape of the answer is that one, one layer out: the raise is a **third
party's**, so what is replaced is not the exception but what the RECORD carries.

---

## 5. The measurement

Driven hermetically against the installed SDK: the real
`StreamableHTTPTransport._handle_sse_event` over an `anyio` memory stream — no
socket, no backend, no Chrome — with a real `LoggingIntegration` and the
**shipped** Sentry client settings. `tests/test_validation_input_echo.py` is
that drive.

### 5.1 The first measurement was wrong, and the shipped setting is why

Run against a default `sentry_sdk.Client`, a marker placed anywhere in the tool
result reached the event. That is **not production**: `observability.sentry_init`
passes `include_local_variables=False` (it has since the third-party-users
finding), so the SDK captures no frame locals — and `sse.data` itself is a local
of `_handle_sse_event`. Re-measured with that one argument, the frame-locals
reach disappears and what is left is the one thing production really ships:
`str(ValidationError)`.

A pin driven without `include_local_variables=False` would have measured a leak
production does not have, and would then have gone green on a fix that changed
nothing. The pin passes it explicitly for that reason.

**And "locals are not a leak path" is a claim with tests behind it, not a
reading of today's config** — which is what makes it safe for this finding to
rest on. Three pins hold that flag, in three different files, each for its own
reason:

* `tests/test_cdp_transport.py::test_the_pii_argument_depends_on_sentry_not_capturing_locals`
  (F-902 review S2) — the link node, written precisely because `cdp_transport`'s
  shape-only rule is defence in depth whose OUTERMOST layer belongs to another
  module. Flipping the flag fails a test in the file that depends on it;
* `tests/test_element_box_exception_repr.py::test_the_sentry_leg_depends_on_include_local_variables`
  (F-912) — the same link for the element-repr leg;
* `tests/test_observability.py` — asserts `sentry_init` really passes it, where
  on its own it "reads as a preference".

F-913 is the third finding to depend on that flag and it does not add a fourth
pin: the two link nodes already fail if it flips, and a pin here would be a
fourth spelling of one fact. What this finding adds is the reason it matters on
THIS leg — the payload is `sse.data`, a local of the very frame that raises.

### 5.2 How much `input_value=` actually quotes — the finding's own §1 was wrong

The stub said the middle truncation "loses neither end — a cookie jar's first
entries and its last are both rendered". Measured, pydantic 2.11.7 caps **each**
echo at a fixed **50 characters of the input**: the first 24, `...`, the last
23. Truncation begins at an input length of 49; above it the echo is a constant
52 characters including quotes, whatever the input's size (measured at 50, 55,
65, 75, 85, 95, 105, 125, 165 and 445 bytes — all 52).

So for a whole JSON-RPC frame the head is **always the envelope**
`{"jsonrpc": "2.0", "id":` and never the jar's first entries. What actually
leaks is:

| Shape | What escapes |
|---|---|
| a frame that arrives CUT (`json_invalid`) | the frame's **last 23 characters** — the END of the tool answer |
| any input value **shorter than 50 characters** | **the whole value**, untruncated |
| a union mismatch | **one echo per arm**, each of the sub-value it tripped over |

Measured on a 273-byte frame whose `id` was `{"tok": "F913_LEAF"}` — the
shipped `LEAF_FRAME` fixture, named so this number has a subject:
**9 errors, 9 echoes, 276 echoed characters** (3 whole-frame echoes of 52 plus
6 short-leaf echoes of 20), and the short leaf rendered in full six times over. `JSONRPCMessage` is a 4-arm union, so one bad frame
reports per arm.

**Every byte count, in one place**, because the shape of this leak is easy to
restate wrongly and the stub already did it once:

| Frame driven | Bytes in | Errors | Echoes | Chars echoed |
|---|---|---|---|---|
| cut `get_cookies` answer (`json_invalid`) | 314 | 1 | 1 | 52 |
| valid JSON, union mismatch on a short `id` (`LEAF_FRAME`) | 273 | 9 | 9 | 276 |
| valid JSON, no arm recognises it | 173 | 4 | 4 | 208 |
| filler payloads at 50 / 55 / 65 / 75 / 85 / 95 / 105 / 125 / 165 / 445 | — | 1 | 1 | **52 each** |

**There is no "a short result is rendered whole" case, and that phrasing should
not survive into the next reading of this finding.** Above 49 bytes the echo is
a flat 52 characters no matter how large the input — the fourth row is that
measurement across a 9× range. Below 49 the echo is untruncated, but a JSON-RPC
envelope alone is already 24 of those bytes, which leaves ~25 for the entire
`result` object: no real tool answer fits. What IS rendered whole is a short
**value** *inside* the frame — row 2 — because each union arm echoes the
sub-value it tripped over and that sub-value is measured against the cap on its
own. That is the distinction the fix is built on, and it is why the pins are
`test_the_echo_is_capped_at_the_measured_number_of_characters` (the frame) and
`test_a_short_value_escapes_the_cap_and_is_echoed_whole` (the value) rather
than one pin about "a short result".

**This moves the finding's severity down and its sharpness up.** Down, because
the volume is bounded at ~50 characters per error rather than "the jar"; up,
because the things that matter are frequently *under* the cap — a cookie value,
a session id, a short bearer token — and those are echoed whole. The two pins
`test_the_echo_is_capped_at_the_measured_number_of_characters` and
`test_a_short_value_escapes_the_cap_and_is_echoed_whole` carry both halves, so
a pydantic release that changes the number goes RED rather than quietly
changing what this finding claims.

**That last sentence was not true of the pin it named until the F-913 review,
and the correction is worth recording because the claim is the whole reason
this section survives a dependency bump.** The first pin asserted `"..." in
echoed[0]` — the PRESENCE of a truncation, not its size. Measured against
synthesised renderings at seven caps, that assertion passes at 52, 80, 100, 200
and 314 and fails only at 20 and 40: it was blind to every cap that leaks MORE
and could only ever have caught one that leaks less, which is the direction
nobody needs protecting from. It asserts the numbers now — the rendering's 52
and, for a `str` input, the 50 characters of the input inside it — so a release
moving pydantic's cap in either direction is RED.

### 5.3 Reach, before the fix

The same drive with `configure_logging` never called, i.e. 2.1.12 exactly:

| sink | before | after |
|---|---|---|
| Sentry **event** (`LoggingIntegration(event_level=ERROR)`) | **leaks** | clean |
| a root handler (a caller's, or the SDK's own) | **leaks** | clean |
| stderr — hence `backend-boot.log` / the client's `mcp-logs-*` | **leaks** | clean |
| Sentry breadcrumb | clean (ERROR is an event, not a crumb) | clean |
| `proxy-<pid>.log` | clean (`stealth.*` has `propagate=False`) | clean |

The RED run of the pin file against the unfixed tree: **12 failed, 23 passed** —
every failure a fix-missing one, and the premise and leak-is-real pins green,
which is what makes the file a pin rather than a wish.

### 5.4 Option 3 was checked first, and it works — and is still rejected

The stub said a library setting beats a rule of ours and should be measured
before either other option. It was. `JSONRPCMessage.model_config["hide_input_in_errors"] = True`
followed by `model_rebuild(force=True)` does suppress the echo (measured: the
marker is gone from `str(exc)`). It is rejected on three grounds, in order of
weight:

1. **It changes the SDK's own control flow.** `:241` sends the very exception it
   just logged downstream (`read_stream_writer.send(exc)`), where
   `BaseSession._receive_loop` hands it to the message handler. Suppressing the
   echo at the model changes what every consumer of that exception sees, not
   just what is logged. The fix that ships replaces only what the **record**
   carries; the live object is untouched and is pinned so
   (`TestTheSdkStillOwnsItsOwnException`).
2. **It covers only the models we enumerate** — `JSONRPCMessage` today,
   `InitializeResult` at `:191`, and every one a future SDK adds. A rule keyed
   on the exception covers them all and any other library's pydantic model.
A third ground was offered — that it costs a schema rebuild in the stdio proxy's
cold start, the process whose whole cost argument is that it imports as little as
possible — and it is **withdrawn**, because it was never measured and does not
survive being measured. `JSONRPCMessage.model_rebuild(force=True)` costs
**0.298 ms** median in a fresh interpreter (n=5, min 0.270, max 0.358; 0.149 ms
median warm, n=20). That is noise against a cold start, and a ground that cannot
carry its own number does not belong beside two structural ones. It is recorded
here rather than deleted so nobody re-offers it.

### 5.5 The fix

**One line of policy: at a site that renders a payload into its EXCEPTION, an
exception that quotes its own input is replaced by a restatement that quotes
none of it and keeps everything else.**

The **rewrite** is a third rule inside `logging_setup`'s existing record factory
— `install_payload_arg_redaction`, still THE one factory install. The **tables
and the matching** join `payload_log_sites`, which F-911 created for exactly
this shape of thing and whose docstring already said the honest next cut was the
rules joining there:

* `PAYLOAD_EXCEPTION_SITES = ("mcp/client/streamable_http.py",)` — the second
  table, beside F-911's `PAYLOAD_LOG_SITES`;
* `_EXCEPTION_SITE_FILENAMES`, derived from it, the cheap first gate;
* `_site_in`, now THE one matching rule, shared by both tables so the
  separator-boundary decision (F-911 review S1) has one home;
* `INPUT_QUOTING_NAMES` / `INPUT_QUOTING_MODULE_ROOTS` — the structural test;
* `WithheldInputError`, `restated_exc_info`, `RESTATED_TEMPLATE`,
  `MAX_RESTATED_ERRORS`, `MAX_LOC_CHARS`.

It is a fourth rule in a family of three, not a fourth mechanism: the factory
runs once, for every record, upstream of filters, handlers, `lastResort` and
Sentry's `callHandlers` patch at once. **No second record factory** — a factory
chain is ordered by install time, so two installs make the outcome depend on
call order, which F-911 §4 states explicitly. **No second `before_send`** — for
F-911's measured reason: `makeRecord` runs upstream of `callHandlers`, so the
Sentry event is built from an already-restated record, and a hook in
`observability` would close one sink of four.

### Why the key is the SITE **and** the exception's structure

Two conditions, both structural, neither textual.

The **structural** half is what keeps this a redaction rather than a blanket: a
`ConnectionError` out of `:574` quotes nobody's input and keeps its text, because
its text is the diagnostic. It is matched on the type's NAME and module ROOT —
`expected_events.Kind.matches`' dotted-boundary rule — and never with
`isinstance`, for `PAYLOAD_ARG_PACKAGE`'s reason: resolving the class needs
`import pydantic` and this leaf is loaded in the stdio proxy. Never on message
text, which F-906 §3 and F-911 §3 both reject and which fails OPEN.

The **site** half is the one a reader is most likely to want to remove, so the
reason is measured rather than argued. A type-only rule would also fire for
`fastmcp/tools/tool_manager.py`'s `logger.exception(f"Error calling tool {key!r}")`,
whose `exc_info` is a pydantic `ValidationError` too — and
`expected_events.CALLER_VALIDATION` recognises that event **by the exception's
type NAME and MODULE**. Substituting it would stop `caller-input` classifying
and re-open the class that was 466 events a week when F-887 measured it. Two
pins hold that door (`TestExpectedEventsIsUnaffected`).

### What survives, and what does not

The restatement keeps the exception's TYPE and defining MODULE, the MODEL being
validated, the error COUNT, the length of the rendering it replaced, and **up to
`MAX_RESTATED_ERRORS` distinct `type`/`loc` pairs, with a count of how many more
there were**:

```
<pydantic_core._pydantic_core.ValidationError for JSONRPCMessage: 1 error(s),
 text withheld: 283 chars; json_invalid at <root>>
```

Built from the library's **own structured accessor** —
`errors(include_input=False, include_url=False, include_context=False)` — not by
cutting pydantic's rendered sentence, so there is no pattern over a third
party's text anywhere in the rule. The `loc` path is a field NAME, which is
F-907's measured safe half (`value=` is the secret, `name=` is the vocabulary);
it is bounded anyway because an `extra_forbidden` error's last segment is a key
the input supplied.

**It is "up to", and the union shape this finding is about OVERFLOWS.** This
section said "each error's `type` slug and `loc` path" until the F-913 review,
full stop, and that sentence was false for the very fixture the finding is built
on. Measured on `LEAF_FRAME`, the 4-arm `JSONRPCMessage` union: 9 errors and
**9 DISTINCT** `type`/`loc` pairs — the dedup collapses none of them — against a
cap of 8, so the shipped restatement really does end `…+1` and one field path is
dropped.

The cap is deliberately **not** raised to fit that fixture. A bound raised to
make a sentence true is raised again for the next fixture, and this tree already
has the precedent for the other answer: `control_state.MAX_OPTIONS` keeps its cap
and gives an `index=` beyond it its OWN message rather than a silence.
`RESTATED_OVERFLOW` is that message here — the loss is visible in the line
itself, to the reader who needs it — so what was wrong was the sentence, and the
sentence is what changed.

The **traceback is handed through unchanged**, so every frame survives.
Measured: the serialized Sentry frame list and the formatted traceback's file
lines are byte-identical before and after
(`test_the_traceback_frames_are_unchanged`). The frame list is the whole of what
that measurement covers — the exception's type and value change by construction,
which is residual 5.

`msg` and `ctx` are dropped, and that cost is §6.

---

## 6. Scope, and residuals

### 6.0 Which legs this closes — all three, and the reason is the key

Said out loud rather than left for a reader to infer, because a fix that closed
one leg of three would be a much weaker change than this one and the two read
identically from the CHANGELOG.

**`:240`, `:394` and `:574` are ALL in scope and all closed by one table entry.**
The rule is keyed on the record's `pathname`, so its unit is the MODULE — F-911's
reasoning, unchanged: a line-keyed rule goes silently inert on the next release,
and one edit above `:240` moves every number below it. `mcp/client/streamable_http.py`
appears once in `PAYLOAD_EXCEPTION_SITES` and that covers the SSE leg (`:240`),
the non-SSE response leg (`:394`), the writer catch-all (`:574`) and any line the
SDK adds to that file later. All three are the same shape — `logger.exception`
with a static literal, no args, at ERROR — and `:574` is the broadest of the
three because it catches whatever escaped the whole writer, including the other
two.

**Measured on all three, and each is load-bearing** — the claim is driven, not
inferred from the key:

| leg | shipped leaks the marker | fixed leaks the marker |
|---|---|---|
| `:240` SSE | yes | **no** |
| `:394` JSON response | yes | **no** |
| `:574` post_writer catch-all | yes | **no** |

Run **without swapping any file**: a record factory is process-global, so only
one can be live at a time. The shipped column is measured with the factory put
back to `logging.LogRecord`, then ours is installed and the fixed column is
measured — in that order, uninstall to the captured original afterwards. Nothing
in the working copy is written, so there is no restore to get wrong (which
matters here: `logging_setup.py` is at its budget, where a botched restore would
not show up as a LOC change). Pinned as
`test_all_three_legs_are_covered_by_the_one_table_entry`, parametrised over the
three line numbers.

`:198` is the one adjacent site that is NOT closed, and it is a different
mechanism rather than a missed leg — see residual 2.

### 6.1 Residuals — what this does NOT cover

1. **The pydantic `msg` is dropped with the input, and `json_invalid`'s column
   number goes with it.** `"Invalid JSON: EOF while parsing a list at line 1
   column 314"` is a genuine diagnostic and it no longer ships; what remains is
   the `type` slug `json_invalid`, which names the same condition in a stable,
   documented token. It is dropped rather than kept because for
   `value_error`/`assertion_error` pydantic's `msg` is literally
   `"Value error, " + str(<the validator's own exception>)` — text a validator
   composed out of the input — and there is no accessor that distinguishes the
   two families without a closed-set claim about pydantic that would go stale.
   `ctx` goes for the same reason, one level down. The trade is deliberate and
   it is the same shape as F-911 §6.3, where `session.py`:478's real diagnostic
   loses its text to module granularity.
2. **`:196` and `:198` are adjacent, and are deliberately NOT in scope.**
   `_maybe_extract_protocol_version_from_message` logs
   `logger.warning(f"Failed to parse initialization response as InitializeResult: {exc}")`
   and then `logger.warning(f"Raw result: {message.root.result}")` — both
   **f-strings**, so the payload is in `record.msg` and this rule, which only
   ever touches `exc_info`, cannot see them. They are F-911's SITE rule's shape,
   and that rule cannot be applied to this file without deleting the static,
   safe message of the three lines this finding IS about (§2). What they carry
   is also different in kind: an `InitializeResult` is the server's own
   capabilities, `serverInfo` and instructions — this product's backend
   describing itself — not a caller's arguments and not a page's content. Named
   here so the next reader does not have to re-derive that they were considered.
   They fire only when our own backend answers `initialize` with something
   FastMCP itself would not build.
3. **`mcp/shared/session.py`:444's `exc_info` is still untouched** — F-911
   residual 2, unchanged. `logging.exception(f"Unhandled exception in receive
   loop: {e}")` has its f-string half withheld by F-911's rule and its traceback
   passed through. It is not in `PAYLOAD_EXCEPTION_SITES` because no
   payload-quoting exception has been MEASURED reaching it: `:358` routes an
   exception arriving on the read stream to `_handle_incoming`, not to that
   handler, so the SSE `ValidationError` this finding is about does not land
   there. Adding it is one line the day one is measured — F-906's "no `uc`
   entry" reasoning.
4. **A chain whose quoting link is not the outermost loses the other links'
   text.** The rule walks the chain, and when ANY link quotes its input the
   whole chain is replaced: the quoting links are restated and the others
   contribute their TYPE only. That is deliberate — a wrapper's own text
   commonly interpolates what it wrapped (`f"...: {e}"`), and a pre-rendered
   string has no half we have measured to be safe (F-911's words) — but it does
   mean a `RuntimeError("could not reach the backend")` wrapping a
   `ValidationError` loses its own sentence. At all three measured sites the
   quoting error IS the outermost exception, so the cost is theoretical today.

   **The walk covers `ExceptionGroup` as of the F-913 review, and that was a
   real hole rather than a completeness flourish.** A group's own `str()`
   carries NONE of its leaves (measured), so the cause/context walk answered
   "nothing quotes its input" and the rule did not fire — while
   `sentry_sdk.utils.exceptions_from_error_tuple` branches on
   `isinstance(exc_value, BaseExceptionGroup)` and serialises every leaf as its
   own `exception.values` entry carrying the whole `input_value=` echo
   (measured, and pinned as
   `test_a_group_that_carries_a_quoting_leaf_is_restated_too`). No site in
   `PAYLOAD_EXCEPTION_SITES` reaches it today — `:240` catches the
   `ValidationError` itself — so it ships as insurance on `element_box`'s
   precedent, where F-907's three sites were measured unreachable and the
   closure shipped anyway; the SDK does run under anyio task groups.

   **`observability._exception_chain` has the identical limitation and is
   deliberately NOT changed.** Until this review, the two walks agreeing was
   `_chain`'s stated reason for being a re-spelling rather than an import; they
   now differ, in one direction, and `_chain`'s docstring says so. That function
   decides which Sentry events `expected_events.classify` DROPS, so widening it
   changes a drop rule rather than a redaction rule — a different question with
   a different blast radius, and not an implied follow-up of this change. It is
   named here so the divergence is a decision on the record and not a drift.
5. **The Sentry issue's exception TYPE and VALUE both change**, from
   `ValidationError` to `WithheldInputError`. What was MEASURED is the frame
   list, which is byte-identical before and after; Sentry's default grouping is
   stacktrace-first, so on that strategy the fingerprint does not move. That is
   as far as the measurement reaches, and the earlier "grouping is essentially
   unaffected" overstated it: a project configured with a grouping strategy or
   a fingerprint rule that reads the exception type or message WILL see these
   events group differently, and nothing here measured that case. The pydantic
   type is the first thing in the new message, so the events remain findable by
   text. Named because a maintainer searching Sentry for `ValidationError` will
   not find these by type, and they were previously titled that way.
6. **F-906's factory residual is unchanged**: a caller who installs their own
   record factory *after* ours replaces it.
7. **The rule costs one `getattr` per record** — `exception_site_of`'s filename
   gate — on top of F-911's. Both are one `replace`, one `rpartition` and one
   frozenset lookup, False for everything, and only then is `record.exc_info`
   read at all.

## 7. Related

* `audit/stage2/finding_F911_root_logger_tool_payload.md` — §6 residual 1, where
  this was measured and named; and the module this fix extends.
* `audit/stage2/finding_F908_sse_starlette_logs_tool_result.md` — the SENDING end
  of the same answer. F-908 capped `mcp.client` at WARNING precisely because
  both ends of one round trip render it; this is the third rendering on that
  leg, above the floor.
* `audit/stage2/finding_F907_nodriver_warning_renders_page_content.md` — the
  "an exception is never shaped" ruling. It is NOT reopened: it is about an
  exception that quotes nobody, where the text is pure diagnostic and a
  traceback renders it anyway. See §4.
* F-902 / `embedded/cdp_transport.py` — the precedent for bounding what an
  unreadable payload may say about itself, decided at the raise.
