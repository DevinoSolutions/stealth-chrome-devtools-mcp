# F-902 — any CDP result carrying a cookie ends the tab's CDP listener

**Severity** CRITICAL (silent, and it mislabels itself as a browser crash).
**Ref** `origin/main` = f18ecc5 (2.1.11). **Status** FOUND, NOT FIXED — this is
F-898's measurement branch and the fix is not its to make.
**Found by** the F-898 storage matrix, which could not be measured through the
door this breaks.

## 1. What happens

On **Chrome 153.0.8010.50**, calling `get_cookies` on a page that has at least
one cookie:

* never returns — the call hangs until the caller's own deadline, and
* **leaves the tab's CDP connection dead**. Every later call on that tab hangs
  to `CDP_OPERATION_TIMEOUT` and the operator is told *"The browser may have
  crashed or the connection dropped"* about a browser that is fine.

MEASURED through the product's own tools, one instance, two pages on one
loopback fixture origin (2026-09-21, Windows 11 26200, Chrome 153.0.8010.50):

| after | tool | result |
|---|---|---|
| page with **no** cookie | `get_cookies` | OK, `[]`, 0.00 s |
| page with **no** cookie | `get_page_content` | OK, 0.01 s |
| page with **one** cookie | `get_cookies` | **never returns** (20 s deadline) |
| …then | `get_page_content` | **timeout** |
| …then | `execute_script` | `ToolError: CDP operation timed out after 10s … The browser may have crashed` |
| …then | `query_elements` | **timeout** |
| …then | `get_instance_state` | answered in 10.01 s — **degraded**, the F-869 `partial` path |

## 2. The mechanism

Two facts meet.

**(a) Chrome 153 no longer sends `Network.Cookie.sameParty`.** The raw reply,
captured off the wire:

```
{'cookies': [{'name': ..., 'domain': '127.0.0.1', 'path': '/', 'expires': -1,
  'size': 8, 'httpOnly': False, 'secure': False, 'session': True,
  'priority': 'Medium', 'sourceScheme': 'NonSecure', 'sourcePort': 50599}]}
```

`sameParty` is absent. It was First-Party Sets / SameParty, which Chrome
removed.

**(b) `nodriver` 0.47 reads it unconditionally**, and the read is on the one
path its listener does not guard. `nodriver/cdp/network.py`:

```python
same_party=bool(json['sameParty']),
```

`nodriver/core/connection.py` `Transaction.__call__` turns that `KeyError` into
another `KeyError` — and `Connection._listener` calls it **bare**:

```python
if "id" in message:
    tx: Transaction = self.mapper.pop(message["id"])
    tx(**message)                      # <-- no try/except
else:
    try:
        event = cdp.util.parse_json_event(message)
    except Exception:                  # <-- the EVENT path IS guarded
        continue
```

so the exception ends `_listener` — the one task that resolves every future and
dispatches every event on that connection. This is **exactly the failure mode
`cdp_transport.py` was written for** (F-883 B1, and F-788/F-794 before it),
reached through the other door: F-883 stops a *cancellation* from killing the
listener, and its shield is untouched by a *parse error* inside `tx(**message)`.

## 3. Blast radius

Every site that parses a `Network.Cookie`, i.e. every one of these, whenever the
page has ≥ 1 cookie in scope:

| site | reached by |
|---|---|
| `network_interceptor.get_cookies` (`:885`, `:887`) | the **`get_cookies` tool** |
| `network_interceptor.clear_cookies(url=…)` (`:843`) | the **`clear_cookies` tool** with a url |
| `browser_manager.get_page_state` (`:1391`) | **`get_instance_state`**, the general health read |

The third is the widest: `get_instance_state` is what an agent calls to find out
whether anything is wrong, and on any logged-in page it both degrades and kills
the tab it was asked about.

`Storage.getCookies` is affected identically — it returns the same type — which
is what made it F-898's blocker.

## 4. Why nothing caught it

`tests/test_e2e_transport_cookies.py` and `tests/test_instance_state_cookies.py`
exist, and the hermetic tier uses doubles that return already-typed objects, so
no pin has ever driven a REAL Chrome 153 cookie through
`nodriver`'s generated parser. This is the shape of
`fixtures-from-the-same-serializer-cannot-fail`: the assertion is against our own
idea of the wire.

It is also, very likely, the mechanism behind the already-recorded observation
that "`get_cookies` hangs on the `.fn` seam but works over stdio" — it does not
depend on the transport at all, it depends on whether the page had a cookie.

## 5. Shape of a fix (not made here)

Three candidates, in the order this repo's conventions suggest:

1. **Guard the result path in the listener**, the same way the event path
   already is — but that is a patch to a vendored library's control flow, and
   it turns a wrong answer into a silent missing one.
2. **Extend `cdp_transport.install()`**. It is already THE one home for "the
   listener must survive what an individual transaction does", it is already
   idempotent and called once, and a `Transaction.__call__` wrapper that
   converts a parse failure into `set_exception` keeps the failure *attributable
   to the call that caused it* instead of losing the connection. This looks like
   the right home: the module's whole sentence is "a transaction must not be
   able to kill the listener", and today it only enforces the cancellation half.
3. **Stop parsing cookies through the generated type** — read them as raw JSON
   at the three product sites. Smallest blast radius, but it fixes three call
   sites and leaves the library's next removed field to kill a connection again.

(2) and (3) are not alternatives; (2) is the general rule and (3) is what makes
the cookie tools correct rather than merely non-fatal. A pin must drive a **real
Chrome** cookie, not a double.

## 6. Residuals

* **Unmeasured on Linux/macOS**, but the cause is a Chrome protocol change and a
  library constant, so platform is very unlikely to matter — Chrome version is
  what matters, and CI's Chrome may still be old enough to send `sameParty`,
  which would explain a green gate.
* **Other removed fields may do the same to other domains.** Every generated
  `from_json` in `nodriver` 0.47 indexes required fields directly; cookies are
  simply the one this measurement walked into.
* **F-898's C2 is blocked on this.** The hand-off is `Storage.getCookies` →
  `Storage.setCookies`, and the first of those is the call that kills the
  connection. The measurement worked around it with a raw websocket; a shipped
  implementation cannot, and must not be written to.
