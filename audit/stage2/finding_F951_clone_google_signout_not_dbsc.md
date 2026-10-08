# F-951 — a clone read as signed out of Google: not DBSC, not a stale timestamp cookie, and it does not reproduce

**Severity:** Medium as reported (Google SSO is the owner's default login path, and a
clone that cannot reach Google defeats seeding for the most important site). **No
product change**: both proposed causes were measured, and neither holds.
**Files:** none changed. The code read: `embedded/login_persistence.py`
(`DBSC_FEATURES`, `protect_logins`), `embedded/google_rotation_guard.py`,
`embedded/cookie_handoff.py`, `embedded/platform_utils.py`
(`_apply_default_user_agent`), `embedded/desktop_launch.py`.
**Seen:** 2026-10-08. An auto-clone seeded at 15:22Z with `seeded_via: "cdp-cookies"`
and 1515 cookies carried was reported as follows: myaccount.google.com went to
`/account/about`, the account chooser showed the account as "Signed out"
(`data-authuser=-1`), and an earlier clone at 14:35Z reached the password challenge.
The master was signed in. Chrome had updated to 154.0.8037.98 on 2026-10-05.

---

## 1. Hypothesis 1: Chrome 154 enabled DBSC, and a copied jar cannot answer the refresh. REFUTED as a fix

The proposed fix was to launch the master and the clones with DBSC disabled.

- **That fix has shipped since 2.1.19 (F-937) and 2.1.20 (F-938).**
  `login_persistence.DBSC_FEATURES` is passed as the LAST `--disable-features`
  switch on every spawn: `EnableBoundSessionCredentials`, `DeviceBoundSessions`,
  `DeviceBoundSessionsFederatedRegistration`, `DeviceBoundSessionsForRestrictedSites`,
  `EnableBoundSessionCredentialsContinuity`, `EnableChromeRefreshTokenBinding`,
  `EnableChromeRefreshTokenBindingUpgrade` and `EnableCookieBindingCookieUpgrade`.
  `--allow-browser-signin=false` is passed with it.
- **Measured, the switch was in effect on the browsers involved.** The
  `Win32_Process` command lines were read at 22:40Z. The full list is present on the
  master (headless, started 08:16Z) and on the failing clone (headed, normal launch,
  started 15:22Z). It is also present on every other live session and on a delegated
  desktop-launch clone.
- **Measured, the legacy binding left in the profile is inert under those
  switches.** The master and its seed still carry a pre-2.1.19 legacy Google binding
  in `Preferences` (`bound_session_credentials_bound_session_params`, one
  `https://google.com/` entry). The test copied ONLY that preference into a
  cookie-less scratch profile and recorded a netlog while loading accounts.google.com
  and google.com:

  | profile | DBSC list | `RotateBoundGaps` | `Sec-Session` |
  |---|---|---|---|
  | pref | on (the product's) | 0 | 0 |
  | pref | off | 16 | 3 |
  | no pref | off | 0 | 0 |

  Chrome does try a bound refresh from that preference, but only when the features
  are on. Removing it from clones would change nothing today, so it was not done.

  The profile's standard `Device Bound Sessions` store has one row, and that row is
  for a non-Google site.
- **The mechanism argument (inferred).** DBSC does not sign each request: the
  short-lived cookie is the credential, and only the refresh needs the device key. A
  clone whose unexpired jar is identical to the master's therefore sends the same
  Cookie header as the master. DBSC cannot tell the two apart.

## 2. Hypothesis 2: a stale timestamp cookie that F-939's guard stops the clone from rotating. REFUTED

- **Measured, read-only,** with `Storage.getCookies` on both debug ports at 22:45Z
  (only expiries and equality booleans were compared; no value was printed or
  stored). The master and the failing clone each hold 1515 cookies, 110 of them
  Google. 1450 of the 1452 shared keys have equal values, and every Google session
  cookie is equal. The master's session-timestamp pair (the one F-939 documents) was
  last rotated at 04:27Z (its expiry minus one year). That is 11 h before the clone
  was seeded, because the headless master was idle.
- **Measured, the deciding experiment** (23:25Z, headless, scratch root, isolated
  backend from this worktree).
  1. An unnamed spawn was made while the scratch default was open, so it was a clone
     with the F-939 guard armed.
  2. The guard was verified BEFORE any credential was loaded: a page `fetch` to
     `accounts.google.com/RotateCookiesPage` and `/RotateCookies` failed with
     `TypeError` (blocked), while a control fetch to `www.google.com/robots.txt`
     went through.
  3. The owner's 22:53Z backup jar of the live shared browser (the same chain, last
     rotated 04:27Z, so 19 h stale) was loaded with `Storage.setCookies`.

  The results:
  - myaccount.google.com stays on `/` and shows the workspace account.
  - console.cloud.google.com goes to `/welcome` and shows the account's project.
  - The account chooser shows the workspace account **signed in**.

  After all three loads, the timestamp pair still read last-rotated 04:27Z, so the
  guard held and nothing rotated. **A guarded clone with a 19 h-stale timestamp
  cookie is signed in.**
- **`data-authuser=-1` is not a sign-out marker.** In the same chooser, the
  signed-in workspace entry and a different, genuinely signed-out account BOTH carry
  `data-authuser="-1"`. Only the second carries the "Signed out" label. The original
  report's chooser evidence may have read the label of another account.

## 3. Request shape (measured, against a local header echo server)

A product-spawned headless default and a headed unnamed clone sent identical
headers: UA, every `Sec-CH-UA*`, `Accept*` and `Sec-Fetch-*`. One divergence exists,
but not on the failing path. The delegated desktop launch (F-810) carries no
`--user-agent` override. It therefore sends Chrome's real high-entropy client hints
(arch, bitness, full version list, platform version, form factors), where the normal
path sends them empty. So a Google session minted on one path and used on the other
presents different device signals. Whether Google acts on that is UNVERIFIED. It is
recorded as a lead, not a finding.

## 4. Status

- **The symptom does not reproduce** on the same session chain three hours later, in
  a product-guarded clone. The 14:35–15:22Z failures had a cause that this session
  could not observe after the fact. Candidates that remain open, none measured:
  - a transient server-side state on the account at that time;
  - the delegated-launch device-signal mismatch of §3 (not the failing clone's path);
  - a misread chooser (§2).
- **Blocked, and needs a throwaway Google account, not the owner's.** Any experiment
  where a browser holding a chain ROTATES: an unguarded clone, a refresh-in-source
  trigger, or an idle scratch master aging its own login. By F-939, a second rotator
  on the owner's live chain can sign the live browser out. The owner also ruled out
  further sign-in windows.
- **The planned fix is not built:** refresh the source's timestamp pair before the
  hand-off. It would act on a cause that §2 measured not to be one.

## 5. Isolation of the live experiment

The live experiment ran under a separate HOME/USERPROFILE/LOCALAPPDATA, a separate
`STEALTH_MCP_BROWSER_SESSION_ROOT` and `STEALTH_MCP_NO_ERROR_REPORTING=true`, on its
own port. The real `master`, `master-snapshot`, the shared browser and the live
backends were read only: two `Storage.getCookies` reads, process command lines,
directory listings and a read-only open of `Preferences` and of the
`Device Bound Sessions` store. Every scratch browser was closed. The clone that held
the backup jar was reaped by the product on close. The scratch profiles, the
preference copy and the netlogs were then deleted.
