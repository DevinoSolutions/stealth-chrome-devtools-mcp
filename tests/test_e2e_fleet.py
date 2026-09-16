"""The CI stand-in for the manual multi-browser fleet run.

Every defect this branch adds coverage for — F-873 through F-881 — was found
the same way: by driving a LIVE release against several real browsers at once
and comparing every answer with the page. The committed suite was green
throughout, because each of its nodes drives one browser through one tool and
then asserts something about that tool's own vocabulary. The three things that
fleet run did which no committed node did are what this module reproduces:

* **several browsers at once**, so an answer that is really about "whichever
  instance the manager looked at last" cannot hide behind there being only one;
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

Sizing. Six members, measured locally (Windows 11, Chrome 152). Cold, with the
six named profiles being cloned for the first time: ``spawn 9.3s, navigate
1.3s, actions 1.6s, total 12.4s`` (15.6 s pytest wall). Warm, with the profiles
already on disk: ``spawn 1.7s, navigate 1.3s, actions 1.7s, total 5.0s`` (8.3 s
wall). The cold number is the one the budget is sized against, because a CI cell
is always cold. Six rather than four because each member owns
exactly ONE of the six (page shape, tool) pairs the finding set needs, and
folding two onto one member would make those two serial. The 60 s budget below
is roughly 5x the measured total, which is the headroom a hosted 2-core cell
needs; it is not a performance assertion but a shape one — a fleet that needs
longer than that has stopped being a fleet and has become a queue. The three
phase times are printed on every run so a cell that is drifting says so before
it fails.

Isolation: a temp session root (``tmp_empty_root``), named profiles under it,
and the session fixture app. Nothing here reaches the network or a real
profile.
"""

from __future__ import annotations

import asyncio
import logging
import time

import pytest

