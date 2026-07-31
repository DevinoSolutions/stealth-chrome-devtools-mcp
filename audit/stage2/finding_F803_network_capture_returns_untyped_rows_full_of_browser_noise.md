# F-803 — network capture returns untyped rows full of the browser's own traffic, making `filter_type` a dead parameter

**Status: RESOLVED** on branch `fix/network-capture-resource-type` (for 2.0.1).
Opened from a live measurement against released **2.0.0** over the real stdio
transport.
**Severity: HIGH** — this is the primary read surface of the network-debugging
section. Every captured row was untyped, one documented filter could therefore
never match, and the real page requests were outnumbered 24-to-1 by noise the
caller did not ask for.

---

## The measurement

One page load, captured by the interceptor, read back through
`list_network_requests`:

| observation | value |
|---|---|
| rows returned, unfiltered | 30 |
| rows with a non-null `resource_type` | **0** |
| rows returned by `list_network_requests(filter_type="document")` | **0** |
| rows that were `chrome://new-tab-page/*` | **24** |
| rows that were the real `https` document | 1 |

Three distinct defects, one cluster: (1) causes (2), and (3) is independent.

## Defect 1 — `resource_type` was never populated

```python
# 2.0.0, network_interceptor._on_request
resource_type = event.type.value if hasattr(event, "type") else None
```

`event` is a `nodriver.cdp.network.RequestWillBeSent`. nodriver's CDP bindings
are code-generated, and the generator renames fields that collide with Python
builtins — the resource type is spelled **`type_`**, not `type`:

```python
# nodriver/cdp/network.py
class RequestWillBeSent:
    ...
    #: Type of this resource.
    type_: typing.Optional[ResourceType]
```

So `hasattr(event, "type")` was **permanently False**. Not "sometimes null on
odd requests" — null on every row, on every page, for the whole 2.0.0 release.
The `if hasattr(...) else None` shape is what hid it: the guard turned a
would-be `AttributeError` into a plausible-looking `None`.

The identical hazard existed for `ResponseReceived`, which also carries `type_`
and which the response handler never read at all.

## Defect 2 — `filter_type` could not match anything

`list_requests` (and `search_requests`) filter on
`filter_type.lower() in request.resource_type.lower()`, guarded by
`if request.resource_type` — correct, and already case-insensitive. With
defect 1 present, that guard was False for every stored row, so **every**
`filter_type` value returned zero rows while the unfiltered call returned
thirty. The parameter was documented, accepted, and inert: a caller filtering
for `"document"` could only conclude the page made no document request.

Case-insensitivity was therefore *not* the bug — it was already implemented and
merely unreachable. It is now pinned by test so it stays that way.

## Defect 3 — Chrome's own traffic was captured as if it were the page's

`setup_interception` runs at spawn, before the first navigation, and Chrome
spends that window loading its own UI. Nothing filtered by URL scheme, so the
new-tab page, extension background pages, `devtools://` and error pages all
landed in the same store as the page's requests, and the caller had no way to
tell them apart (see defect 1: they had no type either). 24 of 30 rows were
browser-internal.

This is a signal-to-noise defect rather than a crash, which is what makes it
worth writing down: every downstream tool — `list_network_requests`,
`search_network_requests`, `export_network_data` — inherited the noise, and the
request-count cap (`STEALTH_MCP_NETWORK_REQUEST_MAX_COUNT`) was being spent on
it.

---

## The fix

All in `src/stealth_chrome_devtools_mcp/embedded/network_interceptor.py` unless
noted.

1. **One home for the type read.** New module-level `resource_type_of(event)`
   reads `type_` first, falls back to `type`, unwraps the `ResourceType` enum,
   and returns the CDP-cased string (`"Document"`, `"XHR"`, …) or `None`.
   `_on_request` calls it; so does `_on_response`.
