"""E2E: the CSS shapes only a real Chrome can settle (audit of schema v2).

``test_e2e_animations.py`` proves the happy path over the real transport. This
file is the adversarial half: every case here is one where a HERMETIC fixture
would have to encode an assumption about the CSSOM, and encoding that assumption
is how a fixture ends up agreeing with the bug. What does Chrome report for a
shorthand containing ``var()``? Is an adopted constructed stylesheet even
enumerable? What does a ``@layer`` block look like in ``at_rule_context``?

Two properties are asserted throughout, and only these two are worth an E2E:

* **The find literal is the author's bytes.** Every ``find`` is checked against
  the fixture file's own text, read off disk — never against a string this test
  also composes. That is the only check Chrome's re-serialization cannot pass by
  accident (F-849).
* **A payload never claims more than it saw.** Where the collector genuinely
  cannot reach the CSS (a constructed sheet, a shadow root), the record has to
  say so rather than degrade into silence, because silence reads as "nothing to
  do here".
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from e2e_helpers import (
    get_fn,
    integration_pytestmark,
    navigate_and_settle,
    sandbox_kwargs,
    warmup_once,
)

pytestmark = integration_pytestmark()

PAGE = Path(__file__).parent / "fixture_app" / "animations_edge.html"


@pytest.fixture(autouse=True)
async def _warm():
    await warmup_once()


@pytest.fixture(scope="module")
def author_bytes() -> str:
    """The fixture page's own text, read off disk.

    THE point of this file: a ``find`` literal is only trustworthy if it occurs
    in the bytes an editor would open. Comparing it to a string this test also
    builds compares one serializer to itself.
    """
    return PAGE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def style_element_text(author_bytes: str) -> str:
    """The text INSIDE the page's one ``<style>`` element, cut from the file.

    ``document.styleSheets`` lists exactly one sheet for this page (the adopted
    constructed sheet is not enumerable and the shadow root's ``<style>`` is not
    in the document), so this is what a recipe's ``char_offset`` is counted in.
    Cut from the bytes on disk rather than read back out of the payload: the
    point of every check in this file is that the two agree.
    """
    return author_bytes.split("<style>", 1)[1].split("</style>", 1)[0]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def payloads(fixture_app_server):
    """One extraction per target element, taken in a single browser session.

    ``loop_scope`` is stated rather than inherited. This is the suite's only
    non-function-scoped async fixture, and pytest-asyncio currently falls the
    loop scope back to the caching scope only under a deprecation warning
    (``asyncio_default_fixture_loop_scope`` is unset); once that default flips
    to function scope, the browser would be spawned on a loop that closes
    before the teardown that has to await ``close_instance`` on it. Stating it
    keeps the session's setup and teardown on one loop under either default.
    """
    spawn, close = get_fn("spawn_browser"), get_fn("close_instance")
    extract = get_fn("extract_element_animations")
    instance = await spawn(headless=True, **sandbox_kwargs())
    iid = instance["instance_id"]
    try:
        await navigate_and_settle(iid, f"{fixture_app_server}/animations_edge.html")
        out = {}
        for selector in (
            "#var-target",
            "#layer-target",
            "#important-target",
            "#none-slot",
            "#adopted-host",
            "#shadow-host",
        ):
            out[selector] = await extract(instance_id=iid, selector=selector)
        yield out
    finally:
        await close(instance_id=iid)


def animation(payload, name):
    found = [a for a in payload["animations"] if a["name"] == name]
    assert len(found) == 1, (
        f"expected one {name!r}, got {[a['name'] for a in payload['animations']]}"
    )
    return found[0]


def knobs(payload, name):
    return {edit["knob"]: edit for edit in animation(payload, name)["edits"]}


def applied(edit) -> str:
    """The recipe applied the way ``edit_protocol`` says to apply it."""
    assert edit["token"] in edit["find"], "the token must be inside the find literal"
    return edit["replace"].replace("{{NEW_VALUE}}", "NEW")


class TestEveryFindLiteralIsInTheFileOnDisk:
    """The load-bearing E2E assertion: recipes address the author's bytes.

    Chrome's ``cssText`` re-serializes (name to the end of the shorthand, ``.1``
    expanded to ``0.1``, ``running`` injected). Any recipe built from it would
    fail this check, and no hermetic fixture can make that failure visible.
    """

    async def test_no_recipe_offers_a_literal_that_is_not_in_the_page(
        self, payloads, author_bytes
    ):
        checked = 0
        for selector, payload in payloads.items():
            for record in payload["animations"]:
                for edit in record.get("edits", []):
                    if "find" not in edit:
                        continue
                    assert edit["find"] in author_bytes, (
                        f"{selector} / {edit['knob']}: find={edit['find']!r} does "
                        f"not occur in animations_edge.html; it came from a "
                        f"re-serialization, not from the author's text"
                    )
                    checked += 1
        assert checked >= 10, f"only {checked} applicable recipes — fixture too thin"

    async def test_every_offset_lands_on_its_own_find_literal(
        self, payloads, style_element_text
    ):
        """The other half of the same property, for the addressing the audit
        asked for: a ``char_offset`` is only worth having if slicing the file at
        it produces the literal. One that is off by anything is worse than none,
        because a search for the literal would have worked.
        """
        checked = 0
        for selector, payload in payloads.items():
            for record in payload["animations"]:
                for edit in record.get("edits", []):
                    if "char_offset" not in edit:
                        continue
                    start = edit["char_offset"]
                    assert (
                        style_element_text[start : start + len(edit["find"])]
                        == edit["find"]
                    ), f"{selector} / {edit['knob']}: char_offset {start} misses"
                    line = style_element_text[:start].count("\n") + 1
                    assert edit["line"] == line, edit["knob"]
                    checked += 1
        assert checked >= 10, f"only {checked} located recipes — fixture too thin"

    async def test_the_sources_say_what_the_offsets_are_counted_in(self, payloads):
        """``<style> #0`` is not an openable path; the DOCUMENT that contains it
        is, and the offsets are into the element's own text rather than the
        document's bytes. Both halves have to be said or neither is usable."""
        for payload in payloads.values():
            for source in payload["sources"]:
                if not source.get("source_text_available"):
                    continue
                opened = source["open"]
                assert opened["url"] == payload["url"]
                assert opened["offsets_in"] == "style_element_text"

    async def test_applying_a_recipe_yields_a_declaration_not_a_fragment(
        self, payloads
    ):
        """R1's corruption case: a ``replace`` that is not a whole declaration
        turns ``animation: fade 2s ease`` into ``3s``."""
        for payload in payloads.values():
            for record in payload["animations"]:
                for edit in record.get("edits", []):
                    if "find" not in edit:
                        continue
                    result = applied(edit)
                    assert ":" in result, f"{edit['knob']} replace is not a declaration"
                    assert result.split(":")[0].strip(), edit["knob"]


