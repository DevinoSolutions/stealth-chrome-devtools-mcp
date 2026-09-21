# F-912 — nodriver's `could not find position` exception carries the whole element repr

**Status:** fixed
**Date:** 2026-09-21
**Area:** `embedded/element_box.py` (new), `embedded/tool_runtime.py`
**Filed by:** F-907's review (S3), which named it as residual 7 rather than
folding it into a closed finding.
**Measured against:** nodriver 0.47.0, Chrome 153.0.8010.50, sentry-sdk 2.64.0,
CPython 3.13.11

---

## 1. The finding

`nodriver/core/element.py`:498-499 raises a bare `Exception` whose message is
the element rendered in full:

```python
quads = await self.tab.send(
    cdp.dom.get_content_quads(object_id=self.remote_object.object_id)
)
if not quads:
    raise Exception("could not find position for %s " % self)   # :499
```

and `Element.__repr__` (`element.py`:1131-1158) renders three things: the tag,
**every attribute as `name="value"`**, and the element's **whole recursive
descendant TEXT** (`str(child)` over every child; a text node's own `__repr__`
answers its raw `node_value`). So an `<input type=password>`'s `value=`, a
`data-*` session token and a balance in a `<div>` all ride in the message.

**The novel exposure is the DURABLE LOG and SENTRY, and the client leg is a
change of SHAPE rather than of disclosure.** `get_element_state` already returns
`attributes` (including `value`), `text` and `text_all` to the caller on the
SUCCESS path by design (`dom_handler.py`:578-586) — a caller that asked for an
element's state is entitled to the field's value, and nothing here makes the
tool more secretive about it. What was new is that the same content left the
process: into `backend-boot.log`/the backend log through `click_element`'s
relay, and into a Sentry event on a third party's machine (§2.4), neither of
which anyone asked for. It also arrived as the error text of a call that
FAILED, where a caller gets a rendering of an element rather than an answer.

F-907 shaped exactly that rendering, for LOG RECORDS, and could not reach this
one: there is no `LogRecord`, and its record factory sits inside
`Logger.makeRecord`, upstream of every SINK but downstream of nothing that
raises. So it named this as residual 7, and after F-907's own severity
correction it is **the larger of the two**: F-907's three box-model WARNINGs are
unreachable in nodriver 0.47, and this one runs for ordinary pages.

## 2. Measurement

### 2.1 The census — this is the only raise of its kind in the library

An AST pass over the installed `nodriver` 0.47.0 package: **37 raise sites**,
of which `element.py`:499 is the only one that interpolates `self` where `self`
is an `Element`. The neighbours worth naming so a later reader does not
re-derive them:

| Site | Renders | Verdict |
|---|---|---|
| `element.py`:499 | `"could not find position for %s " % self` — the whole repr | **this finding** |
| `connection.py`:125 | `response['result']` — the whole CDP reply | F-902's; closed at `cdp_transport` half 2 |
| `tab.py`:1983 | `self.__class__.__name__` only | nothing to redact |
| `element.py`:909, `tab.py`:1431 | a caller's filename | the caller's own |
| `config.py`:161/:208, `tab.py`:1153/:1296/:1307/:1718, `util.py`:377/:4696, `browser.py`:108/:368 | a caller's argument or a constant | not page content |

`element.py`:510's `except IndexError` branch DEBUG-logs the repr `%`-interpolated
into the message, so neither F-906's floor nor F-907's args rewrite could ever
have reached it either — but F-906's floor keeps `nodriver` at WARNING, so it
never emits, and the branch is not taken anyway (see 2.2).

### 2.2 Reachability — MEASURED against a real Chrome, and it is LIVE

The question F-907's severity correction turned on was whether the branch can
run. For :499 that is a question about Chrome: does `DOM.getContentQuads` answer
an EMPTY LIST for an element that lays out nothing, or an error? An error would
be a `ProtocolException` carrying Chrome's own words, and there would be no
finding.

Measured — headless Chrome 153, raw nodriver, one `data:` page, nine shapes:

| shape | `DOM.getContentQuads` | `Element.get_position()` |
|---|---|---|
| ordinary box | 1 quad | `Position(x=8, y=8, w=80, h=20)` |
| `visibility:hidden` | 1 quad | `Position(x=8, y=28, w=40, h=10)` |
| zero-size box | 1 quad | `Position(w=0, h=0)` |
| empty inline `<span>` | 1 quad | `Position(w=0, h=0)` |
| `content-visibility:hidden` | 1 quad | `Position(w=50, h=50)` |
| **`display:none`** | **`[]`** | **raises, `type(exc) is Exception`** |
| **`<option>` in a `<select>`** | **`[]`** | **raises, `type(exc) is Exception`** |
| `<input type=password display:none>` | `[]` | **raises, with `value="SECRET-VALUE"`** |

