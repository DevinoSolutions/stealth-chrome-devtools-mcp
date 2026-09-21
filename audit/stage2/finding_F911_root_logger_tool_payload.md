# F-911 — the MCP SDK logs a caller's arguments and whole messages on the ROOT logger, where no level floor and no argument rule can reach them

**Status:** fixed
**Date:** 2026-09-21
**Area:** `embedded/logging_setup.py` (the observability spine)
**Follows:** F-906 / F-908 (the level floor) and F-907 (the record factory). Named as
their follow-up in F-908 §6; this is the fourth door of one family
**Measured against:** mcp 1.27.1, pydantic 2.11, sentry-sdk 2.64.0, CPython 3.13.11,
Windows 11

---

## 1. The finding

`mcp/shared/session.py` logs with the module-level `logging.warning` /
`logging.debug` / `logging.exception` **functions**, not through a logger of
its own. So the records it makes are the **root logger's**, and both mechanisms
this file already owns miss them — not narrowly, but structurally:

* **`PAYLOAD_LOG_FAMILIES` is inert.** F-906/F-908's floor works by setting an
  explicit level on a family ROOT, which beats `basicConfig` because
  `getEffectiveLevel` stops at the first ancestor carrying one. A record on
  `root` has no family to name, and root is *the* ancestor — so there is no
  entry that tuple could grow to cover this, and "cap root" is not the missing
  entry either: root's level belongs to the caller, and lowering it silences
  the whole process.
* **F-907's argument rule is blind.** Every one of these lines is an
  **f-string**, so the payload is interpolated before `logging` is called:
  `record.msg` is a finished string and `record.args` is `()`. This is the
  identical shape that made a filter impossible at `connection.py`:451 in
  F-906, arriving at the one mechanism F-906 chose instead.

The three lines, and what each carries:

```python
# mcp/shared/session.py, in BaseSession._receive_loop
383:  logging.warning(f"Failed to validate request: {e}")
384:  logging.debug(f"Message that failed validation: {message.message.root}")
430:  logging.warning(
431:      f"Failed to validate notification: {e}. Message was: {message.message.root}"
432:  )
```

`e` is a pydantic `ValidationError`, whose `str()` echoes `input_value=` — the
caller's own arguments — for every failing arm of the union. `message.message.root`
is the whole `JSONRPCRequest` / `JSONRPCNotification`, rendered by pydantic's
model repr: **not truncated**, because the f-string renders the model rather
than pydantic's error formatter.

**Two of the three are at WARNING, so this needs no `basicConfig` from anybody.**
That is what separates it from F-906 and puts it beside F-907: it is live in
the plain shipped backend and the plain shipped proxy.

And the SDK arranges its own sink. `logging.warning` at module level is:

```python
def warning(msg, *args, **kwargs):
    if len(root.handlers) == 0:
        basicConfig()
    root.warning(msg, *args, **kwargs)
```

Measured: in a process where root deliberately carries no handler, one such
call takes `root.handlers` from `[]` to `[<StreamHandler>]` — a dependency
permanently installing a stderr handler on our root. For the backend, stderr is
redirected into `backend-boot.log`, a durable file. At WARNING these are also
Sentry breadcrumbs on the next event (`LoggingIntegration`'s breadcrumb handler
sits at INFO).

---

## 2. The measurement

Driven hermetically against the installed SDK: the real
`BaseSession._receive_loop` over two `anyio` memory object streams — no socket,
no backend, no Chrome. `tests/test_root_logger_payload.py::emit_payload_lines`
is that drive; what follows is its output.

### The records, as they arrive

```
name='root' WARNING mod='session':383 args=()
  msg="Failed to validate request: 31 validation errors for ClientRequest
       … InitializeRequest.params.protocolVersion
         Field required [type=missing,
           input_value={'tok': 'F911_REQUEST_ARGUMENT_PAYLOAD'}, input_type=dict]"

name='root' DEBUG   mod='session':384 args=()
  msg="Message that failed validation: method='x/unknown'
       params={'tok': 'F911_REQUEST_ARGUMENT_PAYLOAD'} jsonrpc='2.0' id=1"

name='root' WARNING mod='session':430 args=()
  msg="Failed to validate notification: 12 validation errors for ClientNotification
       … Message was: method='notifications/unknown'
         params={'tok': 'F911_NOTIFICATION_PAYLOAD'} jsonrpc='2.0'"
```

