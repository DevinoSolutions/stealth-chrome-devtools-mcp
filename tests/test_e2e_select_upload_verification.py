"""F-877 in a REAL Chrome: the option is selected, and the input holds the files.

The hermetic half (``tests/test_select_upload_verification.py``) pins the record
shape and the order of the two applies. This half pins what Chrome DOES, because
every claim F-877 rests on is a claim about Blink:

* ``select.value = "nope"`` sets ``selectedIndex`` to ``-1`` — it CLEARS the
  page's standing selection rather than leaving it alone — and fires a
  ``change`` saying so;
* an out-of-range ``index`` and a ``text`` that matches nothing each leave the
  control exactly where it was, silently;
* ``option.text`` is the COLLAPSED text and ``option.label`` can differ from it,
  which is the collision Chrome's typeahead resolved to the wrong option;
* ``DOM.setFileInputFiles`` SUCCEEDS with two files on an input that has no
  ``multiple`` attribute and Chrome keeps only the first — the one measured row
  where ``upload_file``'s reported count was a lie.

Runs against ``interactions.html``, whose F-877 block adds the selects and file
inputs above and logs ``input``/``change`` with ``isTrusted`` for each. Its own
session root is a ``tmp_path`` (``tmp_empty_root``), so no run of this file can
touch a real profile.
"""

from __future__ import annotations

import pytest

from e2e_helpers import (
    eval_js,
    get_fn,
    integration_pytestmark,
    navigate_and_settle,
    read_actions,
    sandbox_kwargs,
    warmup_once,
)
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

pytestmark = integration_pytestmark()

PAGE = "interactions.html"


@pytest.fixture(autouse=True)
async def _warmup():
    await warmup_once()
    yield


async def _selected(iid, selector):
    """``(selectedIndex, value)`` read straight from the page."""
    index = await eval_js(iid, f"document.querySelector({selector!r}).selectedIndex")
    value = await eval_js(iid, f"document.querySelector({selector!r}).value")
    return int(index), value


async def _file_count(iid, selector):
    return int(await eval_js(iid, f"document.querySelector({selector!r}).files.length"))


# ---------------------------------------------------------------------------
# select_option
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"value": "nonexistent"},
        {"text": "NoSuchOption"},
        {"index": 99},
        {"index": -1},
    ],
    ids=["value", "text", "index-high", "index-low"],
)
async def test_a_selection_that_cannot_happen_raises(
    fixture_app_server, tmp_empty_root, kwargs
):
    """All four answered ``True`` (measured, Chrome 152). The ``value`` row is
    the one that is not even a no-op: it left ``selectedIndex`` at ``-1``, i.e.
    it destroyed the selection the page already had, and reported success."""
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    select_option = get_fn("select_option")
    close = get_fn("close_instance")

    iid = (await spawn(headless=True, **sandbox_kwargs()))["instance_id"]
    try:
        await navigate_and_settle(iid, f"{base}/{PAGE}")
        before = await _selected(iid, "#select-fidelity")

        with pytest.raises(ToolError) as caught:
            await select_option(instance_id=iid, selector="#select-fidelity", **kwargs)

        assert await _selected(iid, "#select-fidelity") == before
        assert "#select-fidelity" in str(caught.value)
    finally:
        await close(instance_id=iid)


@pytest.mark.parametrize(
    "kwargs,index",
    [
        ({"value": "three"}, 2),
        ({"index": 1}, 1),
        ({"text": "two"}, 1),
        ({"text": "thr"}, 2),
        ({"text": "TWO"}, 1),
    ],
    ids=["value", "index", "text-exact", "text-prefix", "text-case"],
)
async def test_a_selection_that_works_reports_the_index_it_landed_on(
    fixture_app_server, tmp_empty_root, kwargs, index
):
    """The guard against over-correcting, and the proof the prefix /
    case-insensitive behaviour the shipped typeahead had is preserved."""
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    select_option = get_fn("select_option")
    close = get_fn("close_instance")

    iid = (await spawn(headless=True, **sandbox_kwargs()))["instance_id"]
    try:
        await navigate_and_settle(iid, f"{base}/{PAGE}")

        record = await select_option(
            instance_id=iid, selector="#select-fidelity", **kwargs
        )

        assert record["selected_index"] == index
        assert record["option_count"] == 3
        assert (await _selected(iid, "#select-fidelity"))[0] == index
    finally:
        await close(instance_id=iid)


async def test_the_selection_fires_input_then_change(
    fixture_app_server, tmp_empty_root
):
    """Measured: Chrome's own typeahead fires ``input`` then ``change``. The
    shipped ``value``/``index`` arms fired ``change`` alone, so a page listening
    on ``input`` never heard the change the tool called a success."""
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    select_option = get_fn("select_option")
    close = get_fn("close_instance")

    iid = (await spawn(headless=True, **sandbox_kwargs()))["instance_id"]
    try:
        await navigate_and_settle(iid, f"{base}/{PAGE}")

        await select_option(instance_id=iid, selector="#sel-labels", value="l1")

        actions = [a for a in await read_actions(iid) if "sel-labels" in a]
        assert actions == [
            "input:sel-labels:l1:untrusted",
            "change:sel-labels:l1:untrusted",
        ]
    finally:
        await close(instance_id=iid)


