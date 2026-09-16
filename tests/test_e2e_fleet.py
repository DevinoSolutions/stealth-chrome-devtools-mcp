"""The CI stand-in for the manual multi-browser fleet run.

Every defect this branch adds coverage for — F-873 through F-881 — was found
the same way: by driving a LIVE release against several real browsers at once
and comparing every answer with the page. The committed suite was green
throughout, because each of its nodes drives one browser through one tool and
then asserts something about that tool's own vocabulary. Four things that fleet
run did which no committed node did are what this module reproduces:

* **several browsers at once**, so an answer that is really about "whichever
  instance the manager looked at last" cannot hide behind there being only one;
* **the ADVERTISED profile path as well as the named one.** The manual run was
  one master plus nine auto-clones, because `user_data_dir` is documented as
  "leave UNSET for normal use". Half this fleet is unnamed for that reason, and
  it is also the only half that can assert the disposable-profile promise;
* **a mix of page shapes in one run** — a plain page, a page whose ``load`` is
  held open, an app shell whose document cannot scroll, and a form — so a tool
  that quietly assumes the ordinary shape is visible next to one that does not;
* **every answer checked against the page itself**, in JavaScript, rather than
  against the tool's own record. A record that agrees with itself is what
  F-873, F-875, F-876 and F-877 each shipped.

It is one node on purpose. The fleet is the unit: six browsers spawned in ONE
``gather``, navigated in ONE ``gather``, driven in ONE ``gather``, and only
then asked — collectively — whether ``list_instances`` can still name what each
one is showing. Split into six nodes it would cost six fleets and stop being
the shape that found the defects.

**Two members move their page WITHOUT the ``navigate`` tool**, and that is what
makes the listing block evidence rather than decoration. F-874 §2's mechanism is
that ``BrowserInstance``'s cached pair had exactly two writers, ``spawn_browser``
and ``navigate`` — so on a fleet where every page arrived through ``navigate``
the stale cache would have been RIGHT and the block would pass against the
defect. The ``click`` member's own click handler retitles its page (§1 row 1) and
the ``tabswitch`` member ends up on a tab it opened afterwards (§1 row 4).

Sizing. Six members, measured locally (Windows 11, Chrome 152): ``spawn 3.6s,
navigate 1.3s, actions 1.7s, total 6.7s`` over ``roles ['clone', 'explicit',
'master']`` — all three profile kinds in one run, which is the manual fleet's
own shape. Six rather than four because each
member owns exactly ONE of the six (page shape, tool) pairs the finding set
needs, and folding two onto one member would make those two serial. The phase
times are PRINTED and nothing asserts them: on a loaded 2-core runner a wall
budget is the one thing in this file that could go red for a reason that is not
about the product, and a diagnostic that always prints is worth more than an
assertion nobody would trust.

Isolation. The browser-session root is redirected for the whole test session at
``tests/conftest.py`` import time, BEFORE any fixture runs — see the comment
there for why a per-test fixture cannot do it (``get_settings`` is ``lru_cache``d
and the module's own autouse ``_warmup`` spawns a browser ahead of any
function-scoped root fixture, which is how an earlier revision of this very file
wrote six 108 MB profiles into the developer's real session root). This node
therefore does NOT declare ``tmp_empty_root``: it would be decorative, and the
disk assertions below are made against ``clone_storage.clone_root_dir()`` — the
root the product actually used — rather than against a temp directory nothing
wrote to.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import pytest

from e2e_helpers import (
    eval_js,
    get_fn,
    instance_entry,
    integration_pytestmark,
    sandbox_kwargs,
    warmup_once,
)
from fixture_routes import (
    COV_CLICKED_TITLE,
    COV_FORM_TITLE,
    COV_PLAIN_SENTINEL,
    COV_SHELL_TITLE,
    COV_TITLE_AFTER_LOAD,
)

pytestmark = integration_pytestmark()

#: One member per (page shape, tool) pair. See the module docstring for why
#: this is six and not four.
FLEET_SIZE = 6

#: How long ``/cov/slow_load.html``'s ``load`` is held open for the member that
#: carries F-881's question INTO the fleet: shorter than the standalone node's
#: hold (``test_e2e_load_milestone``) because here it runs beside five other
#: browsers and the claim is only that the fleet still waits for it.
HELD_MS = 1_200

#: How far the app-shell member is asked to scroll. Inside its 6000 px filler,
#: so the scroller has somewhere to go and is nowhere near its far edge.
SCROLL_AMOUNT = 800

#: How long an auto-clone's directory may take to disappear after its instance
#: closes. `process_cleanup._cleanup_auto_profile` removes it on close, but a
#: Windows file lock defers the delete to a later retry, so this is a bounded
#: poll and not a sleep. Measured locally: gone before the first poll.
RECLAIM_BUDGET_SECONDS = 15.0


@pytest.fixture(autouse=True)
async def _warmup():
    await warmup_once()
    yield


def _clone_root():
    """The clone root the PRODUCT resolved, not one a test hoped for."""
    from stealth_chrome_devtools_mcp.embedded import clone_storage

    return clone_storage.clone_root_dir()


def _dirs_in(root: Path) -> set[str]:
    if not root.exists():
        return set()
    return {path.name for path in root.iterdir() if path.is_dir()}


def _selection(spawn_result: dict) -> dict:
    """The profile the spawn actually got: role, and the directory on disk."""
    return spawn_result["spawn_diagnostics"]["profile_selection"]


async def _drive_shell(iid):
    """F-875 + F-878 inside the fleet: the nested scroller, truthfully."""
    scroll_page = get_fn("scroll_page")
    return await scroll_page(instance_id=iid, direction="down", amount=SCROLL_AMOUNT)


async def _drive_type(iid, marker):
    """F-873 inside the fleet: a trailing newline submits a one-field form."""
    type_text = get_fn("type_text")
    return await type_text(
        instance_id=iid,
        selector="#cov-field",
        text=f"{marker}\n",
        parse_newlines=True,
    )


async def _drive_click(iid):
    """F-876 inside the fleet: where did the click actually go.

    The click also retitles the page, which is this member's SECOND job — see
    the module docstring on F-874 §1 row 1.
    """
    click_element = get_fn("click_element")
    return await click_element(instance_id=iid, selector="#cov-button")


async def _drive_select(iid):
    """F-877 inside the fleet: the Python matching rule, over a label."""
    select_option = get_fn("select_option")
    return await select_option(instance_id=iid, selector="#cov-select", text="Gamma")


async def _drive_slow(iid):
    """F-881 inside the fleet: the page's own ``load``, under contention."""
    return await eval_js(iid, "document.readyState")


