# prep-t7 verification addendum — checked against worktree HEAD 69e48ad

Produced 2026-08-02 by a read-only verification agent. Source of truth for every
line below: `.claude/worktrees/f808` on branch `fix/F808-headed-visibility`,
HEAD **69e48ad** (`F-808 step 6b: a recorded desktop backend only silences the
remedy while it is alive`). There is **no** later "6c" commit — 69e48ad is the tip.

Read this file **beside** `prep_F808_T7_docs_edit_list.md`, not instead of it. The
prep file is accurate in the large; the corrections below are the places where it
would ship stale text, a drifted anchor, or an incomplete edit set.

---

## VERIFIED-ACCURATE

Anchors and drafted strings that still hold exactly as the prep file states.

- `cli.py:378` prints the header `contexts    :` (four spaces before the colon, matching the other doctor labels), and each backend line is printed indented by two spaces (`cli.py:379-380`).
- Per-line format at `cli.py:229-235` is exactly `backend  {context}  port {p}  pid {n}  version {v}  {status}  ({note})` — two spaces between every field.
- Missing `port` / `pid` / `version` each render as `-` (`cli.py:230-232`).
- Note token is `(headless only)` iff `context == HEADLESS`, else `(can show windows)` (`cli.py:234`).
- Liveness vocabulary is `responsive` / `wedged` / `down` / `no port recorded` (`cli.py:186-190`); `no port recorded` is returned only for `port is None`.
- Empty record prints exactly `backend  (none recorded)` and nothing else (`cli.py:236-240`).
- Remedy string at `cli.py:248-252` is exactly: `no live backend can display a window: headed spawns will fail — start one from a desktop session and any session will use it automatically` (one em dash, one logical line assembled from three source lines).
- Remedy suppression rule is exactly as the task brief states: suppressed only when a window-capable entry is **responsive** — `serviceable = serviceable or (context != HEADLESS and status == "responsive")` (`cli.py:228`).
- Ordering is `backend_registry.window_capable_first(...)` (`cli.py:222`), i.e. doctor mirrors discovery's preference.
- Spawn guard lives at `embedded/server.py:390-397`, outside the `try`, before any profile-clone work; message tokens as quoted in the brief.
- `DESIGN.md` §2.2 **is** already updated: schema-v2 JSON sample at `:102-112`, the v1-still-reads sentence at `:114-117`. Prep's "do not redraft" is correct.
- `DESIGN.md:21` reads `There are **two front-ends** over **one shared backend process**:` — the prep's 2a target string is exact.
- `DESIGN.md:30-36` is the "Both talk to the **one** ***backend***" paragraph — 2a's replacement range is exact.
- `DESIGN.md` §2.6 ends at `:175`; the `---` is at `:177`. Prep 2c's "insert new §2.7 at :176" is a valid insertion point.
- `DESIGN.md` §10 ledger table runs `:418-429`; file total is 433 lines, so "append six rows after :429" is exact.
- `window_sizing.py:13-18` is precisely the paragraph the prep replaces, and `:13-14` do hold the "1024x768 desktop" imprecision. Ends `reported a size it never had.` at `:18`, matching the drafted replacement's tail.
- `embedded/server.py:352-356` `viewport_width` docstring is already corrected and already names the **LAUNCHING** context's desktop plus F-808. Prep shape-finding #2 is right; do not redraft.
- Every symbol named in prep 1b's `backend_registry.py` row exists: `SCHEMA_VERSION` (:82), `read_backends` (:157), `record_backend` (:336), `forget_backend` (:373), `clear_record` (:442), `adoption_candidates` (:178), `window_capable_first` (:162), `port_for_context` (:215), `own_or_first_port` (:246), `port_conflict` (:275), `STATE_DIR` (:74), `SERVER_STATE_FILE` (:80), `PORT_FILE` (:75).
- Every symbol/token named in prep 1b's `display_context.py` row exists: `HEADLESS` (:29), `UNVERIFIED` (:32), `display_context()` (:108), `can_show_windows()` (:139); tokens `win-session-N` (:119), `wayland-<display>` (:131), `x11-<display>` (:133), `aqua-<uid>` (:104).
- Prep 1a's claim that path names are "re-exported here for legacy callers" is true: `singleton.py:29-34` imports `PORT_FILE`, `SERVER_STATE_FILE`, `STATE_DIR` from `backend_registry`.
- Prep 3c's RUNBOOK replacement matches the code: `stop_backend` forgets only its own display-context entry, then calls `_clear_server_state()` only when `read_backends(...)` is empty (`singleton.py:599-604`).
- `CLAUDE.md` anchors hold: singleton nav row at `:60`, **backend** glossary row at `:119`, **cloner engine** row at `:130`.
- `RUNBOOK.md` anchors hold: doctor verb row `:25`, `version     : 2.0.3` `:45`, "Port already in use" `:115-120` with the stale sentence as the last line `:120`, "Code edit didn't take effect" `:144-148`, `---` at `:150`.
- `README.md` anchors hold: `## Usage Examples` at `:147` (so `:146` is the blank line to insert before), Requirements bullets `:285-287`, the unrelated headless code sample at `:159-160`.
- `tools/check_file_budgets.py:32-33` carries `CAP RAISE 3389->3401 PENDING HUMAN RATIFICATION in the F-808 PR.` verbatim; the cap it guards is on `:34`.
- `RELEASE_CONTRACT.md:4` says `# Release contract — version 2.0.3`; `pyproject.toml:7` says `version = "2.0.3"`. Prep §10's warning is live.
- `CONTRIBUTING.md` contains no F-808 content (confirmed); `:31-50` is the `uv run pytest` caveat section.
- Prep §11's "verified clean" list holds: `tests/release_gate_harness.py:489-496` explicitly parses both schemas; `tests/MANUAL_QA_PROTOCOL.md:1703` prose stays true; `audit/STAGE3_RESUME_PROMPT.md:46` reads as quoted.
- F-804 finding file `:3`-style status and the F-808 finding file's `**Status: OPEN. Severity: HIGH. Regression from 1.0.0.**` at `:3`, `## Workaround available today` at `:96`, content ending `:100` — all as the prep describes.
- Prep §8 commit SHAs resolve correctly: T1 = `a1b3075` + `8a78561`; T2 = `efed9d0` + `663da1a`; schema row `7989dee`, `32b3185`, `62d4813`; adoption row `85f7fe6`, `09433a0`, `f02334c`, `4e2ede3`.
- Tool count is still 94 (`tests/test_doc_claims.py:198-206` asserts registry == 94), so the "94 tools" text in every draft is safe.

