# F-907 — nodriver renders a page ELEMENT into its own WARNING text, above F-906's floor

**Status:** fixed
**Date:** 2026-09-21
**Area:** `embedded/logging_setup.py` (the observability spine)
**Follows:** F-906 (the payload log FLOOR), whose §5 residual 2 named this
**Measured against:** nodriver 0.47.0, websockets 16.0, sentry-sdk 2.64.0, CPython 3.13.11

---

## 1. The finding

F-906 held `nodriver` and `websockets` at WARNING because everything below that
line quotes raw CDP. **This is the half above it.**

`nodriver/core/element.py` logs a live `Element` at WARNING, three times:

| Site | Level | Call |
|---|---|---|
| `element.py`:537 | `WARNING` | `logger.warning("could not calculate box model for %s", self)` — in `Element.mouse_click` |
| `element.py`:624 | `WARNING` | the same, in `Element.mouse_drag` (the drag SOURCE) |
| `element.py`:633 | `WARNING` | the same, for `destination` (the drag TARGET) |

and `Element.__repr__` (`element.py`:1131-1158) renders **three** things, not
the two F-906's residual note named:

```python
attrs = " ".join([f'{k if k != "class_" else "class"}="{v}"' for k, v in self.attrs.items()])
s = f"<{tag_name} {attrs}>{content}</{tag_name}>"
```

* the **tag**;
* **every attribute as `name="value"`** — including an `<input type=password>`'s
  `value=`, and any `data-*` carrying a session token;
* and `content`, the element's **whole recursive TEXT CONTENT**, built by
  `str(child)` over every child (a text node's `__repr__` returns its raw
  `node_value`). F-906 §5 said "tag and attributes"; the text half was not
  known then and is the larger surface.

Measured, on an element built from nodriver's own constructors:

```
<input type="password" value="hunter2-SECRET"
       data-session-token="eyJhbGciOiJIUzI1NiJ9.TOKEN" class="form-control"></input>
<div id="balance">Your balance is $12,345.67</div>
```

`Tab.__repr__` (`tab.py`:1987-1992) is the same shape one object up — it
renders `self.target.url`, and a URL carries tokens in its query string.

### Reachability — the F-906 review said "hot path"; MEASURED, it is not

The review reported these three as an every-user leak: `dom_handler.py`:273
calls `element.mouse_click()` on every `click_element`, and the warning
"fires on exactly the zero-size / not-rendered case". **The first half is true
and the second is not**, and the correction is recorded here because it moves
this finding's severity **down**, not up.

`if not center:` cannot open through a real `Position`:

```
zero-size quad at origin   center=(0.0, 0.0)      truthy=True   w=0 h=0
zero-size quad offscreen   center=(10.0, 20.0)    truthy=True   w=0 h=0
ordinary box               center=(50.0, 25.0)    truthy=True
negative/offscreen         center=(-450.0, -475.0) truthy=True
```

`Position.center` is `(left + width/2, top + height/2)` — a non-empty 2-tuple,
so **always truthy**, however degenerate the box.

And whichever branch `get_position` takes, the warning is not reached anyway:

* `if not quads: raise Exception(...)` (`element.py`:499) — not an
  `AttributeError`, so it propagates straight past `mouse_click`'s
  `except AttributeError` and out of the call;
* the `except IndexError` branch (`element.py`:509-513) DEBUG-logs and returns
  `None`, and `mouse_click`'s `except AttributeError: return` swallows that
  **before** the warning line.

So **the three box-model WARNINGs are unreachable in nodriver 0.47**. This fix
is insurance against one nodriver change, not a patch for a live every-user
leak, and `tests/…::TestReachability` goes RED the day that change lands —
which is when the severity really does rise.

### What IS live on that same code path, and is not a log line

`element.py`:499 raises `Exception("could not find position for %s " % self)`.
Measured, the message carries the whole repr:

