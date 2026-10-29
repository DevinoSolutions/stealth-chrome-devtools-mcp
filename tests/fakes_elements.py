"""Fake element-level doubles: target listing, text field, click target, select, file input.

Split out of :mod:`fakes` (T-5431); that module re-exports every name here, so
tests keep writing ``from fakes import ...``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import nodriver.cdp.dom as cdp_dom
import nodriver.cdp.target as cdp_target
from nodriver.core.connection import ProtocolException

from fakes_consts import _UNSET

# ---------------------------------------------------------------------------
# Fake target-listing seam (nodriver ``Browser.tabs`` entries)
# ---------------------------------------------------------------------------


def fake_target(
    target_id: str = "T1",
    url: str = "https://fake.test/page",
    title: str = "Fake Page",
    type_: str = "page",
) -> SimpleNamespace:
    """A nodriver ``cdp.target.TargetInfo`` double.

    This is the metadata ``list_tabs`` reads off every entry of ``Browser.tabs``,
    and the metadata ``Browser.update_targets()`` refreshes in place.

    ``target_id`` is a real :class:`nodriver.cdp.target.TargetID` (a ``str``
    subclass), not a bare ``str``: every by-id CDP command serialises it with
    ``target_id.to_json()`` (``cdp/target.py:258``), so a bare ``str`` here would
    make the F-775 by-id pins pass against a fake that could not exist.
    """
    return SimpleNamespace(
        target_id=cdp_target.TargetID(target_id), url=url, title=title, type_=type_
    )


def fake_element(node_id: int = 1, **attrs: Any) -> SimpleNamespace:
    """A nodriver element double carrying a REAL ``cdp.dom.NodeId``.

    The exact twin of :func:`fake_target`'s reasoning, one level down. ``NodeId``
    is an ``int`` subclass whose only addition is ``to_json()``, and every by-node
    CDP command serialises with ``node_id.to_json()`` (``cdp/css.py:1814``), so a
    bare ``int`` here is an element that could not exist: production's
    ``_resolve_node_id`` returns ``element.node.node_id``, always a real
    ``NodeId``.

    ``NodeId(n) == n``, so assertions comparing against a plain int still hold.
    """
    return SimpleNamespace(node_id=cdp_dom.NodeId(node_id), **attrs)


class FakeTextField:
    """A nodriver ``Element`` double for a text control, PAGE-BACKED (F-873).

    Models the one thing the typing path depends on and nothing else: a CDP key
    event carrying ``text`` inserts that text at the caret — **only if the
    control accepts typed characters**. ``accepts=False`` is the whole of
    F-873's "reported success, typed nothing" class: a ``readonly`` input, a
    ``range``/``date``/``color`` control, or a page whose script cancels the
    key. In every one of them Chrome DELIVERS the events and the value never
    moves (measured on Chrome 152 — see the finding's §2 matrix), which is why
    a double that simply appended whatever it was sent could not express the
    defect at all.

    ``"\\r"``/``"\\n"`` are deliberately NOT inserted into a single-line
    control: a literal newline character is dropped by Chrome (measured), so
    the ONLY thing that can produce a newline or a submit is a real Enter key
    press, which is what the Enter pins assert against ``FakeTab.cdp_frames``.

    ``content_editable=True`` is the OTHER control shape the typing path has to
    hold: such an element has no ``.value`` at all (measured — it is
    ``undefined``), it carries its text in ``textContent``, and it takes an
    Enter as a newline where a single-line ``<input>`` drops it. The double
    reports itself as ``editable`` in the read-back so the surrounding contract
    is pinnable here; whether the JS picks the right property is the page's to
    evaluate and its witness is a real Chrome
    (``tests/test_e2e_hard_dom.py::test_contenteditable_and_multiselect``).

    The read-back answer is COMPUTED from this object's own state, never
    supplied by the test, so no fixture here can quietly encode the bug.
    """

    def __init__(
        self,
        value: str = "",
        accepts: bool = True,
        content_editable: bool = False,
        multiline: bool = False,
    ) -> None:
        self.value = value
        self.accepts = accepts
        self.content_editable = content_editable
        self.multiline = multiline
        self.focused = False
        self.apply_calls: list[str] = []

    async def focus(self) -> None:
        self.focused = True

    async def apply(self, js_function: str, *args: Any, **kwargs: Any) -> Any:
        """``Element.apply`` — ``Runtime.callFunctionOn(returnByValue=True)``.

        Answers the three functions the typing path sends: the focus call, the
        programmatic clear, and the read-back (whose answer is a JSON STRING,
        which is what ``return_by_value`` really hands back).
        """
        self.apply_calls.append(js_function)
        if "focus()" in js_function:
            self.focused = True
            return None
        if "elem.value = ''" in js_function:
            self.value = ""
            return None
        if "JSON.stringify" in js_function:
            return json.dumps({"editable": self.content_editable, "text": self.value})
        return None

    def receive(self, params: dict[str, Any]) -> None:
        """Insert one key event's ``text``, the way the renderer would."""
        text = params.get("text")
        if not text or params.get("type") == "keyUp":
            return
        self.insert(text)

    def insert(self, text: str) -> None:
        """Commit *text* at the caret — the ONE place this double changes value.

        Both entry seams land here: a key event carrying ``text``
        (``Input.dispatchKeyEvent``) and ``paste_text``'s single
        ``Input.insertText`` (F-876). One home, because the whole point of the
        double is that a control which refuses one refuses the other, which is
        what the F-876 matrix measured on Chrome 152.
        """
        if not text or not self.accepts:
            return
        if text in ("\r", "\n") and not (self.multiline or self.content_editable):
            return
        self.value += "\n" if text == "\r" else text


class FakeClickTarget:
    """A nodriver ``Element`` double for a CLICK target, PAGE-BACKED (F-876).

    ``FakeTextField``'s sibling, one interaction over. It models the two things
    the click path depends on and nothing else:

    * **where the click goes** — ``Element.mouse_click`` clicks
      ``Position(quads[0]).center``, i.e. the centre of the element's FIRST box.
      This double is given that box (``rect``) and computes the point from it,
      so no test can state a point the geometry does not produce. An element
      with ``rendered=False`` has no box at all (``display:none``): nodriver's
      ``get_position`` raises there and the product falls back to the synthetic
      ``Element.click``, which is what ``mouse_click_error`` expresses.
    * **what is under that point** — ``document.elementFromPoint``. Measured on
      Chrome 152 (F-876 §2c): it names an overlay for a covered target, ``BODY``
      for a ``pointer-events:none`` / zero-size / ``visibility:hidden`` target,
      and the target itself for a ``disabled`` one (whose click Chrome still
      suppresses — which is why ``disabled`` is a separate fact and not a
      hit-test outcome).

    ``disabled`` here is what ``:disabled`` MATCHES, not the ``elem.disabled``
    IDL attribute: a ``<button>`` inside a ``<fieldset disabled>`` reports
    ``elem.disabled === false``, matches ``:disabled``, hit-tests to itself and
    receives nothing (measured). The double carries the one that decides the
    answer, so a test can express that shape without a real fieldset.

    The aim answer is a JSON **string** COMPUTED from this object's own state —
    never supplied by a test — for the same reason ``FakeTextField``'s read-back
    is: a fixture that hands over the answer can quietly encode the bug.

    ``text`` exists only so a pin can assert it never reaches the record: an
    overlay is frequently a consent banner or a modal and its words are the
    page's, not the tool's to echo.

    ``detached`` is the element having LEFT the document (T-10034): every call
    that addresses it starts with ``DOM.resolveNode``, and Chrome answers that
    with the error :func:`left_the_document` builds, so ``apply`` and ``click``
    raise it. ``detach_on`` names the dispatch kind whose DELIVERY removes it —
    a page that re-renders on hover or on mousedown — so a pin states when the
    page acted and the error follows from it, never a raise supplied by hand.
    """

    def __init__(  # noqa: PLR0913  PERMANENT(one field per measured DOM fact)
        self,
        tag: str = "button",
        element_id: str = "target",
        classes: tuple[str, ...] = (),
        rect: tuple[float, float, float, float] = (10.0, 20.0, 40.0, 20.0),
        hit: tuple[str, str, tuple[str, ...]] | None = None,
        disabled: bool = False,
        pointer_events: str = "auto",
        visibility: str = "visible",
        rendered: bool = True,
        text: str = "SECRET-BUTTON-LABEL",
        mouse_click_error: Exception | None = None,
        aim_answer: Any = _UNSET,
        viewport: tuple[float, float] = (1280.0, 720.0),
        input_scale: float = 1.0,
        frame_offset: tuple[float, float] = (0.0, 0.0),
        navigates_on_press: bool = False,
        swallows_press: bool = False,
        hit_disabled: bool = False,
        fail_on: dict[str, tuple[BaseException, bool]] | None = None,
        read_error: BaseException | None = None,
        detached: bool = False,
        detach_on: str | None = None,
    ) -> None:
        self.detached = detached
        self.detach_on = detach_on
        self.viewport = viewport
        self.input_scale = input_scale
        self.frame_offset = frame_offset
        self.navigates_on_press = navigates_on_press
        self.swallows_press = swallows_press
        self.pressed = False
        # ``held`` is the one fact a stuck button is: the page was pressed and
        # never released. ``fail_on`` maps a dispatch kind to (error, delivered):
        # the command raises ONCE, and ``delivered`` says whether the page got
        # the event before the reply was lost (a 5 s timeout does not prove it
        # did not).
        self.held = False
        self.hit_disabled = hit_disabled
        self.fail_on = dict(fail_on or {})
        self.read_error = read_error
        self.armed: list[str] | None = None
        self.observed: list[dict[str, Any]] = []
        self.landings: list[tuple[str, float, float, bool]] = []
        self.tag = tag
        self.element_id = element_id
        self.classes = tuple(classes)
        self.rect = rect
        self.hit = hit
        self.disabled = disabled
        self.pointer_events = pointer_events
        self.visibility = visibility
        self.rendered = rendered
        self.text = text
        self.mouse_click_error = mouse_click_error
        self._aim_answer = aim_answer
        self.calls: list[str] = []
        self.apply_calls: list[str] = []

    # -- the shape vocabulary, the only thing said about any element ---------
    def _self_shape(self) -> dict[str, Any]:
        return {"tag": self.tag, "id": self.element_id, "classes": list(self.classes)}

    def _in_viewport(self, x: float, y: float) -> bool:
        """``document.elementFromPoint`` answers ``null`` outside the viewport.

        Measured (F-876 §2c): an ``absolute; left:-500px`` button keeps a real
        33.5 x 21 box at a NEGATIVE point, ``scroll_into_view`` does not bring it
        back, and the hit-test there is ``null`` — a different fact from "another
        element was on top", which is why the double derives it from the geometry
        rather than letting a test assert it directly.
        """
        width, height = self.viewport
        return 0 <= x <= width and 0 <= y <= height

    def _aim(self) -> str:
        left, top, width, height = self.rect if self.rendered else (0.0, 0.0, 0.0, 0.0)
        point = (
            {"x": left + width / 2, "y": top + height / 2} if self.rendered else None
        )
        visible_point = point is not None and self._in_viewport(point["x"], point["y"])
        if not visible_point:
            hit: dict[str, Any] | None = None
        elif self.hit is None:
            hit = self._self_shape()
        else:
            tag, element_id, classes = self.hit
            hit = {"tag": tag, "id": element_id, "classes": list(classes)}
        return json.dumps(
            {
                "rendered": self.rendered,
                "rect": {
                    "left": left,
                    "top": top,
                    "width": width,
                    "height": height,
                },
                "point": point,
                "target": self._self_shape(),
                "hit": hit,
                "hit_is_target": visible_point and self.hit is None,
                "disabled": self.disabled,
                "under_disabled": self.hit_disabled,
                "pointer_events": self.pointer_events,
                "visibility": self.visibility,
            }
        )

    # -- the nodriver Element surface the click path touches -----------------
    async def scroll_into_view(self) -> None:
        self.calls.append("scroll_into_view")

    async def mouse_click(self) -> None:
        """nodriver 0.50's ``Element.mouse_click``: ``get_position``, then a press
        AND a release at the box centre, in CSS pixels, straight into
        ``Input.dispatchMouseEvent`` — with no idea what scale the page's
        emulation applies to it — then ``Tab.flash_point``.

        The product no longer calls it (T-10034: it sends that press and release
        itself, so it can tell whether input went out); it stays modelled so a
        pin can show what calling it did.
        """
        x, y = (await self.get_position()).center
        self.receive_mouse({"type": "mousePressed", "x": x, "y": y})
        self.receive_mouse({"type": "mouseReleased", "x": x, "y": y})

    def _quad_centre(self) -> tuple[float, float]:
        """``DOM.getContentQuads``' first-box centre: TOP-LEVEL viewport px, so
        inside a frame it is the frame-relative box shifted by the frame's origin
        (``getClientRects`` stays frame-relative)."""
        left, top, width, height = self.rect
        return (
            left + width / 2 + self.frame_offset[0],
            top + height / 2 + self.frame_offset[1],
        )

    async def get_position(self) -> Any:
        """``DOM.getContentQuads``: nodriver raises for a node with no box."""
        from types import SimpleNamespace

        if self.detached:
            raise left_the_document()
        if not self.rendered:
            raise self.mouse_click_error or RuntimeError("could not find position")
        return SimpleNamespace(center=self._quad_centre())

    def receive_mouse(self, params: dict[str, Any]) -> None:
        """Route one dispatch, then raise the failure armed for its kind (once)."""
        kind = params["type"]
        failure = self.fail_on.pop(kind, None)
        if failure is not None and not failure[1]:
            raise failure[0]
        self._deliver(params)
        if failure is not None:
            raise failure[0]

    def _deliver(self, params: dict[str, Any]) -> None:
        """One ``Input.dispatchMouseEvent`` as the PAGE receives it.

        ``input_scale`` models an ``Emulation.setDeviceMetricsOverride`` carrying
        a ``scale`` (F-1017, measured on Chrome 155): the input is in widget
        coordinates, so the page sees ``x / scale`` while the geometry the aim
        reads stays in CSS pixels. Whether the event lands INSIDE this element is
        derived from its box and from what is on top of it, never stated.
        """
        kind = params["type"]
        # The page reports frame-relative coordinates, after the emulation scale.
        top_x = params["x"] / self.input_scale
        top_y = params["y"] / self.input_scale
        x = top_x - self.frame_offset[0]
        y = top_y - self.frame_offset[1]
        left, top, width, height = self.rect
        over_box = left <= x <= left + width and top <= y <= top + height
        inside = self.rendered and over_box and self.hit is None
        self.landings.append((kind, x, y, inside))
        if kind == "mousePressed":
            self.calls.append("mouse_click")
            self.pressed = True
            self.held = True
        if kind == "mouseReleased":
            self.held = False
        if kind == self.detach_on:
            self.detached = True
        wanted = {"mouseMoved": "mousemove", "mousePressed": "mousedown"}.get(kind)
        if wanted == "mousedown" and self.swallows_press:
            return
        if self.armed and wanted in self.armed and self._in_viewport(top_x, top_y):
            self.observed.append(
                {
                    "type": wanted,
                    "x": x,
                    "y": y,
                    "inside": inside,
                    "tag": self.tag if inside else "body",
                    "id": self.element_id if inside else "",
                    "classes": list(self.classes) if inside else [],
                }
            )

    async def click(self) -> None:
        if self.detached:
            raise left_the_document()
        self.calls.append("click")

    async def apply(self, js_function: str, *args: Any, **kwargs: Any) -> Any:
        self.apply_calls.append(js_function)
        if self.detached:
            raise left_the_document()
        if "elementFromPoint" in js_function:
            self.calls.append("aim")
            if self._aim_answer is not _UNSET:
                return self._aim_answer
            return self._aim()
        if PROBE_KEY in js_function and "addEventListener" in js_function:
            self.armed = _probe_kinds(js_function)
            self.observed = []
            return True
        if PROBE_KEY in js_function:
            if self.read_error is not None and self.pressed:
                raise self.read_error
            if self.navigates_on_press and self.pressed:
                # The page navigated on mousedown: the document the read would
                # have asked is gone, which is how Chrome answers it.
                raise RuntimeError("Cannot find context with specified id")
            seen, self.observed, self.armed = self.observed, [], None
            return json.dumps(seen)
        return None


PROBE_KEY = "stealth.click.probe"


def left_the_document(
    message: str = "No node with given id found",
) -> ProtocolException:
    """Chrome's answer to ``DOM.resolveNode`` for an element no longer in the page.

    The default is STEALTH-…-ER's wording (the node was removed); the other one
    Chrome uses, "Node with given id does not belong to the document", is
    STEALTH-…-EP's (detached but still referenced). Built from nodriver's own
    exception constructor, the way a real reply reaches the product.
    """
    return ProtocolException({"message": message, "code": -32000})


def _probe_kinds(js_function: str) -> list[str]:
    """The event types the product armed its probe for, read off the script."""
    match = re.search(r"const kinds = (\[.*?\]);", js_function)
    return json.loads(match.group(1)) if match else []


class FakeSelect:
    """A nodriver ``Element`` double for a ``<select>``, PAGE-BACKED (F-877).

    ``FakeTextField``'s and ``FakeClickTarget``'s sibling, one control over. It
    models the DOM semantics the selection path depends on and nothing else:

    * an ``<option>`` has a ``value``, a ``text`` and a ``label`` and they can
      all differ (measured, Chrome 152: ``<option label="LabelOne">TextOne``
      answers ``.text == "TextOne"``, ``.label == "LabelOne"``);
    * ``.text`` is the **collapsed** text, not ``textContent`` (measured: an
      option written over three lines as ``\\n  Spaced   Out\\n`` answers
      ``.text == "Spaced Out"``), so this double takes the collapsed form
      directly and a test that wants the raw one is asking the wrong layer —
      the witness for the collapsing itself is a real Chrome
      (``tests/test_e2e_select_upload_verification.py``);
    * assigning ``selectedIndex`` fires **nothing** on its own. The ``input``
      and ``change`` pair, in that order and bubbling, is what Chrome's own
      typeahead produced for a real selection (measured), so this double
      records exactly what the code under test dispatched and a pin can assert
      the pair rather than trusting it;
    * the selection is a **SET**, and the spec's ``selectedIndex`` SETTER
      selects exactly the option it names and deselects every other. That is
      why the state here is a tuple and not a single int: a ``<select
      multiple>`` holding ``[0, 2]`` still answers ``selectedIndex == 0`` after
      index 0 is assigned, so a double that modelled the selection as one
      number could not express the write that silently drops option 2 while
      that number does not budge.

    The answer to both reads is COMPUTED from this object's own state, never
    supplied by a test, so no fixture here can quietly encode the bug.

    ``read_cap`` is the product's ``MAX_OPTIONS`` truncation, stated by the test
    rather than imported: the read answers at most that many options while
    ``option_count`` stays the control's true length, exactly as the real script
    does. ``None`` by default, so no pin that does not ask for it moves.

    ``on_selected`` is the page: a callable invoked right after the events are
    dispatched, with this double as its argument. It exists so a pin can model
    the page that resets the control inside its own ``change`` handler — which
    a read-back taken *before* the events could not see.
    """

    #: One ``<option>`` as ``(value, text)``, ``(value, text, label)`` or
    #: ``(value, text, label, disabled)``. ``label`` defaults to ``text``,
    #: which is what ``HTMLOptionElement.label`` does when the attribute is
    #: absent (measured).
    DEFAULT_OPTIONS = (("one", "Alpha"), ("two", "Beta"), ("three", "Gamma"))

    def __init__(  # noqa: PLR0913  PERMANENT(one field per modelled DOM fact)
        self,
        options: tuple[tuple[str, ...], ...] = DEFAULT_OPTIONS,
        selected_index: int = 0,
        multiple: bool = False,
        tag: str = "select",
        on_selected: Any = None,
        answer: Any = _UNSET,
        selected: tuple[int, ...] | None = None,
        read_cap: int | None = None,
    ) -> None:
        self.tag = tag
        self.multiple = multiple
        self.read_cap = read_cap
        self.options = [self._option(i, raw) for i, raw in enumerate(options)]
        #: ``selectedOptions``' indices, ascending. ``selected=`` states the set
        #: directly — the only way to express a real multi-selection — while
        #: ``selected_index=`` is the one-option shorthand every other pin uses.
        self.selected: list[int] = (
            sorted(selected)
            if selected is not None
            else ([] if selected_index < 0 else [selected_index])
        )
        self.on_selected = on_selected
        self._answer = answer
        self.events: list[str] = []
        self.apply_calls: list[str] = []
        self.keys_sent: list[str] = []

    async def send_keys(self, text: str) -> None:
        """nodriver's ``Element.send_keys`` — what the shipped ``text=`` arm
        called, and the reason a request for a ``<select disabled>`` moved a
        DIFFERENT select. It records and does nothing else: the double's job
        here is to make "nothing ever typed" assertable, not to re-implement
        Chrome's typeahead."""
        self.keys_sent.append(text)

    @staticmethod
    def _option(index: int, raw: tuple[str, ...]) -> dict[str, Any]:
        value, text = raw[0], raw[1]
        label = raw[2] if len(raw) > 2 else text
        disabled = bool(raw[3]) if len(raw) > 3 else False
        return {
            "index": index,
            "value": value,
            "text": text,
            "label": label,
            "disabled": disabled,
        }

    @property
    def selected_indexes(self) -> list[int]:
        """``selectedOptions``' indices."""
        return list(self.selected)

    @property
    def selected_index(self) -> int:
        """``HTMLSelectElement.selectedIndex`` — the FIRST selected option, or
        ``-1`` when none is, which is the state ``select.value = "nope"`` leaves
        behind (measured)."""
        return self.selected[0] if self.selected else -1

    @selected_index.setter
    def selected_index(self, index: int) -> None:
        """The spec's setter: selects exactly that option, deselects every other.

        On a multiple select holding ``[0, 2]``, assigning ``0`` leaves the
        GETTER answering ``0`` while the set shrinks to ``[0]`` — which is why
        "did anything move" can only be asked of the set.
        """
        self.selected = [] if index < 0 else [index]

    def _state(self) -> dict[str, Any]:
        return {
            "selected_index": self.selected_index,
            "selected_count": len(self.selected_indexes),
            "selected_indexes": list(self.selected_indexes),
            "option_count": len(self.options),
            "multiple": self.multiple,
        }

    def _read(self) -> str:
        if self.tag != "select":
            return json.dumps(
                {
                    "tag": self.tag,
                    "is_select": False,
                    "multiple": False,
                    "option_count": 0,
                    "selected_index": -1,
                    "selected_count": 0,
                    "selected_indexes": [],
                    "options": [],
                }
            )
        readable = (
            self.options if self.read_cap is None else self.options[: self.read_cap]
        )
        return json.dumps(
            dict(
                self._state(),
                tag=self.tag,
                is_select=True,
                options=[dict(o) for o in readable],
            )
        )

    def _apply(self, js_function: str) -> str:
        """Set ``selectedIndex``, then fire ``input`` and ``change``.

        The wanted ``{index, value}`` is read out of the script the product
        sent, because ``Element.apply`` carries no arguments — a criterion
        reaches the page embedded in the function source or not at all. The
        ``value`` is re-checked against the option that is there NOW, exactly as
        the product's own script does: the options can be replaced between the
        read and the write (a dependent dropdown repopulating), and an index
        resolved against the old list addresses a different option in the new
        one.
        """
        match = re.search(r"const want = (\{.*?\});", js_function, re.DOTALL)
        want = json.loads(match.group(1)) if match else {}
        index = want.get("index", -1)
        option = self.options[index] if 0 <= index < len(self.options) else None
        if option is None or option["value"] != want.get("value"):
            return json.dumps(dict(self._state(), applied=False, stale=True))
        before = self.selected_indexes
        self.selected_index = index
        if before != self.selected_indexes:
            # Chrome fires nothing when the selection lands where it already
            # was — measured: a typeahead query that resolves to the currently
            # selected option produces no ``change`` at all. The comparison is
            # of the SET, never of ``selectedIndex``: see that setter.
            self.events.extend(("input", "change"))
            if self.on_selected is not None:
                self.on_selected(self)
        return json.dumps(dict(self._state(), applied=True, stale=False))

    async def apply(self, js_function: str, *args: Any, **kwargs: Any) -> Any:
        self.apply_calls.append(js_function)
        if self._answer is not _UNSET:
            return self._answer
        if "dispatchEvent" in js_function:
            return self._apply(js_function)
        return self._read()


class FakeFileInput:
    """A nodriver ``Element`` double for an ``<input type="file">`` (F-877).

    One measured rule, and it is the whole of the upload defect: when the input
    has no ``multiple`` attribute and more than one path is sent,
    ``DOM.setFileInputFiles`` **succeeds** — the raw CDP call answers ``None``,
    no error anywhere — and Chrome keeps only the **first** file. Measured on
    Chrome 152 against a freshly loaded page: two paths in, ``files.length ==
    1``. A double that simply stored whatever it was handed could not express
    that, which is exactly the answer ``upload_file`` reported.

    ``size`` is ``len(name)`` — a stand-in, because a hermetic double has no
    file to stat. It exists so a pin can prove ``total_bytes`` is read from the
    ``FileList`` rather than composed from the request.
    """

    def __init__(
        self,
        multiple: bool = False,
        files: tuple[str, ...] = (),
        tag: str = "input",
        accepts: bool = True,
        answer: Any = _UNSET,
    ) -> None:
        self.multiple = multiple
        self.files = list(files)
        self.tag = tag
        self.accepts = accepts
        self.attrs = {"type": "file", "id": "upload"}
        self.tag_name = tag
        self._answer = answer
        self.sent: list[tuple[str, ...]] = []
        self.apply_calls: list[str] = []

    async def send_file(self, *paths: str) -> None:
        """``DOM.setFileInputFiles``, with Chrome's measured truncation rule."""
        self.sent.append(tuple(paths))
        if not self.accepts:
            return
        names = [Path(str(p)).name for p in paths]
        self.files = names if self.multiple else names[:1]

    async def apply(self, js_function: str, *args: Any, **kwargs: Any) -> Any:
        self.apply_calls.append(js_function)
        if self._answer is not _UNSET:
            return self._answer
        return json.dumps(
            {
                "tag": self.tag,
                "has_files": self.tag == "input",
                "count": len(self.files),
                "multiple": self.multiple,
                "total_bytes": sum(len(n) for n in self.files),
            }
        )


class FakeDiscoveredTarget:
    """nodriver's raw ``Connection``, exactly as ``Browser.tabs`` yields it after
    a rediscovery (the F-771 shape).

    ``Browser.update_targets()`` appends a ``Connection`` — **not** a ``Tab`` —
    for every target it did not already know about, and ``Browser.tabs`` returns
    it anyway (it filters on ``type_ == "page"`` despite the ``List[Tab]``
    annotation). Two behaviours matter and both are modelled here:

    * **not awaitable** — only ``Tab`` defines ``__await__``, so awaiting this
      raises ``TypeError: object ... can't be used in 'await' expression``;
    * **attribute fall-through** — ``Connection.__getattr__`` delegates to
      ``self.target``, so ``.url``/``.title`` still resolve to REAL values. A
      listing that returns blank urls is therefore a product defect, not an
      unavoidable consequence of the object type.
    """

    def __init__(self, target: Any) -> None:
        self.target = target

    def __getattr__(self, item: str) -> Any:
        # ``self.__dict__`` (not ``self.target``) — attribute access inside a
        # ``__getattr__`` would recurse for anything not yet set.
        return getattr(self.__dict__["target"], item)


class FakeAttachedTab(FakeDiscoveredTarget):
    """nodriver's ``Tab``: a ``Connection`` that additionally defines
    ``__await__`` (which resolves to ``Tab.wait()``).

    Counts awaits in ``awaited`` so a test can pin that a metadata-only read
    pays no per-tab lifecycle wait.
    """

    def __init__(self, target: Any) -> None:
        super().__init__(target)
        self.awaited = 0

    def __await__(self) -> Any:
        async def _wait() -> None:
            self.awaited += 1

        return _wait().__await__()
