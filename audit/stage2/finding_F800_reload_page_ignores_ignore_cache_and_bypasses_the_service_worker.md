# F-800 — `reload_page` discards its `ignore_cache` argument, so every reload is a hard reload that bypasses the service worker

**Status: OPEN.** Opened by RELEASE-16 (W16) from the PWA fixture.
**Severity: MEDIUM** — a declared parameter has no effect, and the side effect
is that a reloaded page silently loses its service worker. Any PWA driven
through this tool behaves correctly on first load and incorrectly after a
reload, with nothing in the tool's return value saying so (`reload_page`
returns `True` either way).

---

## The finding

`reload_page` declares an `ignore_cache` parameter, documents it as "Whether to
ignore cache when reloading", and then never passes it:

```python
# src/stealth_chrome_devtools_mcp/embedded/server.py
async def reload_page(instance_id: str, ignore_cache: bool = False) -> bool:
    tab = await _require_tab(browser_manager, instance_id)
    await _with_cdp_timeout(tab.reload(), instance_id=instance_id)
    return True
```

`tab.reload()` is nodriver's, and its signature is
`async def reload(self, ignore_cache: Optional[bool] = True, ...)` — the
default is **True**. So:

* `reload_page(ignore_cache=False)` — the documented default — performs a
  cache-**ignoring** reload.
* `reload_page(ignore_cache=True)` performs the identical reload.

The parameter is inert in both directions; there is no way to ask this tool for
a normal reload.

## Why that matters beyond the dead parameter

CDP `Page.reload(ignoreCache=true)` is the shift-reload path. Chrome does not
merely skip the HTTP cache on it — it loads the main resource **without the
service worker**. The reloaded document is therefore uncontrolled:

| step (same page, same active registration) | `navigator.serviceWorker.controller` |
|---|---|
| first load + `register()` + `clients.claim()` | controlled |
| `navigate` to the identical URL | **controlled** |
| `reload_page(...)` | **null — uncontrolled** |

The registration itself survives (`getRegistrations()` still reports one
`activated` worker for the scope) — only the *document* lost it. The
consequence for a caller is concrete: after a reload, every `fetch` from the
page goes to the network instead of the worker, so cached/offline behaviour,
request rewriting, and any push/sync wiring a PWA relies on are all absent,
while the page looks fine.

Two tools that a caller would reasonably treat as interchangeable —
"reload this page" and "navigate to this page again" — therefore produce
different application state. That is the part that makes it a defect rather
than a documentation gap.

## Evidence

`tests/test_stateful_i18n.py::test_reload_page_leaves_the_service_worker_page_uncontrolled`
(`@pytest.mark.characterization`). The node registers the W16 service worker on
a throwaway profile against a short-lived loopback origin, proves the
**control** case first (a fresh `navigate` to the same URL yields
`controllerAtLoad == "controlled"`), then calls `reload_page` and pins
`controllerAtLoad == "uncontrolled"`. Without the control half, "uncontrolled
after reload" would be equally consistent with a fixture whose worker never
controls anything.

The pin is written in the direction that makes a FIX go red: when F-800 closes,
the reloaded document becomes controlled and the node fails with a message
telling the next author to promote MQ-157 and bind its `--mq` id.

## What closing it requires

`reload_page` should forward its argument —
`tab.reload(ignore_cache=ignore_cache)` — so the documented default is a normal
reload and the service worker keeps controlling the page. Whether the default
should stay `False` is a separate product call; the bug is that the argument is
dropped. That is a `src/` change and a plan_RELEASE non-goal here.

## Routing

- MQ-157 in `tests/MANUAL_QA_PROTOCOL.md` is `planned`: "controlled reload" is
  one of its named halves and is false at HEAD. The install/activate/controller
  /unregister/cache-deletion halves are recorded as current support.
- No `--mq` id in `.github/workflows/release-gate.yml` is bound to the pin.
- Related: F-787 (`networkidle` is a fixed sleep) and F-788 (a navigation
  timeout wedges the connection) are the other two navigation-family findings.
  Together they are the argument for W5 stating what the navigation tools do
  and do not guarantee, rather than leaving it to their parameter lists.
