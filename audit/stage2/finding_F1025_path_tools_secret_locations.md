# F-1025 — the path tools read and wrote anywhere, and `get_cookies` returned the whole jar (T-6207)

**Severity:** MEDIUM (an agent steered by a hostile page leaks or clobbers files; sweep 2026-10-22, F-E / F-F / F-H).

## Finding

* `upload_file` read any file the user could read and handed it to the page's `<form>` via `DOM.setFileInputFiles` ("attach `~/.ssh/id_ed25519`").
* `export_debug_logs`, `export_network_data`, `take_screenshot(file_path=)` wrote to any path, silently replacing what was there, with the default umask. The network export held `Cookie` / `Authorization` in clear text.
* `get_cookies` is documented "current page" and, with no `urls`, called `Network.getAllCookies`: every site's jar, HttpOnly included, into the LLM transcript.

## Fix

`embedded/path_confinement.py` (deny-list of secret locations on the REAL path, no-overwrite `O_EXCL`, mode 0600) and `embedded/network_export.py` (redaction). A deny-list rather than an allow-root: `Settings` has no allow-root, and one would break every working call until configured. `get_cookies` scopes to the tab's live URL (`all_sites=True` for the jar); the doc was right, the behaviour moved. Pins: `tests/test_path_tool_confinement.py`.

## Not covered

`import_network_data` (F-G), the `browser://{id}/cookies` resource, a login form's POST body in an export (`post_data`), `execute_cdp_command` (reads the jar by design).
