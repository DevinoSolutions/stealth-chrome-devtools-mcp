"""F-877: ``select_option`` and ``upload_file`` reported a dispatch, not a result.

The third and fourth tools with F-873's shape, and the two F-876 §6 named and
left unmeasured. Measured on Chrome 152.0.7977.83, product code path, throwaway
profile:

* **``select_option``** answered ``True`` for eleven cases that did not select
  what was asked. ``value="nope"`` does not merely fail — ``select.value = …``
  sets ``selectedIndex`` to ``-1``, i.e. it CLEARS the selection the page
  already had, fires a ``change`` saying so, and reports success. ``index=99``
  and ``index=-1`` evaluate a script whose whole body is inside an ``if`` that
  does not run. ``text=…`` is ``send_keys``, so its real consumer is Chrome's
  ``<select>`` typeahead — a live buffer with a ~1 s timeout SHARED with the
  previous call, searching from the option after the current one, skipping
  ``disabled`` options — and on a ``<select disabled>``, which cannot take
  focus, the keys go wherever focus already was: measured, a request for
  ``#sel-disabled`` moved ``#sel-basic`` from ``one`` to ``two`` with a trusted
  ``input``+``change`` pair, and the tool answered ``True``. There is no check
  that the element is a ``<select>`` at all, so the same call against an
  ``<input type="text">`` writes into it.

* **``upload_file``** is honest in ten of eleven measured cases — every
  "resolved to the wrong thing" case already raises. Its defect is narrower and
  structural: ``{"uploaded": resolved, "count": len(resolved)}`` is built from
  the caller's own argument list, before the CDP call and regardless of it. Two
  paths into an input with no ``multiple`` attribute: ``DOM.setFileInputFiles``
  succeeds, Chrome keeps the FIRST file only, and the tool reports ``count:
  2``.

Both fixes are the same one: read the control back, and report what it holds as
counts and indices. Hermetic (``FakeTab`` + the new ``FakeSelect`` and
``FakeFileInput`` from ``tests/fakes.py``); the real-Chrome half is
``tests/test_e2e_select_upload_verification.py``.
"""

from __future__ import annotations

import json

import pytest

from fakes import FakeFileInput, FakeSelect, FakeTab
from stealth_chrome_devtools_mcp.embedded.dom_handler import DOMHandler
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

pytestmark = pytest.mark.asyncio

SELECT = "#country"
UPLOAD = "#avatar"


def _select_tab(**kwargs):
    element = FakeSelect(**kwargs)
    return FakeTab(select_result=element), element


def _upload_tab(**kwargs):
    element = FakeFileInput(**kwargs)
    return FakeTab(select_result=element), element


# ---------------------------------------------------------------------------
# select_option -- the option has to actually be selected
# ---------------------------------------------------------------------------


async def test_a_value_no_option_carries_raises_and_changes_nothing():
    """The headline row. ``select.value = "nope"`` set ``selectedIndex`` to -1,
    fired a ``change`` announcing the empty selection, and answered ``True`` —
    so a caller who asked for a value that is not in the list got a form in a
    state no user could have produced, reported as a success."""
    tab, element = _select_tab(selected_index=1)

    with pytest.raises(ToolError) as caught:
        await DOMHandler.select_option(tab, SELECT, value="nonexistent")

    assert element.selected_index == 1, "the standing selection must survive"
    assert element.events == [], "nothing may be dispatched for a non-match"
    assert SELECT in str(caught.value)


async def test_a_text_no_option_carries_raises():
    tab, element = _select_tab()

    with pytest.raises(ToolError):
        await DOMHandler.select_option(tab, SELECT, text="Delta")

    assert element.events == []


@pytest.mark.parametrize("index", [99, -1, 3])
async def test_an_index_outside_the_option_list_raises(index):
    """The shipped script's entire body was inside ``if (i >= 0 && i < len)``,
    so an out-of-range index evaluated to nothing at all and returned ``True``."""
    tab, element = _select_tab()

    with pytest.raises(ToolError):
        await DOMHandler.select_option(tab, SELECT, index=index)

    assert element.selected_index == 0
    assert element.events == []


async def test_an_empty_select_raises_rather_than_answering_true():
    """A ``<select>`` with no options: measured, the shipped value arm still
    fired a ``change`` and answered ``True``."""
    tab, _ = _select_tab(options=(), selected_index=-1)

    with pytest.raises(ToolError) as caught:
        await DOMHandler.select_option(tab, SELECT, value="one")

    assert "0" in str(caught.value), "the option count is the diagnostic"


async def test_a_non_select_element_raises_instead_of_being_written_to():
    """Measured: the same call against an ``<input type="text">`` executed
    ``elem.value = "x"`` and fired a ``change``. There was no check anywhere
    that the resolved element is a ``<select>``."""
    tab, element = _select_tab(tag="input")

    with pytest.raises(ToolError) as caught:
        await DOMHandler.select_option(tab, SELECT, value="two")

    assert element.events == []
    assert element.selected_index == 0
    assert "input" in str(caught.value)


