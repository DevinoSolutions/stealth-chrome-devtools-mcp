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

**It is reachable from our own code.** `dom_handler.py`:273 calls
`element.mouse_click()`, and site :537 fires whenever `get_position().center` is
falsy — i.e. an element with no box model, which is exactly the `display: none`
case `click_element`'s synthetic fallback exists for (`click_target`'s reason
code `not-rendered`). So the line is not hypothetical library noise; it is on a
path this product takes deliberately.

## 2. The matrix — MEASURED, not reasoned

Harness: a real `Element` from `cdp.dom.Node.from_json` + `Element(node, tab)`,
one distinct marker per half of `__repr__`, against a real `RotatingFileHandler`
on a tmp log dir, the real `debug_logger` ring, a real `LoggingIntegration` +
refusing transport, and a capture handler on the root logger. `✗` = it arrived.

### Before

| Configuration | nodriver effective | (a) `backend-<pid>.log` | (b) debug ring | (c) Sentry | (d) root handler / stderr |
|---|---|---|---|---|---|
| shipped backend | WARNING | — | — | **✗** | **✗** |
| shipped proxy | WARNING | — | — | **✗** | **✗** |
| backend `--debug` | WARNING | — | — | **✗** | **✗** |
| caller `basicConfig(DEBUG)` **before** our init | DEBUG | — | — | **✗** | **✗** |
| caller `basicConfig(DEBUG)` **after** our init | DEBUG | — | — | **✗** | **✗** |

**This is strictly worse than F-906's**, and the difference is the whole point:
F-906's leak needed a caller to turn root DEBUG on. This one leaks in the
**shipped backend configuration**, with nothing misconfigured, because WARNING
is above the floor and the floor was all that stood there.

(a) and (b) are unreachable by construction, before and after, for F-906's
reasons: our file handler is on `stealth.<role>` with `propagate = False`, and
the ring is a structure no library writes to.

### After

Every payload cell `—`, in all five configurations. The LINE still arrives:

```
nodriver.core.element WARNING could not calculate box model for
  <input attrs=[type, value, data-session-token, class_]>
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
its tag and its attribute **NAMES** and loses every VALUE and all of its text —
"which control had no box model" is the entire diagnostic value of the line, and
a name is the page's vocabulary while a value is the user's secret (`value=` and
`data-session-token=` are exactly the pair that makes the point). Anything else
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

`tests/test_nodriver_element_repr_logging.py` — 26 nodes. RED-first: 20 failing
(7 behavioural across all five configurations × the all-sinks assertion and the
named stderr/Sentry assertion, plus 13 mechanism/bounds/surface), against 6
already-green invariants.

* every marker, every sink, every shipped config **and** caller root DEBUG in
  both orders;
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
