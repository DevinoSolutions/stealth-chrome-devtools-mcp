# F-908 — the SSE transport DEBUG-logs every serialised tool RESULT, and only the inherited log LEVEL stands between it and our sinks

**Status:** fixed
**Date:** 2026-09-21
**Area:** `embedded/logging_setup.py` (the observability spine)
**Follows:** F-906, whose mechanism this extends; found by F-906's reviewer as the
completeness gap under the identical premise
**Measured against:** sse-starlette 3.4.4, mcp 1.27.1, fastmcp 2.11.2, starlette 1.0.0,
uvicorn 0.35.0, httpx 0.28.1, httpcore 1.0.9, anyio 4.x, sentry-sdk 2.64.0, CPython 3.13

---

## 1. The finding

F-906 closed the door on what **Chrome said to us**. This is the same door at
the other end of the same request: what **we said back**.

`sse_starlette/sse.py`:362 is one line:

```python
async for data in self.body_iterator:
    chunk = ensure_bytes(data, self.sep)
    logger.debug("chunk: %s", chunk)        # <-- sse.py:362
```

`chunk` is the whole serialised SSE frame. For this backend that frame is the
answer to a `tools/call` — so the line renders `get_cookies`' jar,
`get_page_content`'s HTML, `get_instance_state`'s localStorage, whatever the
tool answered.

Measured, by driving the **real** `EventSourceResponse` over the event dict
`mcp/server/streamable_http.py` builds for a tool answer:

```
sse_starlette.sse DEBUG chunk: b'event: message\r\ndata: {"jsonrpc": "2.0", "id": 3,
  "result": {"content": [], "structuredContent": {"cookies": [{"name": "SID",
  "value": "F908_TOOL_RESULT_COOKIE_VALUE", "domain": ".example.com"}]}}}\r\n\r\n'
```

The logger is `sse_starlette.sse` (from `__name__`), it carries **no level of
its own**, and it **propagates** — so, exactly as in F-906, the only thing
holding it shut is a level inherited from root, and root's level is root's to
give away.

### That the SSE path is the live one — corrected evidence

The brief for this finding offered `_create_json_response`'s `# pragma: no
cover` as proof that the JSON branch is dead and the SSE branch live. **That
does not hold**: `# pragma: no cover` appears on ~40 lines of
`streamable_http.py`, including `_handle_get_request`, `_validate_session` and
`_handle_delete_request`, all of which are unarguably live. The pragma
witnesses nothing about which branch runs.

What does hold is the pair of **defaults**, plus the fact that neither door
onto them is open:

| Claim | Evidence |
|---|---|
| The SDK defaults to SSE | `StreamableHTTPServerTransport(is_json_response_enabled: bool = False)` |
| FastMCP defaults to SSE | `create_streamable_http_app(json_response: bool = False)` |
| We never ask otherwise | AST scan of `src/`: zero calls pass `json_response` or `is_json_response_enabled` as a keyword |
| An inherited env var cannot ask either | `backend_env.scrub` drops the whole `FASTMCP_` prefix (F-890), so `FASTMCP_JSON_RESPONSE=1` never reaches fastmcp's settings |

All four are pinned in `TestTheSseChunkIsTheToolResult::test_the_sse_path_is_the_live_one`.

## 2. The matrix — MEASURED, not reasoned

Same harness as F-906 (that is the point — one mechanism, one measurement
rig): the payload line emitted with its own marker against a real
`RotatingFileHandler` on a tmp log dir, the real `debug_logger` ring, a real
`LoggingIntegration` + capturing transport, and a capture handler on the root
logger. `✗` = the payload arrived.

### Before

| Configuration | `sse_starlette` effective | (a) `backend-<pid>.log` | (b) debug ring | (c) Sentry | (d) root handler / stderr |
|---|---|---|---|---|---|
| shipped backend | WARNING | — | — | — | — |
| shipped proxy | WARNING | — | — | — | — |
| backend `--debug` | WARNING | — | — | — | — |
| `STEALTH_MCP_LOG_LEVEL=DEBUG` | WARNING | — | — | — | — |
| caller `basicConfig(DEBUG)` **before** our init | DEBUG | — | — | — | **✗ sse.py:362** |
| caller `basicConfig(DEBUG)` **after** our init | DEBUG | — | — | — | **✗ sse.py:362** |
| caller `basicConfig(DEBUG)` **+ `--debug`** | DEBUG | — | — | — | **✗ sse.py:362** |