async def _drive_tabswitch(iid, base):
    """F-874 §1 row 4: end up on a tab this member OPENED, not one it navigated.

    Reads its own plain page first — the control that the fleet's most ordinary
    member is fine — then opens a second tab and switches to it. ``new_tab``
    and ``switch_tab`` are not writers of the cached url/title pair, so after
    this the only way to name what this browser is showing is to ask Chrome.
    """
    new_tab = get_fn("new_tab")
    switch_tab = get_fn("switch_tab")
    sentinel = await eval_js(iid, "document.getElementById('sentinel').textContent")
    opened = await new_tab(instance_id=iid, url=f"{base}/cov/app_shell.html")
    assert await switch_tab(instance_id=iid, tab_id=opened["tab_id"]) is True
    return sentinel


async def test_a_fleet_of_six_browsers_answers_truthfully_about_every_page(
    fixture_app_server, caplog
):
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    navigate = get_fn("navigate")
    list_instances = get_fn("list_instances")
    close = get_fn("close_instance")

    # The backend's own durable-warning channel. `debug_logger.log_warning`
    # writes here, which is how a degraded `list_instances` row (F-874), a
    # storage read the page refused (F-869) or any other quiet fallback would
    # announce itself. Asserting it stayed empty is what stops this node
    # passing on six browsers that all half-worked.
    caplog.set_level(logging.WARNING, logger="stealth.backend")

    #: (page, what to do with it, whether the profile is NAMED). Three of each:
    #: the unnamed half is the advertised path and the only half whose profile
    #: is disposable; the named half is what an explicit `user_data_dir` does.
    plan = [
        ("/cov/app_shell.html", "shell", False),
        ("/cov/form.html", "type", True),
        ("/cov/form.html", "click", False),
        ("/cov/form.html", "select", True),
        (f"/cov/slow_load.html?ms={HELD_MS}", "slow", False),
        ("/cov/plain.html", "tabswitch", True),
    ]
    assert len(plan) == FLEET_SIZE
    kinds = [kind for _, kind, _ in plan]

    clone_root = _clone_root()
    before_dirs = _dirs_in(clone_root)

    started = time.monotonic()
    spawned = await asyncio.gather(
        *(
            spawn(
                headless=True,
                **({"user_data_dir": f"fleet-{kind}"} if named else {}),
                **sandbox_kwargs(),
            )
            for _, kind, named in plan
        )
    )
    spawn_seconds = time.monotonic() - started
    ids = [result["instance_id"] for result in spawned]
    assert len(set(ids)) == FLEET_SIZE, f"the fleet shares instance ids: {ids}"

    # Every member got its OWN profile, and got the KIND of profile it asked
    # for. Three concurrent unnamed spawns resolving to one directory would be
    # six browsers on three profiles, and every disk claim below would be about
    # something other than what ran.
    selections = [_selection(result) for result in spawned]
    roles = {
        kind: selection["profile_role"]
        for kind, selection in zip(kinds, selections, strict=True)
    }
    used_dirs = {
        kind: Path(selection["user_data_dir"]).name
        for kind, selection in zip(kinds, selections, strict=True)
    }
    assert len(set(used_dirs.values())) == FLEET_SIZE, used_dirs
    for _, kind, named in plan:
        if named:
            assert roles[kind] == "explicit", (kind, roles[kind])
        else:
            assert roles[kind] in {"master", "clone"}, (kind, roles[kind])
    # The advertised path really is exercised: at least one disposable clone.
    # (The FIRST unnamed spawn takes the master profile when it is free, which
    # is exactly the manual run's one-master-plus-clones shape.)
    assert "clone" in set(roles.values()), roles

    try:
        nav_started = time.monotonic()
        navigations = await asyncio.gather(
            *(
                navigate(instance_id=iid, url=f"{base}{path}")
                for iid, (path, _, _) in zip(ids, plan, strict=True)
            )
        )
        nav_seconds = time.monotonic() - nav_started

        # Every navigation answered about ITS OWN page. A fleet is the only
        # place this can be wrong: with one browser, "the last page navigated"
        # and "this browser's page" are the same string.
        for iid, (path, _, _), result in zip(ids, plan, navigations, strict=True):
            assert result["url"].endswith(path), (iid, path, result["url"])

        # The held page, in the fleet: its load fired before navigate answered.
        slow_index = kinds.index("slow")
        assert navigations[slow_index]["title"] == COV_TITLE_AFTER_LOAD, (
            "a navigation in the fleet answered before its page's own load "
            f"({navigations[slow_index]['title']!r}) — F-881"
        )

        id_by_kind = dict(zip(kinds, ids, strict=True))
        marker = "fleet-typed"
        act_started = time.monotonic()
        answers = await asyncio.gather(
            _drive_shell(id_by_kind["shell"]),
            _drive_type(id_by_kind["type"], marker),
            _drive_click(id_by_kind["click"]),
            _drive_select(id_by_kind["select"]),
            _drive_slow(id_by_kind["slow"]),
            _drive_tabswitch(id_by_kind["tabswitch"], base),
        )
        act_seconds = time.monotonic() - act_started
        by_kind = dict(
            zip(
                ["shell", "type", "click", "select", "slow", "tabswitch"],
                answers,
                strict=True,
            )
        )

        # ── Each answer, against the page's own state ────────────────────────
        # F-875/F-878: the app shell's DIV was driven, and the record's
        # `scroll_y_after` is that div's real `scrollTop`, read independently.
        shell = by_kind["shell"]
        assert shell["scrolled"] is True, shell
        assert shell["settled"] is True, shell
        assert shell["scroller_is_document"] is False, shell
        assert shell["scroller"]["id"] == "shell", shell
        real_top = await eval_js(
            id_by_kind["shell"], "document.getElementById('shell').scrollTop"
        )
        assert round(real_top) == shell["scroll_y_after"], (real_top, shell)
        # And the DOCUMENT did not move, which is what makes the pick the claim.
        assert await eval_js(id_by_kind["shell"], "window.scrollY") == 0

        # F-873: the tool said the text landed, and the form says it submitted.
        assert by_kind["type"] is True
        typed = await eval_js(
            id_by_kind["type"], "document.getElementById('cov-field').value"
        )
        assert typed == marker, typed
        assert await eval_js(id_by_kind["type"], "window.__covForm.submits") == 1

        # F-876: the click record names the target, and the page reacted.
        click = by_kind["click"]
        assert click["dispatch"] == "coordinate", click
        assert click["reason"] is None, click
        assert click["hit_is_target"] is True, click
        assert click["target"]["id"] == "cov-button", click
        assert (
            await eval_js(
                id_by_kind["click"],
                "document.getElementById('cov-clicked').textContent",
            )
            == "CLICKED"
        )

        # F-877: the option the rule picked is the one the control holds, and
        # the page's own change listener saw it.
        selection = by_kind["select"]
        assert selection["by"] == "text", selection
        assert selection["changed"] is True, selection
        assert selection["selected_index"] == 2, selection
        assert (
            await eval_js(
                id_by_kind["select"], "document.getElementById('cov-select').value"
            )
            == "c"
        )
        assert (
            await eval_js(
                id_by_kind["select"], "JSON.stringify(window.__covForm.selected)"
            )
            == '["c"]'
        )

        # F-881: the held page is complete by the time anything else ran.
        assert by_kind["slow"] == "complete"
        assert await eval_js(id_by_kind["slow"], "window.__covLoaded") is True

        # The control member read its own plain page before opening a tab.
        assert by_kind["tabswitch"] == COV_PLAIN_SENTINEL

        # ── F-874: the listing names what each browser is showing NOW ────────
        # Six live titles at once, each read fresh from its own browser. TWO of
        # the six moved without `navigate` — `click` retitled its own page and
        # `tabswitch` is on a tab it opened — so the pre-fix cached pair is
        # wrong for those two and this block goes red against the defect. The
        # url is asserted beside the title per member so a listing that mixed
        # two members up passes neither.
        listing = await list_instances()
        want = {
            "shell": (COV_SHELL_TITLE, "/cov/app_shell.html"),
            "type": (COV_FORM_TITLE, "/cov/form.html"),
            "click": (COV_CLICKED_TITLE, "/cov/form.html"),
            "select": (COV_FORM_TITLE, "/cov/form.html"),
            "slow": (COV_TITLE_AFTER_LOAD, "/cov/slow_load.html"),
            "tabswitch": (COV_SHELL_TITLE, "/cov/app_shell.html"),
        }
        for kind, (title, path) in want.items():
            entry = instance_entry(listing, id_by_kind[kind])
            assert entry["partial"] is False, entry
            assert entry["title"] == title, (kind, entry)
            assert path in entry["current_url"], (kind, entry)

        print(
            f"\nfleet of {FLEET_SIZE}: spawn {spawn_seconds:.1f}s, "
            f"navigate {nav_seconds:.1f}s, actions {act_seconds:.1f}s, "
            f"total {time.monotonic() - started:.1f}s "
            f"(roles {sorted(set(roles.values()))})"
        )
    finally:
        closed = await asyncio.gather(
            *(close(instance_id=iid) for iid in ids), return_exceptions=True
        )

    assert all(result is True for result in closed), closed

    # Nothing of ours is live any more.
    still_live = {
        entry["instance_id"]
        for entry in await list_instances()
        if entry.get("source") == "active"
    }
    assert still_live.isdisjoint(ids), still_live

    # ── The disposable-profile promise, on the path that makes it ───────────
    # `spawn_browser`'s docstring says an unset `user_data_dir` "automatically
    # clones a disposable session from the master profile and deletes it when
    # the instance closes", and the same docstring says a NAMED profile "is NOT
    # auto-cleaned and persists on disk indefinitely". Both halves are asserted,
    # because a fleet of only named profiles could not have caught a broken
    # reclaim and a fleet of only unnamed ones could not have caught a named
    # profile being eaten. The delete is `process_cleanup._cleanup_auto_profile`
    # and it can be DEFERRED by a Windows file lock, so this is a bounded poll.
    clone_dirs = {used_dirs[kind] for kind in kinds if roles[kind] == "clone"}
    deadline = time.monotonic() + RECLAIM_BUDGET_SECONDS
    leftover = clone_dirs & _dirs_in(clone_root)
    while leftover and time.monotonic() < deadline:
        await asyncio.sleep(0.25)
        leftover = clone_dirs & _dirs_in(clone_root)
    assert not leftover, (
        f"disposable auto-clone(s) still on disk {RECLAIM_BUDGET_SECONDS:.0f}s "
        f"after close: {sorted(leftover)}"
    )

    named_dirs = {used_dirs[kind] for kind in kinds if roles[kind] == "explicit"}
    after_dirs = _dirs_in(clone_root)
    assert named_dirs <= after_dirs, (
        f"a NAMED profile was reclaimed, which nothing may do: "
        f"{sorted(named_dirs - after_dirs)}"
    )
    # And the fleet invented nothing: every directory that appeared is one a
    # member was actually given.
    appeared = after_dirs - before_dirs
    assert appeared <= named_dirs, (
        f"the fleet left directories nobody asked for: {sorted(appeared - named_dirs)}"
    )

    # And no browser in the fleet degraded quietly on the way through — with
    # ONE named exception, which is a property of the LANE and not of the run.
    # An auto-clone's directory name is seeded from the MCP client's roots
    # (`clone_storage._client_session_seed`), and the in-process E2E tier has no
    # MCP client at all, so `get_context()` raises, the warning is logged and
    # the documented `codex_workspace`/`claude_project_dir`/`pwd`/`getcwd`
    # fallback chain answers. Named rather than filtered out by level or by
    # logger, so the exception cannot quietly grow: everything else must be
    # empty, and the exception itself is only tolerated where it is EXPECTED —
    # this fleet has unnamed members, and it is also positive evidence that the
    # auto-clone path really ran.
    seed_fallbacks = [
        record
        for record in caplog.records
        if "_client_session_seed" in record.getMessage()
    ]
    others = [
        record
        for record in caplog.records
        if record.name.startswith("stealth.")
        and record.levelno >= logging.WARNING
        and record not in seed_fallbacks
    ]
    assert others == [], [record.getMessage() for record in others]
    assert seed_fallbacks, (
        "no clone-seed fallback was logged, so no member took the auto-clone "
        "path — this fleet is no longer covering the advertised profile path"
    )
    assert len(seed_fallbacks) == len(clone_dirs), (
        f"{len(seed_fallbacks)} clone-seed fallbacks for {len(clone_dirs)} "
        "auto-clone(s) — one per clone is the expected shape"
    )
