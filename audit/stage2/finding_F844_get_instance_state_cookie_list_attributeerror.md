# F-844 (MED, live-evidenced) — `get_instance_state` was `partial: true` on every call

**Status:** FIXED on `fix/F844-F845-tab-state`.
**Found:** 2.0.8, by hand over the real stdio transport.

## Symptom

`get_instance_state` never once returned real page state. Measured on 2.0.8
against the hermetic fixture app — **4 calls out of 4**, on two different
instances, on loaded pages:

```json
{
  "instance_id": "…",
  "state": "active",
  "current_url": "http://127.0.0.1:…/index.html",
  "title": "…",
  "source": "active",
  "partial": true,
  "detail_error": "Failed to collect full page state: AttributeError: 'list' object has no attribute 'get'"
}
```

The tool has a graceful-degradation path (F-746): when full collection fails it
answers with the instance record plus `partial: true` and a `detail_error`.
That path was the *only* one ever taken. Cookies, localStorage, sessionStorage,
`ready_state` and the viewport — everything the tool exists to report — were
never delivered, and the failure was quiet enough (a `partial` flag, not a
raised error) that it read as "this page had nothing to report".

There were **two** raising statements, one hidden behind the other. Only the
first was visible from the symptom.

## Cause 1 — the cookie envelope that is not there

`BrowserManager.get_page_state` (`browser_manager.py:1419-1488` on 2.0.8):

```python
cookies = await tab.send(uc.cdp.network.get_cookies())    # :1438
...
    cookies=cookies.get("cookies", []),                   # :1481
```

nodriver's CDP wrappers are generators that **deserialize the response before
returning it**. `nodriver/cdp/network.py::get_cookies` ends with:

```python
json = yield cmd_dict
return [Cookie.from_json(i) for i in json['cookies']]
```

so `tab.send(...)` hands back a `list[Cookie]` — the array itself, already
unwrapped from the `{"cookies": [...]}` envelope and already typed. Calling
`.get()` on that list raises `AttributeError: 'list' object has no attribute
'get'`.

The blast radius comes from the block structure: lines 1433-1485 are one `try`,
and line 1488 re-raises everything as `Exception("Failed to get page state: …")`.
The URL, title, `ready_state`, both storages and the viewport had **already been
collected** by the time the cookie line ran; they were discarded with it. One
wrong accessor cost the whole tool.

This is the same family as F-778 (`get_cookies` is declared `-> list[dict]` and
returns `Cookie` dataclasses) and F-821 (`set_cookie` handed a `str` where the
generator wanted a `to_json()`-bearing enum): nodriver's CDP boundary is typed
in both directions, and code that assumes raw JSON at it breaks.

## Cause 2 — the object literal that never came back as an object

Fixing the cookie line and re-running against **real Chrome over the real stdio
transport** produced a different partial record, not a green one:

```
Failed to collect full page state: Exception: Failed to get page state:
1 validation error for PageState
viewport
  Input should be a valid dictionary [type=dict_type,
  input_value=[['width', {'type': 'numb... 'number', 'value': 1}]],
  input_type=list]
```

`get_page_state` read the viewport with
`tab.evaluate("({width: window.innerWidth, …})")`. nodriver's `Tab.evaluate`
**always** sends `SerializationOptions(serialization="deep", max_depth=10, …)`
(`nodriver/core/tab.py`), so Chrome answers an object literal as its deep
serialization — `[['width', {'type': 'number', 'value': 1888}], …]` — and
nodriver returns `remote_object.deep_serialized_value.value`, that list.

Its `return_by_value=True` flag does **not** help: CDP honours
`serializationOptions` over `returnByValue`, so `remote_object.value` stays
`None`, nodriver's `if return_by_value: if remote_object.value:` guard falls
through, and the caller gets the raw `RemoteObject`. Measured directly:

```
evaluate("({width: innerWidth, …})")                    -> list  [['width', {'type': 'number', 'value': 748}], …]
evaluate("({width: innerWidth, …})", return_by_value=True) -> RemoteObject(value=None, deep_serialized_value=…)
evaluate("window.location.href")                        -> str   'data:text/html,…'   (primitives are fine)
```

So the viewport read has been broken for as long as the cookie read has; the
`AttributeError` simply raised two statements earlier and hid it. A fix that
stopped at the cookie line would have left `get_instance_state` at
`partial: true` on every call, with a new message.

There is a third way to be partial in the same block: `devicePixelRatio` is
`1.25` at Windows' 125% display scaling and `2.0` on a Retina panel, while
`models.PageState.viewport` was declared `dict[str, int]`. Every such machine
would have failed validation even after both reads were fixed.

## Fix

Use the cookie list, and re-serialize it to the shape the model declares:

```python
# nodriver's wrapper already deserializes: a ``list[Cookie]``, never
# a ``{"cookies": [...]}`` envelope (F-844). PageState wants dicts.
cookies = await tab.send(uc.cdp.network.get_cookies()) or []
...
    cookies=[c.to_json() for c in cookies],
```

* `to_json()` is nodriver's own serializer, so the enum fields (`priority`,
  `sourceScheme`, `sameSite`) come out as the strings Chrome sent rather than
  as `CookiePriority.MEDIUM` reprs. `models.PageState.cookies` is declared
  `list[dict[str, Any]]`, and this makes that annotation true instead of
  merely unenforced.
* `or []` covers `tab.send` answering `None` — a page with no cookies at all,
  and the shape a dropped response takes.

Ask the page for a **string**, which is the one thing nodriver's deep
serialization passes through unchanged:

```python
viewport = json.loads(
    await tab.evaluate(
        "JSON.stringify({width:innerWidth,height:innerHeight,devicePixelRatio})"
    )
)
```