---

## CORRECTIONS

### C1 — Prep §3 (shape-finding 3): "Task 6 has NOT landed" is obsolete

Prep text:

> 3. Task 6 has NOT landed at 58e7725 — every RUNBOOK/README line quoting a
>    `doctor` string below is drafted from the Task 6 spec and MUST be re-verified
>    against the strings impl-t6 actually lands before committing.

Correct at HEAD 69e48ad: Task 6 **has** landed, in three commits — `172e014`
(*doctor reports a backend per display context*), `60e48da`, `69e48ad`. Every
doctor string the prep drafts has now been checked against `cli.py` and needs no
change (see VERIFIED-ACCURATE). The re-verification the prep asks for is done.

### C2 — Prep §1e: the `LIVE_EMBEDDED` line range is wrong

Prep text: *"tests/test_doc_claims.py:92-113"*.

Correct: `LIVE_EMBEDDED` is declared at **`tests/test_doc_claims.py:93-115`**
(class `TestNavMapModules` opens at `:91`, the `ClassVar` at `:93`, last entry
`"element_resolution"` at `:114`, closing bracket `:115`). Its consumer is
`test_live_modules_exist` at `:132-138`.

Also worth knowing before editing it: the list is **not** "every nav-map module".
It already contains `element_resolution` but does **not** contain `window_sizing`,
`dom_handler`, `proxy_forwarder`, or `proxy_utils` — so adding `backend_registry`
and `display_context` is an addition to a partial list, not the completion of a
total one. Both new modules exist, so the additions stay green.

### C3 — Prep §7: the F-804 finding's item 2 starts one line earlier

Prep text: *"audit/stage2/finding_F804_...md:46-48 — replace item 2"*.

Correct: item 2 spans **`:45-48`**. Line 45 is
`2. **Headed Chrome clamps to the desktop work area.** Anything larger than the`,
and `:46` is the continuation `work area (1024x768 on the backend's session here) came back at ~1044x788 —`.
Replacing only `:46-48` would leave the old claim's first line and its old bold
lede in place. Replace `:45-48`.

### C4 — Prep §9: the CHANGELOG insertion line

Prep text: *"insert the whole 2.0.4 block at :2, above '## 2.0.3'"*.

Correct: `CHANGELOG.md:1` is `# Changelog`, `:2` is blank, `:3` is `## 2.0.3`.
Insert **before `:3`** (i.e. after the existing blank line 2) and leave a blank
line before `## 2.0.3`, or the two headings collide.

### C5 — Prep §5: the CONTRIBUTING insertion anchor lands inside the previous section

Prep text: *"RECOMMENDED addition after :139"*.