class TestAValueBehindACustomProperty:
    """``animation: edge-fade var(--edge-dur) ease-in``.

    Chrome resolves the duration to ``0.75s`` in computed style; the file says
    ``var(--edge-dur)``. There is no ``0.75s`` anywhere on disk, so the duration
    knob must degrade — while the knobs that ARE literal in the shorthand (the
    easing, the name) stay fully editable. Losing those too would be an
    over-degradation nearly as bad as the lie.
    """

    async def test_the_duration_degrades_rather_than_addressing_a_resolved_value(
        self, payloads, author_bytes
    ):
        edit = knobs(payloads["#var-target"], "edge-fade")["duration"]
        assert "find" not in edit
        assert edit["confidence"] == "low"
        assert "0.75s" not in author_bytes

    async def test_the_easing_beside_it_is_still_mechanically_editable(self, payloads):
        edit = knobs(payloads["#var-target"], "edge-fade")["easing"]
        assert edit["token"] == "ease-in"
        assert applied(edit) == "animation: edge-fade var(--edge-dur) NEW", (
            "the var() must survive verbatim in the replacement — rewriting it "
            "to the resolved value would delete the author's indirection"
        )

    async def test_the_duration_is_still_reported_as_a_fact(self, payloads):
        """Degrading the RECIPE must not degrade the MEASUREMENT."""
        record = animation(payloads["#var-target"], "edge-fade")
        assert record["timing"]["duration_ms"] == 750.0

    async def test_the_note_names_the_custom_property_chrome_reported(self, payloads):
        """D6, settled against the real CSSOM rather than a fixture's guess:
        Chrome keeps ``var(--edge-dur)`` in the DECLARED value even though the
        computed one resolves it, so the payload has the property's name and the
        note has no excuse for sending anyone to look for a stylesheet."""
        note = knobs(payloads["#var-target"], "edge-fade")["duration"]["note"]
        assert "--edge-dur" in note, note
        assert "cross-origin" not in note, note


