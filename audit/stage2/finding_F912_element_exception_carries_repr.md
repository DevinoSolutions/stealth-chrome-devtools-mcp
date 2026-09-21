# F-912 — nodriver's `could not find position` exception carries the whole element repr

**Status:** OPEN (stub — filed by F-907, not fixed there)
**Date:** 2026-09-21
**Area:** `embedded/dom_handler.py` (`click_element`), `embedded/tool_errors.py`
**Filed by:** F-907's review (S3). F-907 named it as residual 7; this is that
residual given a number, because it is the one leak on that code path that IS
reachable while F-907 ships as insurance.
**Measured against:** nodriver 0.47.0, CPython 3.13.11

---

## 1. The census line

| Site | Kind | What it renders |
|---|---|---|
| `nodriver/core/element.py`:499 | `raise Exception(...)` | `"could not find position for %s " % self` — `Element.__repr__` in full |

Measured, on an element built from nodriver's own constructors
(`cdp.dom.Node.from_json` → `Element(node, tab)`):

```
could not find position for <input type="password" value="SECRET-VALUE"></input>
```

`Element.__repr__` renders the tag, **every attribute as `name="value"`** and
the element's **whole recursive descendant TEXT** (`str(child)` over every
child; a text node's own `__repr__` answers its raw `node_value`). So a password
field's `value=`, a `data-*` session token and a balance in a `<div>` all ride
in the message. That is the same repr F-907 redacts — this is the vector F-907
cannot reach.

## 2. Reach — and why it is worse than F-907's

```python
# nodriver/core/element.py, Element.get_position
if not quads:
    raise Exception("could not find position for %s " % self)   # :499
```

* It is on the **live** branch. F-907 measured its own three WARNING sites
  UNREACHABLE in nodriver 0.47 (`Position.center` is a 2-tuple and so always
  truthy, so `if not center:` cannot open). This one is the `not quads` case,
  which a not-rendered element reaches.
* It is a bare `Exception`, **not** an `AttributeError`, so it propagates
  straight past `Element.mouse_click`'s `except AttributeError` and out of the
  call.
* `dom_handler.click_element` — reached by the `click_element` tool — turns it
  into a `ToolError`, which reaches **the client**, **the debug ring**
  (`log_tool_failure`) and **Sentry** as the exception itself.

## 3. Why F-907 could not close it

There is no `LogRecord`. F-907's whole mechanism is a `logging` record factory
inside `Logger.makeRecord`, which is upstream of every log SINK and reaches
nothing that is not a log record. An exception raised by a library and
re-raised by us never passes through it.

## 4. Proposed home (to be decided, not decided here)

The ONE place a caught nodriver exception becomes a `ToolError` / a log line —
i.e. `dom_handler.click_element`'s handler, or `tool_errors` if the pattern
turns out to be shared. Candidate shapes, cheapest first:

1. **Do not pass nodriver's message through.** `click_element` already knows
   which selector it was asked about and `click_target.Shape` already exists as
   the shape-only descriptor for an element. Raising our own `ToolError` naming
   the selector and the exception TYPE loses nothing an operator can act on —
   `str(exc)` here is nodriver's sentence about our own element — and it needs
   no matching on message text.
2. An exception scrub in `observability._scrub_event`. **F-907 argues against
   this**: it would have to match on the message TEXT, which is exactly the
   keying F-907 §3 rejects (a library is free to reword its own sentences), and
   it would close the Sentry sink only — the client and the debug ring would
   still have it.

(1) is the recommendation. Whoever takes this should measure which other
`except` blocks around nodriver calls interpolate the exception, because the
answer may be "a rule at one site" rather than "a rule per site".

## 5. Pins this should carry

* RED-first: `click_element` against a not-rendered element, with a real
  `Element` built from nodriver's own constructors carrying a marker in
  `value=` and in a child text node — the marker must reach neither the
  `ToolError` message, nor the debug ring entry, nor a Sentry event.
* The premise: `element.py`:499 still raises with `% self`, so the day nodriver
  changes that the pin says so.
* The invariant: the message still NAMES the selector and the failure, so a
  "fix" that silences the error fails.

## 6. Related

* `audit/stage2/finding_F907_nodriver_warning_renders_page_content.md` — the
  logging half, where this was residual 7.
* F-869 / F-873 / F-876 / F-877 — the existing rule that no message of ours
  carries a page's own values. This is that rule applied to a message we relay
  rather than compose.