2. **Backfill.** `RequestWillBeSent.type_` is genuinely optional (absent on some
   redirects/preflights) while `ResponseReceived.type_` is not, so
   `_on_response` fills in a stored request's type when it is still empty — and
   never overwrites one that is already set.
3. **One home for the scheme test.** New module-level `is_internal_url(url)` +
   `INTERNAL_URL_SCHEMES` (`chrome:`, `chrome-error:`, `chrome-extension:`,
   `chrome-native:`, `chrome-search:`, `chrome-untrusted:`, `devtools:`,
   `about:`). Both `_on_request` and `_on_response` drop internal traffic before
   it reaches the stores. A non-`str` URL is never treated as internal — a
   capture handler must not drop traffic because a URL arrived in an odd shape.
4. **One filter vocabulary, not a second mechanism.** The opt-back-in rides the
   existing per-instance filter entry exactly as `capture_bodies` does:
   `set_network_capture_filters(capture_internal_urls=True)` for one instance,
   `STEALTH_MCP_NETWORK_CAPTURE_INTERNAL_URLS=1` for the process, per-instance
   wins. `get_network_capture_filters` reports the resolved value so the
   exclusion is visible rather than silent. The setting is a typed field on
   `Settings` (`settings.py`, the one env home) and is documented in
   `.env.example`; the default is **exclude**.

### Consistency of the other surfaces (checked, no change needed)

`resource_type` has exactly one home — `NetworkRequest`. Every request-shaped
surface already reads it off the model and so is now correct for free:
`list_network_requests` (formats it explicitly), `get_request_details`
(`request.dict()`), `search_network_requests` (emits it per result row), and
`export_network_data` / `import_network_data` (round-trip the field).
`get_response_details` returns a `NetworkResponse`, which deliberately does
**not** carry a resource type — duplicating it there would be a second home for
the same fact. Callers join on `request_id`.

## Evidence

Non-mocked, real headless Chrome:
`tests/test_e2e_network_capture_shape.py::test_capture_shape_resource_type_filter_and_no_internal_noise`
loads the fixture app's `network.html` (which pulls `styles.css` and `app.js`,
so several CDP types appear) and asserts all three halves of the cluster: a
non-null `resource_type` with a `Document` among them, `filter_type="document"`
(lowercase) returning ≥1 row, and zero rows on any internal scheme. It was
verified RED against pre-fix `src/`, failing for the right reason:

```
AssertionError: every captured row still has a null resource_type:
  [{... 'url': 'http://127.0.0.1:20322/network.html', 'resource_type': None},
   {... '/styles.css', 'resource_type': None},
   {... '/app.js',     'resource_type': None},
   {... '/favicon.ico','resource_type': None}]
```

Unit tier — `tests/test_network_interceptor.py`, classes
`TestResourceTypeCapture` and `TestInternalUrlExclusion`. These build **real**
`uc.cdp.network.RequestWillBeSent` / `ResponseReceived` objects through
nodriver's own `from_json` from realistic CDP payloads, rather than hand-rolled
doubles. That is deliberate: the pre-existing `_FakeReqEvent` double set
`self.type`, so it agreed with the *bug* and would have kept passing forever.
`tests/test_server_network_tools.py` pins the same three properties at the tool
layer and imports the one event builder rather than forking a second one.

## Residual, not fixed here

`filter_type` matching is a case-insensitive **substring** test, so
`filter_type="fetch"` matches both `Fetch` and `Prefetch`. Pre-existing
behaviour, pinned by `TestSearchRequests::test_resource_type_filter`; tightening
it to an exact match is a contract change and was left out of a stabilisation
branch.

## Routing

- Depends on nothing; nothing depends on it.
- `DESIGN.md` §6 now states both invariants (what is captured is the page's
  traffic, not the browser's; and the one home for the type read).
- Sibling stabilisation findings for 2.0.1: F-801 (Linux profile release),
  F-802 (concurrent, unrelated).
