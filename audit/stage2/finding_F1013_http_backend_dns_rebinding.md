# F-1013 — the HTTP backend accepted DNS-rebound and foreign-origin requests (T-5587)

**Severity:** HIGH (code execution in the backend process and cookie theft, from a web page the owner merely visits).

## Finding

`embedded/server.py` ran `mcp.run(transport="http", host=…, port=…, path=…)` with no `host_origin_protection`.
At the pinned fastmcp 3.4.8 that setting defaults to `False` (`fastmcp/settings.py`, `http_host_origin_protection`), so
`fastmcp.server.http._host_origin_guard_middleware` returns `None`, and the MCP SDK's own `TransportSecuritySettings`
check is built with `enable_dns_rebinding_protection=False` on every transport. `embedded/backend_env.py` scrubs `FASTMCP_*`
from the backend's environment, so an operator could not turn it on either.

## Threat model

The backend binds `127.0.0.1` and has no authentication. Binding loopback stops other machines and does nothing about a
web page: a page on `evil.example` that re-resolves its name to `127.0.0.1` after the browser's first lookup is same-origin
with `http://evil.example:<port>/mcp`. Its script can `initialize` a session and call any tool, including
`create_python_binding` (`exec` in the backend process), the cookie readers, `execute_cdp_command` on every logged-in
browser the backend drives, and file-writing clone tools. The port is discoverable (the default, or a scan of a small range).
Requires only that the owner has the backend running and visits (or has open) a hostile page.

## Fix

`server.host_origin_guard(bound_host)` returns `{"host_origin_protection": True, "allowed_hosts": [...]}` and is spread into the
one `mcp.run(transport="http")` call. fastmcp's `HostOriginGuardMiddleware` (strict mode) then:

* answers **421** to a `Host` that is not in the allow-list (`127.0.0.1`, `localhost`, `::1`, plus the `--host` the operator bound;
  a wildcard bind `0.0.0.0` / `::` adds nothing). The port is not compared; the name is.
* answers **403** to a request carrying an `Origin` that is neither the request's own origin nor a loopback origin on a loopback Host.
* leaves a request with **no `Origin`** alone, which is what the stdio proxy, the `stealthy` CLI, `doctor`, `status`, the
  liveness probes and a url-transport client (`uvx`) look like.

The guard is a middleware at the front of the app, so it also covers the F-960 health route.

## Clients checked

Every first-party HTTP client builds its URL with `singleton._backend_http_url(port)` = `http://127.0.0.1:<port>/mcp`, so httpx
sends `Host: 127.0.0.1:<port>` and no `Origin`: the stdio proxy bridge (`singleton._proxy_streams` over
`backend_client.http_client`), the CLI (`cli_call.backend_url` → `backend_client.opened`), the readiness poll and identity gate
(`backend_probe.ready` / `await_ready`), the watchdog heartbeat (`backend_probe.Heartbeat`, health route), `doctor` / `status`
(`_probe_backend_status`). `cli serve --http --host X` passes `--host` through the same argparse option the guard reads.

## Pins

`tests/test_http_host_origin_guard.py` evaluates the keyword arguments of the real `mcp.run(...)` call in `server.py`, hands them
to the real `FastMCP.run_async` with only `uvicorn.Server.serve` captured, and drives the app over httpx's ASGI transport:
421 for three rebound Hosts (and the health route); 403 for three web Origins on a loopback Host; 200 + a session id for each
loopback spelling with no Origin; the readiness probe, the proxy bridge's SDK session and the CLI's `list_tools` over the real
`backend_client.http_client`; a wildcard bind and a named bind (`--host backend.lan`) keep refusing a foreign Host while serving
their own name. Mutation (runtime rebinding of `host_origin_guard`, in a `git archive` export, not the worktree): guard
returning `{}` (= the pre-fix app) fails 10 of 16, `allowed_hosts=["*"]` fails 7, a guard that ignores `--host` fails 1.

## Residuals

* **No authentication.** Another local user or process on the same machine can still connect and call any tool. Follow-up: a
  per-user bearer token (file in the state dir, sent by the proxy/CLI/probes), which also closes the cross-user case.
* A web page served from a **loopback origin** (another app on `localhost:<other port>`) passes fastmcp's loopback-pair
  same-origin allowance. A DNS-rebound page is not that case: its Origin is its own foreign name.
* `--host 0.0.0.0` / a LAN bind remains an operator decision with no authentication; the guard only refuses foreign Host *names*
  (a request by raw LAN IP is served: the interface address is appended by fastmcp from the socket).
