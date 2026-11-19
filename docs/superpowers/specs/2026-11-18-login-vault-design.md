# Login vault: design (T-19366)

Status: approved by the owner on 2026-11-18. The one open question (how the vault is unlocked) was settled by taking the recommended option, passphrase unlock.

## 1. Goal and the one rule

Agents driving a stealth browser need to sign in to sites when a session has expired. Today the only ways are a human typing the password, or the password passing through the model's context.

The vault lets a human store logins once, through the CLI, and lets an agent ask the backend to perform a login by label.

**The one rule: no password, two-factor seed, passphrase, key or decrypted vault content ever reaches a model.** It must not appear in a tool argument, a tool result, a log line, an error report, a captured network record or a CLI output. There is no "show password" operation anywhere, including the CLI.

## 2. Threats

| Threat | Answer |
|---|---|
| A prompt-injected agent calls tools to read the password | No tool returns one; the login runs in a tab the agent cannot touch (§6) |
| An agent with a shell decrypts the vault file | The key exists only in backend memory after a human types the passphrase at a TTY (§4) |
| An agent reads the password through Chrome's DevTools endpoint (`chrome://version` gives the port; a page opens a WebSocket and attaches to the sealed target) | No agent page can reach the endpoint: the browser itself refuses every request to its debug port from a page target, the tools refuse such URLs, and Chrome no longer accepts a foreign Origin (§7) |
| An agent opens Chrome's own DevTools on the sealed tab: `navigate("chrome://inspect")`, a click on "inspect", then `execute_script` in the DevTools window it opens, whose `InspectorFrontendHost.sendMessageToBackend` sends raw CDP (no WebSocket, no Origin, no port: reproduced by the second review, password read in full) | No target an agent can address is ever a DevTools frontend or a page that opens one: the URL tools refuse them, the guard bars and closes or blanks them from Chrome's own target events whatever route made them, and the login checks before every field that none exists (§7) |
| An agent uses a tool capability that is RAW or UNBOUNDED to run code as the OS user, or to reach the vault: `create_python_binding` and `create_dynamic_hook` `exec` the caller's Python in the backend (third review: the key, the username and the password in one call; fourth: such code persists itself, e.g. a `.pth`, and runs again after any restart); a tool-written file lands where code loads (a `.pth` in `site-packages`, fourth review F1b); `execute_cdp_command` points Chrome's downloads at that folder or reads a file into a page (fifth review A1); `spawn_browser(browser_args=["--log-file=..."])` makes Chrome write one, and `--renderer-cmd-prefix` starts a program (A2); a `file:` URL reads `vault.json` and the bearer token (A3) | **Vault mode** (§7): one policy, decided by durable state. While a vault file exists, every one of those capabilities is refused in every backend, from one home (`embedded/vault_mode.py`); with no vault each behaves exactly as before. No filter on the code and no list of CDP methods is claimed, because none can be: a namespace with a few builtins is escaped through any object the code is handed, and `Target.sendMessageToTarget` carries a second CDP message as a string, so a method deny-list is bypassed by nesting |
| An agent plants a script or hook before the login | The backend opens its own fresh tab; agent hooks and capture never arm it (§6) |
| A phishing page or a browser launched to spoof the host | Exact-host HTTPS check, and refusal in browsers launched with a proxy or resolver/certificate flags (§7) |
| The secret leaks through logs, Sentry or network capture | Runtime redaction registry, an unarmed tab, and a leak test in the gate (§8) |
| An agent retries a wrong password until the account locks | One `rejected` freezes the entry until a human edits it (§7) |

Out of scope, stated in the README:
- **What the vault does not protect against, in one paragraph.** The guarantee is against an agent that has only this MCP's tools. If caller Python ever ran on this machine before the vault was created, or the agent has any other way to run code as the same OS user (a shell tool, for example), nothing here can protect the vault from what that code left behind. JavaScript-only tools stay available while a vault exists (`execute_script`, `inject_and_execute_script`, `create_persistent_function`, and `execute_python_in_browser`, which TRANSPILES its Python to JavaScript and runs it in the page): they run in the page's renderer, never in the backend, so they cannot reach the key. Python in the backend cannot be made safe short of the rule in §7: the process that holds the key is the process that would run the code.
- The signed-in session itself: an agent in a logged-in browser can act as the user.
- Any process running as the same OS user. It can read the backend's memory, and it can open Chrome's debug port DIRECTLY on the loopback interface: no browser page and no tool is involved, so no guard of ours sits in the way. The DevTools guard (§7) keeps agent *tabs* out of the endpoint; it does not, and cannot, keep a local process out. This is the same boundary as the RUNBOOK's "Local trust boundary".
- What `login` cannot check: whether another client is attached to the sealed target. CDP has no call that lists or counts a target's attached sessions (`Target.getTargetInfo` says `attached: true/false` for the whole target, and the backend's own session makes it true), so `login` does not claim it.
- Extensions installed in the profile (they can read any page), the operating system's proxy and certificate store, and a person's own `chrome://flags` are read where they can be (§7) and otherwise out of scope.
- The username: Chrome's autofill may keep it in the profile's `Web Data` (measured by `tests/test_login_vault_residue.py`; the password is in no file and `Login Data` has no row). Preventing it means setting autofill preferences before Chrome starts, in the person's own profile, which would switch off address and card autofill for every session; the vault does not.
- Login flows that do not fit username, then password. Those return `needs_user`.