class TestALayeredRuleDoesNotOutrankAnUnlayeredOne:
    """``@layer edge-base { #layer-target { … } }`` vs an unlayered
    ``.layer-plain``.

    The browser puts every layered declaration below every unlayered one, so
    ``.layer-plain`` renders even though ``#layer-target`` is far more specific.
    Ranking by specificity picks the layered rule; both declare ``4s``, so there
    is no value disagreement to catch the mistake, and the recipe would go out
    at ``confidence: high`` pointing at a rule whose edit changes nothing.
    """

    async def test_the_collector_really_captured_the_layer(self, payloads):
        """If ``at_rule_context`` were empty this whole class would pass for the
        wrong reason, so it is asserted before anything is concluded from it."""
        contexts = [
            source.get("at_rule_context")
            for source in payloads["#layer-target"]["sources"]
        ]
        assert ["@layer edge-base"] in contexts, contexts

    async def test_the_duration_recipe_refuses_to_pick_a_winner(self, payloads):
        edit = knobs(payloads["#layer-target"], "edge-fade")["duration"]
        assert "find" not in edit
        assert edit["confidence"] == "low"
        assert "@layer" in edit["note"]

    async def test_the_keyframes_are_still_editable(self, payloads):
        """The ``@keyframes`` block is unlayered and unambiguous; the layer
        problem is about which RULE sets the duration, not about the frames."""
        frames = [
            edit
            for knob, edit in knobs(payloads["#layer-target"], "edge-fade").items()
            if knob.startswith("keyframe[")
        ]
        assert frames and all("find" in edit for edit in frames)

    async def test_the_record_names_the_knobs_that_are_only_pointers(self, payloads):
        """The D7 conversion, made deliberately (F-857).

        This test used to pin the opposite: ``"editable" not in record``, with a
        note saying that if a later change made the verdict per-part, this is the
        test to revisit rather than to silently update. This is that change, so
        this is that revisit.

        The shape it pinned was the misreadable one. ``editable`` means "some
        recipe here can be applied", and the applicable recipes on THIS record
        are the keyframes — every timing knob is a pointer. Emitting no verdict
        at all left a reader that stops at the flag concluding it could retime
        this animation. ``not_editable`` now names the five knobs it cannot.
        """
        record = animation(payloads["#layer-target"], "edge-fade")
        timing = ("duration", "delay", "easing", "iterations", "name")
        assert record["editable"] is True, "the keyframe recipes here DO apply"
        assert set(record["not_editable"]) >= set(timing)
        assert record["not_editable_reason"]
        timing_knobs = knobs(payloads["#layer-target"], "edge-fade")
        assert all("find" not in timing_knobs[knob] for knob in timing)


class TestImportantSurvivesTheRoundTrip:
    """``animation-duration: 1.5s !important`` — the rule ``winning_rule`` ranks
    FIRST, so it is the rule recipes point at most often."""

    async def test_the_token_is_the_time_not_the_priority(self, payloads):
        edit = knobs(payloads["#important-target"], "edge-fade")["duration"]
        assert edit["token"] == "1.5s"

    async def test_applying_the_recipe_keeps_the_declaration_winning(
        self, payloads, author_bytes
    ):
        edit = knobs(payloads["#important-target"], "edge-fade")["duration"]
        assert edit["find"] == "animation-duration: 1.5s !important"
        assert edit["find"] in author_bytes
        assert applied(edit) == "animation-duration: NEW !important", (
            "dropping '!important' changes which rule wins the cascade — a "
            "retime that silently becomes a cascade edit"
        )


class TestANoneSlotDoesNotShiftTheListsAfterIt:
    """``animation-name: edge-fade, none, edge-slide`` with three-item duration,
    delay and iteration lists. ``edge-slide`` owns slot 2, not slot 1."""

    async def test_the_second_live_animation_keeps_its_own_timing(self, payloads):
        timing = animation(payloads["#none-slot"], "edge-slide")["timing"]
        assert timing["duration_ms"] == 3000.0
        assert timing["delay_ms"] == 5000.0
        assert timing["iterations"] == "infinite"

    async def test_its_recipe_swaps_the_third_comma_item(self, payloads, author_bytes):
        edit = knobs(payloads["#none-slot"], "edge-slide")["duration"]
        assert edit["find"] in author_bytes
        assert edit["token"] == "3s"
        assert applied(edit) == "animation-duration: 1s, 2s, NEW", (
            "swapping the second item would retime edge-fade instead"
        )

    async def test_the_first_animation_is_unaffected(self, payloads):
        edit = knobs(payloads["#none-slot"], "edge-fade")["duration"]
        assert edit["token"] == "1s"
        assert applied(edit) == "animation-duration: NEW, 2s, 3s"

    async def test_the_switched_off_slot_is_not_a_record(self, payloads):
        names = [a["name"] for a in payloads["#none-slot"]["animations"]]
        assert names == ["edge-fade", "edge-slide"]