```
could not find position for <input id="pwhidden" type="password"
    value="SECRET-VALUE" style="display:none"></input>
```

So the branch is reached by three ordinary shapes — a hidden control, an
`<option>` (which a caller clicks routinely), and by construction any detached
node — and it is a **bare builtin `Exception`**, so it propagates straight past
`Element.mouse_click`'s `except AttributeError` and out of the call. The
`except IndexError` branch is never entered: an empty list raises before an
index is taken.

### 2.3 Where it went — measured through OUR OWN handlers, on that page

Three call sites reach `get_position` in the whole product, and the stub's §2
attributed the leak to the wrong one of them. Measured:

Each row names two lines: where `get_position` is CALLED, and the RELAY that
interpolates what it raised — the relay is the defect, the call is only how the
exception gets there.

| Our site | call → relay | What the relay does | Sinks | Shipped default? |
|---|---|---|---|---|
| `dom_handler.get_element_state` | :595 → **:606** | `raise ToolError(f"Failed to get element state: {e!s}")` | **durable log**, **Sentry**, and the failed call's own error text | **yes** |
| `dom_handler.click_element` | :273 → **:276** | `debug_logger.log_debug(..., str(e))`, then a SYNTHETIC click | backend log at DEBUG; stderr under `--debug` | only with DEBUG on |
| `dom_handler.query_elements` | :142 → **:159** | `f"...: {type(e).__name__}"` | — | the one that was already safe |

```
get_element_state('#pwhidden') RAISED ToolError
  'Failed to get element state: could not find position for <input
   id="pwhidden" type="password" value="SECRET-VALUE-MARKER"
   style="display:none"></input> '
[DEBUG] dom_handler.click_element: could not find position for <input
   id="pwhidden" type="password" value="SECRET-VALUE-MARKER" ...>
```

`click_element` still WORKS — `click_target.aim` had already answered
`reason: "not-rendered"` and the synthetic fallback lands the click — so the
only thing that path produces is the log line.

### 2.4 The Sentry leg is real, and it was worth checking

It looked as though `expected_events` would drop it: a `ToolError` is
convention 2's class, and `error-convention` is the rule that drops the product
working as designed. Measured, assembling the event the way `_emit` does (the
`FastMCP.fastmcp.tools.tool_manager` logger, `event_from_exception`, the real
`_scrub_event`): **it is not dropped**, and the marker is in the serialised
event. The reason is the rule's own second clause — `error-convention` requires
the outermost link to be ours AND tolerates only `TimeoutError` /
`asyncio.CancelledError` behind it, and `get_element_state` raises its
`ToolError` from inside an `except`, so the chain is `[ToolError, Exception]`
and the rule refuses. All three sinks, as shipped.

## 3. Root cause

A message we RELAY can carry a payload we never composed. F-869/F-873/F-876/
F-877 gave our own messages the rule "shape and counts only"; F-907 extended it
to a third party's LOG lines; nothing covered a third party's EXCEPTION text,
and `str(exc)` is interpolated at about twenty `except` blocks in
`dom_handler.py` alone.

The element is an OBJECT at exactly one moment — the raise — and a STRING
everywhere after it. Any rule applied after the raise would have to key on the
message TEXT, which is the keying F-907 §3 rejects in terms: a library is free
to reword its own sentences, and a rule that misses a rewording fails OPEN.

## 4. The fix

`embedded/element_box.py` — a leaf that wraps `Element.get_position` and, for
the ONE exception type that raise produces, replaces the message with F-907's
shape:

```
The element has no layout box, so its position cannot be read:
<input attrs=[type, value, data-session-token] children=1>.
display:none, an <option> and a detached node all render nothing.
```

Five decisions, each with its evidence:

**Keyed on `type(exc) is Exception` — the bare builtin, exactly.** That is what
:499 raises and what nothing else in `get_position` produces: a CDP failure is a
`ProtocolException` or a `cdp_transport.CdpReplyError`, a missing remote object
is an `AttributeError` (which `mouse_click` catches by design), a cancelled
budget is a `BaseException`. Every one of those propagates UNCHANGED, because
its text is Chrome's or Python's own diagnostic and shaping it would withhold
the only thing an operator can act on — `logging_setup._carries_payload`'s
argument, reached from the other side. A nodriver release that re-types or
rewords the raise makes the premise nodes RED rather than making the guard
silently stop firing.

**At the raise, not at the three `except` blocks.** F-907's residual 9 names
this home for a payload-bearing library exception in so many words — "shape-only
**at the source**", on F-902's `cdp_transport.CdpReplyError` precedent. One
wrapper covers all three call sites, plus `mouse_drag`, `mouse_move`,
`save_screenshot` and `flash`, plus whatever calls it next; three site fixes
would cover three and leave the fourth to re-open it. `click_element` in
particular cannot be fixed at its own site in any useful way — it never calls
`get_position`; nodriver does, from inside `mouse_click`.

