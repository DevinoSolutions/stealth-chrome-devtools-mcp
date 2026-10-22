# F-1025 — the path tools read and wrote anywhere, and `get_cookies` returned the whole jar (T-6207)

**Severity:** MEDIUM (an agent steered by a hostile page leaks or clobbers files; sweep 2026-10-22, F-E / F-F / F-H).

## Finding

* `upload_file` read any file the user could read and handed it to the page's `<form>` via `DOM.setFileInputFiles` ("attach `~/.ssh/id_ed25519`").
* `export_debug_logs`, `export_network_data`, `take_screenshot(file_path=)` wrote to any path, silently replacing what was there, with the default umask. The network export held `Cookie` / `Authorization` in clear text.
* `get_cookies` is documented "current page" and, with no `urls`, called `Network.getAllCookies`: every site's jar, HttpOnly included, into the LLM transcript.

## Fix

`embedded/path_confinement.py` (deny-list of secret locations on the REAL path, no-overwrite `O_EXCL`, mode 0600) and `embedded/network_export.py` (redaction). A deny-list rather than an allow-root: `Settings` has no allow-root, and one would break every working call until configured. `get_cookies` scopes to the tab's live URL (`all_sites=True` for the jar); the doc was right, the behaviour moved. Pins: `tests/test_path_tool_confinement.py`.

## Review pass (PR #274)

Covered since the first cut: Windows `\\?\<drive>:` paths (prefix stripped before comparing) and UNC / device paths (`\\server\share`, `\\localhost\C$`, `\\?\UNC\`, `\\.\`, `\??\`), which are refused outright, as typed and as resolved; the deny-list also names `.netrc` / `.git-credentials` / `.npmrc` / `.pypirc` (by name), `~/.config/gh`, `~/.docker`, `~/.kube`, `~/.azure`, `~/.claude`, `~/.stealth-mcp`, macOS Keychains, the Windows `Credentials` / `Protect` / `Vault` stores, the Chrome Dev / SxS / Canary / beta, Edge Beta / Dev / SxS, Vivaldi and Opera profile roots and the platform-default session root; the export redacts `x-api-key`, `x-auth-token`, `x-csrf-token`, `x-xsrf-token`, `x-amz-security-token` and any header named like a token / secret / api key; an unwritable destination is a `ToolError` naming the basename only (F-877). Pins: `tests/test_path_tool_confinement.py` (the `\\?\` / UNC / `\\.\` pins are Windows-only because no other OS can spell them; the string classifier is pinned on every OS).

## Follow-ups N1-N3 (T-6403)

The #274 re-review found three more holes in the same two modules. N4 (a mapped network drive, `Z:\`, is refused as a local path it cannot be compared against) is kept as is.

* **N1, Windows trailing dot / space.** Win32 drops trailing `.` and ` ` from every path segment, so a NOT-yet-existing `~/.kube.\config` or `~/.gnupg.` passed the string comparison and `precreate_private` then created it under the denied name (`os.path.realpath` does not strip them; it was measured). `path_confinement._win_canonical` strips them per segment (`.` / `..` and an all-dots segment are left alone) and `_fold`, the one place the deny-list compares, applies it on Windows only (on POSIX `.kube.` is a different directory); the file NAME rules (`.env`, `.netrc`...) use it too.
* **N2, header redaction.** `is_credential_header` now reads `_` as `-` (so `x_api_key`, `api_key`, `X_Session_Id` match) and its markers add `authorization`, `password`, `passwd`, `jwt`, `session`, `credential` to `token`, `secret`, `api-key`, `apikey`. That covers `x_api_key`, `api_key`, `x-authorization`, `cf-access-jwt-assertion`, `x-session-id` and `password`; `content-type`, `accept`, `user-agent` and the other ordinary headers are pinned as still visible.
* **N3, more secret locations.** `~/.pgpass`, `%APPDATA%\postgresql` (its `pgpass.conf`), `~/.vault-token`, and `/proc/self/environ` / `/proc/thread-self/environ` / `/proc/<pid>/environ` / `/proc/<pid>/task/<tid>/environ` (`proc_environ_path`, a pattern on the real path, applied on every OS since a Windows real path starts with a drive and cannot match). A Chrome profile that `spawn_browser(user_data_dir=<any path>)` put outside the session root: a directory is a profile when it carries `profile_seed.MARKER_NAME` (the clone marker) or one of `profile_seed.LOGIN_WITNESSES`, and every path under such a directory (walked up from the real path) is refused (`_profile_rule`). Those are the repo's own two definitions of a profile, not a third; the session-root entries are untouched. Residual: a brand-new profile Chrome has not yet written a cookie jar, login store or marker into carries neither, and a profile at an arbitrary path is only recognised by those files.

Pins: `tests/test_path_confinement_followups.py` (the pure-string halves run on every OS; the trailing dot / space end-to-end pins are Windows-only, the live `/proc` pins Linux-only).

## Not covered (deferred to a follow-up task)

`import_network_data` (F-G), the `browser://{id}/cookies` resource, `execute_cdp_command` (`DOM.setFileInputFiles` attaches any path with no deny-list check, and the cookie methods read the whole jar), a login form's POST body (`post_data`), tokens in URL query strings and response bodies in an export, `take_screenshot(overwrite=True)` truncating before the capture (a failed capture destroys the file; the fix is write-to-temp then `os.replace`), a hardlink to a secret from an allowed folder, autostart / shell-rc / `.git/hooks` write targets, the check-then-use gap between `upload_file` and the form submit (the browser profile outside the session root chosen with `spawn_browser(user_data_dir=)` moved to the N3 section above). `%APPDATA%` / `%LOCALAPPDATA%` redirected away from `<home>\AppData` are not read (env access has one home, `settings`).