Three facts are load-bearing and all three are pinned rather than asserted:
`record.name` is `root`; `record.args` is empty; and the module is uniformly
root-logging (an AST read of the installed file, so an SDK that gives it a
module logger makes the premise RED and the rule retirable).

`:383`'s echo is **middle-truncated** by pydantic, so a long argument dict can
hide a secret in the ellipsis while a short one does not. The pins use short
params deliberately — a long one would make them pass for a reason that has
nothing to do with the fix.

### Reach, before the fix

The same drive with `configure_logging` never called (i.e. 2.1.12 exactly),
over four configurations — `backend`, `proxy`, and a caller's
`basicConfig(DEBUG)` in both orders:

| sink | before | after |
|---|---|---|
| a root handler (a caller's, or the SDK's own `basicConfig` one) | **leaks** | clean |
| stderr with root bare — hence `backend-boot.log` | **leaks** | clean |
| Sentry breadcrumb on the next event | **leaks** | clean |
| Sentry event | clean (WARNING is a breadcrumb) | clean |
| `backend-<pid>.log` | clean (`stealth.*` has `propagate=False`) | clean |

The RED run of the pin file against the unfixed tree: **13 failed, 13 passed**,
with `test_no_root_logger_payload_reaches_any_sink` red in all four cells.

### The census behind the scope

An AST pass over the whole installed `mcp` package: **206 logging calls, 11 of
them on the root logger.** The eleven:

| site | level | carries |
|---|---|---|
| `shared/session.py`:383 | WARNING | caller's arguments (`input_value=`) |
| `shared/session.py`:384 | DEBUG | the whole request |
| `shared/session.py`:430 | WARNING | the whole notification |
| `shared/session.py`:422 | ERROR | an exception from a progress callback |
| `shared/session.py`:440 | DEBUG | static text |
| `shared/session.py`:444 | ERROR | an arbitrary exception, plus `exc_info` |
| `shared/session.py`:478 | WARNING | a response id |
| `client/session_group.py`:383/:393/:404 | WARNING | an exception from a list call |
| `server/sse.py`:193 | DEBUG | a session id |

Every other module in `mcp` logs through a **named** logger, so it is
`PAYLOAD_LOG_FAMILIES`' question and not this one.

---

## 3. Root cause

A third party chose the root logger, and every mechanism we own is keyed on
something a root record does not have — a family name, or an argument. The
payload is not *hidden* from us; it is simply in the one field neither rule
reads, on the one logger no floor can name.

The second-order half matters as much as the first: because the SDK's own call
installs a root handler when there is none, this is not a leak that waits for a
caller to misconfigure something. It creates its own sink, in a process whose
entire log design is built on root carrying nothing.

---

## 4. The fix

**One line of policy: a record MADE BY a payload-rendering third-party module
has its rendered text withheld, and keeps everything else.**

The **rewrite** lives in `logging_setup`'s existing record factory —
`install_payload_arg_redaction`, which is now THE one factory install carrying
**two rules**. The **table and the matching** are a new leaf,
`embedded/payload_log_sites.py`:

* `PAYLOAD_LOG_SITES = ("mcp/shared/session.py",)` — the table;
* `_SITE_STEMS`, **derived** from it, the cheap first gate;
* `site_of(record)` — which entry made this record, or `None`;
* `withheld(site, record)` — the replacement text;
* `WITHHELD_TEMPLATE` — `<mcp/shared/session.py:430 message withheld: 1847 chars>`.

It is the SITE half of a trio, and the trio is the point: a third party's log
line is held down by **LEVEL** (`PAYLOAD_LOG_FAMILIES`, when the library names
its own logger), by **ARGUMENT** (`PAYLOAD_ARG_PACKAGE`, when the payload rides
in `record.args` as an object), and by **SITE** — here, when neither can.