The last row is F-906's review S3 cell, added by `96d7876`: it is the only one
that puts a record through the debug-ring column while records are actually
flowing, and this finding's line is measured in it for free because the sse
marker joined `PAYLOAD_MARKS` rather than getting its own parametrisation.

### After

Every cell `—`. The RED, reproduced with no plugin and no edit to the product
(the family simply was not in the tuple yet):

```
a raw CDP payload reached a sink: durable=[] ring=[] sentry=[]
downstream=['sse_starlette sse.py:362 tool result (DEBUG)']
```

F-906's own four lines are already `—` in that output, which is what makes the
RED name exactly this gap and nothing else.

### Two things the matrix settles

1. **Sentry is not a sink for this one, and that is a level accident, not a
   protection.** `sse.py`:362 is at DEBUG while `LoggingIntegration`'s
   breadcrumb handler sits at INFO, so unlike F-906's `connection.py`:451 this
   payload never became a breadcrumb. One upstream `logger.debug` →
   `logger.info` would change that silently. The floor removes the dependency
   on that accident.
2. **WARNING is today's shipped effective level**, in all four shipped
   configurations — so setting it explicitly changes nothing an operator sees.

## 3. The fix, and why this home

One string added to `logging_setup.PAYLOAD_LOG_FAMILIES`.

Deliberately **not** a second floor function, a `logging.Filter`, or a
`before_breadcrumb` rule. F-906 established the mechanism and its argument
transfers whole: the level is set explicitly on the family **root**,
`getEffectiveLevel` stops at the first ancestor holding a non-`NOTSET` level,
`basicConfig` only ever sets root's — so ours wins in either order — and
`Logger.handle` reaches `callHandlers` only for a record `isEnabledFor` has
already admitted, which puts the level upstream of all four sinks at once,
Sentry included. A second mechanism would be a second home for one decision
(convention 4).

The family is named by its **ROOT** (`sse_starlette`, not `sse_starlette.sse`),
for F-906's reason: the package builds its logger from `__name__`, and there
is exactly one `getLogger` call in the whole package (`sse.py`:59), so the root
covers it and anything added under it later.

**What the floor costs here is less than it cost for nodriver.** F-906 had to
argue that WARNING preserves nodriver's real diagnostics (`connection.py`:483).
`sse_starlette` has **no call at WARNING or above anywhere in the package**
(AST census), so there is not one diagnostic for the floor to stand in front of.

## 4. The census — and why each other family is OUT

The brief asked for exactly the loggers the census proves render payloads, and
for the in/out argument per family. An AST scan of every `logger.debug` /
`.info` / `.log` call in each installed package — **never a grep**, because a
text search's false negative here reads as proof that a library is silent,
which is precisely how a family gets left out.

| Family | Calls < WARNING | Renders a tool result or tool arguments? | Verdict |
|---|---|---|---|
| `sse_starlette` | 4 | **YES** — `sse.py`:362, the whole serialised answer | **IN** |
| `starlette` | **0** | nothing to render | OUT |
| `anyio` | **0** | nothing to render | OUT |
| `uvicorn` | 55 | No. The only whole-ASGI-message logger is `MessageLoggerMiddleware`, which replaces `body`/`bytes`/`text`/`headers` with `<N bytes>` **before** logging (`message_with_placeholders`), so it cannot render a payload even when enabled; it logs at TRACE (5), which `basicConfig(DEBUG)` does not admit; and the access log is already off (`backend_uvicorn_config`, F-830) | OUT |
| `httpx` | 2 | No — method + full URL at INFO. The only httpx conversation in this tree is proxy/CLI → our own backend on loopback | OUT |
| `httpcore` | 2 | No. `_trace.py`:47/87 render the trace's `info`; `receive_response_headers.complete` carries response **headers**, but the two BODY traces (`receive_response_body`, `response_closed`) set no `return_value`, so their message is the trace **name** alone — measured. A tool result cannot escape here | OUT |
| `mcp` | 109 | **Measured, and the answer is no** — see below | OUT |
| `fastmcp` | 110 | **Measured, and the library already shields it** — see below | OUT |
| `urllib3` | — | Not on the backend's request path (it is `requests`, under `cdp_element_cloner` / `desktop_launch`); renders method + path + status, never a body | OUT |