```
could not find position for <input type="password" value="SECRET-VALUE"></input>
```

That is an **exception**, not a `LogRecord` — it propagates out of
`mouse_click` into `dom_handler.click_element`, where it becomes a `ToolError`
reaching the client, the debug ring and Sentry as the exception itself. **No
logging mechanism can reach it**, so it is out of scope here and is named as
residual 7 rather than quietly folded in.

### The census — every WARNING-or-above call in the installed sources

Swept with `Select-String` over the venv's `site-packages` (ripgrep honours
`.gitignore`, and `.venv` is ignored — a `Grep` sweep here answers "no matches"
for files that do match). Every `logger.warning` / `.error` / `.critical` /
`.exception` in `nodriver` 0.47.0, and the below-the-floor neighbours worth
naming so a later reader does not re-derive them:

| Site | Level | What it renders | Reaches |
|---|---|---|---|
| `element.py`:537 | WARNING | **`Element.__repr__`** — tag + every attribute VALUE + all recursive child TEXT | every sink the level reaches; **this finding** |
| `element.py`:624 | WARNING | the same, the drag SOURCE | ditto |
| `element.py`:633 | WARNING | the same, the drag TARGET | ditto |
| `connection.py`:483 | WARNING | a callback's `repr`, the event CLASS NAME, and `str(exc)` + `exc_info` | passes through UNCHANGED — but **not** because its arguments are the library's (see below) |
| `tab.py`:1702 | WARNING | a constant "install opencv-python" string, no args | nothing to redact |
| `tab.py`:1750, :1757 | WARNING | constant "could not unlink …" strings, no args | nothing to redact |
| `element.py`:499 | — | `Exception("could not find position for %s " % self)` — the whole repr | **not a `LogRecord`**; no logging mechanism reaches it → residual 7 |
| `element.py`:510 | DEBUG | the repr again, but `%`-interpolated INTO the message | below F-906's floor, and `record.args` is empty, so an args rewrite could never have reached it either |
| `util.py`:4599-4600, :4603 | INFO | the proxy URL — `socks5://user:pass@host`, i.e. **credentials** | below F-906's floor; closed by F-906, not by this |
| `mcp/shared/session.py`:383 | WARNING | `f"Failed to validate request: {e}"` on the **root** logger | neither family, and pre-interpolated → residual 8 |
| `mcp/shared/session.py`:384 | DEBUG | the whole JSON-RPC message root — i.e. a tool call's arguments | root logger; below its default level, but a caller at root DEBUG gets it → residual 8 |

So in nodriver 0.47 the payload-rendering WARNING surface is exactly the three
`element.py` lines, and they all pass the element through `record.args` — which
is what makes a type-keyed args rewrite viable here where F-906 rejected a
filter for `connection.py`'s pre-formatted string.

### `connection.py`:483's third argument is the CALLER's, and the first write of this got it wrong

This finding shipped a sentence saying "measured, all three of its arguments are
builtins", in three documents, with a pin that passed `ValueError("boom")` —
**the one exception class that makes the claim true**. It is not measurable
once: the third argument is whatever the user callback raised.

A `nodriver.core.connection.ProtocolException` — the commonest thing a
CDP-touching event handler raises, and a nodriver TYPE — was therefore shaped,
and what vanished was Chrome's own diagnostic rather than any payload:

```
before: exception in callback <lambda> for event TargetInfoChanged
        => Inspected target navigated [code: -32000]
after (wrong):  … => <nodriver.core.connection.ProtocolException>
```

So the rule now reads the argument through `_carries_payload`, and **an
exception is never a payload-carrying argument**, whatever package defined it.
Two reasons, and the second is the decisive one:

1. An exception's `str()` is a diagnostic about a failure. F-902 already made
   the one nodriver reply that carried cookies shape-only **at its source**
   (`cdp_transport.CdpReplyError`), which is where a payload-bearing exception
   belongs.
