# F-821 (MED, Sentry-evidenced) — `set_cookie(same_site=…)` never reached Chrome

**Status:** FIXED on `fix/F821-F825-F836-small-fixes`.
**Sentry:** `STEALTH-CHROME-DEVTOOLS-MCP-3P` — *Exception: Failed to set cookie:
'str' object has no attribute 'to_json'*, 9 events over 17 days, unresolved,
culprit `http://127.0.0.1:…/mcp/` (the backend's HTTP transport).

## Symptom

Any `set_cookie` call that supplied `same_site` failed:

```
Failed to set cookie: 'str' object has no attribute 'to_json'
```

Omitting `same_site` worked, so the tool looked healthy in every smoke test that
did not set the attribute — and a caller who *did* set it could not set that
cookie at all.

## Cause

`server.py:set_cookie` takes `same_site` as a `str` (the only shape an MCP tool
argument can carry) and puts it straight into the cookie dict that
`NetworkInterceptor.set_cookie` splats into `uc.cdp.network.set_cookie(**cookie)`.

nodriver's CDP commands are *generators*: they build their request frame while
being advanced, and that build calls `same_site.to_json()`. A `str` has no
`to_json`, so the command raises `AttributeError` on its first `next()` — inside
`tab.send` — and the interceptor's `except Exception` re-wrapped it into the
opaque message above. Verified against nodriver directly:

```python
>>> next(uc.cdp.network.set_cookie(name='a', value='b', url='http://x', same_site='Lax'))
AttributeError: 'str' object has no attribute 'to_json'
>>> next(uc.cdp.network.set_cookie(..., same_site=uc.cdp.network.CookieSameSite.LAX))
{'method': 'Network.setCookie', 'params': {..., 'sameSite': 'Lax'}}
```

## Fix

`network_interceptor.to_cookie_same_site()` — one new module-level helper, and
**the one home** for the conversion because `network_interceptor.py` is the one
CDP cookie boundary (`set_cookie` / `get_cookies` / `clear_cookies` all live
there, and it is the only caller of `uc.cdp.network.set_cookie`). Putting it here
rather than in `server.py` also keeps the tool layer free of CDP types and keeps
`server.py` at its exact LOC cap.

* Accepts the documented names case-insensitively (`Strict` / `Lax` / `None`).
* Passes an already-typed `CookieSameSite` through unchanged.
* Anything else raises `ToolError` naming the valid options — the F-816
  unknown-input precedent (`build_cdp_call`'s `"…; valid params: …"`).
* Converts on a **copy** of the caller's dict, so the tool body's dict is not
  mutated behind its back.

`server.py`'s `same_site` doc line was reworded (same one line, net 0 LOC) to say
the values are accepted in any case.

## Tests

`tests/test_network_interceptor.py::TestCookieSameSite` — 9 pins, hermetic:

* the three documented names map to the **real** `cdp.network.CookieSameSite`
  members (built from nodriver's own enum, not a hand-written table);
* case-insensitive acceptance; already-typed passthrough;
* invalid value → `ToolError` naming `Strict`, `Lax`, `None`;
* `set_cookie` puts `sameSite` on the frame the **real** command generator
  yields (`FakeTab.send` advances it and swallows nothing — the harness that
  caught the CDP `Headers` crash);
* regression: an omitted `same_site` still succeeds and emits no `sameSite`;
* the caller's dict is not mutated;
* the whole tool path (`call_tool(server, "set_cookie", …, same_site="Lax")`)
  reaches CDP with the enum.

RED evidence (pre-fix, through the production path):
`RED: Exception Failed to set cookie: 'str' object has no attribute 'to_json'`.