`JSON.stringify` rather than `return_by_value=True` because the flag provably
does not work here (measured above), and rather than three separate primitive
`evaluate` calls because that would triple the CDP round trips for one record.

And widen `models.PageState.viewport` to `dict[str, int | float]`. Pydantic's
smart union keeps `1280` an `int` and `1.25` a `float`, so nobody gets
`"width": 1280.0` and nobody at 125% scaling gets a partial record.

`browser_manager.py` sits at an exact no-grow LOC cap (1532). Net change for
this commit: **+2**, exactly restoring the 2 lines the F-845 commit on this
branch freed — the `import json`, the `JSON.stringify` block and its comment
are paid for by replacing `get_page_state`'s own `Args:/Returns:` boilerplate
docstring with a summary that says something the signature does not. Combined
branch delta on the file: **0**. The cap is neither raised nor padded.
`models.py` is not grandfathered and the annotation change is in place.

## Tests

`tests/test_instance_state_cookies.py` — five hermetic pins, no Chrome:

* `test_page_state_survives_the_cookie_list` — a full `PageState` with the
  cookie's own `name`/`value`/`domain` carried through. Asserting the fields,
  not "no exception", is deliberate: a fix that swallowed the `AttributeError`
  and returned `cookies=[]` would pass a no-raise pin while still losing every
  cookie.
* `test_page_state_keeps_the_storage_it_already_collected` — the collateral
  half: URL, localStorage, sessionStorage and viewport all survive.
* `test_get_instance_state_is_not_partial` — the user-facing half through the
  real tool body: `partial is False`, no `detail_error`, cookie data present.
* `test_page_state_reports_a_cookieless_page_as_an_empty_list` — `None` from
  `tab.send` is a cookieless page, not a failure.
* `test_page_state_accepts_a_fractional_device_pixel_ratio` — `1.25` survives
  and `width` stays an `int`.

**Harness discipline** (memory: *mocked fakes can encode the bug*). The canned
CDP answer is built with `nodriver.cdp.network.Cookie.from_json(...)` from a
payload Chrome really sends, **not** a hand-written `{"cookies": [...]}` dict
modelling the assumption the product got wrong. A fake of that second shape is
exactly what would have kept this defect green — the F-803 miss. `tests/fakes.py`
needed no change: it carried **no** `get_cookies` canned answer at all, so no
existing fake encoded the wrong shape; the gap was that nothing exercised
`get_page_state` hermetically. That is what this file closes.

RED evidence (pre-fix, all pins):
`AttributeError: 'list' object has no attribute 'get'` at
`browser_manager.py:1481`, surfacing through the tool as
`assert True is False` on `state["partial"]`.

**The hermetic pins were not sufficient on their own**, and that is worth
recording. The first version of this file answered the viewport `evaluate` with
a plain dict, because that is what the *expression* asks for — the same
assumption the product made. It went green on a fix that was still broken. The
deep-serialization half was found by driving the fix against real Chrome over
the real stdio transport, and the fake was then corrected to answer a JSON
*string*, which is what nodriver really hands back.

GREEN evidence (post-fix, real headless Chrome, real stdio, isolated
`gate_workspace` backend on a free port, fixture app):

```
F-844 partial: False
F-844 detail_error: None
F-844 cookies: [{'name': 'f844probe', 'value': 'live-ok', 'domain': '127.0.0.1',
                 'path': '/', 'size': 16, 'httpOnly': False, 'secure': False,
                 'session': True, 'priority': 'Medium', …}]
F-844 viewport: {'width': 1888, 'height': 977, 'devicePixelRatio': 1}
```

## Residuals (deliberately out of scope)

* **F-778 stays open.** The `get_cookies` *tool* still returns nodriver `Cookie`
  dataclasses behind a `-> list[dict[str, Any]]` annotation
  (`network_interceptor.get_cookies:873-894`). Its wire shape is correct because
  pydantic serializes them, so it is cosmetic where this one was fatal, but it is
  the same mismatch and it now has a working precedent to close against.
* **`network_interceptor.get_cookies` still branches on the response shape**
  (`if isinstance(result, dict): return result.get("cookies", [])` at :888-889).
  That `dict` branch is dead against nodriver 0.47 — the wrapper cannot return
  one. Removing it is a second-way cleanup that belongs with F-778, not here.
* **The localStorage/sessionStorage reads take the same deep-serialized shape.**
  `Object.keys(localStorage)` is an array, so `evaluate` returns Chrome's deep
  serialization of it, not a list of strings. Those two reads sit in their own
  inner `try` that logs and continues, so they degrade to `{}` rather than
  failing the record — which is what the live run showed (`local_storage: {}`
  on a page that had no storage anyway, so the shape is unproven either way).
  Converting them to the same `JSON.stringify` idiom is the obvious follow-up;
  it is left out here because it costs LOC this file does not have and because
  it is a behaviour change (empty → populated) that deserves its own pin
  against a page that really has storage.
* **`tab.evaluate` is a shape hazard everywhere, not just here.** Any caller
  that evaluates a non-primitive expression gets `[[key, {type, value}], …]`.
  A single "evaluate and get the value" home — the one `execute_script` already
  built for F-832 — would retire the class. Out of scope for a defect fix.
* **The single-`try` blast radius.** `get_page_state` still collects six things
  in one `try` and loses all six if any one raises. Splitting it so a partial
  answer is genuinely partial (rather than empty) would make `partial: true`
  mean something; today it means "nothing was collected". Recorded, not fixed —
  it is a shape change to a live tool contract, and this branch is a defect fix.