2. **`exc_info=True` rides beside it at that very site.** Every sink that
   formats a traceback renders the text anyway — so shaping the `%s` withholds
   nothing and costs only the Sentry breadcrumb, which formats no traceback.
   A redaction that is incoherent with the record's own `exc_info` is not a
   redaction, it is a hole in one sink's diagnostic.

The pin now builds a real `ProtocolException` from nodriver's own constructor,
asserts its type IS nodriver's (so the pin cannot silently stop testing the
gated case), and asserts Chrome's text survives.

Its named cost is residual 9: `ProtocolException.__init__` has a
`hasattr(args[0], "to_json")` branch that serialises the whole object into
`.message`, and `tab.py`:1020 raises `ProtocolException(exception_details)` —
a `cdp.runtime.ExceptionDetails` whose description is the PAGE's thrown error.
That shape can carry page-authored text through an exception. It is named and
not closed here, for reason (2): nothing this mechanism does can withhold it
from a sink that formats `exc_info`.

## 2. The matrix — MEASURED, not reasoned

Harness: a real `Element` from `cdp.dom.Node.from_json` + `Element(node, tab)`,
one distinct marker per half of `__repr__`, against a real `RotatingFileHandler`
on a tmp log dir, the real `debug_logger` ring, a real `LoggingIntegration` +
refusing transport, and a capture handler on the root logger. `✗` = it arrived.

### Before

| Configuration | nodriver effective | (a) `backend-<pid>.log` | (b) debug ring | (c) Sentry | (d) root handler / stderr |
|---|---|---|---|---|---|
| **no handler anywhere** (a fresh process) | WARNING | — | — | **✗** | **✗** `logging.lastResort` |
| shipped backend | WARNING | — | — | **✗** | **✗** |
| shipped proxy | WARNING | — | — | **✗** | **✗** |
| backend `--debug` | WARNING | — | — | **✗** | **✗** |
| caller `basicConfig(DEBUG)` **before** our init | DEBUG | — | — | **✗** | **✗** |
| caller `basicConfig(DEBUG)` **after** our init | DEBUG | — | — | **✗** | **✗** |

**This is strictly worse than F-906's**, and the difference is the whole point:
F-906's leak needed a caller to turn root DEBUG on. This one leaks in the
**shipped backend configuration**, with nothing misconfigured, because WARNING
is above the floor and the floor was all that stood there.

### The first row is the one that matters, and it was nearly missed

The F-906 reviewer's second correction: this needs **no caller `basicConfig` at
all**. In a fresh process `nodriver.core.element` is effective WARNING by
inheritance, the root logger has **no handlers**, and `Logger.callHandlers`
then falls through to `logging.lastResort` — an `_StderrHandler` at WARNING
that ships with the stdlib. Measured: root `handlers == []`,
`lastResort.level == 30`, and the line arrives on stderr regardless. **The
backend's stderr IS `backend-boot.log`** (`backend_launch` gives the child
stdout and stderr, and the scheduler rung re-opens that file itself), so this
path is not ephemeral — it is a durable file on disk.

This row nearly went unwritten, and the reason is worth keeping: the first
harness always installed a `_RootCapture` handler in order to observe anything
— **and a root handler suppresses `lastResort`**, which only fires when the
walk up the hierarchy finds no handler at all. So the measurement apparatus
measured the caller-has-a-handler world exclusively and never the shipped one.
`TestLastResortSink` strips the handlers first, asserts both preconditions, and
then reads stderr.

`lastResort` is also the one sink that CAN be pinned this way: its `stream` is
a **property** returning `sys.stderr` at emit time, so
`contextlib.redirect_stderr` captures it. `basicConfig`'s `StreamHandler` is
the inverse — it binds `sys.stderr` into the handler at CREATION time, so a
`redirect_stderr` around the emit captures nothing and a naive pin reads a
leak as absent. Both facts are measured, and the second is a measurement trap
this finding walked into once.

