# Login Vault (phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A passphrase-encrypted login vault in the backend: a human stores logins through `stealthy login`, and an agent calls `login(instance_id, label)`, which signs in inside a sealed backend-owned tab and returns only an outcome.

**Architecture:** Pure leaf modules first (crypto, store, redaction registry, seal), then the page-driving flow, then the tool section, routes and CLI, then counts and docs. The key lives only in backend memory after `stealthy login unlock`. Spec: `docs/superpowers/specs/2026-11-18-login-vault-design.md`.

**Tech Stack:** Python 3.11+, `hashlib.scrypt`, `cryptography==50.0.2` (AES-GCM), nodriver/CDP, FastMCP custom routes, argparse, pytest.

## Rules for every task

- Worktree: `C:\Users\amind\AppData\Local\Temp\wt-login-vault`, branch `feat/login-vault`. Work nowhere else.
- Imports are absolute: `from stealth_chrome_devtools_mcp.embedded.X import Y`. No module under `embedded/` imports `server`.
- Tools raise `tool_errors.ToolError`; never return `{"success": False}`.
- No file may exceed 1000 lines (`tools/check_file_budgets.py`). `cli.py`, `logging_setup.py` and `browser_management.py` are AT the cap: any line added there needs one removed there. Never raise a cap.
- Tests import helpers as `from fakes import …`, never `from tests import …`.
- Never put a secret in an exception message, a log call or a test name.
- Run tests with `uv run --no-sync pytest -q -p no:cacheprovider <files>`.
- Commit with plain `git commit` (hooks on). Never `--no-verify`, never `git -c`, never `git stash`, never change `core.hooksPath`. Do not push.
- Do not run `du`, `ls` or `find` over `%TEMP%`. Do not spawn subagents or reviewers.

## File map

| File | Responsibility |
|---|---|
| `src/stealth_chrome_devtools_mcp/embedded/vault_crypto.py` | File format, scrypt, AES-GCM, atomic owner-only write |
| `…/embedded/secret_registry.py` | Exact-string redaction set, `Secret` wrapper |
| `…/embedded/vault_store.py` | Entries, validation, in-memory key and expiry, freeze, audit |
| `…/embedded/login_seal.py` | Sealed tabs, sealing instances, refusal check |
| `…/embedded/login_form.py` | Page-side field and outcome detection |
| `…/embedded/login_flow.py` | Launch guard, origin guard, the sealed login sequence |
| `…/embedded/tool_sections/logins.py` | `list_logins`, `login` |
| `…/embedded/vault_routes.py` | `POST /_stealth/vault/unlock`, `/lock`, `GET /status` |
| `src/stealth_chrome_devtools_mcp/cli_login.py` | The `login` CLI verb |

---

### Task 1: `vault_crypto`

**Files:** Create `embedded/vault_crypto.py`, `tests/test_vault_crypto.py`. Modify `pyproject.toml` (add `"cryptography==50.0.2",` to `[project.dependencies]`; confirm the version with `grep -A1 'name = "cryptography"' uv.lock`).

Interface:

```python
VERSION = 1
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**17, 8, 1

class VaultError(Exception): ...           # wrong key, tampered file, bad format
def new_header() -> dict                   # {"v": 1, "kdf": {"name": "scrypt", "n", "r", "p", "salt": b64}}
def derive_key(passphrase: str, header: dict) -> bytes      # 32 bytes
def seal(key: bytes, header: dict, plaintext: dict) -> dict  # header + fresh "nonce" + "ciphertext"
def open_sealed(key: bytes, document: dict) -> dict          # raises VaultError
def read_file(path: Path) -> dict                            # raises VaultError if missing/not JSON
def write_file(path: Path, document: dict) -> None           # temp file, owner-only, os.replace
```

The AES-GCM associated data is `json.dumps({"v": h["v"], "kdf": h["kdf"]}, sort_keys=True, separators=(",", ":")).encode()`. `hashlib.scrypt` needs `maxmem=2**28` for n=2**17. For owner-only permissions reuse the approach of `backend_token._owner_only` (read it; if it is private, add a small public wrapper there, not a copy).

- [ ] **Step 1: tests** (use `SCRYPT_N` monkeypatched to `2**10` for speed):

```python
def test_round_trip(tmp_path, fast_kdf):
    h = vault_crypto.new_header(); k = vault_crypto.derive_key("pw", h)
    vault_crypto.write_file(tmp_path / "v.json", vault_crypto.seal(k, h, {"entries": {}}))
    assert vault_crypto.open_sealed(k, vault_crypto.read_file(tmp_path / "v.json")) == {"entries": {}}

def test_wrong_passphrase_fails(fast_kdf): ...          # derive_key("other") -> VaultError on open
def test_a_flipped_ciphertext_byte_fails(fast_kdf): ...
def test_a_changed_header_fails(fast_kdf): ...          # change kdf["n"] in the document -> VaultError
def test_every_write_uses_a_new_nonce(fast_kdf): ...    # seal twice -> nonces differ
def test_the_plaintext_is_not_in_the_file(tmp_path, fast_kdf): ...  # b"hunter2-marker" not in file bytes
```