async def test_no_keystroke_is_ever_dispatched_for_a_selection():
    """The worst measured row, fixed by construction rather than by
    verification. ``send_keys`` focuses the element first, and a
    ``<select disabled>`` cannot take focus, so the characters went wherever
    focus already was — measured, a request for ``#sel-disabled`` moved
    ``#sel-basic`` from ``one`` to ``two`` with a trusted ``input``+``change``
    pair, and the tool answered ``True``. Nothing here types, so that outcome is
    not reachable to be verified."""
    tab, element = _select_tab()

    await DOMHandler.select_option(tab, SELECT, text="Beta")

    assert element.keys_sent == []
    assert [f for f in tab.cdp_frames if "Input." in f.get("method", "")] == []


async def test_the_failure_message_carries_no_option_text_or_value():
    """A raised ``ToolError`` reaches the caller, the debug ring and Sentry, and
    a ``<select>`` is frequently a list of account numbers. Counts only."""
    tab, _ = _select_tab(
        options=(("acct-90210", "Chequing ****4417"), ("acct-90211", "Savings"))
    )

    with pytest.raises(ToolError) as caught:
        await DOMHandler.select_option(tab, SELECT, text="Nope")

    message = str(caught.value)
    assert "acct-90210" not in message
    assert "4417" not in message
    assert "Chequing" not in message


# --- the arms that work, and the record they now answer with ----------------


async def test_the_value_arm_answers_a_record_naming_what_the_control_holds():
    """RED before the fix: the answer was a bare ``True``, which cannot say
    which option is selected, how many are, or whether anything moved."""
    tab, element = _select_tab()

    result = await DOMHandler.select_option(tab, SELECT, value="three")

    assert isinstance(result, dict), result
    assert result == {
        "selector": SELECT,
        "by": "value",
        "selected_index": 2,
        "selected_count": 1,
        "option_count": 3,
        "multiple": False,
        "changed": True,
    }
    assert element.selected_index == 2


async def test_the_index_arm_answers_the_same_record():
    tab, _ = _select_tab()

    result = await DOMHandler.select_option(tab, SELECT, index=1)

    assert result["by"] == "index"
    assert result["selected_index"] == 1
    assert result["changed"] is True


async def test_selecting_the_option_already_selected_is_not_a_failure():
    """ "It was already there" and "it refused" are different facts, and only
    ``changed`` can tell them apart. A bare ``True`` said neither."""
    tab, element = _select_tab(selected_index=1)

    result = await DOMHandler.select_option(tab, SELECT, value="two")

    assert result["selected_index"] == 1
    assert result["changed"] is False
    assert element.events == [], (
        "Chrome fires nothing when the selection lands where it already was"
    )


async def test_the_text_arm_matches_the_option_text_exactly():
    tab, element = _select_tab()

    result = await DOMHandler.select_option(tab, SELECT, text="Gamma")

    assert result["by"] == "text"
    assert element.selected_index == 2


async def test_the_text_arm_keeps_the_case_insensitive_prefix_match():
    """Measured (finding §2b): the shipped arm rode Chrome's typeahead, which
    matches a case-insensitive PREFIX — ``"Bet"`` and ``"beta"`` both reach
    ``Beta``. A fix that dropped that would break a caller who relies on it, so
    the rule is kept; what is gone is the buffer, the wraparound and the ability
    to type into a different element entirely."""
    for query in ("Bet", "beta", "BETA"):
        tab, element = _select_tab()
        await DOMHandler.select_option(tab, SELECT, text=query)
        assert element.selected_index == 1, query


async def test_the_text_arm_prefers_an_exact_match_over_a_shorter_prefix():
    """``Banana`` must reach ``Banana``, not ``Banana split`` — which is what
    Chrome's typeahead did, because it searches from the option AFTER the
    current one and so skipped the exact match sitting under the cursor."""
    tab, element = _select_tab(
        options=(("p1", "Banana"), ("p2", "Banana split")), selected_index=0
    )

    result = await DOMHandler.select_option(tab, SELECT, text="Banana")

    assert element.selected_index == 0
    assert result["changed"] is False


async def test_the_text_arm_matches_an_options_label_attribute():
    """Measured: ``<option label="LabelOne">TextOne</option>`` answers
    ``.text == "TextOne"`` and ``.label == "LabelOne"``, and a browser renders
    the LABEL — so a caller reading the dropdown asks for the label. Chrome's
    typeahead resolved that collision by selecting the wrong option."""
    tab, element = _select_tab(
        options=(("l1", "TextOne", "LabelOne"), ("l2", "TextTwo", "TextTwo"))
    )

    await DOMHandler.select_option(tab, SELECT, text="LabelOne")

    assert element.selected_index == 0


