# F-955 — the delegated desktop launch and a normal spawn present the same User-Agent and client hints (F-951 §3 does not reproduce)

**Severity:** None (no defect). **No product change**: F-951 §3 recorded a divergence as
a lead, "UNVERIFIED". It was verified, and on current main it is not there. This
finding closes the lead and adds a regression pin.
**Files:** `tests/test_desktop_launch.py` (one new test). The code read:
`embedded/platform_utils.py` (`merge_browser_args`, `_apply_default_user_agent`),
`embedded/browser_manager.py` (`_resolve_launch_args`, `_launch_browser`),
`embedded/desktop_launch.py` (`launch_and_attach`, `_launcher_script`),
`embedded/cdp_attach.py` (`config_for`).
**Seen:** 2026-10-09.

---

## 1. How the User-Agent policy is applied (read)

There is one mechanism and it is not CDP. `platform_utils._apply_default_user_agent`
appends a process-wide `--user-agent=<masked reduced UA>` to the launch args unless the
caller chose one. Nothing arms a UA per tab: no `Emulation.setUserAgentOverride`, no
`Network.setUserAgentOverride` and no `userAgentMetadata` appears on the spawn path
(`network_interceptor.set_user_agent` is a hook tool, not part of spawning). The only
post-launch UA work is `reconcile_launched_browser_version`, a read of
`Browser.getVersion` that corrects the version memo and changes nothing in the browser.

`_resolve_launch_args` builds `launch_args` once, and `_launch_browser` passes that same
list to either `uc.start` (normal) or `desktop_launch.launch_and_attach` (delegated).
`launch_and_attach` renders it through nodriver's own `Config()` into the launcher
script. So the switch reaches the delegated Chrome by construction.

## 2. Measured: the two launches are identical

A local header-echo server sent `Accept-CH` and `Critical-CH` for every high-entropy
hint (`Sec-CH-UA-Arch`, `-Bitness`, `-Full-Version-List`, `-Platform-Version`,
`-Form-Factors`, `-Model`, `-WoW64`) and its page POSTed back
`navigator.userAgent`, `navigator.userAgentData.brands` and
`getHighEntropyValues(...)`. Chrome 154.0.0.0 on Windows 11, throwaway profile under
`%TEMP%`, throwaway HOME/USERPROFILE/LOCALAPPDATA, no sign-in page, every Chrome
killed by pid at the end.

- **Normal path:** `subprocess.Popen([exe, *Config()])`, the argv `uc.start` would use.
- **Delegated path:** the real `desktop_launch._launcher_script(...)` text, run with
  `powershell -File` (the same `Start-Process -ArgumentList` quoting layer the
  scheduled task runs; only Task Scheduler's session placement is not exercised).

Run twice, `--headless=new` and headed. Both runs: the request headers (`user-agent`
and every `sec-ch-ua*`) of the final request were equal, and the JS-side values were
equal. Both sent the masked `Chrome/154.0.0.0` UA with the `Chromium` / `Google Chrome`
/ `Not A(Brand` low-entropy brands, and both sent every high-entropy hint EMPTY
(`architecture: ""`, `bitness: ""`, `fullVersionList: []`, `platformVersion: ""`,
`formFactors: []`). No process from either run survived.

## 3. Why F-951 saw a difference

Not established. The measured launch commands carry the switch on both paths today, so
the likely readings are that the delegated browser measured then was launched before the
mask existed on that path, or by a different build, or its request was compared against
a hint the page had not yet been granted (`Accept-CH` takes effect on the NEXT request,
so a first request legitimately has fewer hints than a later one). None of these is
a defect on main, and none can be told apart now.

## 4. Pin

`test_the_delegated_launch_presents_the_same_user_agent_as_a_normal_spawn`
composes args through `_resolve_launch_args` like a real spawn, runs them through
`launch_and_attach`, and asserts the argv Chrome receives carries exactly the one
masked `--user-agent=` the normal args carry. It is GREEN on main by design (there is
no defect to be RED about). Mutation check, by runtime rebinding of
`desktop_launch._launcher_script` to drop `--user-agent=` args: the pin goes RED
(`1 failed`); unmutated, `tests/test_desktop_launch.py` is `45 passed`.

## 5. Status

Closed, no change. F-951 §4's "device-signal mismatch" candidate is eliminated as a
cause of the signed-out clone.