- [ ] **Step 2:** run, expect failures (module missing). **Step 3:** implement. **Step 4:** run, expect 6 passed; also `uv run --no-sync python tools/check_pinned_imports.py` exits 0. **Step 5:** commit `feat(T-19366): vault file format (scrypt + AES-256-GCM)`.

### Task 2: `secret_registry`

**Files:** Create `embedded/secret_registry.py`, `tests/test_secret_registry.py`. Modify `observability.py` (`_redact_text`), `embedded/logging_setup.py` (record factory; net zero lines), `embedded/debug_logger.py` (ring writes).

```python
MASK = "[vault-secret]"
class Secret:                      # holds one string; repr/str -> "***"; .reveal() -> str
def register(value: str) -> None   # ignores values shorter than 4 chars
def clear() -> None
def redact(text: str) -> str       # replaces every registered value, and its urllib.parse.quote / quote_plus forms
```

- [ ] Tests: `redact` masks a registered value, its URL-encoded form, leaves other text alone; `repr(Secret("x"*8))` is `"***"`; after `register`, a Sentry event passed through `observability._scrub_event` has the value masked in message, exception value and breadcrumbs; a `logging` record with the value in `msg` or `args` is masked in its formatted output; `debug_logger` ring entries are masked.
- [ ] Implement; wire the three call sites; run the three existing suites `tests/test_observability*.py tests/test_logging_setup*.py tests/test_debug_logger*.py` (glob what exists) plus the new file. Commit `feat(T-19366): exact-string secret redaction in Sentry, logs and the debug ring`.

### Task 3: `vault_store`

**Files:** Create `embedded/vault_store.py`, `tests/test_vault_store.py`.

```python
VAULT_PATH = backend_registry.STATE_DIR / "vault.json"
AUDIT_PATH = backend_registry.STATE_DIR / "vault-audit.jsonl"

class VaultLocked(Exception): ...
def validate(entries: dict) -> None            # raises ValueError naming the label and the rule
def unlock(key: bytes, ttl_seconds: float | None) -> int   # decrypts to prove the key; registers passwords in secret_registry; returns entry count
def lock() -> None                              # drops key, secret_registry.clear()
def status() -> dict                            # {"exists", "unlocked", "expires_in"}
def public_entries() -> list[dict]              # label, kind, site, via?, frozen, username only if show_username; raises VaultLocked
def entry(label: str) -> dict                   # full entry, password as Secret; raises VaultLocked / KeyError
def freeze(label: str) -> None                  # sets frozen (UTC ISO), rewrites the file with the held key
def audit(label: str, instance_id: str, caller: str, outcome: str) -> None
```

Validation rules: label matches `^[a-z0-9][a-z0-9-]{0,62}$`; `kind` is `password` or `provider`; a password entry has non-empty `username`, `password`, an `https://` `login_url` (or loopback http when the test env var is set) and `hosts` of bare lowercase hostnames, which must include the login URL's host; a provider entry's `via` names a password entry. Expiry: `status()`/`entry()` call `lock()` once the deadline has passed (use `time.monotonic`). The paths are read through module attributes so tests monkeypatch them to `tmp_path`.

- [ ] Tests: each validation rule (one test each, asserting the message names the label); unlock with a wrong key raises `VaultError` and stays locked; `public_entries` hides `username` unless `show_username` and never has `password`; ttl expiry locks; `freeze` survives lock + unlock; the audit line is JSON with exactly `time,label,instance,caller,outcome`; after `unlock`, `secret_registry.redact(password)` is masked, after `lock` it is not.
- [ ] Implement, run, commit `feat(T-19366): vault entries, in-memory unlock, freeze and audit`.

### Task 4: `login_seal` and the refusals

**Files:** Create `embedded/login_seal.py`, `tests/test_login_seal.py`. Modify `embedded/tool_registry.py` (wrapper in `section_tool`), `embedded/tool_sections/cdp_functions.py` (`execute_cdp_command`), `embedded/tool_sections/tabs.py` (`switch_tab`, `list_tabs`), `embedded/server.py` (the `browser://` resources).

```python
def begin(instance_id: str) -> None
def seal_tab(instance_id: str, target_id: str) -> None
def end(instance_id: str) -> None                       # drops the instance and its tabs
def sealing(instance_id: str) -> bool
def is_sealed(instance_id: str, target_id: str | None) -> bool
def refuse_if_sealed(tool: str, kwargs: dict) -> None   # raises ToolError
INSTANCE_WIDE = frozenset({"execute_cdp_command", "switch_tab"})
```