Each OUT row is pinned in `TestTheFamiliesDeliberatelyLeftOut`, so a dependency
bump that makes one of them start rendering payloads fails there. That is the
only thing that keeps "we checked" from decaying into "we assumed".

### Why `mcp` is out

`mcp/server/lowlevel/server.py`:676 is `logger.debug("Received message: %s",
message)` over `session.incoming_messages` — the whole incoming message, and it
**is** admitted at DEBUG under a caller's `basicConfig`. On level alone it
would be IN. It is out because of what `%s` actually renders:

```
RequestResponder.__repr__ owner: object.__repr__
RequestResponder.__str__  owner: object.__str__
rendered: <mcp.shared.session.RequestResponder object at 0x...>
SECRET present: False
```

A **request** — the `tools/call` with its arguments — arrives there as a
`RequestResponder`, which defines neither dunder, so nothing escapes. A
**notification** on the same line does render its whole pydantic model, but the
server's incoming notifications are `initialized` / `cancelled` / `progress`,
none of which carries tool data in this product.

Capping the family would therefore close no door and would silence the SDK's
own INFO diagnostics (`Processing request of type %s`, session lifecycle).

### Why `fastmcp` is out

`fastmcp/server/server.py`:672 is `logger.debug("Handler called: call_tool %s
with %s", key, arguments)` — a genuine tool-ARGUMENT payload line. It is out
because **the library already closes it**, measured:

```
FastMCP                       own=INFO  propagate=False handlers=['RichHandler']
FastMCP.fastmcp.server.server own=NOTSET propagate=True
  effective under root DEBUG: INFO   -> isEnabledFor(DEBUG) == False
```

`fastmcp/utilities/logging.get_logger` prefixes every name with `FastMCP.`, and
`fastmcp/__init__.py` configures that root at import. So a caller's root DEBUG
never reaches it. This confirms the brief's claim, with one correction: the
logger is **`FastMCP.fastmcp.server.server`**, not `fastmcp.server.server`. The
bare `fastmcp` family also exists and *does* inherit root — but it holds no
payload line, so capping it would be F-906's rejected `uc` entry spelled
differently: a name the payload does not live on.

Pinned as a premise, so an upstream change that drops either half goes RED and
re-opens the family question.

## 5. Pins

`tests/test_nodriver_payload_logging.py` is renamed
**`tests/test_payload_log_floor.py`**. It pins one function,
`apply_payload_log_floor`; a third family would otherwise have opened a second
home for one mechanism's evidence.

**35 nodes** after merging F-906's review round (`96d7876`, which brought that
file from 22 to 25 and is where the `reset_logging` docstring and the
seventh matrix cell come from).

Extended, never duplicated:

* the sse line joins `PAYLOAD_MARKS`, so all **six** configuration cells and the
  diagnostics-survive pins cover it without a new parametrisation;
* `emit_real_sse_tool_result` drives the **real** `EventSourceResponse` rather
  than copying `sse.py`:362's call the way the nodriver lines are copied —
  nodriver's line needs a live Chrome, this one is three statements from a
  plain async generator, so the pin can afford the library's own code path.
  It also asserts the frame really went out on the wire, so the absence of a
  marker can never be the absence of a frame (a vacuous pass);
* **F-906's Sentry premise grew its negative half**, and F-908 and F-906's own
  review round reached that conclusion independently. The premise is not "the
  SDK patches `callHandlers`" but "the SDK patches **nothing upstream of**
  `isEnabledFor`": a presence-only assertion stays green if a bump keeps that
  patch and adds a `Logger.handle` / `_log` / `makeRecord` hook beside it.
  `96d7876`'s wording is the one kept — it is already under review and it also
  states exclusivity **positively**, asserting `setup_once` binds exactly one
  name — and this finding's duplicate was dropped rather than merged, because
  it asserted nothing the surviving pin does not. The pin is SHARED across both
  findings: one mechanism, one place its foundation is measured;
