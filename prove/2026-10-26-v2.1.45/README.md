# Headed PROVE of published 2.1.45

## Files

- `01-versions-token-tool-count.png` - published 2.1.45 versions, token auth, tool count 97
- `02-headed-spawn-navigate-click.png` - headed spawn, navigate, click
- `03-session-lock.png` - advisory session lock acquire/refuse/release
- `04-t4686-forced-end-identity.png` - T-4686 forced_end.refusal on the live headed browser
- `05-release-code-checks.png` - T-6232 probe step, T-8493 pins, T-8497 docs
- `06-stealth-walk-pypi-2.1.45.png` - stealth walk: PyPI 2.1.45 project page
- `07-stealth-walk-github-v2.1.45.png` - stealth walk: GitHub release v2.1.45
- `evidence.json` - raw evidence

## Checks

```json
{
 "version": true,
 "not_editable": true,
 "tools_97": true,
 "no_token_401": true,
 "headed": true,
 "clicked": true,
 "lock_refuses_other": true,
 "lock_released": true,
 "t4686_accepts_live_browser": true,
 "t4686_refuses_each_mismatch": true,
 "t4686_nothing_killed": true,
 "t6232": true,
 "t8493": true,
 "t8497": true,
 "closed": true,
 "stopped": true
}
```
