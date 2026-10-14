# F-971: `DevinoSolutions/artifact@v1` was a floating tag that lacked the fix

## Defect

All 18 `upload` and 4 `download` steps in `.github/workflows` named `DevinoSolutions/artifact/*@v1`. That
tag still pointed at 739b3ab (2026-09-02): no retries, and dl.min.io as the only source of the `mc` binary.
MinIO archived the project, so dl.min.io answers `410 Gone` permanently. When the org mirror
(storage.devino.ca) blipped at 17:34Z on 2026-10-14, every upload step of PR #224's gate run 38607713353
failed with "Could not download the MinIO client: HTTP 410: 410 Gone". The tests were green.

## Cause

The fix shipped in v1.2.1 (44df166: retries with backoff, GitHub release-asset fallback checked against a
pinned sha256) but the `v1` tag was never moved. Every other action here is pinned by full SHA, so the
floating ref was also a second way to name an action.

## Fix

All 22 refs now read `@44df16680dcb504375db03d284d75808db18e4ed # v1.2.1`.
`tests/test_release_workflows.py::test_every_third_party_action_is_pinned_to_a_full_sha` covers every
non-`./` `uses:` in every workflow. RED on origin/main (3 files' artifact refs named), GREEN after.