class TestAConstructedStylesheetHasNoFileToPointAt:
    """``new CSSStyleSheet()`` + ``adoptedStyleSheets``.

    ``document.styleSheets`` does not list an adopted constructed sheet, so the
    rule and its ``@keyframes`` are invisible to the collector while the
    animation itself is plainly running. The only honest answer is the animation
    with no recipe and a stated reason; fabricating a ``<style> #N`` pointer
    would send an editor to a file that does not contain the rule.
    """

    async def test_the_animation_is_still_reported(self, payloads):
        record = animation(payloads["#adopted-host"], "edge-adopted")
        assert record["timing"]["duration_ms"] == 2500.0
        assert record["timing"]["iterations"] == "infinite"

    async def test_no_recipe_fabricates_a_source_pointer(self, payloads):
        record = animation(payloads["#adopted-host"], "edge-adopted")
        assert record["editable"] is False
        for edit in record["edits"]:
            assert "find" not in edit
            assert "file" not in edit, (
                "there is no file: these bytes exist only inside the script that "
                "called replaceSync()"
            )

    async def test_the_reason_does_not_blame_a_cross_origin_sheet(self, payloads):
        """D6's third cause, in the one place only real Chrome can settle it.

        Every stylesheet this page enumerates was read successfully — there is
        no ``cross_origin_stylesheet`` warning in the payload to support the old
        "its stylesheet is likely cross-origin", and the sheet that declares
        this animation is not enumerable at all. The remaining causes are the
        ones the reason must name.
        """
        record = animation(payloads["#adopted-host"], "edge-adopted")
        reason = record["not_editable_reason"]
        codes = [w["code"] for w in payloads["#adopted-host"]["warnings"]]
        assert "cross_origin_stylesheet" not in codes, codes
        assert "cross-origin" not in reason, reason
        assert "constructed" in reason.lower(), reason

    async def test_the_missing_keyframes_are_announced_not_merely_empty(self, payloads):
        record = animation(payloads["#adopted-host"], "edge-adopted")
        assert record["keyframes"] == []
        assert "keyframes_not_found" in [w["code"] for w in record["warnings"]], (
            "an empty keyframes array with no warning reads as 'this animation "
            "has no keyframes', which would make a model add a duplicate block"
        )


class TestAShadowRootIsNotSilentlyReportedAsStatic:
    """``getAnimations({subtree:true})`` does not cross a shadow boundary and
    ``document.styleSheets`` does not list a shadow root's ``<style>``.

    So a host whose shadow content is visibly animating produced
    ``has_motion: false`` and NOT ONE WARNING. That is the failure this schema
    ranks below saying nothing at all: a model told an element is static stops
    looking. It does not have to be captured; it has to be admitted.
    """

    async def test_the_host_really_is_invisible_to_the_collector(self, payloads):
        """Pinned so the warning below cannot quietly become redundant: if a
        future Chrome starts traversing shadow roots this fails, which is the
        signal to revisit the warning rather than to keep emitting it."""
        assert payloads["#shadow-host"]["animations"] == []

    async def test_the_payload_admits_it_could_not_look_inside(self, payloads):
        codes = [w["code"] for w in payloads["#shadow-host"]["warnings"]]
        assert "shadow_root_not_traversed" in codes, (
            f"the shadow content is animating and the payload says nothing: {codes}"
        )

    async def test_the_warning_says_motion_is_actually_running_in_there(self, payloads):
        warning = next(
            w
            for w in payloads["#shadow-host"]["warnings"]
            if w["code"] == "shadow_root_not_traversed"
        )
        assert warning["detail"]["hosts"] >= 1
        assert warning["detail"]["animating_elements"] >= 1, (
            "the collector can count animations inside an OPEN root even though "
            "getAnimations(subtree) will not return them; saying 'something is "
            "moving in here' is the difference between a caveat and a shrug"
        )

    async def test_an_element_with_no_shadow_root_gets_no_such_warning(self, payloads):
        codes = [w["code"] for w in payloads["#none-slot"]["warnings"]]
        assert "shadow_root_not_traversed" not in codes