**Its own module, not a fourth half of `cdp_transport`.** That module's
docstring forbids splitting ITS question across two seams, and the question is
the CONNECTION — "handing the listener a reply must not end it". An element's
box is not that, and its headline was deliberately narrowed once already.
`browser_connect` is the standing precedent for a second nodriver patch: a
separate leaf, one question, its own `install()`, installed at the site that
needs it. "Every nodriver patch in one file" is not an invariant this tree has.

**F-907's shaper and no second one.** `element_box` imports
`logging_setup._shape`, which keeps the tag, the attribute NAMES and the child
COUNT and loses every VALUE and all descendant text, under F-907's own bounds
(`SHAPE_MAX_ATTRS`, `SHAPE_MAX_NAME_CHARS`, `SHAPE_OVERFLOW`). Re-spelling that
rendering here would be a second answer to one question. The reach into another
module's private name is `backend_launch`'s into `desktop_launch`'s `schtasks`
seam, and it runs one way only: `logging_setup` executes in the stdio proxy and
must never import the browser stack, which is why `_shape` is duck-typed.

**Raised OUTSIDE the `except` block.** Inside one, Python sets `__context__` to
nodriver's exception, and a `__context__` is not a private detail: stdlib
`traceback` prints "During handling of the above exception…" followed by the
repr, sentry-sdk serialises the chain, and `observability._exception_chain`
walks it.

`raise … from None` would close all three, and the measurement says so:
`sentry_sdk/utils.py`:798-801 and :880-916 both branch on
`__suppress_context__` — with it set, the walk follows `__cause__`, which
`from None` makes `None`, so no child exception is emitted at all — and
`observability.py`:323 reads `if following is None and not
current.__suppress_context__`. Leaving the handler first is **strictly
stronger**: the context is ABSENT rather than SUPPRESSED, so anything reading
`exc.__context__` directly (a custom formatter, a debugger, a future SDK that
stops consulting the flag) finds nothing, and an absence is not a setting
anyone can flip. It costs one line's placement, which is the whole argument for
taking the stronger form — `cdp_transport._guard_result` builds its error
outside the handler for the same reason at a different seam. **The pin asserts
the ABSENCE** (`__cause__ is None` and `__context__ is None`) and never a third
party's walking behaviour; the first draft of this paragraph claimed sentry-sdk
ignores `__suppress_context__`, which is measurably false, and F-908 §"the
first round ruled the whole `mcp` family out … it was wrong" is what a shipped
positive claim about a dependency costs.

`ElementBoxError` is deliberately not a `ToolError`: that class is what
`expected_events` DROPS, and this is a library-level condition each call site
already converts under its own policy. It also names the condition where a bare
`Exception` named nothing, so `query_elements`' `type(e).__name__` line became
more informative rather than less.

`install()` is called once, from `tool_runtime`'s module body beside
`cdp_transport.install()` — the one module loaded once where `embedded/server.py`
is executed three times under runpy — and is idempotent anyway.

**No change to `dom_handler`.** All three relays are correct once what they
relay is shape-only, and the two-line diff to `tool_runtime` plus one new file
is the whole product change.

**Cost — measured, on F-907's precedent of stating it rather than implying it.**
Min of 7 × 20 000 happy-path calls against a hermetic tab, CPython 3.13.11:

```
  nodriver's own get_position     2.087 us/call
  wrapped                         2.200 us/call
  delta                           0.113 us   (+5.39 %)
```

That +5 % is the whole in-process cost of one coroutine frame and one `try`, and
it is measured against a call that never touches a socket. The same method over
real CDP — Chrome 153, headless, one visible `<div>`, n=200 after 20 warm-ups —
is **238.3 µs min / 430.9 µs median**, because `get_position` always makes at
least one round trip (`getContentQuads`, plus `resolve_node` when the node has
no remote object). So the wrapper is **~0.026 % of a real call**, and the number
is here so the next reader does not have to re-measure to decide that.

## 5. Tests

`tests/test_element_box_exception_repr.py` — **28 nodes, all green**. RED-first:
**15 failed** against the tree with the guard made inert by a `%TEMP%` plugin
that turns `element_box.install()` into an uninstall, against 13 already-green
invariants (the premises, the pass-throughs, the source scans).

The pins build a REAL `Element` from nodriver's own constructors
(`cdp.dom.Node.from_json` → `Element(node, tab, tree)`) and drive nodriver's
REAL `get_position`, on F-907's argument: a hand-written double renders whatever
`__repr__` we gave it and could only measure the test against itself. The one
thing faked is the TAB, because what is being reproduced is Chrome answering
`[]` — keyed on `fakes.cdp_command_name`, so the double never learns the wire
format.