It is a module rather than three more screens of `logging_setup` because this
is the one of the three rules that is a **registry**: a table plus the matching
logic that reads it, which any future root-logging third-party module joins.
The **timing** was the 1000-LOC budget, which this finding's addition crossed
and which **ratchets down only** — so the cut is this question's surface rather
than a raised cap (`cli_call` → `cli_render`'s precedent; `logging_setup` lands
at 947). The leaf decides nothing about when it is asked: the record arrives as
an argument, there is no install and no state, and it imports stdlib only, so
the stdio proxy pays nothing for it. If `logging_setup` grows again, the honest
next cut is the other two rules joining it — which would make it THE one home
for "what a third party's log line may carry" entire. That was declined *now*
only because F-906/F-907's public names are referenced from 15+ call sites in
their own pin files, which a concurrent lane is editing.

### Why the record factory and not a root `logging.Filter`

Both were measured, and the answer is *not* the one F-907 gives. F-907 rejected
a filter because `Logger.handle` consults only the filters of the logger the
call was made **on**, so a filter on the family root never fires for a
descendant's record. Here the call **is** on root, so a root filter *would*
fire — and it would reach Sentry too, since `LoggingIntegration` patches
`Logger.callHandlers`, which `handle` only reaches after `self.filter(record)`
has passed.

It is declined anyway, on convention 4 rather than on mechanism: it would be a
**second mechanism answering one question** — "what may a third party's log
line carry" — one import from the first, covering strictly less (only records
made on root), and needing its own idempotency mark, its own install site and
its own entry in `tests/logging_state.py`'s snapshot/restore. The factory
already runs for every record in the process, from any logger, and is upstream
of filters, handlers, `lastResort` and Sentry at once.

### Why no `observability.before_breadcrumb`

Measured, not argued: `Logger.makeRecord` runs upstream of
`Logger.callHandlers`, so the breadcrumb `LoggingIntegration` builds is built
from an already-withheld record. A hook in `observability` would be F-906's and
F-907's rejected second home, closing one sink of four, and could only ever
matter if the factory were removed. **Both halves of that premise are pinned** —
that `callHandlers` is still what the SDK patches, and that nothing has patched
`makeRecord` — so an SDK that moved its hook upstream goes RED here instead of
quietly re-opening the door.

### Why the unit is the MODULE

Not the message text: the library may reword every one of these lines, and
F-906 already ruled that pattern-matching a dependency's strings is not a
mechanism. Not the line number: it is the least stable thing in a dependency —
one edit above moves every number below — and a rule keyed on 383/384/430 would
go silently inert on the next release. Not `record.name`, which is `root` and
says nothing.

A file path is what F-906's family root is, one level finer: the library's own
unit of organisation, and a fact it cannot change without the module ceasing to
exist — at which point the entry is inert and **visible**, because the pins read
the installed SDK's source.

Matched on `record.pathname`, normalised to forward slashes so one spelling
serves both platforms, behind a `record.module` stem gate so the common case is
one frozenset lookup against a string the stdlib already computed. The stem set
is derived from the module list rather than typed beside it, because two
spellings of one fact is how a rule comes to cover nothing.

### Why `mcp/shared/session.py` alone

