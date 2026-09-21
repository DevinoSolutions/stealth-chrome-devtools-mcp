# F-902 — any CDP result carrying a cookie ends the tab's CDP listener

**Severity** CRITICAL (silent, mislabels itself as a browser crash, and leaks
the page's cookie jar into the error path while doing it).
**Ref** `main` = 13e65cc (post-2.1.11). **Status** CONFIRMED and FIXED on
`fix/F902-cookie-result-kills-listener`.
**Reported by** the F-898 storage matrix, which could not be measured through
the door this breaks. **This supersedes that branch's draft** — §1-§4 below are
re-measured here rather than carried over, and two of its claims did not
survive (§7).

Everything marked MEASURED was taken on **Windows 11 Pro 26200, Chrome
153.0.8010.50, nodriver 0.47**, 2026-09-21.

---

## 1. What happens

Calling `get_cookies` on a page that has at least one cookie:

* never returns — it hangs to the caller's own deadline, and
* **leaves the tab's CDP connection dead.** Every later call on that tab hangs
  to `CDP_OPERATION_TIMEOUT` and the operator is told *"The browser may have
  crashed or the connection dropped"* about a browser that is fine.

MEASURED through the product's own tool functions, one instance, two pages on
one loopback fixture origin:

| page | tool | before the fix | after |
|---|---|---|---|
| no cookie | `get_cookies` | OK, `[]`, 0.01 s | OK, `[]`, 0.01 s |
| no cookie | `execute_script` | OK, 0.00 s | OK, 0.00 s |
| **one cookie** | `get_cookies` | **never returned** (gave up at 25 s) | **OK, 0.00 s, cookie returned** |
| …then | `execute_script` | `ToolError: CDP operation timed out after 10s … The browser may have crashed` | OK, 0.00 s |
| …then | `get_cookies` again | **never returned** | OK, 0.00 s |

## 2. The mechanism

Two facts meet, and neither is ours.

**(a) Chrome 153 no longer sends `Network.Cookie.sameParty`, and only that.**
MEASURED over a RAW WEBSOCKET to Chrome 153 with nodriver nowhere in the path,
so the observation is Chrome's and not our parser's idea of it. The reply to
`Network.getCookies`, verbatim:

```json
{"domain": "127.0.0.1", "expires": -1, "httpOnly": false, "name": "f902_probe",
 "path": "/", "priority": "Medium", "secure": false, "session": true,
 "size": 25, "sourcePort": 36829, "sourceScheme": "NonSecure",
 "value": "value-one"}
```

`sameParty` is absent. **Every other field the generated parser requires is
present** — `size`, `session`, `priority`, `sourceScheme`, `sourcePort` all
still arrive, so `sameParty` is the whole of the incompatibility. Identical on
`Network.getCookies`, `Network.getAllCookies` and `Storage.getCookies`. It
carried First-Party Sets / SameParty, which Chrome removed.

**(b) nodriver 0.47 reads it unconditionally, on the one path its listener does
not guard.** `nodriver/cdp/network.py`:1376, inside `Cookie.from_json`:

```python
same_party=bool(json['sameParty']),
```

`Transaction.__call__` (`core/connection.py`:121-128) turns that into another
`KeyError`, and `_listener` (:442-445) calls it **bare**, in an `else:` branch
whose exceptions no handler on that `try` can see:

```python
else:
    message = json.loads(raw)
    if "id" in message:
        tx: Transaction = self.mapper.pop(message["id"])
        tx(**message)                      # <-- RESULT path: no try/except
    else:
        try:
            event = cdp.util.parse_json_event(message)
        except Exception as e:             # <-- EVENT path IS guarded
            ...
            continue
```

So the exception ends `_listener` — the one task that resolves every future and
dispatches every event on that connection. The transaction has already been
`pop`ped out of `mapper`, so it is never completed either: the caller's future
stays pending forever, which is why the call hangs rather than raising.

This is **exactly the failure `cdp_transport.py` was written for** (F-883 B1,
and F-788/F-794 before it) reached through the other door. F-883 stops a
*cancellation* from killing the listener; its shield is untouched by a *parse
error*, because nothing here was cancelled.

**(c) And the exception carries the cookie jar.** `Transaction.__call__`'s
re-raise interpolates `response['result']` — the WHOLE reply — into its message.
MEASURED, the actual string that ended the listener:

```
KeyError: "key '('sameParty',)' not found in message: {'cookies': [{'name':
'f902_probe', 'value': 'synthetic-value', 'domain': '127.0.0.1', ...}]}"
```

It escaped as `Task exception was never retrieved`, i.e. through
`loop.call_exception_handler` → the `asyncio` logger at ERROR → Sentry's
`LoggingIntegration`, whose event threshold is ERROR. **Every cookie name and
value on the page, in one shipped event.** A page's cookie jar is where its
sessions live; this is the subsystem whose whole payload is credentials. The
F-898 report did not have this half, and it is why the severity is CRITICAL
rather than HIGH.

## 3. Blast radius — by source, and measured where cheap

Every door onto a `Network.Cookie`. "WEDGES" means the tab's connection dies.

| site | reaches | verdict (before) |
|---|---|---|
| `tool_sections/cookies_storage.get_cookies` (no `urls`) | `Network.getAllCookies` | **WEDGES** (measured) |
| `tool_sections/cookies_storage.get_cookies(urls=…)` | `Network.getCookies` | **WEDGES** (measured) |
| `browser_manager.get_page_state`:1391 → **`get_instance_state`** | `Network.getCookies` | **WEDGES**, and degrades to F-869's `partial` first (measured) |
| `network_interceptor.clear_cookies(url=…)`:843 | `Network.getCookies` (it reads the jar to name each cookie) | **WEDGES** (measured) |
| `server.py`:291 `browser://{id}/cookies` MCP resource | same `network_interceptor.get_cookies` | WEDGES (same method; not separately driven) |
| `clear_cookies()` with **no** url | `Network.clearBrowserCookies` | **survives** — no cookie is parsed |
| `set_cookie` | `Network.setCookie` | **survives** — `CookieParam`, whose `same_party` is already `Optional` |
| `Network.requestWillBeSentExtraInfo` (and the 3 other cookie-carrying events) | `parse_json_event` | **survives, but the event is SILENTLY DROPPED** (measured: raises `KeyError`, the listener's `except Exception: continue` swallows it) |

The third row is the widest: `get_instance_state` is what an agent calls to
find out whether anything is wrong, so on any logged-in page it both degraded
AND killed the tab it was asked about.

**Cookies are not special, they are just first.** Counted across nodriver
0.47's generated `cdp/` package: **1199 unconditional required field reads**
(`network.py` 208, `page.py` 142, `storage.py` 92, `css.py` 84, …). Every one is
this defect waiting for Chrome to retire its field. That count is the argument
for fixing the LISTENER and not only the cookie.

## 4. Why nothing caught it

`tests/test_e2e_transport_cookies.py` has driven a real Chrome cookie round trip
since plan_RELEASE W5, and `tests/test_instance_state_cookies.py` covers the
state path. They did not catch this because:

* the real-Chrome node stayed green for as long as the Chrome under it still
  sent `sameParty` — it is a test of the browser it runs against, and CI's
  Chrome predates the removal;
* the hermetic tier uses doubles that return already-typed `Cookie` objects, so
  no pin ever drove a raw Chrome reply through nodriver's generated parser —
  the `fixtures-from-the-same-serializer-cannot-fail` shape, asserting our idea
  of the wire against itself;
* the pre-push lane is `-m "not integration"`, so the one tier that could have
  seen it does not run locally at all.

## 5. The fix

In `embedded/cdp_transport.py`, whose sentence widens from "awaiting a CDP reply
must never be able to CANCEL it" to **"no single CDP reply may kill the
connection"** — F-883 and F-902 are two ways into one harm, and both are a
monkeypatch of one nodriver class installed once per process from one call site
(`tool_runtime`'s module body). A second module with a second `install()` called
from the same line would be convention 4's defect: two answers to "patch
nodriver at startup", one import apart. Three halves, each with its own DELETE
condition in the docstring:

1. **(half 1, unchanged)** `Transaction.__await__` shielded — F-883.
2. **(half 2, new)** `Transaction.__call__` wrapped: a parse failure completes
   THAT transaction with a `CdpReplyError` instead of propagating into the
   listener. The caller of the one unreadable command gets a real error, every
   other pending future still resolves, and the listener lives. A transaction
   already `done()` is left alone — a cancelled or delivered future has no
   caller to tell, and `set_exception` on one raises the very `InvalidStateError`
   this exists to stop.
3. **(half 3, new)** `_RETIRED_COOKIE_FIELDS = {"sameParty": False}` supplied at
   `Cookie.from_json`. Half 2 makes `get_cookies` fail honestly; half 3 makes it
   WORK. A NAMED tolerance with its measurement beside it — never "fill in
   whatever is missing", because an invented `sourcePort` is a lie a caller
   could act on where a DELETED field has one meaning left. Patched at the
   classmethod rather than at the three product call sites because that one
   method is also what `Storage.getCookies` and the four cookie-carrying events
   reach.

**The report half 2 delivers is SHAPE ONLY, and that is §2(c)'s rule, not
taste.** It is built from the CDP method, the exception TYPE, the reply's
top-level field COUNT, and a missing FIELD name only when `_missing_field` can
prove the failure named nothing else — a `KeyError` whose single arg is an
identifier-shaped string, which nodriver's payload-carrying re-raise can never
match and a generated parser's own lookup always does. It is constructed
OUTSIDE the `except` block so not even `__context__` can carry the payload out,
and `CdpReplyError` is deliberately **not** a `ToolError`: convention 2's class
is what `expected_events` DROPS from Sentry as the product working as designed,
and a reply we cannot read is the opposite of that.

### Evidence

* **RED first.** The 7 new hermetic nodes fail against unfixed `cdp_transport`
  (11 pre-existing pass); the RED output itself prints the leaked
  `'name': 'f902_probe' … 'value': 'synthetic-value'`. GREEN after: 18/18.
* **Real Chrome.** `tests/test_e2e_cookie_listener.py`, 5 nodes, all green —
  `get_cookies` bare and with `urls=`, `get_instance_state` not `partial`,
  `clear_cookies(url=…)` proved by re-reading, and the PII node.
* Every node's real assertion is the **second** call: `get_cookies` raising
  would be visible, and what F-902 did was leave the connection dead behind an
  answer that never came.

## 6. Residuals

* **`Cookie.to_json` writes `sameParty` unconditionally**, so a cookie read on
  Chrome 153 reports `sameParty: false` — a value Chrome never sent. It is a
  SYNTHESISED field, named here and in the module docstring rather than hidden.
  `False` is what it meant for every cookie outside a First-Party Set, and the
  feature no longer exists, so no caller decision can turn on it. Patching
  `to_json` too would need per-instance state on a library dataclass; not worth
  it, but it is the thing to revisit if a seed hand-off ever forwards the field
  to `Storage.setCookies`.
* **nodriver DEBUG-logs every raw reply verbatim, cookies included**
  (`connection.py`:445). Pre-existing, untouched by this fix, and out of the PII
  pin's scope because it cannot reach anything the product writes:
  `configure_logging` attaches its handler to `stealth.{role}` with
  `propagate = False`, so a `nodriver.*` record goes to the ROOT logger and the
  product's file never. It is below Sentry's INFO breadcrumb threshold too. It
  WOULD surface for anyone who calls `logging.basicConfig(level=DEBUG)`.
* **Other removed fields may do the same to other domains** — 1199 candidates
  (§3). Half 2 makes each of them a reported failure of one command instead of a
  dead connection, which is the point; half 3 is per-field and deliberately
  empty but for the one measured case.
* **Linux/macOS unmeasured.** The cause is a Chrome protocol change plus a
  library constant, so the platform is very unlikely to matter — the CHROME
  VERSION is what matters, which is also why CI has been green.
* **`network_interceptor.get_cookies` calls the deprecated
  `Network.getAllCookies`** (nodriver emits a `DeprecationWarning` on every
  call). Unrelated to this finding, not changed here.
* **`test_e2e_transport_cookies.py` was not run** on this branch: it resolves
  and executes the installed console launcher, which this task was scoped away
  from. It is the node most likely to have been RED on any Chrome ≥ 153 and
  should go green with this fix; worth confirming on CI.

## 7. Two of the reporting branch's claims did not survive re-measurement

Recorded because the draft is superseded, not merged:

* **"`Storage.getCookies` must go to the BROWSER-level socket — on a page
  session it never answers at all."** Not reproduced. Over a raw websocket to a
  **page** target on Chrome 153, `Storage.getCookies` answered normally with
  both cookies. The draft already suspected its symptom was F-902 in both
  places; it was.
* **"`get_cookies` hangs on the `.fn` seam but works over stdio"** (the older
  recorded note the draft proposed to explain). This finding does explain a hang
  that does not depend on the transport — but the explanation is the page having
  a cookie, and that note predates this measurement by long enough that it is
  not re-confirmed here. Treat it as probably-this, not proven.