(a) and (b) are unreachable by construction, before and after, for F-906's
reasons: our file handler is on `stealth.<role>` with `propagate = False`, and
the ring is a structure no library writes to.

### After

Every payload cell `—`, in all six configurations. Measured, the same element
before and after — the tag and the four attribute NAMES survive, the three
values and the child's text do not:

```
before: <input type="password" value="hunter2-SECRET"
        data-session-token="eyJ.TOKEN" class="form-control">BALANCE-12345</input>
after : could not calculate box model for
        <input attrs=[type, value, data-session-token, class_] children=1>
```

## 3. The fix, and why this mechanism

`logging_setup.install_payload_arg_redaction()` installs a **`logging` record
factory** that, for records from the `nodriver` package, replaces every argument
whose **TYPE** is defined in that package with a shape-only string. Called from
`configure_logging` immediately after `apply_payload_log_floor()`, ahead of the
idempotency guard and of everything that can raise `OSError`, for that
function's reason.

**Why a record factory and not a `logging.Filter`.** The brief preferred a
filter on the `nodriver` logger family. Measured, it cannot work:
`Logger.handle` consults only `self.filter(record)` — the filters of the logger
the call was made **on** — and `callHandlers` then walks ancestors for
**handlers**, never for their filters. A filter added to `nodriver` therefore
**never fires** for a `nodriver.core.element` record (pinned, because a fix
written that way passes a test that emits on the family root and leaks every
real line). Filtering each descendant instead cannot work either: at
`configure_logging` time **not one `nodriver.*` logger exists** — the proxy
never imports nodriver and the backend imports it later — so an enumeration
covers nothing and would need a SECOND install site after the import, i.e. two
homes for one decision.

**Why not a handler filter.** Measured, one does reach Sentry (the breadcrumb is
built from the same record object, after our mutation). It is still no use,
because *in the configuration this finding is about we do not own the handler*:
production root carries none, so `callHandlers` falls through to
`logging.lastResort`, and under a caller's `basicConfig` the handler is theirs.

A factory runs inside `Logger.makeRecord`, upstream of filters, handlers,
`lastResort` and Sentry's `callHandlers` patch — measured against all four — and
it covers a logger **created after it is installed**, including a module a
future nodriver adds. It also survives
`logging.config.dictConfig(disable_existing_loggers=True)`, which nothing
attached to a logger does (measured: that call *disables* every nodriver logger
existing at that moment, so the pin drives one created afterwards — the only one
still able to leak).

**Why no `before_breadcrumb` beside it.** F-906's argument exactly: one
mechanism upstream of every sink closes all four at once, and a rule in
`observability` would be a second home for one decision (convention 4) that
could only ever matter if this one were removed.

**Why keyed on the TYPE and never on the message.** A nodriver release is free
to reword these three lines; it is not free to stop passing an `Element`. The
type is read as `type(arg).__module__` and **not** with `isinstance`, because
resolving the class needs `import nodriver` and `configure_logging` runs in the
**stdio proxy**, which must never import the browser stack (`desktop_launch`'s
measured cold-start paragraph).

**And keyed on the ARGUMENT, never on `record.name`.** The first write gated on
the record's logger package as well, so the loop ran only for a `nodriver.*`
record. Measured, that left a `stealth.*` record carrying an `Element` leaking
the whole repr — which is the one shape this finding exists for. Whether a
rendering carries page content is a property of the OBJECT; the logger it was
passed to is not evidence about it. Our own sites keep their own PII discipline
(F-869/F-873/F-876/F-877) and none of them logs a nodriver object today, so this
is a floor under that discipline and not a second answer to it — and the
invariant it replaces (a pin asserting our records were exempt) is INVERTED
rather than deleted, because a silent exemption in the navigation map is how the
next change to this would go wrong.

What that costs is the per-argument half only, and it is stated rather than
implied — see the cost paragraph below.