`client/session_group.py`'s three root WARNINGs render an exception from
`list_tools` / `list_prompts` / `list_resources` — but nothing in this tree
constructs a `ClientSessionGroup`, so naming it would be a claim about a door
this product does not have (F-906's "no `uc` entry" reasoning). `server/sse.py`:193
carries a session id, which is not payload.

### Withholding is not silencing

The record still arrives, on the same logger, at the same level, naming its own
module and line — which is the whole of what makes such a record actionable: an
operator reading `<mcp/shared/session.py:430 …>` knows exactly which condition
fired and can read the SDK's source for the wording. The character count says
how much was dropped without saying any of it (`_shape`'s `children=` rule).

Nothing else survives, and that asymmetry with F-907 is deliberate: an
`Element` has a safe half (its tag and attribute NAMES) and an unsafe half (the
values and the text); a **pre-rendered string** has no half we have measured to
be safe.

### Two mechanical details that would have been outages

* **`record.args` is cleared with the message.** `getMessage()` runs
  `msg % args`, so leaving a `%s`-carrying tuple beside a message that no
  longer has a `%s` raises `TypeError: not all arguments converted` inside
  every handler that formats. `:422` is exactly that shape. Pinned through a
  real handler, which is where it would have bitten.
* **`withheld` never runs the `%`.** Interpolating to measure the length would
  mean executing a third party's format pair inside `Logger.makeRecord` — the
  failure `_shape`'s handler exists for, avoided by not doing it rather than by
  catching it. The length is `len(str(record.msg))`, which for all three
  measured lines *is* the whole rendered text.

`site_of` has **no** `except`, and that is a claim: unlike `_shape` it calls no
library code, only attributes `LogRecord.__init__` computed itself.
`record.module` is always a `str` (that constructor sets `"Unknown module"`
from its own handler), and the one value a caller controls, `pathname`, is
tested for `str` rather than coerced.

---

## 5. Pins

`tests/test_root_logger_payload.py` — 30 nodes, all hermetic, all under
`tests/logging_state.owned()`.

* **`TestTheRootLoggerDoorIsReal`** — the premises, read off the installed SDK:
  the module is uniformly root-logging (AST, so a bump that gives it a module
  logger goes RED and the rule can be retired); a driven record's `name` is
  `root` and no family prefix can reach it; `record.args` is empty for all
  three lines; and `logging.warning` module-level really does install a handler
  on a bare root.
* **`TestTheLeakIsReal`** — the same drive with `configure_logging` never
  called, asserting the payload *does* reach a root handler and *does* reach
  Sentry. A pin for a fix must first be able to fail.
* **`TestNoPayloadReachesAnySink`** — the finding in one assertion over four
  sinks × four configurations, plus the clean-breadcrumb cell and the Sentry
  premise pin behind it.
* **`TestWithholdingIsNotSilencing`** — the record survives at its own level and
  site; `:478`, a real diagnostic on the same module, keeps its site; our own
  records are untouched.
* **`TestTheKeyIsTheSiteAndNotTheText`** — a reworded line is still withheld; a
  third party's own `session.py` elsewhere on disk is not.
* **`TestTheShippedStderrPath`** — root stripped bare, so the SDK's own
  `basicConfig` handler is the sink, which is the production path into
  `backend-boot.log` and the one the `_RootCapture` harness cannot see. Both
  halves: leak visible unconfigured, gone configured.
* **`TestWithholdingCannotBreakFormatting`** — the `%`-args and single-mapping
  cases, driven through a real handler.
* **`TestTheRuleIsDerivedAndIdempotent`** — the stem gate is derived; installing
  twice chains one factory (`server.py` runs three times under runpy); and
  F-907's argument rule still fires, so the two rules compose.

Full related suite (every file importing `logging_setup`, 18 files):
**406 passed, 1 skipped.**

---

### 5.1 The gate run that the Windows lane could not have caught (run 35624857318)

`8efde7e` went red on every Linux and macOS cell of the release gate on
`test_both_host_pathname_flavours_match` while the Windows pre-push lane was
green. `site_of`'s cheap first gate was `record.module`, a value
`LogRecord.__init__` computes with the HOST's `os.path.basename` — which on
POSIX does not split on a backslash, so a Windows-shaped pathname there yields
the whole string as its "module" and the gate refused the record before the
explicit `\` → `/` normalisation ever ran. The fix reads the FILENAME off the
same normalised string (`_SITE_FILENAMES`), so both reads share one spelling
and the host's path flavour cannot reach the decision. The pin
`test_the_stem_is_read_off_the_normalised_path_not_record_module` gives the
constructor POSIX's `basename` on every host (a no-op on POSIX; on Windows the
exact reading those cells computed) and was measured RED against `8efde7e`
before the change — a `record.module` override AFTER `makeRecord` is NOT a
valid RED, because the record factory runs inside `makeRecord`. Same blind
spot as F-903's Windows-green/POSIX-red round: a change to how a PATH is read
must be driven under both flavours on the host that cannot produce the other.
## 6. Residuals — what this does NOT cover

1. **`mcp/client/streamable_http.py`:240 — a tool RESULT in a Sentry EVENT, and
   it is the sharpest thing this finding leaves open. Filed as
   `audit/stage2/finding_F913_validation_error_echoes_tool_result.md`.**
   `logger.exception("Error parsing SSE message")` has a **static** message, so
   this rule sees nothing to withhold, and empty `args`, so F-907's rule sees
   nothing either — but its
   `exc_info` carries a pydantic `ValidationError` whose `input_value=` echoes
   the **SSE data**, which on the proxy leg is the answer to a `tools/call`.
   Measured: the marker reaches the formatted traceback. It sits at **ERROR**,
   so F-908's WARNING floor on `mcp.client` does not reach it *and*
   `LoggingIntegration(event_level=ERROR)` ships it as a full Sentry **event**,
   not a breadcrumb. `:394` (`"Error parsing JSON response"`) and `:574` are the
   same shape on the other legs.
   It is left because it needs a **third** mechanism, not this one: the payload
   is inside an exception, and this file's settled position since F-907 is that
   an exception is a diagnostic and is never shaped — so closing it means
   deciding, at the `observability` scrubber or at `cdp_transport.CdpReplyError`'s
   precedent, what a pydantic validation error may say about its input. That is
   a finding, not a line — **F-913**, which carries this measurement, the reason
   each of the three mechanisms is structurally blind to it, and the candidate
   homes.
2. **`session.py`:444's `exc_info` is untouched.** Its f-string half is
   withheld, but `logging.exception` passes the traceback, and every sink that
   formats one renders the same exception text — so for that line the rule
   withholds nothing. Consistent with F-907's exception clause and stated rather
   than hidden. The three lines the finding is *about* carry no `exc_info` at
   all (measured), so they are covered whole.
3. **The diagnostics on this module lose their text.** `:478`'s "Response ID X
   cannot be normalized" and `:440`'s "Read stream closed by client" are
   withheld with the rest, and `:422` loses its exception's `str()` (it has no
   `exc_info` to carry it). The site survives in every case. This is the price
   of module granularity, taken deliberately over the two alternatives, both of
   which are worse: a line-keyed rule goes inert on the next release, and a
   text-keyed one is the pattern-match F-906 condemns. `:422` is additionally
   unreachable in this product — nothing here passes a `progress_callback`.
4. **The `basicConfig()` self-install is recorded, not prevented.** A dependency
   permanently adds a `StreamHandler` to our root the first time one of these
   lines fires. It does not change *whether* a root WARNING reaches stderr
   (`logging.lastResort` already did that at the same level), but it is a
   process-wide mutation made on our behalf, and preventing it would mean owning
   a root handler ourselves — a much larger policy change than this finding
   justifies. Pinned so it is visible.
5. **`client/session_group.py` is named and left.** Three root-logger WARNINGs
   rendering an exception from a list call. Nothing here constructs a
   `ClientSessionGroup`; if anything ever does, the module joins the table and
   nothing else changes.
6. **F-906's factory residual is unchanged**: a caller who installs their own
   record factory *after* ours replaces it, and a caller who writes
   `logging.getLogger("mcp").setLevel(DEBUG)` is asking for that library's
   output by name, which is a different act from turning DEBUG on globally.
7. **The installer's NAME is narrower than the function.**
   `install_payload_arg_redaction` now carries an argument rule and a site rule.
   It is kept because what must never be duplicated is the **install** — a
   factory chain is ordered by install time, so two installs make the outcome
   depend on call order — and because renaming it churns 15 call sites in
   F-907's pin file, which a concurrent lane is editing. Cosmetic, and worth
   doing when the lanes merge.