async def test_an_option_whose_label_differs_from_its_text_is_reachable_by_either(
    fixture_app_server, tmp_empty_root
):
    """``<option value="l1" label="LabelThree">TextThree</option>``: measured,
    ``.text`` is ``"TextThree"`` and ``.label`` is ``"LabelThree"``, a browser
    renders the LABEL, and Chrome's typeahead answered this collision by
    selecting a DIFFERENT option."""
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    select_option = get_fn("select_option")
    close = get_fn("close_instance")

    iid = (await spawn(headless=True, **sandbox_kwargs()))["instance_id"]
    try:
        await navigate_and_settle(iid, f"{base}/{PAGE}")
        assert (
            await eval_js(iid, "document.querySelector('#sel-labels').options[2].label")
            == "LabelThree"
        )

        for query in ("LabelThree", "TextThree"):
            await navigate_and_settle(iid, f"{base}/{PAGE}")
            record = await select_option(
                instance_id=iid, selector="#sel-labels", text=query
            )
            assert record["selected_index"] == 2, query
    finally:
        await close(instance_id=iid)


async def test_a_disabled_option_is_unreachable_by_text_and_reachable_by_value(
    fixture_app_server, tmp_empty_root
):
    """The measured asymmetry, preserved deliberately (finding §6): Chrome's
    typeahead refuses a ``disabled`` ``<option>`` and a user cannot reach one,
    but ``select.value = …`` onto it is permitted and this tool does not
    second-guess the browser."""
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    select_option = get_fn("select_option")
    close = get_fn("close_instance")

    iid = (await spawn(headless=True, **sandbox_kwargs()))["instance_id"]
    try:
        await navigate_and_settle(iid, f"{base}/{PAGE}")

        with pytest.raises(ToolError):
            await select_option(
                instance_id=iid, selector="#sel-labels", text="Delta Two"
            )

        record = await select_option(
            instance_id=iid, selector="#sel-labels", value="d2"
        )
        assert record["selected_index"] == 1
    finally:
        await close(instance_id=iid)


async def test_a_disabled_select_can_only_ever_move_itself(
    fixture_app_server, tmp_empty_root
):
    """The worst measured row. ``send_keys`` focuses first, a ``<select
    disabled>`` cannot take focus, and the characters went wherever focus
    already was — measured, a request for the disabled select moved a DIFFERENT
    one with a trusted ``input``+``change`` pair, and the tool answered ``True``.

    Nothing here types, so the only control this call can touch is the one it
    named. It DOES select: Chrome permits ``selectedIndex = …`` on a disabled
    control, the shipped ``value``/``index`` arms already did exactly that
    (measured, honestly), and this fix makes the third arm uniform with them
    rather than adding a refusal the browser does not make (finding §6)."""
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    select_option = get_fn("select_option")
    close = get_fn("close_instance")

    iid = (await spawn(headless=True, **sandbox_kwargs()))["instance_id"]
    try:
        await navigate_and_settle(iid, f"{base}/{PAGE}")
        # give focus to another select first, exactly as the measurement did
        await select_option(instance_id=iid, selector="#select-fidelity", text="two")
        before = await _selected(iid, "#select-fidelity")

        record = await select_option(
            instance_id=iid, selector="#sel-disabled", text="Beta"
        )

        assert record["selected_index"] == 1
        assert (await _selected(iid, "#sel-disabled"))[0] == 1
        assert await _selected(iid, "#select-fidelity") == before
    finally:
        await close(instance_id=iid)


async def test_an_empty_select_raises(fixture_app_server, tmp_empty_root):
    """``<select id="sel-empty"></select>``: measured, the shipped value arm
    still fired a ``change`` and answered ``True``."""
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    select_option = get_fn("select_option")
    close = get_fn("close_instance")

    iid = (await spawn(headless=True, **sandbox_kwargs()))["instance_id"]
    try:
        await navigate_and_settle(iid, f"{base}/{PAGE}")

        with pytest.raises(ToolError):
            await select_option(instance_id=iid, selector="#sel-empty", value="one")

        assert not [a for a in await read_actions(iid) if "sel-empty" in a]
    finally:
        await close(instance_id=iid)


