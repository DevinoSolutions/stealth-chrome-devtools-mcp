# F-819 — the runner image's Chrome updates mid-run, so the resolved identity and the launched browser are two different binaries

**Status: FIXED (environment-side) — `tools/resolve_chrome.py --freeze-updater`,
wired into every CI invocation.**
**Severity: MEDIUM (CI integrity)** — no product defect and no user impact. What
it costs is the thing a gate exists to provide: a red that means red. The macOS
cell fails on byte-identical trees, so a reviewer cannot tell a regression from
a runner that upgraded Chrome while the job was running, and the honest response
to a red macOS cell becomes "re-run it", which is how a gate stops gating.

---

## The behaviour

Every Chrome-launching job in `release-gate.yml` starts by resolving the image's
Chrome Stable identity:

```yaml
- name: Resolve image Chrome Stable identity
  run: uv run python tools/resolve_chrome.py --out chrome-identity.json
```

That reading is then trusted for the **rest of the job** — minutes later, by the
uploaded artifact and by `tests/test_browser_integration.py`'s
`TestChromeIdentity`, which asserts that production's auto-discovery and the
launched browser's CDP `Browser.getVersion` agree with it.

The trust is only earned if the binary behind the path cannot change in between.
On GitHub's macOS runners it can. Google's Keystone updater is live on the image
and upgrades Chrome Stable in place — the path does not move, only the bytes
behind it — so a job can resolve `150.x`, launch minutes later, and be answered
by `151.x`. The gate then reports a mismatch that is entirely real and entirely
about the runner.

## Evidence

**PR #64, two runs on byte-identical trees, both red on macOS.** The tree was
re-pushed unchanged precisely to separate a defect from a flake; both runs went
red on macOS through the same mechanism, in different nodes.

*Run 1* — the identity comparison itself, from the JUnit report:

```
tests/test_browser_integration.py::test_autodiscovery_and_cdp_match_image_chrome
AssertionError comparing ('Chrome/151.0.7922.76', '150.0.7871.187')
```

CDP `Browser.getVersion` reported **151.0.7922.76** while the identity resolved
at the top of the same job said **150.0.7871.187**. Nothing in the repo produced
that gap: the resolver and the browser were reading two different binaries,
because the updater swapped one between the two measurements.

*Run 2* — the same mechanism surfacing one layer down: the two
`tests/test_stealth.py` UA-coherence gates went red, which is the F-806 shape
(the masked UA's major disagreeing with the browser's own `sec-ch-ua`) arriving
by way of a mid-run binary swap rather than a stale memo.

**Prior history.** This is the same runner behaviour that opened F-806. CI run
**30607810053**, macOS, on 2.0.1: the stealth coherence predicate failed on
product code byte-identical to the run before it, which had passed — the
runner's Chrome had moved from 150 to 151 and nothing in the repo had moved at
all (see
[`finding_F806_masked_ua_advertises_a_version_the_browser_no_longer_has.md`](./finding_F806_masked_ua_advertises_a_version_the_browser_no_longer_has.md)).

## Why the fix is environment-side, not product-side

F-806's product fix is merged (`ca63b77`, shipped 2.0.4) and is not in question
here. It closed the parts a program can close: the probe now reads the binary
itself rather than its neighbouring staged directories, the memo is re-keyed on
the executable's on-disk identity so an in-place upgrade expires it, and a
post-launch reconciliation corrects the memo from the browser that actually
launched.

None of that can close this one, and the reason is structural: **the product
takes two measurements at two moments, and the defect is that the subject
changes in between.** A program cannot make a binary hold still. It can only
narrow the window — F-806 narrowed it from days to the probe-to-launch interval
of one spawn — and a narrower window is still a window a scheduled updater can
land in. The gate needs the window closed, not narrowed, and the only place it
can be closed is the run environment.

So the fix stops the updater rather than out-measuring it.

## The fix

`tools/resolve_chrome.py` — already the ONE home of the expected Chrome identity
— gains `--freeze-updater`. When passed, it neutralises the OS's background
Chrome updater **before** `_read_version` runs, so the identity it reports is by
construction the frozen one. Without the flag, behaviour is byte-identical to
before, and `resolve_chrome()` itself stays side-effect-free: importers
(`TestChromeIdentity`) get the reading and nothing else. The flag is opt-in
because it changes machine state, and only CI should ask for that.

**macOS — remove Keystone, then keep it removed.** Its launchd jobs are unloaded
and deleted (user agents in the calling user's domain, system daemons under
`sudo`), both `GoogleSoftwareUpdate` trees are deleted, and each path is
recreated as a **root-owned mode-000 stub** inside a root-owned parent.