* **premises**: `get_position` still raises the EXACT builtin `Exception` with
  all three halves of `__repr__` in it; the AST of nodriver's installed source
  still shows exactly one raise in that function and it still renders `% self`
  (keyed on the AST, not a line number, which drifts); `getContentQuads`
  answering `[]` is the branch, so a Chrome that answered an ERROR instead makes
  the finding unreachable in CI rather than silently;
* **every sink**: the exception text, the `__cause__`/`__context__` chain, the
  formatted traceback, `get_element_state`'s client-facing `ToolError`, the
  debug ring through `log_tool_failure`, the backend log line `click_element`
  writes, a naked Sentry event and **the production-shaped one** — assembled on
  the tool_manager logger and asserted NOT DROPPED first, so the node cannot go
  green because `expected_events` swallowed it;
* the Sentry leg's **dependency on another module**: `observability` still sets
  `include_local_variables=False`, because the element is a LOCAL of the raising
  frame in both the old and the new version — `cdp_transport`'s F-902 node,
  same link, same reason;
* **redacting is not silencing**: the tag and the attribute NAMES survive, the
  type NAMES the condition, and F-907's attribute-count bound is exercised
  through this path;
* **everything else passes through**: a real `ProtocolException` keeps Chrome's
  own words and is the SAME OBJECT, an `Exception` SUBCLASS is untouched, a
  `BaseException` still cancels, a successful read returns its `Position`;
* **mechanism**: `install()` is idempotent, the wrapper keeps nodriver's own
  function on its marker, `installed()` is False without it, the guard covers
  `mouse_click`'s internal call, `tool_runtime` has exactly one install line,
  and `ElementBoxError` is not a `ToolError`.

Regression: 961 nodes green across every `tests/` file that imports
`tool_runtime`, `logging_setup`, `dom_handler` or `element_box` (`-m "not
integration"`).

## 6. Residuals

1. **The rule covers `get_position` and nothing else in nodriver.** It is the
   only raise in 0.47 that renders an `Element` (§2.1, an AST census of all 37
   raise sites), so today that is the whole surface — but the same defect
   written into a different method would need a second wrapper here. Keyed by
   census rather than by mechanism, deliberately: a blanket "wrap every nodriver
   coroutine" would shape `ProtocolException` too, which is F-907's measured
   mistake with `connection.py`:483.
2. **A nodriver release that raises a SUBCLASS stops being guarded**, by design
   — the alternative is keying on text. It goes RED at
   `TestPremise::test_get_position_raises_a_bare_exception_carrying_the_whole_repr`,
   which asserts `type(exc) is Exception` in as many words.
3. **`Element.__repr__` itself is untouched, and must stay so.** Replacing it
   would fix every route at once, and it is not available: nodriver builds
   `Element.text` / `text_all` out of `str(child)` over the children, so a
   shape-only `__repr__` would break the text `query_elements` and
   `get_element_state` return.
4. **Frame locals are the other way out, and they are somebody else's setting.**
   The element is a local of the raising frame before and after this fix, so the
   message being shape-only depends on `observability`'s
   `include_local_variables=False`. Pinned here, exactly as F-902 pinned it in
   `cdp_transport`, so flipping it fails in the file that depends on it.
5. **`dom_handler` still interpolates `{e!s}` at about twenty `except` blocks**,
   and this finding removes the one payload that was measured to travel through
   them. Whether any OTHER library exception in the tree renders page content is
   a census nobody has run; F-902 closed the CDP-reply one at its source and
   this closes the element one at its source, which is the pattern a third
   would follow.
6. **`click_element`'s DEBUG line is a relay of a message it never inspected.**
   It is safe now because the message is, not because the site is careful. A
   site-level rule ("never interpolate a library exception") is not statically
   decidable and was not attempted.
7. **The measurement used a real Chrome; the pins do not.** The `[]`-vs-error
   premise is the one fact the hermetic suite takes on trust from §2.2, and it
   is the fact that would make this finding moot. It is encoded as a node
   (`test_empty_quads_is_the_branch_and_an_error_is_not`) driving nodriver's own
   code with `[]`, which pins what WE do with that answer but cannot pin what
   Chrome sends — a Chrome release that starts erroring here would leave the
   guard installed and idle, which is harmless.

## 7. Related

* `audit/stage2/finding_F907_nodriver_warning_renders_page_content.md` — the
  logging half, where this was residual 7, and the home of the `_shape` this
  reuses.
* `audit/stage2/finding_F906_*` — the level FLOOR below both.
* F-902 (`cdp_transport`) — the precedent: a payload-bearing library exception
  is made shape-only **at the source**, never by matching its text downstream.
* F-869 / F-873 / F-876 / F-877 — the standing rule that no message of ours
  carries a page's own values. This is that rule applied to a message we relay
  rather than compose.
