# F-1018 — any local process or OS user could drive the HTTP backend (T-5597, with T-5727's follow-ups)

**Severity:** HIGH on a shared machine (another user, or any unprivileged process, gets code execution in the backend process and every logged-in browser it drives); LOW on a single-user machine, where the same account can already read the profiles.

## Finding

F-1013 (2.1.39) made the backend refuse a foreign `Host` (421) and a foreign `Origin` (403), which closes the web-page and DNS-rebinding route. Its own
"Residuals" section left the rest open: the backend still has no authentication, so another local user or process can `initialize` a session and call
`create_python_binding` (`exec` in the backend), the cookie readers or `execute_cdp_command`. The port is discoverable (`~/.stealth-mcp/server.json`, the default, a scan).

## Fix

`embedded/backend_token.py` is the one home, and `server.bearer_token_guard()` sits beside `host_origin_guard()` in the one `mcp.run(transport="http")` call.

* **One token per user** in `<state dir>/backend.token` (`secrets.token_urlsafe(32)`, 43 characters). NOT one per backend startup: the registry holds a backend per
  (display context, identity), and a token rotated at each start would lock the newest backend's siblings' clients out. `ensure` creates it once, before the backend
  binds, and reuses it while well-formed; a malformed file is replaced. A backend that cannot secure it raises `ToolError` and does not serve.
* **Owner-only, measured.** A new file in a state dir on Windows 11 inherited Modify for `CodexSandboxUsers` and five other non-owner SIDs, plus SYSTEM and
  Administrators. The file is created empty, its DACL replaced with a protected one holding one ACE (`SetNamedSecurityInfoW`, SDDL `D:P(A;;FA;;;<user SID>)`), and only then
  written, via a scratch file hard-linked into place. `icacls` afterwards: exactly `AMIN\amind:(F)`, nothing `(I)`. The first draft used `icacls /inheritance:r /grant:r SID:(F)`,
  which the widened-ACL pin showed leaves an explicit `Everyone:(R)` in place (`/grant:r` replaces only the named principal's entry), so it was replaced by the single call above.
  POSIX: `0600` at creation, `chmod`ed on every `ensure`, a file owned by another uid refused.
* **`BearerTokenMiddleware`** (pure ASGI, constant-time compare): any HTTP request without `Authorization: Bearer <token>` is 401 with `WWW-Authenticate: Bearer`, on `/mcp/`
  and the health route alike; no hint which part was wrong; the token is never echoed; one WARNING per minute. It is appended after FastMCP's guard, so 421/403 still come first.
* **T-5727**: `--allowed-host NAME` (repeatable) is the explicit opt-in for an extra Host name (`host_origin_guard(bound_host, extra_hosts)`; `*` and a blank name raise);
  RELEASE_CONTRACT's "no token, no session auth, no allow-list" row is regenerated from `tools/release_evidence.py` (three more rows said "anything that reaches the port");
  `test_a_rebound_page_is_refused_on_both_checks` asserted only the 421, so it is renamed `..._that_also_sends_its_origin_is_refused_at_the_host` and says it pins which refusal wins;
  F-1013's finding said the mutation "fails 10 of 16" while its test file had a different number of tests, corrected in that finding.

## Clients (census)

Found by search of every `httpx`, `streamable_http_client`, `backend_probe.*`, `backend_client.*` and `_backend_http_url` use in `src/` and `tools/`; every one routes through two seams.

| caller | seam | sends the token |
|---|---|---|
| stdio proxy bridge (`singleton._proxy_streams`) | `backend_client.http_client` | yes, at client construction |
| CLI verbs (`cli_call` -> `backend_client.opened`) | `backend_client.http_client` | yes |
| readiness poll, identity gate (`singleton._await_backend_http`, `_same_identity_backend_ready`), self-heal re-probe (`proxy_selfheal`) | `backend_probe.await_ready` / `ready` | yes, per request (`_headers()`), incl. the throwaway-session DELETE |
| `doctor` / `status` (`_probe_backend_status` -> `http_ready`) | `backend_probe.ready` | yes |
| watchdog heartbeat (F-960) | `backend_probe.Heartbeat.alive` | yes, per beat |
| `tools/probe_backend_memory.py` (evidence tool) | its own `Http` | yes, from the isolated home's file |
| hermetic tests that build `tiny.http_app(...)` | none of ours | unaffected: an extra header is ignored |

## Upgrade path

* **New client, old backend:** the old backend ignores the header. The identity gate still refuses to adopt it (version/fingerprint), so it is evicted if idle, or spared and run beside if it owns live browsers (F-886).
* **Old client, new backend:** no header, 401. Probes read it as "not ready", the same fail-closed verdict the identity gate already gives another version; an idle new backend is replaced by the old proxy's own (the known mixed-install ping-pong), one that owns browsers is never evicted.
  The one real cost: an old proxy whose idle backend was replaced by a new one cannot re-bridge until its Claude Code session restarts. Nothing is needed on the new side; the first new backend creates the file.
* A token file deleted under a running backend locks its clients out until `stealthy restart`.

## Pins

All in `tests/test_http_host_origin_guard.py` (hermetic, a real in-process uvicorn socket, the backend's own `mcp.run` keywords evaluated from `server.py`'s source): no token 401, five wrong shapes 401,
the right token 200 and a session id, a 401 that does not echo the token, the health route 401/200, a rebound Host is 421 before the token is looked at, a query-string token is refused, a backend that cannot
secure the file does not build its guard, `ensure` idempotent / replaces a non-token / narrows a widened file (POSIX mode or Windows ACL) / converges across 8 racing threads / raises `ToolError` for an unusable
dir, each first-party client (probe, heartbeat, CLI session) against the real socket and, with the file unreadable, refused by every path with the CLI mapping the 401 to its one sentence.
`tests/test_backend_bearer_token_e2e.py` (integration, no Chrome): the installed launcher over real stdio starts a real backend in an isolated home, the handshake, `tools/list` and `list_instances` cross the bridge,
and the same backend answers a raw socket 401 / 401 / 200 with the file owner-only.

Mutation results (runtime rebinding in a `git archive` export, not the worktree) are in the PR body.

## Residuals

* A shared secret for a loopback socket, not a channel binding. A client sends it to `127.0.0.1:<recorded port>` without proving who answers, so a hostile local user who binds a dead backend's port before the next launch reads it from the first probe. The same user's own processes read the file as they read the browser profiles.
* `--host 0.0.0.0` / a LAN bind is still the operator's decision; the token now protects it, but the transport is plain HTTP, so on an untrusted network the token travels in clear.
* An old proxy whose backend was replaced cannot reconnect until its session restarts (above).