## 3. Vault file

Path: `~/.stealth-mcp/vault.json` (`backend_registry.STATE_DIR`). `path_confinement` already refuses that directory to every file-taking tool.

Format, version 1:

```json
{"v": 1, "kdf": {"name": "scrypt", "n": 131072, "r": 8, "p": 1, "salt": "<b64 16 bytes>"},
 "nonce": "<b64 12 bytes>", "ciphertext": "<b64>"}
```

- The key is `scrypt(passphrase, salt)`, 32 bytes, from `hashlib.scrypt` (stdlib).
- The ciphertext is AES-256-GCM (`cryptography`, pinned `==50.0.2`, already in `uv.lock`). The associated data is the canonical JSON of `{"v", "kdf"}`, so the header cannot be swapped.
- A fresh nonce is drawn on every write. The whole plaintext is one JSON document; labels and sites are encrypted too.
- Writes are atomic (temp file, owner-only, replace), following `backend_token._create` and `backend_registry._commit`.

Plaintext:

```json
{"entries": {"google-work": {"kind": "password", "site": "google.com",
   "login_url": "https://accounts.google.com/", "hosts": ["accounts.google.com"],
   "username": "…", "password": "…", "show_username": false, "frozen": null},
  "shopify-store": {"kind": "provider", "site": "shopify.com", "via": "google-work",
   "allow_consent": false}}}
```

- `kind: "password"` holds a secret. `hosts` is the exact list of hosts the secret may be typed on.
- `kind: "provider"` holds no secret: it says this site signs in with that other entry's account. `via` must name a `password` entry.
- `frozen` is null, or the UTC time a login was rejected.

## 4. Unlocking (the ssh-agent model)

- `stealthy login unlock [--for 8h]` prompts for the passphrase with `getpass`. It refuses to run when stdin is not a TTY, so a script or an agent cannot feed it.
- The CLI derives the key locally and posts it to the backend at `POST /_stealth/vault/unlock`. This is a custom route, behind the existing bearer-token middleware, and is not an MCP tool.
- The backend proves the key by decrypting the file, then keeps the key in memory only. `--for` sets an expiry; the default is until the backend exits. `stealthy login lock` posts to `/_stealth/vault/lock`.
- A locked vault makes `list_logins` and `login` raise a `ToolError` saying a human must run `stealthy login unlock`.
- The route accepts a key, never returns one, and there is no route that reads entries with secrets.

Every write (`add`, `edit`, `remove`) is done by the CLI process itself, which asks for the passphrase each time. The backend never writes the vault, with one exception: setting `frozen` after a rejected login, which it can do because it holds the key.

## 5. Surfaces

### CLI (`cli_login.py`, a new module; `cli.py` is at its 1000-line cap)

| Command | Does |
|---|---|
| `stealthy login init` | Creates the vault; lists everything vault mode will restrict, then asks for a new passphrase twice (and warns when it has fewer than 12 characters: a copy of the file can be guessed at offline). Refuses while any backend in `server.json` has run caller Python or cannot say (an older release, a refused connection to a process that still runs, an unreadable registry); writes the file; asks again, and takes the file back on a late finding, an interrupt or a crash |
| `stealthy login add LABEL --site S --login-url URL [--host H]... [--user U] [--show-username]` | Prompts for username (if not given) and password, hidden |
| `stealthy login add LABEL --site S --via OTHER [--allow-consent]` | Adds a provider entry; no secret prompt |
| `stealthy login list` | Labels, kinds, sites, hosts, frozen state. Never a password; a username only with `--usernames` |
| `stealthy login edit LABEL [...]` | Changes fields, re-prompts the password with `--password`, clears `frozen` |
| `stealthy login remove LABEL` | Removes an entry |
| `stealthy login unlock [--for D]` / `lock` / `status` | §4. `unlock` runs the same backend check as `init` before it asks for a passphrase (a vault restored from a backup never went through `init`). `status` prints the vault, **vault mode ON or OFF** with every restriction, the backend check, and a warning when this shell and the backend disagree about whether a vault exists |
| `stealthy login audit [-n N]` | Prints the audit log |
| `stealthy login destroy` | A terminal and the typed word `DESTROY`. Locks EVERY backend `server.json` records (and the one this shell resolves), then deletes the file; a backend that could not be locked is named, and the file is still deleted. A directory at the path, or a delete that fails, is a one-line error and the mode stays on. This is how vault mode ends |