At HEAD, `CONTRIBUTING.md:138-139` is the closing sentence of the release-workflows
discussion (`tests/test_release_workflows.py` pins those properties…), `:141` is
the `---` that closes that section, and `:143` is `## Golden discipline (two-tier)`.
A new `### The release contract is generated` placed after `:139` would sit inside
the previous section, before its own `---`. Correct anchor: **insert after `:141`**
(after the horizontal rule), so the new section stands between the release-workflows
section and Golden discipline.

Second, smaller point on the same draft: prep §10 cites CONTRIBUTING.md:31-50 as
the source for `uv run python -m pytest`. That section actually recommends
`.venv\Scripts\python.exe -m pytest` (`:40-44`) and says only that `uv run pytest`
fails to canonicalize. `uv run python -m pytest` is the working worktree form but is
**not** the form CONTRIBUTING prints — do not quote CONTRIBUTING as its source.

### C6 — Prep §3b bumps one version string and misses the rest of the set

Prep text: *"RUNBOOK.md:45 — sample status block 'version : 2.0.3' → 2.0.4."*

The 2.0.3 release commit `67b7f28` moved **all** version strings in one commit:
`pyproject.toml:7`, `README.md:55` (`"args": ["stealth-chrome-devtools-mcp==2.0.3"]`),
`README.md:64` (`pip install stealth-chrome-devtools-mcp==2.0.3`), `RUNBOOK.md:45`,
`RELEASE_CONTRACT.md:4`, `CHANGELOG.md` heading, `uv.lock`. Prep §4c explicitly
clears README of changes and never mentions `:55` / `:64`.

Recommendation, consistent with prep §10's own reasoning and with `67b7f28`:
**leave every version string to Task 8's release commit**, including `RUNBOOK.md:45`.
If Task 7 does bump `RUNBOOK.md:45`, it must also bump `README.md:55` and
`README.md:64` in the same commit, or the published install pin disagrees with the
runbook sample.

### C7 — Prep §8 commit table: one row mislabels a commit and the table is missing Task 6

Prep row: `| 2b22fe1, d209b46, 58e7725 | spawn_browser(headless=False) raises … ; the F-804 docstring clamp correction |`

`58e7725` is *"F-808 step 5c: the doomed_spawn fixture is all tripwires, as
documented"* — a test-fixture documentation commit, not part of the refusal or the
docstring correction. The refusal + docstring pair is `2b22fe1` + `d209b46`.

Also missing from the table entirely: the Task 6 doctor commits `172e014`,
`60e48da`, `69e48ad`, and `29f02a0` (*Test runs no longer ship injected failures to
the real Sentry*) — the latter is the landed commit behind the CHANGELOG's
"Fixed — test runs no longer ship injected failures to the real Sentry" section, so
the section is backed by real work, but the table does not cite it. Add a row:

    | 172e014, 60e48da, 69e48ad | `doctor` reports one line per recorded backend with its display context, and an explicit remedy when no live backend can show a window |
    | 29f02a0 | test runs no longer ship injected failures to the real Sentry |

---

## NEW OBSERVATIONS

### N1 — DESIGN.md:119-121 carries the SAME stale `stop` sentence the prep fixes in RUNBOOK, and the prep does not list it

`DESIGN.md:119-121` currently reads:

> Discovery and reuse **read the recorded port**; they never assume `19222`. `stop`
> clears `server.json`, so the next start falls back to `DEFAULT_PORT`. **Never
> re-hardcode `19222`** anywhere in the path — the port is data, not a constant.

That middle sentence is now wrong for the same reason prep §3c rewrites
`RUNBOOK.md:120`: `stop_backend` forgets only its own display-context entry and
clears the file only when nothing else is recorded (`singleton.py:599-604`).

**This is the highest-value item on this page**: if Task 7 fixes RUNBOOK and not
DESIGN, the two root docs will directly contradict each other on a behaviour F-808
changed. Suggested replacement for that one sentence:

> `stop` forgets the stopped backend's own display-context entry and clears
> `server.json` only once nothing else is recorded, so the next start falls back to
> `DEFAULT_PORT` when it was the last backend on the machine.

### N2 — README.md:37-39 states the old one-backend invariant and is not in the prep's edit list

Prep §4c declares README needs only 4a and 4b. But `README.md:37-39` is a Key
Features bullet:

> - **One shared backend across sessions** — every client session proxies to a single
>   backend process rather than starting its own; simultaneous cold start is scale-tested
>   at 40 concurrent sessions, all usable in seconds against one backend

This is the user-facing statement of exactly the invariant CLAUDE.md:119 is being
changed for (prep 1c). It needs at minimum a "one per desktop" qualification, e.g.
"…proxies to a shared backend rather than starting its own — one per desktop, so a
headed window opens where you are…". `README.md:217-218` ("The backend is a single
shared process…") is in the `.env` discussion and is more tolerable, but it is the
same family; judge it once you have settled the wording at `:37-39`.

