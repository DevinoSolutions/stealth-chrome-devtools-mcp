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
| `connection.py`:483 | WARNING | a callback's `repr`, the event CLASS NAME, and `str(exc)` + `exc_info` | passes through UNCHANGED — the callback is ours, and a class name is not payload. Deliberately not redacted |
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

**What nodriver's real diagnostic costs.** Nothing. `connection.py`:483 — the
one genuine WARNING in the library — passes the callback, the event **class
name** and the exception, and measured, all three are builtins. The rule that
redacts the leak does not touch it.

**The tolerance is TOTAL and is not a swallow.** `_shape` reads `value.tag` and
`value.attrs.keys()`, both of which run arbitrary library code, inside
`Logger.makeRecord` — so anything escaping breaks every log call in the process,
including the one that would report it. It was written first as a narrow
exception tuple and a `RuntimeError` from a property walked straight through it
and took the whole `logger.warning` call with it (that is a pin now). The
handler names the exception's **TYPE** in the rendered shape — the only channel
left when logging about it would recurse — and never `str(exc)`, which on a
page-derived object is page-authored, which is this finding's whole subject.

**Cost.** One package test per argument of every record in the process:
measured ~370 ns, against ~1 485 ns to build the record itself.

## 4. Pins

`tests/test_nodriver_element_repr_logging.py` — 33 nodes, all green. RED-first:
20 of them failed against the unfixed tree (7 behavioural across the
configurations × the all-sinks assertion and the named stderr/Sentry assertion,
plus 13 mechanism/bounds/surface), against 6 already-green invariants; the
seven added while folding in the review's premises are `TestReachability` (5)
and `TestLastResortSink` (2).

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
* nodriver's real `connection.py`:483 WARNING, websockets' WARNINGs, and our own
  `stealth.*` records all pass through untouched;
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
2. **Only applied where `configure_logging` is called** — both shipped
   processes, and only the backend imports nodriver. A third party importing
   `browser_manager` directly gets today's behaviour. Moving it to import time
   was rejected for F-906's reason: a module that mutates global logging state
   on import is the thing these two findings complain about a dependency doing.
3. **The rule is the record's LOGGER package, so our OWN sites are untouched** —
   deliberately. `stealth.*` records carrying a nodriver object keep their own
   PII discipline (F-869/F-873/F-876/F-877), and a blanket rewrite here would be
   a second, invisible answer to it. Pinned as an invariant, not a gap.
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
7. **`element.py`:499's exception carries the whole repr** —
   `Exception("could not find position for %s " % self)`, measured to render
   `value="SECRET-VALUE"`. Unlike the three WARNINGs this one IS reachable: it
   is the `not quads` branch, it propagates past `mouse_click`'s
   `except AttributeError`, and `dom_handler.click_element` turns it into a
   `ToolError` that reaches the client, the debug ring and Sentry as the
   exception itself. **No logging mechanism can touch it** — there is no
   `LogRecord` — so closing it means either an exception scrub in
   `observability._scrub_event` (which would have to match on message text, the
   thing §3 argues against) or not letting nodriver's message through
   `dom_handler`. It is a different finding with a different mechanism and is
   deliberately not folded in here.
8. **`mcp/shared/session.py`:383-384 log on the ROOT logger** — a WARNING
   rendering `str(e)` for a request that failed validation, and a DEBUG
   rendering the whole JSON-RPC message root, i.e. a tool call's arguments.
   Neither is in the `nodriver` or `websockets` family, both are f-string
   pre-interpolated, and the logger is the root itself — so neither F-906's
   floor nor this redaction reaches them, and a floor on the root logger is not
   a thing a library may install. Out of scope for both findings; named so the
   next census does not re-discover it.