**What the replacement says.** Redacting is not silencing. An `Element` keeps
its tag, its attribute **NAMES** and its **child COUNT**, and loses every VALUE
and all of its text — "which control had no box model" is the entire diagnostic
value of the line, and a name is the page's vocabulary while a value is the
user's secret (`value=` and `data-session-token=` are exactly the pair that
makes the point). The count is `Element.child_node_count`, an `int` the page
cannot author into a string, and it is deliberately the ONLY thing said about
the children: `__repr__`'s `content` half renders every descendant text node's
`node_value` bare, which is page TEXT — a bank balance, a message body — and is
the larger of the two surfaces here. Anything else
— a `Tab`, a `Connection`, a generated CDP record — keeps its TYPE and nothing
else, because there is no half of it measured to be safe.

This is **stricter than `click_target.Shape`**, which reports an id and a class
list by VALUE, and the asymmetry is deliberate rather than an oversight: that
module reads a known element through our own code with its own bounds, this one
is handed an arbitrary object on a third-party line we do not control, and
"never a value" is a rule with no edge cases to get wrong.

**Why the rewrite is EAGER and lands a `str`.** A lazy wrapper would keep the
element alive for the life of the record and would still have to render for any
sink that formats; a `str` leaves nothing downstream able to re-derive the
payload. It costs nothing extra, because a `LogRecord` only exists once
`isEnabledFor` has admitted it — so under F-906's floor nodriver's DEBUG and
INFO payload lines never reach this function at all. **The two findings compose
exactly along that line**: the floor takes everything below WARNING, the
redaction takes what the floor lets through.