async def test_the_text_arm_skips_a_disabled_option():
    """Preserved from the measurement, not invented: Chrome's typeahead refuses
    a ``disabled`` ``<option>`` (measured — ``"Delta Two"`` selected nothing),
    and a user cannot reach one either."""
    tab, _ = _select_tab(
        options=(
            ("d1", "Delta", "Delta", False),
            ("d2", "Delta Two", "Delta Two", True),
        )
    )

    with pytest.raises(ToolError):
        await DOMHandler.select_option(tab, SELECT, text="Delta Two")


async def test_the_value_arm_still_reaches_a_disabled_option():
    """The other half of the measured asymmetry, deliberately unchanged: Chrome
    permits ``select.value = "d2"`` onto a ``disabled`` option and this tool
    does not second-guess it (F-876's reasoning for the ``disabled`` click)."""
    tab, element = _select_tab(
        options=(
            ("d1", "Delta", "Delta", False),
            ("d2", "Delta Two", "Delta Two", True),
        )
    )

    await DOMHandler.select_option(tab, SELECT, value="d2")

    assert element.selected_index == 1


async def test_a_multiple_select_reports_how_many_options_it_holds():
    """The tool's signature takes ONE criterion, so a ``multiple`` select can
    never be driven past one selection (finding §6). The record says so."""
    tab, _ = _select_tab(multiple=True, selected_index=-1)

    result = await DOMHandler.select_option(tab, SELECT, value="two")

    assert result["multiple"] is True
    assert result["selected_count"] == 1


# --- the events, and the page that answers them -----------------------------


async def test_the_selection_dispatches_input_then_change():
    """Measured: Chrome's own typeahead fired ``input`` then ``change``, both
    trusted. The shipped ``value``/``index`` arms fired ``change`` alone and no
    ``input`` at all, so a page listening on ``input`` never heard the change
    the tool reported as a success."""
    tab, element = _select_tab()

    await DOMHandler.select_option(tab, SELECT, value="two")

    assert element.events == ["input", "change"]


async def test_a_page_that_resets_the_select_in_its_change_handler_raises():
    """``dispatchEvent`` is synchronous, so the page's handler has already run
    when the control is read back. A read taken before the events could not see
    this, and the shipped tool did not read at all."""

    def snap_back(element):
        element.selected_index = 0

    tab, element = _select_tab(on_selected=snap_back)

    with pytest.raises(ToolError) as caught:
        await DOMHandler.select_option(tab, SELECT, value="three")

    assert element.selected_index == 0
    assert SELECT in str(caught.value)


async def test_the_options_are_read_before_they_are_selected():
    """Two applies, in that order: the criterion cannot be resolved to an index
    without the option list, and the index cannot address an option list that
    was never read."""
    tab, element = _select_tab()

    await DOMHandler.select_option(tab, SELECT, value="two")

    assert len(element.apply_calls) == 2, element.apply_calls
    assert "dispatchEvent" not in element.apply_calls[0]
    assert "dispatchEvent" in element.apply_calls[1]


async def test_options_replaced_between_the_read_and_the_write_raise():
    """A dependent dropdown that repopulates itself makes an index resolved
    against the old list address a different option in the new one. The write
    re-checks the option's value and refuses."""

    class _Repopulating(FakeSelect):
        async def apply(self, js_function, *args, **kwargs):
            answer = await super().apply(js_function, *args, **kwargs)
            if "dispatchEvent" not in js_function:
                self.options = [
                    self._option(0, ("x1", "Xi")),
                    self._option(1, ("x2", "Omicron")),
                    self._option(2, ("x3", "Pi")),
                ]
            return answer

    tab = FakeTab(select_result=_Repopulating())

    with pytest.raises(ToolError) as caught:
        await DOMHandler.select_option(tab, SELECT, value="two")

    assert SELECT in str(caught.value)


async def test_no_criteria_still_raises():
    """The one thing the shipped tool already got right."""
    tab, _ = _select_tab()

    with pytest.raises(ToolError) as caught:
        await DOMHandler.select_option(tab, SELECT)

    assert "criteria" in str(caught.value)


async def test_a_select_read_that_cannot_be_read_raises():
    """Same discipline as ``text_entry.entered_text`` and ``click_target.aim``:
    the promised answer is a JSON string, and anything else is "could not be
    read", never an empty option list."""
    tab, _ = _select_tab(answer=None)

    with pytest.raises(ToolError) as caught:
        await DOMHandler.select_option(tab, SELECT, value="two")

    assert "could not read" in str(caught.value)


async def test_the_record_carries_no_option_text_or_value():
    tab, _ = _select_tab(
        options=(("acct-90210", "Chequing ****4417"), ("acct-90211", "Savings"))
    )

    result = await DOMHandler.select_option(tab, SELECT, index=0)

    assert "acct-90210" not in json.dumps(result)
    assert "Chequing" not in json.dumps(result)