* `test_the_families_named_are_the_families_that_exist` now walks all three
  families and asserts each is `__name__`-derived.

New: `TestTheSseChunkIsTheToolResult` (the live-path premise, the below-the-floor
premise, and the end-to-end render) and `TestTheFamiliesDeliberatelyLeftOut`
(the census's negative half, one pin per OUT family).

## 6. Residuals

1. **`mcp/shared/session.py`:383-384 — RECORDED, not fixed, and not fixable by
   this mechanism.** Both use **module-level `logging.warning` / `logging.debug`**,
   i.e. the **ROOT** logger — so no family cap can reach them however
   `PAYLOAD_LOG_FAMILIES` grows.

   :384 (`f"Message that failed validation: {message.message.root}"`) renders
   the full request **including arguments**; it is at DEBUG, so it needs a
   caller's `basicConfig` like everything else here. :383
   (`f"Failed to validate request: {e}"`) is at **WARNING**, i.e. **reachable
   in the shipped configuration** through `logging.lastResort` → stderr →
   `backend-boot.log`, and through Sentry's INFO breadcrumb handler. What it
   carries is pydantic's own rendering of the `ValidationError`, which includes
   an `input_value=` echo — **truncated in the middle** by pydantic, measured:

   ```
   Failed to validate request: 1 validation error for CallToolRequest
   params.name  Field required [type=missing,
     input_value={'arguments': {'script': ...IN_A_VALIDATION_ERROR'}}, input_type=dict]
   ```

   So it is a **partial** echo of a caller's arguments on the validation-failure
   path only. Both lines are pinned as premises, so if either ever moves onto
   `mcp.shared.session` the family question re-opens. Closing :383 would need a
   root-logger filter or a `before_breadcrumb` rule — a different mechanism
   with a different argument, and deliberately out of scope here.

2. **`fastmcp/server/server.py`:672 is NOT reachable** — recorded per the
   brief, confirmed, and the logger name corrected to `FastMCP.fastmcp.server.server`.
   It is unreachable only because fastmcp configures its own family at import;
   a fastmcp bump that stops doing so re-opens it, which is why the premise is
   a pin and not a sentence.

3. **The same residual F-906 named, now over three families.** A caller who
   writes `logging.getLogger("sse_starlette").setLevel(DEBUG)` still gets
   DEBUG. That is them asking for this library's frames **by name**, which is a
   different act from turning DEBUG on globally, and the floor deliberately
   does not override it.

4. **`httpcore` renders response HEADERS at DEBUG.** Out of scope for this
   finding (neither a tool result nor tool arguments), and on this tree's only
   httpx conversation — loopback to our own backend — those headers are ours
   (`mcp-session-id`), not a page's. Worth its own finding only if an httpx
   client of ours ever talks to a third party.

5. **`reset_logging()` in the pin file is still a global mutation with no
   restore** (F-906 review N2, whose `96d7876` gave it a docstring saying so).
   Unchanged by this work, and the slice was run with the mutating file both
   **first** and **last** to confirm order-independence. `pytest-randomly` is
   not installed in this venv, so this remains latent rather than live.

   F-908 does add one interaction worth naming: `reset_logging` wipes the
   `FastMCP` root that `fastmcp/__init__.py` configures at import, so
   `test_the_fastmcp_argument_line_is_unreachable_from_root` re-runs the
   library's **own** configurator before asserting. That is the honest shape —
   the premise under test is "fastmcp still shields its own family", not "this
   process happened to be configured" — but it is a second reason to replace
   the reset with a snapshot-and-restore if random ordering ever arrives.

6. **F-907 is the third line this floor sits below**, and the three are
   deliberately separate findings rather than one widened cap: F-907's
   `element.py` WARNINGs and `mcp/shared/session.py`:383 are both **above** the
   floor and reachable as shipped, and each needs a mechanism this one does not
   provide — an args-side filter for F-907 (its records carry the element in
   `record.args`), a root-logger rule for `session.py`. Raising
   `PAYLOAD_LOG_FLOOR` over either would silence real diagnostics, which is the
   trade all three findings refuse.