**Why `websockets` is not in the table.** F-906 pairs the two families because
the same raw payload is reachable one layer down at DEBUG. Measured across
websockets 16.0, every one of its WARNING-and-above sites logs a static message
or a `str` — there is nothing there to redact, and naming it anyway would be a
claim the evidence does not support (F-906's "no `uc` entry" reasoning).

**What nodriver's real diagnostic costs.** Nothing — but not for the reason
this finding first gave. `connection.py`:483, the one genuine WARNING in the
library, passes the callback, the event **class name** and **whatever the
callback raised**. The first two are ours and a builtin; the third's type is the
caller's, and a nodriver `ProtocolException` is the commonest case. It is
untouched because an EXCEPTION is never a payload-carrying argument, not because
its arguments happened to be builtins — see §1's `connection.py`:483 section.

**The tolerance is TOTAL and is not a swallow.** `_shape` reads `value.tag` and
`value.attrs.keys()`, both of which run arbitrary library code, inside
`Logger.makeRecord` — so anything escaping breaks every log call in the process,
including the one that would report it. It was written first as a narrow
exception tuple and a `RuntimeError` from a property walked straight through it
and took the whole `logger.warning` call with it (that is a pin now). The
handler names the exception's **TYPE** in the rendered shape — the only channel
left when logging about it would recurse — and never `str(exc)`, which on a
page-derived object is page-authored, which is this finding's whole subject.

**Cost — re-measured, because the first number described a cost that was not
paid.** §3 originally said "one package test per argument of every record in
the process: ~370 ns". With the `record.name` gate in place that loop never ran
for a non-nodriver logger, so the sentence described work the short-circuit
skipped. With the gate dropped it is now true, and the split matters:

```
min of 7 x 200_000, CPython 3.13.11
  makeRecord, no args     bare 1563.9 ns   ours 1917.2 ns   +353.3
  makeRecord, two args    bare 1744.0 ns   ours 2411.6 ns   +667.7
  makeRecord, five args   bare 1875.9 ns   ours 2633.7 ns   +757.7
  _redacted alone:  ()  102.1    ('x',) 179.0    ('x', 3) 257.1    5 strs 476.9
```

**~350 ns is the chained factory CALL**, paid by every record the moment any
factory is installed and independent of this rule; **~80 ns per argument** is
the scan, and that per-argument half is the whole price of reading the argument
instead of the logger name. Against ~1.7 µs to build a record. The product logs
on failures, warnings and lifecycle transitions, never per request — uvicorn
access logging is off (`backend_uvicorn_config`) — so this is not on any hot
path, and a record whose arguments carry nothing gets its OWN tuple back rather
than an equal copy, which a pin asserts by identity.

## 4. Pins

`tests/test_nodriver_element_repr_logging.py` — 36 nodes, all green. RED-first:
20 of them failed against the unfixed tree (7 behavioural across the
configurations × the all-sinks assertion and the named stderr/Sentry assertion,
plus 13 mechanism/bounds/surface), against 6 already-green invariants; the
seven added while folding in the review's premises are `TestReachability` (5)
and `TestLastResortSink` (2), and three more came out of the review of the fix
itself (the `ProtocolException` exemption, the inverted `stealth.*` invariant,
and the untouched-args identity assertion). Re-verified RED at the tip with a
`%TEMP%` plugin that no-ops `install_payload_arg_redaction`: **16 failed, 20
passed** — one more than before the review, because the inverted `stealth.*`
invariant is a behavioural pin where the exemption it replaces was green either
way.

* every marker, every sink, every shipped config **and** caller root DEBUG in
  both orders;
* the **no-handler** configuration, measured with the root handlers stripped and
  both preconditions asserted (`root.handlers == []`,
  `lastResort.level == WARNING`): one pin proves the unredacted element DOES
  reach stderr, the other that after the install it does not;
* the **reachability** premises — `Position.center` is truthy for all four quad
  shapes including zero-size at the origin, and `mouse_click` returns on a
  `None` position before the warning line — so the day a nodriver change makes
  those three sites live, the finding's severity claim fails in CI;
* the line still NAMES the element (tag + attribute names survive) — so a
  future "fix" that silences the logger fails;
* nodriver's real `connection.py`:483 WARNING passes through **with a real
  `ProtocolException` built from nodriver's own constructor** — and the pin
  first asserts that exception IS a nodriver type, so it cannot silently stop
  testing the gated case the way the `ValueError("boom")` it replaces did;
* websockets' WARNINGs and our own `stealth.*` records with ordinary arguments
  pass through untouched, asserted by **identity** on the args tuple;
* **our own `stealth.*` record carrying an `Element` IS redacted** — the
  inversion of what shipped first, pinned so the logger-name gate cannot come
  back as an unnoticed exemption;
* the mechanism: a family-root filter never fires; a logger created after
  install is covered; `dictConfig(disable_existing_loggers=True)` is survived;
  install is idempotent; a pre-existing caller factory is CHAINED, not replaced;
  a shape read that raises degrades to the type and names the exception class;
* the bounds: attribute-name count, attribute-name length and tag length are all
  page-authored and all clamped;
* **premises**: the installed `Element.__repr__` still renders values *and*
  text (if that goes green on its own, nodriver fixed it and this can be
  deleted), and the three sites are still WARNING with a **lazy `%s` argument**
  — a nodriver bump that pre-interpolates would put them out of reach exactly as
  `connection.py`:451 is.

## 5. Residuals

1. **A caller who installs their own record factory AFTER ours replaces it.**
   F-906's residual 1 in this mechanism's terms. We chain to whatever we find;
   nobody can make a later caller chain to us.
2. **Only applied where `configure_logging` is called.** Three console-script
   NAMES over two mains, and only TWO of the three reach it: the backend and
   the stdio proxy do, and a `stealthy` / `stealth-chrome-devtools` ops process
   calls `sentry_init()` but never `configure_logging`, so it has neither
   F-906's floor nor this factory. Harmless today — no nodriver object exists in
   that process — and it is F-906's pre-existing gap rather than one this
   introduces, but "both shipped processes" was the wrong count and is corrected
   here. A third party importing `browser_manager` directly likewise gets
   today's behaviour. Moving it to import time was rejected for F-906's reason:
   a module that mutates global logging state on import is the thing these two
   findings complain about a dependency doing.
3. **Our own `stealth.*` sites keep their own PII discipline** —
   F-869/F-873/F-876/F-877 — and this rule is the FLOOR under it, not a
   replacement. It used to be a gap dressed as a decision: the factory gated on
   `record.name`'s package, so one of our records carrying an `Element` leaked
   the whole repr, and the pin asserted that as an invariant. The gate is gone
   and the pin is inverted. What remains residual is the direction the rule does
   NOT run: an argument of OURS that carries page content and is not a nodriver
   type is this mechanism's blind spot by construction, and closing that is each
   site's own job.
4. **An attribute name is rendered as nodriver stores it**, so `class` reads as
   `class_`. nodriver's `__repr__` maps it back; re-spelling that cosmetic
   mapping here would be a second home for it, and the key is not wrong, only
   nodriver-flavoured.
5. **A nodriver bump can move one of these lines out of reach** (pre-interpolate
   it, as `connection.py`:451 already does) or apply a numeric conversion to a
   nodriver-typed argument. Both are pinned as premises, so they fail in CI
   rather than in Sentry.
6. **`_shape` duck-types `tag`/`attrs`.** Any nodriver object offering both
   renders as an element. That is the intended generosity — it is the shape we
   want for anything element-like — and everything else falls to the type name.
7. **`element.py`:499's exception carries the whole repr — filed as
   [F-912](./finding_F912_element_exception_carries_repr.md).**
   `Exception("could not find position for %s " % self)`, measured to render
   `value="SECRET-VALUE"`. Unlike the three WARNINGs this one IS reachable: it
   is the `not quads` branch, it propagates past `mouse_click`'s
   `except AttributeError`, and `dom_handler.click_element` turns it into a
   `ToolError` that reaches the client, the debug ring and Sentry as the
   exception itself. **No logging mechanism can touch it** — there is no
   `LogRecord` — so it needs a different mechanism at a different home, and
   after this finding's severity correction it is the *larger* of the two. It
   has its own number rather than living as residual text under a closed
   finding.
8. **`mcp/shared/session.py`:383-384 log on the ROOT logger** — a WARNING
   rendering `str(e)` for a request that failed validation, and a DEBUG
   rendering the whole JSON-RPC message root, i.e. a tool call's arguments.
   Neither is in the `nodriver` or `websockets` family, both are f-string
   pre-interpolated, and the logger is the root itself — so neither F-906's
   floor nor this redaction reaches them, and a floor on the root logger is not
   a thing a library may install. Out of scope for both findings; named so the
   next census does not re-discover it.
9. **A nodriver EXCEPTION can itself carry page text, and is deliberately not
   shaped.** `ProtocolException.__init__` has a `hasattr(args[0], "to_json")`
   branch that serialises the whole object into `.message`, and `tab.py`:1020
   raises `ProtocolException(exception_details)` — a `cdp.runtime
   .ExceptionDetails` whose `description` is the PAGE's own thrown error. The
   exemption is still right: at the one site that logs an exception, `exc_info`
   rides beside it, so nothing this mechanism does can withhold that text from a
   sink that formats a traceback. Closing it belongs at the raise, on F-902's
   `cdp_transport.CdpReplyError` precedent — shape-only **at the source**.
10. **`_redacted` maps over the TOP LEVEL of `record.args` only.** A nodriver
   `Element` nested inside a list or tuple argument is rendered in full. No
   measured nodriver line passes a container — so this is not live — but it is
   the shape a future one would most plausibly take, and recursing was declined
   because an unbounded walk of an arbitrary argument runs inside
   `Logger.makeRecord`, where `_shape`'s own total `except` already exists
   because code that runs there must not be able to end a log call.