The documented soft knob — `defaults write com.google.Keystone.Agent
checkInterval 0` — was considered and rejected. Chrome re-registers Keystone
through `KeystoneRegistration.framework` every time it launches, and launching
Chrome is exactly what this job does next; an advisory setting can therefore be
undone by the very act it has to survive. Deleting the trees alone has the same
weakness in a different form, because a reinstall would simply recreate them.
The stub is the part that makes the removal durable: there is nowhere for a
reinstall to land. The parent directory is locked too, because a root-owned stub
inside a user-owned parent is still removable by that user — unlinking is
governed by the parent's write bit, not the stub's.

**Windows — stop, disable, and forbid.** The `gupdate` and `gupdatem` services
are stopped and then disabled (disabling a running service leaves it running for
the current boot, so the order matters), the `GoogleUpdateTaskMachineCore` /
`GoogleUpdateTaskMachineUA` scheduled tasks are disabled, and the enterprise
policy under `HKLM\SOFTWARE\Policies\Google\Update` sets both `UpdateDefault=0`
and `AutoUpdateCheckPeriodMinutes=0`. Runners are administrators.

**Linux — an explicit no-op.** The images carry no background Chrome updater;
the package moves only when a job runs `apt`, and none does. The freeze says so
in the log rather than staying silent, because a silent no-op reads as a bug.

**Nothing here may turn a green run red.** Every sub-step is best-effort by
construction: exit codes are recorded and never checked, an unrunnable tool is
swallowed, and a missing service, task, plist or directory is the *normal* case
on at least one OS. The freeze narrates every step to stderr so the run log
shows what it actually did on this image; stdout stays the identity JSON alone,
unchanged, so any consumer parsing it is unaffected.

## What the gate now claims

Before: *"the browser we launched matches whichever Chrome Stable the image
supplied"* — a claim the runner could invalidate mid-job.

After: **"the Chrome Stable identity is frozen at run start, and the browser we
launched matches it."** A macOS identity mismatch is now a product signal, and a
re-run is no longer a legitimate response to one.

## Tests

`tests/test_resolve_chrome_freeze.py` — 25 nodes, fully hermetic. The freeze
cannot be exercised for real anywhere (running it on a developer workstation
would disable that human's Chrome updates), so the pins are command-level: they
fake `subprocess.run` with a recorder that answers with a real
`subprocess.CompletedProcess` built by the stdlib's own constructor, and assert
the exact argv per OS. That is the only assertable surface, and without it a
silent change to the commands would reach CI unreviewed.

Pinned: the per-OS command tables (Keystone plists, both `GoogleSoftwareUpdate`
roots, the stubs and the parent lock; the Windows services, tasks and policy
values); that user agents are *not* unloaded under `sudo` (that would target
root's domain and unload nothing); that Linux issues no command at all but still
reports; that a non-zero exit, a missing tool and a timeout each leave the
freeze — and the CLI's exit code — successful; that without the flag no freeze
runs and stdout is byte-identical; and that the version is read **after** the
freeze ran.

`tests/test_release_workflows.py::test_every_chrome_identity_resolution_freezes_the_updater_first`
— the structural half, in the existing home for workflow-YAML pins: every
`resolve_chrome.py` invocation across `release-gate.yml` and `canary.yml` must
carry the flag. A job added later that resolves an identity without freezing is
resolving something it cannot rely on, and this is what says so.

## Residual

**The one-instance probe-to-launch window from F-806 remains accepted, and is
unchanged by this work.** On a developer workstation, where nothing freezes the
updater, an upgrade that completes between a spawn's version probe and that
spawn's launch still costs exactly that one instance a skewed UA; every later
spawn is corrected by the post-launch reconciliation. Widening the product fix
to cover that instance was judged the wrong trade in F-806 and this finding does
not revisit it.

**A runner image whose Chrome is old stays old for the whole run — that is the
point, not a side effect.** The freeze buys determinism within a run, not
currency across runs. If an image ships a Chrome the product genuinely does not
support, this finding will not surface it; that is the canary's job, and the
canary's identity capture remains informational and still gates nothing.

**The freeze is unverifiable from here.** No test in this repo executes it, and
none can: the only machines where the real commands may run are the CI runners,
and this workstation is explicitly not one of them. The pins assert what is
issued, not what it accomplishes. The first real evidence that Keystone stayed
down will be a macOS gate cell that stops disagreeing with itself on
byte-identical trees.

**Nothing restores the runner.** The freeze is one-way within a job, which is
correct for an ephemeral VM that is destroyed afterwards, and would be wrong
anywhere else. The flag's help text says so, and it is off by default for
exactly that reason.
