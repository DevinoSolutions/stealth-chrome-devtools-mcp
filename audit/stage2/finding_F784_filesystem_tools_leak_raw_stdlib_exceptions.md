# F-784 — the filesystem tools leak raw stdlib exceptions

**Status:** OPEN — characterized by plan_RELEASE W15, not fixed (W15 is zero-`src/`).
**Severity:** MEDIUM. Convention break plus an unavoidable host-path disclosure
in the failure message.
**Surface:** `src/stealth_chrome_devtools_mcp/embedded/network_interceptor.py`
(`export_to_json`, `import_from_json`), reached via the `export_network_data` /
`import_network_data` tools in `embedded/server.py`;
`embedded/debug_logger.py` for `export_debug_logs`.
**Found by:** plan_RELEASE §2.15 (W15), the "filesystem failure" oracle.

## The behavior

None of the hermetic filesystem paths wraps its failure. The tool bodies have no
`try/except` around the I/O, so the caller receives the stdlib exception itself:

| Trigger | What the caller gets |
|---|---|
| destination directory missing | `FileNotFoundError` (`OSError`), message `[Errno 2] No such file or directory: '<abs path>'` |
| unwritable destination | `PermissionError` (`OSError`) |
| source file is not JSON | `json.decoder.JSONDecodeError`, message `Expecting value: line 1 column 1 (char 0)` |
| source JSON missing a key | `KeyError` |

None of these is a `ToolError`, so `CLAUDE.md` convention 2 does not hold on this
path. None of them carries a next step. `JSONDecodeError` in particular names a
line and column but never the file, so the caller cannot tell *which* path was
malformed if several are in flight.

## The disclosure edge

`OSError.__str__` embeds the **absolute host path**, which routinely contains the
operator's account name. W15 plants a `sensitive-path-component` canary in a
directory name and confirms that after `release_evidence.redact` the canary is
gone — the path leak is contained by the policy, provided the diagnostic goes
through the policy. Nothing forces it to; that is the standing risk this note
records.

Cross-platform trap worth keeping: `str(OSError)` renders the filename through
`repr`, so a Windows message contains **doubled** backslashes. A test that
asserts `str(path) in str(exc)` passes on Linux and macOS and fails on Windows.
W15 asserts `exc.filename == str(path)` plus a separator-free basename instead.

## Pins

`tests/test_observability.py`:

* `TestDiagnosticOracle::test_filesystem_failure_names_the_path_it_could_not_reach`
* `TestErrorConventionGaps::test_filesystem_paths_leak_raw_stdlib_exceptions`
  (`@pytest.mark.characterization`, `route:F-784`)
* `TestSecretCanaries::test_no_canary_survives_the_canonical_policy[sensitive-path-component]`
  (the containment half — not characterization; a red here is a release blocker)

## Contract limitation wording (for W5 §Limitations)

> `export_network_data`, `import_network_data` and `export_debug_logs` do not
> wrap filesystem failures. The caller receives the underlying `OSError`,
> `json.JSONDecodeError` or `KeyError` unchanged. `OSError` messages embed the
> absolute host path, which may contain the operator's account name; diagnostics
> derived from them must be passed through the canonical redaction policy before
> being written anywhere.