`refuse_if_sealed`: if `kwargs.get("instance_id")` is sealing and (`tool in INSTANCE_WIDE` or `is_sealed(instance, kwargs.get("tab_id"))`) raise `ToolError("a vault login is running in this browser; retry when it finishes")`. Call it first thing in the `section_tool` wrapper (read `tool_registry.py:102-131` and `logging_setup.with_correlation_id` to pick the spot where kwargs are final, after `tab_id` injection). `list_tabs` and the resources filter out sealed targets.

- [ ] Tests: unit tests of each function; through the real registry (use the harness in `tests/fakes.py` and `call_tool(server_mod, name, /, **kwargs)`), with a sealed tab: one tab-taking tool of each section is refused, the same tool on another tab is not, `execute_cdp_command` and `switch_tab` are refused on any tab, `list_tabs` omits the sealed tab; after `end()` everything works.
- [ ] Implement, run the new file plus `tests/test_tool_registry.py tests/test_tool_dispatch.py`, commit `feat(T-19366): sealed tabs are refused to every tool`.

### Task 5: `login_form`

**Files:** Create `embedded/login_form.py`, `tests/test_login_form.py` (integration marker, real Chrome; follow `tests/test_captcha_integration.py` for the browser fixture and local pages).

```python
PROBE_JS: str    # returns {"username": bool, "password": bool, "otp": bool, "password_has_value": bool}
async def probe(tab, frame_id: str | None = None) -> dict       # runs PROBE_JS in page_natives.frame_world
async def find_field(tab, kind: str)                             # kind in {"username","password"} -> (element, frame_id) or None; top document then direct child frames
async def clear_passwords(tab) -> None                           # sets value "" on every password input, every frame
```

Detection rules are in the spec §6. "Visible" means a non-zero box, not `display:none`/`visibility:hidden`, not `disabled`.

- [ ] Tests against local HTML pages served on loopback: one-step form; username-only step; hidden decoy password field ignored; form inside an iframe; an OTP field; `clear_passwords` empties a filled field.
- [ ] Implement, run with `-m integration`, commit `feat(T-19366): login form detection`.

### Task 6: `login_flow`

**Files:** Create `embedded/login_flow.py`, `tests/test_login_flow_guards.py` (unit), `tests/test_login_flow.py` (integration).

```python
FORBIDDEN_FLAGS = ("--host-resolver-rules", "--host-rules", "--ignore-certificate-errors",
                   "--ignore-certificate-errors-spki-list", "--allow-insecure-localhost",
                   "--unsafely-treat-insecure-origin-as-secure", "--proxy-server")
def launch_refusal(options, cmdline: list[str]) -> str | None   # a reason, or None when the browser may log in
def origin_allowed(url: str, hosts: list[str]) -> bool
async def run(browser_manager, instance_id: str, label: str, caller: str) -> dict   # {"outcome","label","detail"}
```

`origin_allowed`: parse with `urllib.parse.urlsplit`; scheme `https` and `hostname` exactly in `hosts`; or, only when `os.environ.get("STEALTH_MCP_VAULT_ALLOW_HTTP_LOOPBACK") == "1"`, scheme `http` with hostname `127.0.0.1`. Check `get_settings()` in `settings.py`: unknown `STEALTH_MCP_*` keys crash it, so register this key there.

`run` follows spec §6 exactly: guards, `login_seal.begin`, `tab_open.open_tab(..., background=True)` + `mark_internal` + `seal_tab`, navigate, wait up to 15 s for a field, origin check before each field, type with `text_entry.type_characters`, `press_enter`, wait up to 15 s for an outcome, then in `finally`: `login_form.clear_passwords`, close the tab, `login_seal.end`. Outcomes per spec §5. `rejected` calls `vault_store.freeze`. Every exit calls `vault_store.audit`. A provider entry runs the flow for its `via` entry and returns `use_provider` when that gives `logged_in` or `already_logged_in`.

- [ ] Unit tests: each forbidden flag refuses (also in `--flag=value` form); a proxy option refuses; `origin_allowed` rejects `http`, `accounts.google.com.evil.io`, `xaccounts.google.com`, a `chrome-error://` URL, and accepts the exact host.
- [ ] Integration tests with a local login server (set the loopback env var): one-step → `logged_in` and a session cookie visible from a second tab; two-step → `logged_in`; wrong password → `rejected`, entry frozen, second call refused; already signed in → `already_logged_in`; unknown form → `needs_user`; the sealed tab is gone afterwards and `login_seal.sealing` is False, including when the flow raises.
- [ ] Implement, run, commit `feat(T-19366): the sealed login flow and its guards`.