Secrets are read only from a hidden prompt, or from stdin with `--password-stdin`. They are never an argument or an environment variable.

### MCP tools (a new section `logins`, 100 → 102 tools, 13 → 14 sections)

- `list_logins() -> {"unlocked": bool, "logins": [{"label", "kind", "site", "via"?, "frozen", "username"?}]}`. `username` is present only for entries with `show_username`.
- `login(instance_id, label) -> {"outcome", "label", "detail"}`.

`outcome` is one of:

| Outcome | Meaning |
|---|---|
| `logged_in` | Submitted; the password field is gone and the page left the login hosts |
| `already_logged_in` | The login URL redirected away from the login hosts without showing a form |
| `needs_totp` | A one-time-code field is showing |
| `needs_user` | A step the vault cannot take (passkey, device prompt, captcha puzzle, unknown form) |
| `rejected` | The password field came back after submit; the entry is now frozen |
| `use_provider` | A provider entry: the provider account is signed in; the agent now clicks the site's provider button, then calls `choose_account` |

- `choose_account(instance_id, label)` is deferred to phase 2 (§10) with the rest of the provider flow. Phase 1 returns `use_provider` after making sure the provider account is signed in, which already removes the password from the agent's path.

## 6. The sealed login

`login_seal.py` owns one fact: which `(instance, target)` pairs are sealed, and which instances have a seal in progress.

Sequence for a `password` entry:

1. Check the vault is unlocked, the entry is not frozen, and the instance passes the launch check (§7).
2. Mark the instance as sealing. From here until step 8:
   - `ToolRegistry.section_tool`'s wrapper refuses any tool call naming a sealed `tab_id`.
   - `execute_cdp_command`, `switch_tab`, `go_back` and `go_forward` are refused for the whole instance: `Target.*` commands and a bind can reach any tab, and history can return a tab to a page the URL tools refuse.
   - `list_tabs` and the `browser://` resources leave the sealed tab out.
3. Open a backend-owned tab with `tab_open.open_tab(browser, "about:blank", background=True)` and `tab_open.mark_internal`. It is never passed to `tab_binding.adopt`, so the network interceptor and dynamic hooks never arm it. A popup it opens is sealed and hidden in the `targetCreated` handler itself (`login_seal.seal_popup`) and then closed; the seal is dropped only when Chrome no longer lists it. The captcha monitor scans `tab_open.visible_tabs`, the tools' own view, so it never touches the sealed tab.
4. Navigate to `login_url`. Wait for a visible username or password field in the top document or a direct child frame.
5. Before each field: re-read the frame's URL and require `https` and a host in `hosts` (§7).
6. Type with `text_entry.type_characters`. Two-step forms: username, Enter, wait for the password field, password, Enter.
7. Wait up to 15 s for an outcome (§5). `logged_in` needs POSITIVE evidence: the page left the login hosts, or the site set or changed a cookie for them since just before Enter (`login_evidence.py`). A page that reloads with no form and neither (an "account locked" page, a captcha, "approve on your phone") is `needs_user`: never `logged_in`, never a freeze. `logged_in` after a gained cookie ALSO needs the login URL, visited again, to no longer show its form (a "locked" page that sets a CSRF cookie is `needs_user`). `rejected` (which freezes) needs POSITIVE evidence on three consecutive polls and for 2 s: the login form back in a NEW document, or an error (`[role=alert]`, `[aria-live=assertive]`, `[aria-invalid=true]`) the page did not show before Enter. An emptied password field is not evidence (a slow single-page app blanks it on submit and signs in seconds later). Every ambiguity is `needs_user`, never a guessed `logged_in` and never a freeze. A `rejected` whose freeze could not be written says so. A single-page app that signs in with a token it keeps in `localStorage` and sets no cookie therefore reads `needs_user`, the safe side.
8. Always, in `finally`, in a shielded task so a cancellation cannot cut it short: clear any password field still holding a value, close the sealed tab and check that Chrome no longer lists it (three tries), and only THEN drop the seal. A tab that will not close stays sealed (hidden from every tool until the backend restarts) and the answer says so. The cookies are in the profile, so the agent's own tabs are signed in after a reload.

Field detection (`login_form.py`), in the tab's isolated world (`page_natives.frame_world`):
- password: a visible `input[type=password]`.
- username: a visible `input` with `autocomplete` `username` or `email`, or `type=email`, or the text input before the password field in the same form.
- one-time code: `autocomplete="one-time-code"` or `inputmode="numeric"` with `maxlength` ≤ 8.