async def test_a_non_select_element_is_refused_not_written_to(
    fixture_app_server, tmp_empty_root
):
    """Measured: the shipped tool executed ``elem.value = "x"`` against an
    ``<input type="text">`` and fired a ``change``. There was no check anywhere
    that the resolved element is a ``<select>``."""
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    select_option = get_fn("select_option")
    close = get_fn("close_instance")

    iid = (await spawn(headless=True, **sandbox_kwargs()))["instance_id"]
    try:
        await navigate_and_settle(iid, f"{base}/{PAGE}")

        with pytest.raises(ToolError) as caught:
            await select_option(instance_id=iid, selector="#key-probe", value="INJECT")

        assert await eval_js(iid, "document.querySelector('#key-probe').value") == ""
        assert "input" in str(caught.value)
    finally:
        await close(instance_id=iid)


async def test_a_multiple_select_reports_what_it_now_holds(
    fixture_app_server, tmp_empty_root
):
    """One criterion in, one option selected — the record says so, which is how
    a caller discovers the limit the signature imposes (finding §6)."""
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    select_option = get_fn("select_option")
    close = get_fn("close_instance")

    iid = (await spawn(headless=True, **sandbox_kwargs()))["instance_id"]
    try:
        await navigate_and_settle(iid, f"{base}/{PAGE}")

        record = await select_option(
            instance_id=iid, selector="#sel-multi", value="two"
        )

        assert record["multiple"] is True
        assert record["selected_count"] == 1
        assert record["selected_index"] == 1
    finally:
        await close(instance_id=iid)


# ---------------------------------------------------------------------------
# upload_file
# ---------------------------------------------------------------------------


@pytest.fixture
def two_files(tmp_path):
    first, second = tmp_path / "a.txt", tmp_path / "bb.txt"
    first.write_text("a", encoding="utf-8")
    second.write_text("bb", encoding="utf-8")
    return str(first), str(second)


async def test_two_files_into_a_single_file_input_raises(
    fixture_app_server, tmp_empty_root, two_files
):
    """The one measured upload row. The raw CDP call answers ``None`` — no
    error anywhere — and Chrome keeps the FIRST file only. ``upload_file``
    reported ``count: 2``."""
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    upload_file = get_fn("upload_file")
    close = get_fn("close_instance")

    iid = (await spawn(headless=True, **sandbox_kwargs()))["instance_id"]
    try:
        await navigate_and_settle(iid, f"{base}/{PAGE}")

        with pytest.raises(ToolError) as caught:
            await upload_file(
                instance_id=iid, selector="#file-single", file_paths=list(two_files)
            )

        assert await _file_count(iid, "#file-single") == 1
        message = str(caught.value)
        assert "#file-single" in message
        assert two_files[0] not in message  # counts only, never a path
    finally:
        await close(instance_id=iid)


async def test_a_multiple_input_takes_both_and_says_what_it_holds(
    fixture_app_server, tmp_empty_root, two_files
):
    """The guard against over-correcting, and the shape that replaces the
    absolute-path echo."""
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    upload_file = get_fn("upload_file")
    close = get_fn("close_instance")

    iid = (await spawn(headless=True, **sandbox_kwargs()))["instance_id"]
    try:
        await navigate_and_settle(iid, f"{base}/{PAGE}")

        record = await upload_file(
            instance_id=iid, selector="#file-multi", file_paths=list(two_files)
        )

        assert record == {
            "selector": "#file-multi",
            "requested": 2,
            "attached": 2,
            "multiple": True,
            "total_bytes": 3,
        }
        assert await _file_count(iid, "#file-multi") == 2
    finally:
        await close(instance_id=iid)


async def test_a_single_upload_reports_the_bytes_the_input_holds(
    fixture_app_server, tmp_empty_root, two_files
):
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    upload_file = get_fn("upload_file")
    close = get_fn("close_instance")

    iid = (await spawn(headless=True, **sandbox_kwargs()))["instance_id"]
    try:
        await navigate_and_settle(iid, f"{base}/{PAGE}")

        record = await upload_file(
            instance_id=iid, selector="#file-single", file_paths=two_files[1]
        )

        assert record["attached"] == 1
        assert record["total_bytes"] == 2
        assert record["multiple"] is False
        assert "a.txt" not in str(record)
    finally:
        await close(instance_id=iid)


async def test_a_disabled_file_input_still_takes_the_file_and_says_so(
    fixture_app_server, tmp_empty_root, two_files
):
    """Deliberately unchanged (finding §6): ``DOM.setFileInputFiles`` attaches to
    a ``disabled`` input and Chrome fires a trusted ``change``, which a user
    could not produce. The tool does not refuse what CDP permits; what it does
    now is report the count truthfully."""
    base = fixture_app_server
    spawn = get_fn("spawn_browser")
    upload_file = get_fn("upload_file")
    close = get_fn("close_instance")

    iid = (await spawn(headless=True, **sandbox_kwargs()))["instance_id"]
    try:
        await navigate_and_settle(iid, f"{base}/{PAGE}")

        record = await upload_file(
            instance_id=iid, selector="#file-disabled", file_paths=two_files[0]
        )

        assert record["attached"] == 1
        assert await _file_count(iid, "#file-disabled") == 1
    finally:
        await close(instance_id=iid)