### Task 7: tool section, routes, counts

**Files:** Create `embedded/tool_sections/logins.py`, `embedded/vault_routes.py`, `tests/test_logins_tools.py`, `tests/test_vault_routes.py`. Modify `embedded/tool_sections/__init__.py`, `embedded/server.py` (`--disable-logins`, route install next to `session_hygiene.install`), and every count site.

Tools (docstrings follow `tool_sections/captcha.py`): `list_logins()` and `login(instance_id, label)`; `VaultLocked` becomes `ToolError("the login vault is locked: a person must run `stealthy login unlock`")`.

Routes (bearer middleware already covers custom routes): `POST /_stealth/vault/unlock` body `{"key": b64, "ttl_seconds": number|null}` → `{"unlocked": true, "entries": n}` or 403 on `VaultError`; `POST /_stealth/vault/lock`; `GET /_stealth/vault/status`. No route returns an entry.

Count sites to move from 100 to 102 and 13 to 14 sections: `tests/test_tool_dispatch.py` (`EXPECTED_TOOL_COUNT`), `tests/test_tool_registry.py`, `tests/test_tool_module_reload.py`, `tests/release_gate_harness_consts.py`, `tests/test_e2e_functions_hooks.py`, `tests/test_doc_claims.py` (count and stale list), `tests/test_correlation_id.py` (one representative per section), then regenerate: `uv run --no-sync python tools/dump_tool_surface.py --write` and `uv run --no-sync python tools/gen_release_contract.py --write`. Docs: `README.md`, `CLAUDE.md`, `DESIGN.md`, `agent-setup/prompt.md`.

- [ ] Tests: tools locked → ToolError with the unlock hint; unlocked → `list_logins` shape, no `password` key anywhere in the JSON; routes: wrong key 403 and still locked, right key unlocks, no token 401.
- [ ] Run the whole non-integration suite: `uv run --no-sync pytest -q -p no:cacheprovider -m "not integration" -x`. Commit `feat(T-19366): list_logins and login tools, vault unlock routes`.

### Task 8: `cli_login`

**Files:** Create `src/stealth_chrome_devtools_mcp/cli_login.py`, `tests/test_cli_login.py`. Modify `cli.py` (one dispatch entry; remove one line elsewhere to stay at 1000), `tests/test_doc_claims.py` (`VERBS`, dispatch tables).

Follow `cli_supervise.py` (`add_parsers(sub)`, `DISPATCH`). Subcommands per spec §5. Password input: `getpass.getpass`, or stdin with `--password-stdin`. `unlock` and `init` require `sys.stdin.isatty()` and exit 2 with a message otherwise. `add`/`edit`/`remove` ask for the passphrase, decrypt, change, validate, re-seal and write; if the backend is running and unlocked they then re-post the key so it reloads. `unlock` derives the key locally and posts it with `backend_token.auth_headers()` to `singleton._backend_http_url(port)` with the path replaced.

- [ ] Tests (monkeypatch `getpass.getpass` and `isatty`): `init` + `add` + `list` round trip; `list` output contains no password and no username without `--usernames`; argparse has no `--password VALUE` option (assert `add --password x` exits 2); `unlock` with a non-TTY stdin exits 2; `edit` clears `frozen`; `remove` removes.
- [ ] Run, commit `feat(T-19366): the stealthy login CLI`.

### Task 9: leak test, docs, changelog

**Files:** Create `tests/test_login_vault_leak.py` (integration). Modify `README.md`, `NAVMAP.md`, `RUNBOOK.md`, `CHANGELOG.md` (`## Unreleased`), `.github/workflows` only if integration tests are selected by file list (check `release-gate.yml`).

- [ ] The leak test per spec §8: a random 24-character password; after one successful and one forced-failure sealed login, assert the plain and URL-encoded password is absent from the tool results, `get_debug_view`, `export_debug_logs`, `list_network_requests` + details, the backend log file and a captured Sentry event (use the capture approach of the existing observability tests).
- [ ] README: a "Login vault" section (what the model sees and never sees, the CLI, the limits from spec §2). NAVMAP rows for the nine new modules. CHANGELOG entry.
- [ ] Run the full non-integration suite and the integration files of tasks 5, 6, 9. Commit `test(T-19366): the vault leak test; docs`.

### Task 10: mutants

- [ ] In an out-of-tree `git archive` export, by runtime rebinding (see `tests/` conventions; never edit the tree), disable one guard at a time and confirm the suite fails: `origin_allowed` → always True; `launch_refusal` → always None; `refuse_if_sealed` → no-op; `secret_registry.redact` → identity; `vault_store.freeze` → no-op. Record the failing test for each in the PR body.