async def test_the_option_read_is_bounded():
    """A page can carry a ``<select>`` with a hundred thousand options; the one
    read must not answer with all of them."""
    from stealth_chrome_devtools_mcp.embedded import control_state

    assert control_state.MAX_OPTIONS <= 5000
    assert str(control_state.MAX_OPTIONS) in control_state.READ_SELECT_JS


# ---------------------------------------------------------------------------
# upload_file -- the input has to actually hold the files
# ---------------------------------------------------------------------------


async def test_two_files_into_a_single_file_input_raises(tmp_path):
    """The one measured upload row. ``DOM.setFileInputFiles`` SUCCEEDS with two
    files on an input that has no ``multiple`` attribute — the raw CDP call
    answers ``None``, no error — and Chrome keeps the first file only. The tool
    reported ``count: 2``."""
    first, second = tmp_path / "a.txt", tmp_path / "b.txt"
    first.write_text("a", encoding="utf-8")
    second.write_text("bb", encoding="utf-8")
    tab, element = _upload_tab(multiple=False)

    with pytest.raises(ToolError) as caught:
        await DOMHandler.upload_file(tab, UPLOAD, [str(first), str(second)])

    assert element.files == ["a.txt"], "Chrome kept the first file"
    message = str(caught.value)
    assert "2" in message and "1" in message


async def test_an_input_that_took_nothing_raises(tmp_path):
    """The zero case of the same check — the tool could not previously report
    an empty ``FileList`` at all, because it never looked at one."""
    path = tmp_path / "a.txt"
    path.write_text("a", encoding="utf-8")
    tab, element = _upload_tab(accepts=False)

    with pytest.raises(ToolError):
        await DOMHandler.upload_file(tab, UPLOAD, [str(path)])

    assert element.files == []


async def test_the_upload_failure_message_carries_no_path_or_file_name(tmp_path):
    """An absolute path names the operating user, and a file name is often the
    document. Counts only — the same discipline as F-873's text."""
    secret = tmp_path / "Q3-severance-agreement.pdf"
    secret.write_text("x", encoding="utf-8")
    other = tmp_path / "b.txt"
    other.write_text("y", encoding="utf-8")
    tab, _ = _upload_tab(multiple=False)

    with pytest.raises(ToolError) as caught:
        await DOMHandler.upload_file(tab, UPLOAD, [str(secret), str(other)])

    message = str(caught.value)
    assert "severance" not in message
    assert str(tmp_path) not in message


async def test_a_successful_upload_answers_what_the_input_holds(tmp_path):
    """RED before the fix: the answer was ``{"uploaded": [absolute paths],
    "count": len(paths)}`` — the request, echoed back, with the operating user's
    home directory in it."""
    first, second = tmp_path / "a.txt", tmp_path / "bb.txt"
    first.write_text("a", encoding="utf-8")
    second.write_text("bb", encoding="utf-8")
    tab, _ = _upload_tab(multiple=True)

    result = await DOMHandler.upload_file(tab, UPLOAD, [str(first), str(second)])

    assert result == {
        "selector": UPLOAD,
        "requested": 2,
        "attached": 2,
        "multiple": True,
        "total_bytes": len("a.txt") + len("bb.txt"),
    }


async def test_the_upload_record_carries_no_paths(tmp_path):
    path = tmp_path / "Q3-severance-agreement.pdf"
    path.write_text("x", encoding="utf-8")
    tab, _ = _upload_tab(multiple=False)

    result = await DOMHandler.upload_file(tab, UPLOAD, [str(path)])

    assert str(tmp_path) not in json.dumps(result)
    assert "severance" not in json.dumps(result)


async def test_the_files_are_read_back_after_they_are_sent(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("a", encoding="utf-8")
    tab, element = _upload_tab()

    await DOMHandler.upload_file(tab, UPLOAD, [str(path)])

    from stealth_chrome_devtools_mcp.embedded import control_state

    assert element.sent == [(str(path),)]
    assert control_state.READ_FILES_JS in element.apply_calls


async def test_a_file_read_that_cannot_be_read_raises(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("a", encoding="utf-8")
    tab, _ = _upload_tab(answer=None)

    with pytest.raises(ToolError) as caught:
        await DOMHandler.upload_file(tab, UPLOAD, [str(path)])

    assert "could not read" in str(caught.value)


async def test_a_path_that_does_not_exist_still_raises_before_any_dispatch(tmp_path):
    """The one thing the shipped tool already got right, kept."""
    tab, element = _upload_tab()

    with pytest.raises(ToolError):
        await DOMHandler.upload_file(tab, UPLOAD, [str(tmp_path / "nope.txt")])

    assert element.sent == []