## 7. Guards

- **Launch check** (`browser_trust.py`). Refuse when the instance has a proxy, or when its command line (`browser_cmdline.arguments(pid)`, which also covers re-attached browsers) carries a switch that changes name resolution, proxying, certificate checking or the same-origin policy, or lets another party's code or client in. Switches are normalised the way Chrome's `base::CommandLine` reads them (`--name`, `-name`, `/name`; case-insensitive; `=value`), so `-Ignore-Certificate-Errors` is `--ignore-certificate-errors`. The refused set comes from the switch names in Chrome 158's own binary, read against Chromium's switch sources (the module lists them and says so). It is a deny list because a re-attached browser has only its argv and Chrome's argv holds a hundred benign switches, so a list of the safe ones cannot be kept. The browser process's environment (`SSLKEYLOGFILE`, the proxy variables on Linux and macOS; an unreadable environment counts as a refusal), the profile's `Local State` (`chrome://flags` experiments) and enterprise policy (Windows registry, Linux `policies/managed`) are read as well.
- **The debug port** (`debug_port_guard.py`). Chrome's endpoint on `127.0.0.1:<random>` is what let a page read the sealed tab (reproduced by the reviewer: `chrome://version` for the port, a WebSocket from page script, `Target.attachToTarget`, `Runtime.evaluate` on the password field). Two things are made true. (1) `--remote-allow-origins=*` is no longer passed (and is stripped from caller `browser_args`): nodriver connects with no `Origin` header, which Chrome 158 accepts without the flag, while a page's WebSocket carries an Origin and gets 403. (2) Chrome still accepts the endpoint's own origin, so a page navigated to `http://127.0.0.1:PORT/` could open the socket. A guard-owned browser-level connection therefore sets `Target.setAutoAttach` (`waitForDebuggerOnStart`, `flatten`) and, on every page target BEFORE it runs, `Fetch.enable` with a pattern for the debug port that answers `failRequest`: navigations, redirects, iframes, popups, `fetch` and XHR to the port, whatever loopback spelling, never load. The tools also refuse such URLs (`navigate`, `new_tab`, `create_simple_dynamic_hook`: a clear error, not the guarantee). **Which mechanism blocks WebSocket handshakes was proved by test:** neither `Network.setBlockedURLs` nor `Fetch` sees a WebSocket handshake, so the socket itself is not blocked by interception; it is refused by Chrome's Origin check (1), and (2) denies the one origin that check lets through. The guard's `Fetch` session is its own, so it does not touch the network interceptor (the `Network` domain) or dynamic hooks (their own `Fetch` session on the page): the e2e network-capture and hook tests pass with the guard active. The same interception is put on EVERY target that can make requests, not only pages: auto-attach is recursive, so a cross-site iframe (its own process), a dedicated, shared or service worker is guarded before it runs. Measured on Chrome 158: `Fetch.enable` is refused on a dedicated worker (best effort there: its page's interception and Chrome's Origin check stand for it), and a frame nested at the endpoint's own origin still commits despite the interception, so a document seen at that origin is left paused (its script never runs) and the page that holds it is sent to `about:blank`. The login's own tab and its popups get the interception but are not watched for children (`login_seal.owns`): a paused popup of a watched page held its opener inside `window.open` until the login's popup handling had closed it, and the key press that opened it never returned (measured, `tests/test_login_vault_window.py`). Those pages are the site's, which the vault types into anyway; an agent's pages keep the watch. A browser started by an older backend, with the flag still on, is covered by (2) only.
- **Inspectors** (`debug_port_guard.py`: `inspector_url`, `verify_clean`; `login_seal.bar`). A DevTools frontend is a `page`-type target at `devtools://devtools/bundled/...`, opened by Chrome itself (`chrome://inspect`, F12), so it needs no WebSocket and the port guard never sees it. The invariant: no target the agent can address is ever one. (a) `navigate`, `new_tab` and `create_simple_dynamic_hook` refuse `devtools:`, `chrome-devtools:` and the `chrome://` pages that open an inspector, in every spelling Chrome accepts (case, `about:inspect`, a tab or newline inside the scheme, `view-source:`); those pages are `inspect` (an "inspect" link per target), `serviceworker-internals` (an "Inspect" link per service worker) and `extensions` ("Inspect views"), picked from Chrome 158's `chrome://chrome-urls` (about 95 pages; the rest show information or settings with no debugger action). (b) Whatever route made one (a redirect, an in-page location change, `execute_cdp_command`, history, a click, F12), the guard reads Chrome's own `targetCreated` / `targetInfoChanged` events, judges the COMMITTED URL, bars the target from `list_tabs` and every tool at once, sends a page that navigated to `chrome://inspect` back to `about:blank`, and CLOSES a `devtools://` window when a login is running or opening (otherwise it is only hidden). (c) Before the tab opens and before every field it types, `verify_clean` requires the guard to be running and no inspector to exist: it closes one that is there, and a window that stays open after three tries refuses the login; a guard whose connection dropped while the browser lives refuses the same way (fail closed). Answers asked of this design: *a DevTools window already open when a login starts* (a headed person's F12) is hidden until then and closed by the login before it types; *`chrome://inspect` in a second browser window* is a new target judged the same way and blanked; *the remote frontend* `http://127.0.0.1:PORT/devtools/inspector.html` is the debug port, refused as a URL, failed by the interception and blanked by the belt above.
- **Browser-internal pages, as a class (re-review 3, F3).** The inspector list above is a denylist of pages that open a debugger; a class rule does not depend on knowing the next one. For the duration of a login, every target whose scheme is `chrome:`, `chrome-untrusted:`, `devtools:`, `chrome-devtools:`, `chrome-extension:` or `chrome-search:` is barred: hidden from `list_tabs`, unaddressable by every tool (`login_seal._internal`, judged from the same Chrome events), closed where it is a DevTools window, and `navigate` / `new_tab` refuse to load one (`debug_port_guard.refusal`). `about:blank`, the new-tab page and `chrome-error:` pages are not of the class. Outside a login only the narrow inspector refusal applies, so ordinary `chrome://` use is untouched.
- **Spellings (F2).** URLs are judged the way Chrome reads them: leading and trailing whitespace and control characters dropped, the scheme and host lower-cased, the host percent-decoded to a fixed point (more readings than Chrome makes: the safe side) and NFKC-folded (full-width forms), a trailing dot and a default port removed, userinfo ignored, `about:` taken as `chrome:`, and a `view-source:` prefix looked through. `chrome://%69nspect/#pages` is `chrome://inspect/`. The tests carry the spellings that were found, and the class rule above is the backstop for any that are not.
- **When the guard drops (F4).** A guard whose socket ends does not lift its bars: a DevTools window it barred stays barred and unaddressable, because the moment the guard is gone is the moment a window must stay out of reach; only Chrome's own `targetDestroyed`, which only a live guard hears, lifts one. A login in flight runs inside `debug_port_guard.watch(browser)`: when the guard ends, the login is cancelled at its next await, its `finally` clears the field it typed and closes its tab, and the call raises `GUARD_DOWN`. Nothing is submitted after the drop.
- **What a login does to the browser (F5).** It closes every DevTools window open in that browser, and `login` says how many (`closed_devtools_windows`, 0 when none). It refuses `go_back` / `go_forward` for its duration (§6). A barred DevTools window with no login running is refused with its own sentence, not with "a vault login is running".
- **Vault mode (`embedded/vault_mode.py`).** The fourth review found that closing routes one at a time does not converge (a tool-written `.pth`, then raw CDP, then Chrome's own `--log-file`, each a way to put an agent's bytes where code loads or to read a file), so there is ONE policy and ONE home: **while a vault file exists, every raw or unbounded tool capability is refused, in every backend**. `tool_registry._refusing_sealed_tabs` calls `vault_mode.admit(tool, arguments)` for every call of a gated tool; `debug_port_guard`, `debug_logger` and the CLI ask the same module. It is decided by durable state and never by process history (a per-process "taint" was replaced because the code can persist itself): `vault_exists()` is an `lstat` of `vault_store.VAULT_PATH`, a file, a directory or a dangling link all count, and an error other than "not there" counts too (fail closed). With no vault file nothing here changes, so a person who never makes a vault sees no difference. Every refusal is a `CallerError` that names the vault and `stealthy login destroy`.
  - **Caller Python** (`RUNS_CALLER_PYTHON`): `create_python_binding` and `create_dynamic_hook` are refused whole. The process that holds the key would run the code, and restricting builtins does not help. `create_simple_dynamic_hook` is not in the set: its code is a fixed template with the caller's values written in as `repr` literals. *Order:* a vault is not created while a backend has run one: `stealthy login init` asks every backend in `server.json` (`GET /_stealth/vault/caller-python`, the tool name only), writes the file, and asks again; `admit` marks the process BEFORE it reads the file, so whichever comes later sees the other (Dekker order). A census test walks `src` by AST and fails when an `exec`, `eval`, `compile` or import-by-name call on caller-derived input is not pinned to a tool in the set.
  - **Raw CDP** (`RAW_CDP`): `execute_cdp_command` is refused whole; `list_cdp_commands` stays (information). `Browser.setDownloadBehavior` / `Page.setDownloadBehavior` make Chrome save a page's bytes under a page's name in any folder, `DOM.setFileInputFiles` reads a local file into a page, `Extensions.loadUnpacked` loads code, `Target.createTarget` loads a `file:` URL, and `Target.sendMessageToTarget` / `attachToTarget` / `setAutoAttach` carry a second CDP message as a string, so a deny-list of methods is bypassed by nesting; the product itself uses none of them (nothing in `src` sets a download folder but the guard). A census test fails when a tool hands a caller's method to `tab.send` or reaches the executor's entry point without being in the set.
  - **Browser arguments** (`ALLOWED_SWITCHES`, `check_browser_args`): `spawn_browser(browser_args=...)` takes an ALLOW-list, because Chrome has switches that write a named file (`--log-file`, `--net-log`, `--trace-startup-file`) or start a program (`--renderer-cmd-prefix`, `--utility-cmd-prefix`, `--gpu-launcher`, `--browser-subprocess-path`) and a list of what to refuse is the shape that failed twice. Names come through `browser_trust.switch_name`, the one normaliser (`--name`, `-name`, `/name`, any case), and the value must match a pattern. Chosen from what the product's README, docs and tests use (the stealth notes and test files: size, language, user agent, the feature lists), one by one: `--window-size=W,H` (digits; where the window opens), `--window-position=X,Y` (integers; the same), `--start-maximized` (no value), `--lang=` (a locale tag; UI language), `--accept-lang=` (locale list; a header), `--user-agent=` (any text without control characters; a header, and the product's own switch). The feature-list switches (`--disable-features=`, `--enable-features=`, `--enable-blink-features=`, and `--disable-blink-features=` with any value but one) are NOT allowed (audit 6, N1); the one exception (audit 7, F6) is the exact literal `--disable-blink-features=AutomationControlled`, which stops Chrome telling a page it is automated and names no file, program, address or security control (the pattern is that literal alone, so `AutomationControlled,Foo` is refused): the names are an open vocabulary of hundreds of features, some of which are security controls (site isolation, private-network protection, certificate and resolver paths), and no short list of names can be argued from a use the product documents. A login launch check does not read their values either: the product's own launch passes `--disable-features=IsolateOrigins,site-per-process` (nodriver) and the DBSC list, so a value rule there would refuse the product's own browsers. A bare argument is Chrome's start page and is allowed only as `about:blank` or an http(s) URL that is not the debug port. Everything else is refused **naming the switch, never its value**. The proxy switch stays refused (`browser_trust` refuses a login in a proxied browser), and a login is refused in a browser started with any program-launch or trace-file switch or a `--log-file` that is not the product's own `chrome-*.log` in its log directory (a browser started before the vault existed). `user_data_dir` (the deprecated path alias) takes a NAME only; `session` is unchanged.
  - **`file:` URLs.** `navigate`, `new_tab` and `create_simple_dynamic_hook` refuse `file:` and `view-source:file:` in every spelling `debug_port_guard.file_url` reads as Chrome does (case, a tab or newline inside, leading control characters, nested `view-source:`, a Windows drive or UNC path written where a URL goes). That is the clear answer, not the guarantee: a target that COMMITS a `file:` URL by any route (a redirect, `go_back`, a page that was open before the vault) is barred from every tool and blanked exactly like an inspector (`barred_url`, judged from Chrome's own target events). Without it a tools-only agent reads `vault.json` (scrypt + AES-GCM, so a copy can be attacked offline) and the bearer token.
  - **Pickle** (`export_format`): `export_debug_logs` writes JSON only. `auto` means JSON in the mode, and an explicit `pickle` or `gzip-pickle` is refused, at the one place the format is chosen (so the lock-free fallback cannot pick one either). A pickle keeps an agent's strings raw, which puts a line start of the agent's choosing into any tolerant line-oriented file a path rule does not name.
  - **Downloads** (`download_pin`, kept by `debug_port_guard`): every browser the guard protects is pinned with `Browser.setDownloadBehavior(allowAndName)` to one folder, `~/Downloads/stealth-mcp-downloads`: Chrome saves a download under a generated name with no extension, so a page chooses neither the folder nor a name code runs by. If that folder cannot be made or lies where `path_confinement` refuses a write, downloads are `deny`ed: a pin that cannot be made safe is no pin. **How it is enforced for a re-attached browser:** spawn and re-attach (and the delegated headed launch) all end in `debug_port_guard.protect`, which sends the pin before anyone is handed the browser and fails the spawn or the attach when it cannot (the guard is already how a browser is refused when it cannot be guarded). It is also re-sent every `KEEP_SECONDS` (5 s) by the guard that holds the browser, so a vault created after the browser was started, or a folder an agent set before the vault existed, is taken back within that time; the same pass judges every open target again, so a `file:` page that was open when the vault appeared is blanked. When the mode ends the guard sends `default`. Nothing can undo the pin in between: `execute_cdp_command` is refused, and no other tool sets a download folder. A folder set on ONE TAB (`Page.setDownloadBehavior`, before the vault existed) does not win over the browser-level pin: measured on Chrome 158 by `tests/test_login_vault_mode.py` (a control with no vault proves the tab-level setting works; with the vault the next download lands in the pinned folder), so the keeper sends no per-page reset; that test goes red if a Chrome release changes it. **A pin that fails is not silent:** spawn and attach fail when it cannot be made, and a LATER pass that fails marks the browser (`_Guard.pin_failed`), logs one error, and `verify_clean` refuses every vault login in that browser until a pass succeeds. (No tool depends on downloads, so nothing else needs to say why.)
  - **Browser WebUI pages** (`webui_url`, `login_seal.mark_webui`): while the mode is on, a target at `chrome://`, `chrome-untrusted://` or `devtools://` (the new-tab page excepted; `view-source:` read through) is hidden from the tools and refused by `tab_id` with the vault sentence, and every URL tool refuses to load one, with or without a login (a login still hides the wider internal class, only while it runs). `about:blank` and the new-tab page work; with no vault nothing changes. Hidden, not blanked, because the product's own session restore opens `chrome://settings` in a persistent profile and a blanked page would stop logins from being kept. A tool call with no `tab_id` acts on the browser's current tab, which a person could leave on such a page; that is the one residual.
  - **Paths the tools read or write** take `path_confinement` in every mode (fourth review F1b): `upload_file` and `import_network_data` read through the secret-location rule; `export_debug_logs`, `export_network_data` and `take_screenshot` write through `refuse_write_path` (the secret-location rule, plus no code-loading location, plus no code-like name; the real path is the one opened). The check resolves dozens of roots, which is a second on a cold disk, so every tool calls it on a worker thread, never on the event loop. A census test fails when a tool takes a file-path parameter that is not pinned to its rule, and a spy test proves each writer asks `refuse_write_path` before it touches a path.
  - **What vault mode does not do** (the one-paragraph honesty statement is in §2, unchanged). A backend of a release before vault mode has no such gate: `init`, `unlock` and `status` name it ("cannot say") and refuse, but a backend started from an older install AFTER a vault exists has both Python tools enabled until it is restarted on this release. A native file or folder dialog in a headed browser (a person's own click) is not an agent's tool. A download's content is whatever the page sent, in a generated name in one folder. The pin and the `file:` bar are re-asserted every five seconds, so there is a window of that length for a browser that was already running when the vault appeared. A browser started before the vault keeps the launch switches it was started with (a proxy, `--remote-allow-origins=*`, an old `--user-data-dir` path): the mode pins its downloads and bars `file:` and WebUI pages, it does not rewrite the browser.
- **Origin check.** Scheme `https`, the default port, host exactly in `hosts` (no suffix matching), re-checked immediately before each field is typed in BOTH the field's frame and the top frame (a login page framed by another site is not typed into), and the form around the field must submit (its `action`, or the default button's `formaction`, resolved by the browser) to a login host. A field in no `<form>` has no action to check. A certificate error page has a `chrome-error://` URL and fails this check.
- **Freeze.** A `rejected` outcome sets `frozen`; `login` then refuses the entry until `stealthy login edit` clears it. The write is version-checked under a lock file the CLI's writes share: a `stealthy login edit` that lands in between is not lost (the freeze is applied again to the new file), and a CLI edit made from an older read is refused. An agent can still cause a freeze it did not earn (it can change cookies or headers in its own tab, which the site reads); that is an availability trade-off, not a leak.
- **Chrome's password manager.** Chrome saves a password only when its save prompt is accepted, and the sealed tab is closed without touching the prompt. `tests/test_login_vault_residue.py` asserts that `Login Data` holds no row and that the password is in no file of the profile after a sealed login; the username is measured (§2).
- **Audit log.** `~/.stealth-mcp/vault-audit.jsonl`, one line per attempt: time, label, instance, caller id, outcome. Never a secret, never a username.
- **Test origins.** `STEALTH_MCP_VAULT_ALLOW_HTTP_LOOPBACK=1` lets `http://127.0.0.1` pass the origin check. It exists for the test suite and is documented as unsafe.

## 8. Leak guards

- `secret_registry.py`: a process-wide set of exact secret strings (each password while the vault is unlocked). `redact(text)` replaces any of them with `[vault-secret]`.
- It is applied in three places: `observability._redact_text` (Sentry), the logging record factory (`logging_setup`), and `DebugLogger`'s ring (so `get_debug_view` and `export_debug_logs` are covered).
- The login code never puts a secret in an exception message. Secrets travel in a `Secret` wrapper whose `repr` and `str` are `***`.
- The sealed tab is never armed, so its requests are not in the network store.

**The leak test** (`tests/test_login_vault_leak.py`, integration, in the release gate): a local login page, a vault holding a random 24-character password, one sealed login. Then every one of these is searched for the password, plain and URL-encoded, and must not contain it:
- the `login` tool result and `list_logins`;
- `get_debug_view`, `export_debug_logs`;
- `list_network_requests` and each request's details;
- the backend log file;
- a captured Sentry event from a forced failure inside the login.

## 9. Code layout

| File | Owns |
|---|---|
| `embedded/vault_crypto.py` | The file format, scrypt, AES-GCM, atomic owner-only write |
| `embedded/vault_store.py` | Entry model and validation, the in-memory key and expiry, freeze, audit |
| `embedded/secret_registry.py` | The redaction set |
| `embedded/login_seal.py` | Sealed tabs and sealing instances; the refusal check |
| `embedded/login_form.py` | Field detection and outcome detection (the page-side JS and its reader) |
| `embedded/login_flow.py` | The sealed login sequence and the launch and origin guards |
| `embedded/vault_mode.py` | THE one home of vault mode: `vault_exists`, the restriction list (`RESTRICTIONS`, printed by `init` and `status`), `admit` (caller Python, raw CDP, browser arguments), the `file:` sentence, `export_format`, `download_pin` |
| `embedded/tool_sections/logins.py` | `list_logins`, `login` |
| `embedded/vault_routes.py` | The four custom routes (unlock, lock, status, caller-python) |
| `cli_login.py` | The `login` verb |

Existing files touched, each by a few lines: `tool_registry.py` (seal check), `tool_sections/cdp_functions.py` and `tabs.py` (instance-wide refusal, hidden tab), `embedded/server.py` (section, routes, `--disable-logins`), `observability.py`, `logging_setup.py`, `debug_logger.py` (redaction), `cli.py` (one dispatch entry), `pyproject.toml` (`cryptography==50.0.2`). Files at their line cap get no net growth.

Counts and docs updated in the same PR: the tool-count assertions and goldens listed in the plan, `RELEASE_CONTRACT.md` (generated), README, CLAUDE.md, DESIGN.md, NAVMAP.md, `agent-setup/prompt.md`, CHANGELOG.

## 10. Phases

- **Phase 1 (this spec):** the vault, the CLI, unlock, the sealed password login for one-step and two-step forms, provider entries that return `use_provider`, all guards, the audit log and the leak test.
- **Phase 2:** `choose_account` for the Google account chooser and consent handling; TOTP seeds and `needs_totp` completion.
- **Phase 3:** an opt-in OS-keychain "remember" mode, importers.

## 11. Tests

- **Crypto (unit):** round trip; wrong passphrase fails; a flipped ciphertext byte fails; a changed header fails; every write uses a new nonce.
- **Store (unit):** validation (a provider entry must point at a password entry; hosts are bare hostnames); expiry locks; freeze persists; the audit line has no secret.
- **Seal (unit):** a sealed tab is refused through the registry wrapper for a representative tool of every section; `execute_cdp_command` and `switch_tab` are refused instance-wide; the seal is dropped on success, failure and cancellation.
- **Guards (unit):** each forbidden launch flag refuses; lookalike hosts (`accounts.google.com.evil.io`, `xaccounts.google.com`) and `http` are refused.
- **Vault mode (unit):** one test file per side. Each capability class is pinned on both sides (refused with the mode on, behaves as it did with none): `tests/test_vault_mode.py` (caller Python, the CLI, destroy, unlock, status, init races), `tests/test_vault_mode_classes.py` (raw CDP, the switch allow-list and its every spelling, `file:` URLs, the export format, the download pin) and the guard's conversation with a fake endpoint in `tests/test_debug_port_guard.py` (the pin before the browser is handed on, re-asserted, lifted; a `file:` page barred and blanked). **Census tests** fail when a NEW tool forwards raw CDP, takes browser arguments, a URL, a file path or an export format without being in the home's list. `tests/test_launcher_script.py` parses the PowerShell launcher the way its tokenizer does (quote characters U+2018 to U+201B, backtick, `$(`) and checks the BOM; nothing is launched.
- **Flow (integration, real Chrome, local pages):** one-step form, two-step form, wrong password → `rejected` and frozen, already signed in, a form inside an iframe, an unknown form → `needs_user`.
- **Leak test:** §8.
- **CLI (unit):** no command prints a password; `unlock` refuses a non-TTY stdin; `add` refuses a password argument.
- **Mutants:** one per guard (origin check, launch check, seal check, redaction, freeze); each must fail the suite.