from e2e_helpers import (
    eval_js,
    get_fn,
    integration_pytestmark,
    sandbox_kwargs,
    warmup_once,
)
from fixture_routes import (
    COV_FORM_TITLE,
    COV_PLAIN_SENTINEL,
    COV_PLAIN_TITLE,
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

#: The whole node, spawn to teardown. Not a performance assertion — a fleet
#: that needs longer than this on any cell has stopped being a fleet and has
#: become a queue, which is exactly the failure mode the manual run saw.
FLEET_DEADLINE_SECONDS = 60.0

#: How far the app-shell member is asked to scroll. Inside its 6000 px filler,
#: so the scroller has somewhere to go and is nowhere near its far edge.
SCROLL_AMOUNT = 800


@pytest.fixture(autouse=True)
async def _warmup():
    await warmup_once()
    yield


def _entry(listing, instance_id):
    [entry] = [e for e in listing if e["instance_id"] == instance_id]
    return entry


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
    """F-876 inside the fleet: where did the click actually go."""
    click_element = get_fn("click_element")
    return await click_element(instance_id=iid, selector="#cov-button")


async def _drive_select(iid):
    """F-877 inside the fleet: the Python matching rule, over a label."""
    select_option = get_fn("select_option")
    return await select_option(instance_id=iid, selector="#cov-select", text="Gamma")


async def _drive_slow(iid):
    """F-881 inside the fleet: the page's own ``load``, under contention."""
    return await eval_js(iid, "document.readyState")


async def _drive_plain(iid):
    """The control: a page with nothing special about it still reads back."""
    return await eval_js(iid, "document.getElementById('sentinel').textContent")


async def test_a_fleet_of_six_browsers_answers_truthfully_about_every_page(
    fixture_app_server, tmp_empty_root, caplog
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

    #: (page, what to do with it, what the answer must agree with).
    plan = [
        ("/cov/app_shell.html", "shell"),
        ("/cov/form.html", "type"),
        ("/cov/form.html", "click"),
        ("/cov/form.html", "select"),
        (f"/cov/slow_load.html?ms={HELD_MS}", "slow"),
        ("/cov/plain.html", "plain"),
    ]
    assert len(plan) == FLEET_SIZE

    started = time.monotonic()
    spawned = await asyncio.gather(
        *(
            spawn(
                headless=True,
                user_data_dir=f"fleet-{index}",
                **sandbox_kwargs(),
            )
            for index in range(FLEET_SIZE)
        )
    )
    spawn_seconds = time.monotonic() - started
    ids = [result["instance_id"] for result in spawned]
    assert len(set(ids)) == FLEET_SIZE, f"the fleet shares instance ids: {ids}"

    try:
        nav_started = time.monotonic()
        navigations = await asyncio.gather(
            *(
                navigate(instance_id=iid, url=f"{base}{path}")
                for iid, (path, _) in zip(ids, plan, strict=True)
            )
        )
        nav_seconds = time.monotonic() - nav_started

        # Every navigation answered about ITS OWN page. A fleet is the only
        # place this can be wrong: with one browser, "the last page navigated"
        # and "this browser's page" are the same string.
        for iid, (path, _), result in zip(ids, plan, navigations, strict=True):
            assert result["url"].endswith(path), (iid, path, result["url"])

        # The held page, in the fleet: its load fired before navigate answered.
        slow_index = [kind for _, kind in plan].index("slow")
        assert navigations[slow_index]["title"] == COV_TITLE_AFTER_LOAD, (
            "a navigation in the fleet answered before its page's own load "
            f"({navigations[slow_index]['title']!r}) — F-881"
        )

        drive = {
            "shell": _drive_shell,
            "click": _drive_click,
            "select": _drive_select,
            "slow": _drive_slow,
            "plain": _drive_plain,
        }
        marker = "fleet-typed"
        act_started = time.monotonic()
        answers = await asyncio.gather(
            *(
                _drive_type(iid, marker) if kind == "type" else drive[kind](iid)
                for iid, (_, kind) in zip(ids, plan, strict=True)
            )
        )
        act_seconds = time.monotonic() - act_started
        by_kind = {
            kind: answer for (_, kind), answer in zip(plan, answers, strict=True)
        }
        id_by_kind = {kind: iid for (_, kind), iid in zip(plan, ids, strict=True)}
        path_by_kind = {kind: path for path, kind in plan}

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

        # The control member.
        assert by_kind["plain"] == COV_PLAIN_SENTINEL

        # ── F-874: the listing names what each browser is showing NOW ────────
        # Six live titles at once, each read fresh from its own browser. The
        # defect was a cached pair written only by spawn and navigate: it would
        # answer here too, and on these pages it would even be RIGHT — which is
        # why the URL is asserted beside the title, per member. A listing that
        # mixed two members up passes neither.
        listing = await list_instances()
        want = {
            "shell": COV_SHELL_TITLE,
            "type": COV_FORM_TITLE,
            "click": COV_FORM_TITLE,
            "select": COV_FORM_TITLE,
            "slow": COV_TITLE_AFTER_LOAD,
            "plain": COV_PLAIN_TITLE,
        }
        for kind, title in want.items():
            entry = _entry(listing, id_by_kind[kind])
            assert entry["partial"] is False, entry
            assert entry["title"] == title, (kind, entry)
            assert entry["current_url"].endswith(path_by_kind[kind]), (kind, entry)

        elapsed = time.monotonic() - started
        print(
            f"\nfleet of {FLEET_SIZE}: spawn {spawn_seconds:.1f}s, "
            f"navigate {nav_seconds:.1f}s, actions {act_seconds:.1f}s, "
            f"total {elapsed:.1f}s (budget {FLEET_DEADLINE_SECONDS:.0f}s)"
        )
        assert elapsed <= FLEET_DEADLINE_SECONDS, (
            f"the fleet took {elapsed:.1f}s (> {FLEET_DEADLINE_SECONDS:.0f}s) — "
            "concurrent work is being served as a queue"
        )
    finally:
        closed = await asyncio.gather(
            *(close(instance_id=iid) for iid in ids), return_exceptions=True
        )

    assert all(result is True for result in closed), closed

    # Nothing of ours is live any more, and nothing DISPOSABLE was left on disk.
    # The six profiles are NAMED, so they survive by design (that is what a
    # named session is); what must not survive is an auto-clone or a per-attempt
    # clone directory, because those are the ones nothing will ever come back
    # for. Anything under the clone root that is not one of the six names is one.
    still_live = {
        entry["instance_id"]
        for entry in await list_instances()
        if entry.get("source") == "active"
    }
    assert still_live.isdisjoint(ids), still_live
    expected_dirs = {f"fleet-{index}" for index in range(FLEET_SIZE)}
    left_on_disk = {
        path.name for path in tmp_empty_root["sessions"].iterdir() if path.is_dir()
    }
    assert left_on_disk <= expected_dirs, (
        f"the fleet left profile directories nothing will reclaim: "
        f"{sorted(left_on_disk - expected_dirs)}"
    )

    # And no browser in the fleet degraded quietly on the way through.
    warnings = [
        record
        for record in caplog.records
        if record.name.startswith("stealth.") and record.levelno >= logging.WARNING
    ]
    assert warnings == [], [record.getMessage() for record in warnings]