### N3 — The doctor remedy and the spawn guard are near-identical but NOT word-identical

Guard (`server.py:394-396`): "**Start the backend from a desktop session and this
session will use it automatically**, or pass headless=True. Run
`stealth-chrome-devtools doctor` to see which contexts have a backend."

Doctor (`cli.py:249-251`): "…headed spawns will fail — **start one from a desktop
session and any session will use it automatically**".

Three differences: capitalisation/"the backend" vs "one"; "this session" vs "any
session"; and the doctor remedy does **not** repeat the `headless=True` escape or
the doctor pointer (it is already doctor). Note that `cli.py`'s own comment at
`:243-247` says the remedy is "deliberately the spawn refusal's own words" — that
is an intent statement, not a literal-equality claim. **No doc may assert the two
strings are identical**, and any doc quoting one should quote it verbatim rather
than paraphrasing across both. Prep §3d and §9 both stay on the safe side of this
already (they paraphrase without claiming equality); keep it that way.

### N4 — `unverified` prints as window-capable, and it satisfies the remedy suppression

`cli.py:228,234` branch on `context == HEADLESS` only. So an `unverified` entry —
which is what **every** record written by 2.0.3 and earlier reads as — prints
`(can show windows)` and, when responsive, sets `serviceable`, suppressing the
remedy. This is correct behaviour (an unverified context is treated as capable
everywhere), but it means doctor does **not** distinguish "proven desktop" from
"unclassifiable". Do not write a doc sentence implying doctor tells you which
backend has a *proven* desktop.

### N5 — The harness follow-on should probably also cover `TestLoadBearingSymbols`

Prep §1e covers `LIVE_EMBEDDED` only. `tests/test_doc_claims.py:146-188`
(`TestLoadBearingSymbols.test_symbols_resolve`) pins, per module, the specific
symbols the docs name — and it currently has **no** entry for `embedded.backend_registry`
or `embedded.display_context`. Since prep 1b's two nav rows name ~15 symbols, adding

    "embedded.backend_registry": ["SCHEMA_VERSION", "read_backends", "record_backend",
        "forget_backend", "clear_record", "adoption_candidates", "window_capable_first",
        "port_for_context", "own_or_first_port", "port_conflict",
        "STATE_DIR", "SERVER_STATE_FILE", "PORT_FILE"],
    "embedded.display_context": ["display_context", "can_show_windows",
        "HEADLESS", "UNVERIFIED"],

is the natural companion edit. Every one of those symbols was verified to exist at
HEAD, so the addition lands green.

### N6 — `backend_registry`'s public surface is larger than the drafted nav row

The drafted row is accurate but partial. Also public and in use: `read_record`
(:90), `backends_in` (:106), `first_backend` (:135), `backend_on_port` (:145),
`recorded_int` (:231). `cli.py` itself depends on `first_backend` + `recorded_int`
(`cli.py:152-155`). Adding "plus the entry normalizers (`read_record`,
`first_backend`, `backend_on_port`, `recorded_int`)" to the row would make it a
complete map; leaving it is not wrong, just incomplete.

### N7 — `own_or_first_port`'s own-context branch is documented as production-inert

Per `backend_registry.py:246-268` (landed in `60e48da`), the only caller is
`singleton.restart_backend`, which uses the answer **only to seed**
`_select_backend_port`; selection re-reads `port_for_context` itself and discards
the seed whenever our own context has an entry. So the branch that still decides
anything is the `first_backend` fallback. If any drafted DESIGN §2.7 or CLAUDE.md
prose implies `own_or_first_port` decides restart's port, soften it — restart
terminates the port **selection** returned (Task 4d, `4e2ede3`).

### N8 — RUNBOOK has no doctor sample block today

`RUNBOOK.md:41-49` is a `status` sample only; there is no `doctor` output sample
anywhere. If Task 7 wants to show the new `contexts    :` block, that is a **new**
fenced block — and per prep §10 it must **not** carry
`<!-- doc-example: runnable -->`, or `tests/test_doc_examples.py` will try to
execute it. The exact rendering to copy, if you add one:

```
contexts    :
  backend  win-session-1  port 19222  pid 12345  version 2.0.4  responsive  (can show windows)
  backend  headless  port 19223  pid 12346  version 2.0.4  down  (headless only)
```

### N9 — Nothing in the prep's §2c/§2.7 draft contradicts the code

Spot-checked the load-bearing claims: `display_context.py:9-12` states the
observational/never-enter-another-session contract and the session-2-vs-session-1
anecdote; `WTSGetActiveConsoleSessionId` appears nowhere in `src/`; the guard names
the context, the desktop remedy, `headless=True`, **and** `doctor` (three pointers,
not two — the draft says "the two remedies", which is fine as written since doctor
is a diagnostic, not a remedy).
