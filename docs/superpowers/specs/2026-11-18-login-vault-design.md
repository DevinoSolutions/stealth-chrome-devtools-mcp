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
| An agent plants a script or hook before the login | The backend opens its own fresh tab; agent hooks and capture never arm it (§6) |
| A phishing page or a browser launched to spoof the host | Exact-host HTTPS check, and refusal in browsers launched with a proxy or resolver/certificate flags (§7) |
| The secret leaks through logs, Sentry or network capture | Runtime redaction registry, an unarmed tab, and a leak test in the gate (§8) |
| An agent retries a wrong password until the account locks | One `rejected` freezes the entry until a human edits it (§7) |

Out of scope, stated in the README:
- The signed-in session itself: an agent in a logged-in browser can act as the user.
- Malware running as the same OS user. It can read the backend's memory or attach to Chrome's debugging port.
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
| `stealthy login init` | Creates the vault; asks for a new passphrase twice |
| `stealthy login add LABEL --site S --login-url URL [--host H]... [--user U] [--show-username]` | Prompts for username (if not given) and password, hidden |
| `stealthy login add LABEL --site S --via OTHER [--allow-consent]` | Adds a provider entry; no secret prompt |
| `stealthy login list` | Labels, kinds, sites, hosts, frozen state. Never a password; a username only with `--usernames` |
| `stealthy login edit LABEL [...]` | Changes fields, re-prompts the password with `--password`, clears `frozen` |
| `stealthy login remove LABEL` | Removes an entry |
| `stealthy login unlock [--for D]` / `lock` / `status` | §4 |
| `stealthy login audit [-n N]` | Prints the audit log |

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
   - `execute_cdp_command` and `switch_tab` are refused for the whole instance, since `Target.*` commands and a bind can reach any tab.
   - `list_tabs` and the `browser://` resources leave the sealed tab out.
3. Open a backend-owned tab with `tab_open.open_tab(browser, "about:blank", background=True)` and `tab_open.mark_internal`. It is never passed to `tab_binding.adopt`, so the network interceptor and dynamic hooks never arm it. Popups it opens are closed, not armed.
4. Navigate to `login_url`. Wait for a visible username or password field in the top document or a direct child frame.
5. Before each field: re-read the frame's URL and require `https` and a host in `hosts` (§7).
6. Type with `text_entry.type_characters`. Two-step forms: username, Enter, wait for the password field, password, Enter.
7. Wait up to 15 s for an outcome (§5).
8. Always, in `finally`: clear any password field still holding a value, close the sealed tab, drop the seal. The cookies are in the profile, so the agent's own tabs are signed in after a reload.

Field detection (`login_form.py`), in the tab's isolated world (`page_natives.frame_world`):
- password: a visible `input[type=password]`.
- username: a visible `input` with `autocomplete` `username` or `email`, or `type=email`, or the text input before the password field in the same form.
- one-time code: `autocomplete="one-time-code"` or `inputmode="numeric"` with `maxlength` ≤ 8.

## 7. Guards

- **Launch check.** Refuse when the instance has a proxy, or its command line (`browser_cmdline.arguments(pid)`, which also covers re-attached browsers) carries `--host-resolver-rules`, `--host-rules`, `--ignore-certificate-errors`, `--ignore-certificate-errors-spki-list`, `--allow-insecure-localhost`, `--unsafely-treat-insecure-origin-as-secure` or `--proxy-server`.
- **Origin check.** Scheme `https`, host exactly in `hosts` (no suffix matching), re-checked immediately before each field is typed. A certificate error page has a `chrome-error://` URL and fails this check.
- **Freeze.** A `rejected` outcome sets `frozen`; `login` then refuses the entry until `stealthy login edit` clears it.
- **Chrome's password manager.** Chrome saves a password only when its save prompt is accepted, and the sealed tab is closed without touching the prompt. A test asserts `Login Data` in the profile holds no row for the test host after a sealed login.
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
| `embedded/tool_sections/logins.py` | `list_logins`, `login` |
| `embedded/vault_routes.py` | The two custom routes |
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
- **Flow (integration, real Chrome, local pages):** one-step form, two-step form, wrong password → `rejected` and frozen, already signed in, a form inside an iframe, an unknown form → `needs_user`.
- **Leak test:** §8.
- **CLI (unit):** no command prints a password; `unlock` refuses a non-TTY stdin; `add` refuses a password argument.
- **Mutants:** one per guard (origin check, launch check, seal check, redaction, freeze); each must fail the suite.
