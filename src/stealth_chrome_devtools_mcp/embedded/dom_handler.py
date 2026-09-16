"""DOM manipulation and element interaction utilities."""

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from nodriver import Tab, cdp

from stealth_chrome_devtools_mcp.embedded import (
    click_target,
    control_state,
    scroll_position,
    text_entry,
)
from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.embedded.element_resolution import (
    refresh_element,
    resolve_by_text,
    resolve_element,
    resolve_elements,
)
from stealth_chrome_devtools_mcp.embedded.models import ElementInfo
from stealth_chrome_devtools_mcp.embedded.tool_errors import (
    ToolError,
    _require_js_value,
)

#: Chrome's compile-time complaint when a script carries a top-level ``return``
#: (lower-cased for matching). ``execute_script`` treats it as "this script is a
#: function body, not an expression" and retries once — see F-812.
ILLEGAL_RETURN = "illegal return statement"

#: "the CDP result carried no ``value`` field at all", which is NOT the same
#: thing as a ``value`` that IS ``None`` (an explicit JS ``null``). Reading an
#: evaluate result needs both cases named, and the whole of F-832 is that they
#: were conflated with "the value was falsy" — see :func:`_json_value`.
_ABSENT = object()


def _json_value(remote_object: object) -> object:
    """Read a by-value ``Runtime.evaluate`` result as a plain JSON value (F-832).

    The test is None-vs-ABSENT, never truthiness. ``nodriver``'s ``Tab.evaluate``
    reads its result with ``if remote_object.value:`` / ``if
    remote_object.deep_serialized_value:``, so ``0``, ``""``, ``false`` and
    ``null`` — all legitimate answers — failed the test and fell through to a
    bare ``RemoteObject`` husk in their place. That is the trap this function
    exists not to fall into.

    The mapping, in branch order:

    * ``undefined`` → ``None`` (Python has one nullish, so JS's two agree here)
    * a present ``value`` → that value, **verbatim**, falsy or not
    * ``null`` (by ``subtype``) → ``None``, reached by its own named branch
    * ``unserializableValue`` (``Infinity`` / ``NaN`` / ``-0``) → its token
    * nothing serializable (a live DOM node, a cycle) → the ``description``

    A ``RemoteObject`` is never returned: it is not JSON-serializable, so the
    tool composing it into its payload must not be able to crash on it.
    """
    if remote_object is None:
        return None
    if getattr(remote_object, "type_", None) == "undefined":
        return None
    value = getattr(remote_object, "value", _ABSENT)
    if value is not _ABSENT and value is not None:
        return value
    # No value came back. WHICH of the ways that happens decides the answer —
    # "it was falsy" is not one of them, and never reaches this point.
    if getattr(remote_object, "subtype", None) == "null":
        return None
    unserializable = getattr(remote_object, "unserializable_value", None)
    if unserializable is not None:
        return str(unserializable)
    description = getattr(remote_object, "description", None)
    return None if description is None else str(description)


def _script_value(remote_object: object, exception_details: object) -> object:
    """Turn one ``Runtime.evaluate`` answer into the script's value, or raise.

    Chrome answers a thrown script with BOTH a result object (the thrown value)
    and ``exceptionDetails``, so the details are consulted FIRST — reading the
    result would report the exception as the script's value and call it a
    success, which is the F-795 defect. The record is routed into
    ``tool_errors._require_js_value`` rather than raising here: that is the ONE
    place a thrown script becomes the error convention, and it stays the one
    place now that the raw command hands the record over explicitly.
    """
    if exception_details is not None:
        _require_js_value(exception_details)
    return _json_value(remote_object)


class DOMHandler:
    """Handles DOM queries and element interactions."""

    @staticmethod
    async def query_elements(  # noqa: C901,PLR0912,PLR0915  PERMANENT(stable-but-complex per stage0/metrics)
        tab: Tab,
        selector: str,
        text_filter: str | None = None,
        visible_only: bool = True,
        limit: Any | None = None,
    ) -> list[ElementInfo]:
        """
        Query elements with advanced filtering.

        Args:
            tab (Tab): The browser tab object.
            selector (str): CSS or XPath selector for elements.
            text_filter (Optional[str]): Filter elements by text content.
            visible_only (bool): Only include visible elements.
            limit (Optional[Any]): Limit the number of results.

        Returns:
            List[ElementInfo]: List of element information objects.
        """
        processed_limit = None
        if limit is not None:
            try:
                if isinstance(limit, int):
                    processed_limit = limit
                elif isinstance(limit, str) and limit.isdigit():
                    processed_limit = int(limit)
                elif isinstance(limit, str) and limit.strip() == "":
                    processed_limit = None
                else:
                    debug_logger.log_warning(
                        "DOMHandler",
                        "query_elements",
                        f"Invalid limit parameter: {limit} (type: {type(limit)})",
                    )
                    processed_limit = None
            except (ValueError, TypeError) as e:
                debug_logger.log_error(
                    "DOMHandler",
                    "query_elements",
                    e,
                    {"limit_value": limit, "limit_type": type(limit)},
                )
                processed_limit = None

        debug_logger.log_info(
            "DOMHandler",
            "query_elements",
            f"Starting query with selector: {selector}",
            {
                "text_filter": text_filter,
                "visible_only": visible_only,
                "limit": limit,
                "processed_limit": processed_limit,
            },
        )
        try:
            # CSS or XPath is decided inside element_resolution (F-831), so both
            # inherit its stale-document/handler-race recovery. This used to
            # branch on ``selector.startswith("//")`` and call ``tab.xpath``
            # directly -- a second, unprotected way to resolve a selector that
            # only this one tool had.
            elements = await resolve_elements(tab, selector)
            debug_logger.log_info(
                "DOMHandler",
                "query_elements",
                f"Selector resolved to {len(elements)} elements",
            )

            results = []
            for idx, elem in enumerate(elements):
                try:
                    # NEVER ``elem.update()``: it is a DOM.getDocument (F-884).
                    await refresh_element(tab, elem)

                    tag_name = elem.tag_name if hasattr(elem, "tag_name") else "unknown"
                    text_content = elem.text_all if hasattr(elem, "text_all") else ""
                    attrs = elem.attrs if hasattr(elem, "attrs") else {}

                    if text_filter and text_filter.lower() not in text_content.lower():
                        continue

                    is_visible = True
                    if visible_only:
                        try:
                            is_visible = await elem.apply(
                                """(elem) => {
                                    var style = window.getComputedStyle(elem);
                                    return style.display !== 'none' &&
                                           style.visibility !== 'hidden' &&
                                           style.opacity !== '0';
                                }"""
                            )
                            if not is_visible:
                                continue
                        except (
                            AttributeError,
                            RuntimeError,
                            ConnectionError,
                            Exception,
                        ) as e:
                            debug_logger.log_info(
                                "dom_handler",
                                "query_elements",
                                "Visibility check skipped for element: "
                                f"{type(e).__name__}",
                            )

                    bbox = None
                    try:
                        position = await elem.get_position()
                        if position:
                            bbox = {
                                "x": position.x,
                                "y": position.y,
                                "width": position.width,
                                "height": position.height,
                            }
                    except (
                        AttributeError,
                        RuntimeError,
                        ConnectionError,
                        Exception,
                    ) as e:
                        debug_logger.log_info(
                            "dom_handler",
                            "query_elements",
                            f"Position unavailable for element: {type(e).__name__}",
                        )

                    is_clickable = False

                    children_count = 0
                    try:
                        if hasattr(elem, "children"):
                            children = elem.children
                            children_count = len(children) if children else 0
                    except (AttributeError, TypeError):
                        pass  # children property not iterable or element detached

                    element_info = ElementInfo(
                        selector=selector,
                        tag_name=tag_name,
                        text=text_content[:500] if text_content else None,
                        attributes=attrs or {},
                        is_visible=is_visible,
                        is_clickable=is_clickable,
                        bounding_box=bbox,
                        children_count=children_count,
                    )

                    results.append(element_info)

                    if processed_limit and len(results) >= processed_limit:
                        debug_logger.log_info(
                            "DOMHandler",
                            "query_elements",
                            f"Reached limit of {processed_limit} results",
                        )
                        break

                except Exception as elem_error:
                    debug_logger.log_error(
                        "DOMHandler",
                        "query_elements",
                        elem_error,
                        {"element_index": idx, "selector": selector},
                    )
                    continue

            debug_logger.log_info(
                "DOMHandler", "query_elements", f"Returning {len(results)} results"
            )
            return results

        except ToolError:
            # The resolution layer already raised THE canonical error for this
            # selector, names the selector itself, and logged its own diagnosis.
            # Re-wrapping would spell the selector twice in one message and bury
            # that diagnosis under a generic prefix.
            raise
        except Exception as e:
            debug_logger.log_error(
                "DOMHandler",
                "query_elements",
                e,
                {"selector": selector, "tab": str(tab)},
            )
            raise ToolError(
                f"Failed to query elements for selector {selector!r}: {e!s}"
            ) from e

    @staticmethod
    async def click_element(
        tab: Tab,
        selector: str,
        text_match: str | None = None,
        timeout: int = 10000,  # noqa: ASYNC109  plan_M7
    ) -> dict[str, Any]:
        """
        Click an element with smart retry logic.

        Where the click WENT belongs to ``click_target``; what lives here is the
        ORDER (F-876). The aim is read once, AFTER ``scroll_into_view`` and
        BEFORE the click, so the record describes the page the click was aimed
        at: reading it afterwards would describe a page the click may already
        have changed, and a ``display:none`` target has no box left to ask
        about. The synthetic fallback is kept — for such a target it is the only
        thing that reaches the element at all — and is now LABELLED rather than
        silent.

        Args:
            tab (Tab): The browser tab object.
            selector (str): CSS selector for the element.
            text_match (Optional[str]): Match element by text content.
            timeout (int): Timeout in milliseconds.

        Returns:
            Dict[str, Any]: where the click went — see ``click_target.record``.

        Raises:
            ToolError: the selector resolved to nothing, or the page could not
                be asked where a click on it would land.
        """
        try:
            element = None

            if text_match:
                element = await resolve_by_text(tab, text_match, best_match=True)
            else:
                element = await resolve_element(tab, selector, timeout=timeout / 1000)

            if not element:
                raise ToolError(f"Element not found: {selector}")

            await element.scroll_into_view()
            await asyncio.sleep(0.5)

            aim = await click_target.aim(element, selector)

            try:
                await element.mouse_click()
                dispatch = click_target.COORDINATE
            except Exception as e:
                debug_logger.log_debug("dom_handler", "click_element", str(e))
                await element.click()
                dispatch = click_target.SYNTHETIC

            return click_target.record(selector, aim, dispatch)

        except Exception as e:
            raise ToolError(f"Failed to click element: {e!s}")

    @staticmethod
    async def upload_file(
        tab: Tab,
        selector: str,
        file_paths: list[str],
        timeout: int = 10000,  # noqa: ASYNC109  plan_M7
    ) -> dict[str, Any]:
        """
        Attach local file(s) to a file input via CDP (DOM.setFileInputFiles).

        This is the correct, non-blocking way to upload files. It sets the
        files directly on the input element without touching the network or
        the renderer's main thread, so it never freezes the page (unlike
        fetch/base64/DataTransfer hacks run through execute_script).

        What the input IS and HOLDS belongs to ``control_state``; what lives
        here is the ORDER (F-877), and why it is load-bearing is stated there.

        Args:
            tab (Tab): The browser tab object.
            selector (str): CSS selector or XPath for the <input type="file">.
            file_paths (List[str]): Absolute paths of the file(s) to attach.
            timeout (int): Element lookup timeout in milliseconds.

        Returns:
            Dict[str, Any]: what the input holds — see
                ``control_state.upload_record``.

        Raises:
            ToolError: a path does not exist, the selector resolved to something
                that is not a file input, or the input holds a different number
                of files than were sent.
        """
        try:
            if not file_paths:
                raise ToolError("No file paths provided")

            resolved: list[str] = []
            for position, raw_path in enumerate(file_paths, start=1):
                path = Path(str(raw_path)).expanduser()  # noqa: ASYNC240  plan_M7
                if not path.is_file():
                    # Shape and position, never the path (F-877): an absolute
                    # path names the operating user, and this message reaches
                    # the caller, the debug ring and Sentry exactly as the
                    # leaf's do. The suffix stays — a file TYPE, not a name.
                    raise ToolError(
                        f"File not found: path {position} of {len(file_paths)} "
                        f"does not exist ({len(str(path))} characters, suffix "
                        f"{path.suffix or 'none'!r})"
                    )
                resolved.append(str(path.resolve()))

            element = await resolve_element(tab, selector, timeout=timeout / 1000)
            if not element:
                raise ToolError(f"File input not found: {selector}")
            control_state.require_file_input(element, selector)

            await element.send_file(*resolved)

            facts = await control_state.read_files(element, selector)
            control_state.verify_attached(selector, len(resolved), facts)

            return control_state.upload_record(selector, len(resolved), facts)

        except Exception as e:
            raise ToolError(f"Failed to upload file: {e!s}")

    @staticmethod
    async def type_text(  # noqa: PLR0913  PERMANENT(function interface)
        tab: Tab,
        selector: str,
        text: str,
        clear_first: bool = True,
        delay_ms: int = 50,
        parse_newlines: bool = False,
        shift_enter: bool = False,
    ) -> bool:
        """
        Type text with human-like delays and optional newline parsing.

        Every key press and the "did the page take it" check belong to
        ``text_entry``; what lives here is the ORDER (F-873). A line's
        characters are verified BEFORE that line's Enter, never after: an Enter
        that submits may navigate the page away, and a read against the
        detached element would report a failure the page had in fact accepted.
        An empty line is skipped entirely, which is what keeps the common
        ``"query\\n"`` \u2014 type, submit, done \u2014 from reading back across its own
        navigation.

        Args:
            tab (Tab): The browser tab object.
            selector (str): CSS selector for the input element.
            text (str): Text to type.
            clear_first (bool): Clear input before typing.
            delay_ms (int): Delay between keystrokes in milliseconds.
            parse_newlines (bool): If True, parse \n as Enter key presses.
            shift_enter (bool): If True, use Shift+Enter instead of Enter
                (for chat apps).

        Returns:
            bool: True \u2014 the characters were typed AND the page took them.

        Raises:
            ToolError: the selector resolved to nothing, the element could not
                be read back, or every key event was delivered and the
                element's text did not move.
        """
        try:
            element = await resolve_element(tab, selector)
            if not element:
                raise ToolError(f"Element not found: {selector}")

            await element.focus()
            await asyncio.sleep(0.1)

            if clear_first:
                try:
                    await element.apply(text_entry.CLEAR_JS)
                except Exception as e:
                    debug_logger.log_debug("dom_handler", "type_text", str(e))
                    await text_entry.clear_via_keyboard(tab)
                await asyncio.sleep(0.1)

            delay = delay_ms / 1000
            lines = text.split("\n") if parse_newlines else [text]
            for index, line in enumerate(lines):
                if line:
                    before = await text_entry.entered_text(element, selector)
                    await text_entry.type_characters(tab, element, line, delay)
                    after = await text_entry.entered_text(element, selector)
                    text_entry.verify_received(selector, line, before, after)
                if index < len(lines) - 1:
                    await text_entry.press_enter(tab, shift=shift_enter)
                    await asyncio.sleep(delay)

            return True

        except Exception as e:
            raise ToolError(f"Failed to type text: {e!s}")

    @staticmethod
    async def paste_text(
        tab: Tab, selector: str, text: str, clear_first: bool = True
    ) -> bool:
        """
        Paste text instantly using nodriver's insert_text method.
        This is much faster than typing character by character.

        The read-back that decides whether the page TOOK the text is
        ``text_entry``'s, shared with ``type_text`` (F-876): the baseline is read
        AFTER the clear — a baseline read before it would be the preset value,
        and a control that refused everything would still look like it had moved
        — and an empty ``text`` is skipped, because pasting nothing that changes
        nothing is not a refusal.

        Args:
            tab (Tab): The browser tab object.
            selector (str): CSS selector for the input element.
            text (str): Text to paste.
            clear_first (bool): Clear input before pasting.

        Returns:
            bool: True — the text was pasted AND the page took it.

        Raises:
            ToolError: the selector resolved to nothing, the element could not
                be read back, or the insert was delivered and the element's text
                did not move.
        """
        from nodriver import cdp

        try:
            element = await resolve_element(tab, selector)
            if not element:
                raise ToolError(f"Element not found: {selector}")

            await element.focus()
            await asyncio.sleep(0.1)

            if clear_first:
                try:
                    await element.apply(text_entry.CLEAR_JS)
                except Exception as e:
                    debug_logger.log_debug("dom_handler", "paste_text", str(e))
                    await text_entry.clear_via_keyboard(tab)
                await asyncio.sleep(0.1)

            if not text:
                await tab.send(cdp.input_.insert_text(text))
                return True

            before = await text_entry.entered_text(element, selector)
            await tab.send(cdp.input_.insert_text(text))
            after = await text_entry.entered_text(element, selector)
            text_entry.verify_received(selector, text, before, after)

            return True

        except Exception as e:
            raise ToolError(f"Failed to paste text: {e!s}")

    @staticmethod
    async def select_option(
        tab: Tab,
        selector: str,
        value: str | None = None,
        text: str | None = None,
        index: int | None = None,
    ) -> dict[str, Any]:
        """
        Select an option from a dropdown, and report what the control now holds.

        Which option a criterion names, and whether the control took it, belong
        to ``control_state``; what lives here is the ORDER (F-877) — read
        before any write, read back after the events — and why each half of it
        is load-bearing is stated there. Criterion precedence: text, value,
        index.

        Args:
            tab (Tab): The browser tab object.
            selector (str): CSS selector for the select element.
            value (Optional[str]): Option value to select.
            text (Optional[str]): Option text (or label) to select.
            index (Optional[int]): Option index to select.

        Returns:
            Dict[str, Any]: what the control holds — see
                ``control_state.select_record``.

        Raises:
            ToolError: the selector resolved to nothing or to a non-``<select>``,
                no option matches the criterion, the options changed underneath,
                or the control did not keep the selection.
        """
        try:
            select_element = await resolve_element(tab, selector)
            if not select_element:
                raise ToolError(f"Select element not found: {selector}")

            if text is not None:
                by = control_state.BY_TEXT
            elif value is not None:
                by = control_state.BY_VALUE
            elif index is not None:
                by = control_state.BY_INDEX
            else:
                raise ToolError(
                    "No selection criteria provided (value, text, or index)"
                )

            before = await control_state.read_select(select_element, selector)
            options = control_state.options_of(before)
            target = control_state.resolve_option(
                options, by=by, value=value, text=text, index=index
            )
            control_state.verify_matched(selector, by, before, target, index)

            after = await control_state.apply_selection(
                select_element,
                selector,
                target,
                control_state.value_at(options, target),
            )
            control_state.verify_selected(selector, target, after)

            return control_state.select_record(selector, by, before, after)

        except Exception as e:
            raise ToolError(f"Failed to select option: {e!s}")

    @staticmethod
    async def get_element_state(tab: Tab, selector: str) -> dict[str, Any]:
        """
        Get complete state of an element.

        Args:
            tab (Tab): The browser tab object.
            selector (str): CSS selector for the element.

        Returns:
            Dict[str, Any]: Dictionary of element state properties.
        """
        try:
            element = await resolve_element(tab, selector)
            if not element:
                raise ToolError(f"Element not found: {selector}")

            await refresh_element(tab, element)

            return {
                "tag_name": element.tag_name
                if hasattr(element, "tag_name")
                else "unknown",
                "text": element.text if hasattr(element, "text") else "",
                "text_all": element.text_all if hasattr(element, "text_all") else "",
                "attributes": element.attrs if hasattr(element, "attrs") else {},
                "is_visible": True,
                "is_clickable": False,
                "is_enabled": True,
                "value": element.attrs.get("value")
                if hasattr(element, "attrs")
                else None,
                "href": element.attrs.get("href")
                if hasattr(element, "attrs")
                else None,
                "src": element.attrs.get("src") if hasattr(element, "attrs") else None,
                "class": element.attrs.get("class")
                if hasattr(element, "attrs")
                else None,
                "id": element.attrs.get("id") if hasattr(element, "attrs") else None,
                "position": await element.get_position()
                if hasattr(element, "get_position")
                else None,
                "computed_style": {},
                "children_count": len(element.children)
                if hasattr(element, "children") and element.children
                else 0,
                "parent_tag": None,
            }

        except Exception as e:
            raise ToolError(f"Failed to get element state: {e!s}")

    @staticmethod
    async def wait_for_element(
        tab: Tab,
        selector: str,
        timeout: int = 30000,  # noqa: ASYNC109  plan_M7
        visible: bool = True,
        text_content: str | None = None,
    ) -> bool:
        """
        Wait for element to appear and match conditions.

        Args:
            tab (Tab): The browser tab object.
            selector (str): CSS selector for the element.
            timeout (int): Timeout in milliseconds.
            visible (bool): Wait for element to be visible.
            text_content (Optional[str]): Wait for element to contain text.

        Returns:
            bool: True if element matches conditions, False otherwise.
        """
        start_time = time.time()
        timeout_seconds = timeout / 1000

        while time.time() - start_time < timeout_seconds:
            try:
                # timeout=0: THIS loop is the wait (F-884, see that module).
                element = await resolve_element(tab, selector, timeout=0)

                if element:
                    if visible:
                        try:
                            is_visible = await element.apply(
                                """(elem) => {
                                    var style = window.getComputedStyle(elem);
                                    return style.display !== 'none' &&
                                           style.visibility !== 'hidden' &&
                                           style.opacity !== '0';
                                }"""
                            )
                            if not is_visible:
                                await asyncio.sleep(0.5)
                                continue
                        except (  # noqa: S110  plan_M10a
                            AttributeError,
                            RuntimeError,
                            ConnectionError,
                            Exception,
                        ):
                            # visibility check may fail on detached/stale
                            # elements during wait
                            pass

                    if text_content:
                        text = element.text_all
                        if text_content not in text:
                            await asyncio.sleep(0.5)
                            continue

                    return True

            except (AttributeError, RuntimeError, ConnectionError):
                pass  # element not found or detached during wait loop

            await asyncio.sleep(0.5)

        return False

    @staticmethod
    async def execute_script(
        tab: Tab, script: str, args: list[Any] | None = None
    ) -> Any:
        """
        Execute JavaScript in page context and return its plain JSON value.

        The value is what the script evaluated to — a nested object comes back
        whole, and ``0`` / ``""`` / ``false`` / ``null`` come back as themselves
        (F-832 / issue #17). See :meth:`_evaluate_by_value` for how, and
        :func:`_json_value` for the undefined/null/unserializable mapping.

        A script is evaluated as-is. If — and only if — that fails with Chrome's
        "Illegal return statement", it is re-evaluated ONCE as a function body so
        a top-level ``return`` works (F-812: the single most common way an
        agent-authored script fails). The retry is keyed on that one error rather
        than wrapping every script, because a wrapper changes what a script
        MEANS: top-level ``var``/``function`` declarations that a caller expects
        to persist on the page would become locals of the wrapper instead.

        Args:
            tab (Tab): The browser tab object.
            script (str): JavaScript code to execute.
            args (Optional[List[Any]]): Arguments for the script.

        Returns:
            Any: Result of script execution, as a plain JSON value (F-832).
        """
        if args:
            serialized_args = ",".join(json.dumps(a) for a in args)
            expression = f"(function() {{ {script} }})({serialized_args})"
        else:
            expression = script

        answer = await DOMHandler._evaluate_by_value(tab, expression)

        # Outside the send on purpose: a script that THREW is a failure of the
        # script, not of the CDP call, so it must not be re-wrapped in the
        # "Failed to execute script" (operational) message. F-795.
        try:
            return _script_value(*answer)
        except ToolError as exception:
            if ILLEGAL_RETURN not in str(exception).lower():
                raise

        return await DOMHandler._evaluate_as_function_body(tab, script)

    @staticmethod
    async def _evaluate_by_value(tab: Tab, expression: str) -> tuple[Any, Any]:
        """Evaluate *expression* asking Chrome for the value ITSELF (F-832, #17).

        A raw ``Runtime.evaluate`` with ``return_by_value=True`` rather than
        ``nodriver``'s ``Tab.evaluate``, which asks for a *deep-serialized*
        result instead: a BiDi-shaped graph of ``{"type": …, "value": …}`` nodes
        capped at depth 10. A caller who asked for an object therefore got a CDP
        envelope to unwrap — or a truncated one — instead of their JSON.

        No ``serialization_options`` is sent: CDP documents it as **overriding**
        ``returnByValue``, so passing both would quietly reinstate the envelope
        this exists to remove. ``user_gesture`` and
        ``allow_unsafe_eval_blocked_by_csp`` ARE carried over from nodriver's own
        call — dropping either would regress a page whose CSP blocks unsafe-eval,
        or a handler gated on user activation.

        Returns the ``(result, exceptionDetails)`` pair verbatim; reading it is
        :func:`_script_value`'s job.
        """
        try:
            remote_object, exception_details = await tab.send(
                cdp.runtime.evaluate(
                    expression=expression,
                    return_by_value=True,
                    user_gesture=True,
                    allow_unsafe_eval_blocked_by_csp=True,
                )
            )
        except Exception as e:
            raise ToolError(f"Failed to execute script: {e!s}")
        return remote_object, exception_details

    @staticmethod
    async def _evaluate_as_function_body(tab: Tab, script: str) -> Any:
        """Re-evaluate *script* wrapped in a function so its top-level ``return``
        is legal (F-812), reporting a failure as the script's own.

        The wrapped attempt's error is the one surfaced: the "Illegal return
        statement" that sent us here is an artifact of how the FIRST attempt
        evaluated the script, so re-reporting it would name our strategy instead
        of the caller's actual defect (a ``ReferenceError`` in the body, say).
        The wrapper is named in the message because it is visible in the stack.
        """
        answer = await DOMHandler._evaluate_by_value(tab, f"(() => {{\n{script}\n}})()")
        try:
            return _script_value(*answer)
        except ToolError as exception:
            raise ToolError(
                f"{exception} (the script was re-evaluated inside a wrapper "
                "function because it has a top-level 'return')"
            ) from None

    @staticmethod
    async def get_page_content(
        tab: Tab, include_frames: bool = False
    ) -> dict[str, str]:
        """
        Get page HTML and text content.

        Args:
            tab (Tab): The browser tab object.
            include_frames (bool): Include iframe contents.

        Returns:
            Dict[str, str]: Dictionary with page content.
        """
        try:
            html = await tab.get_content()
            text = await tab.evaluate("document.body.innerText")

            content = {
                "html": html,
                "text": text,
                "url": await tab.evaluate("window.location.href"),
                "title": await tab.evaluate("document.title"),
            }

            if include_frames:
                frames = []
                iframe_elements = await resolve_elements(tab, "iframe")

                for i, iframe in enumerate(iframe_elements):
                    try:
                        src = (
                            iframe.attrs.get("src")
                            if hasattr(iframe, "attrs")
                            else None
                        )
                        if src:
                            frames.append(
                                {
                                    "index": i,
                                    "src": src,
                                    "id": iframe.attrs.get("id")
                                    if hasattr(iframe, "attrs")
                                    else None,
                                    "name": iframe.attrs.get("name")
                                    if hasattr(iframe, "attrs")
                                    else None,
                                }
                            )
                    except Exception as e:
                        debug_logger.log_debug(
                            "dom_handler", "get_page_content", str(e)
                        )
                        continue

                content["frames"] = frames

            return content

        except Exception as e:
            raise ToolError(f"Failed to get page content: {e!s}")

    @staticmethod
    async def scroll_page(
        tab: Tab, direction: str = "down", amount: int = 500, smooth: bool = True
    ) -> dict[str, object]:
        """
        Scroll the page and report where it actually ended up (F-875, F-878).

        The answer used to be an unconditional ``True``, which reported that the
        evaluate did not throw while promising that the page scrolled — three
        different states wearing one word. It is a record now: the position
        before and after, the page's extent, the element that was driven, and
        what was asked for, so "arrived", "still moving when the budget ran out"
        and "there was nothing to scroll" are all sayable. Picking the scroller,
        reading its position and waiting for it to stop are all
        ``scroll_position``'s; this method owns the budget and the record.

        Args:
            tab (Tab): The browser tab object.
            direction (str): 'down', 'up', 'right', 'left', 'top' or 'bottom'.
            amount (int): Pixels to scroll (ignored for 'top' and 'bottom'); a
                distance, never negative.
            smooth (bool): Use smooth scrolling.

        Returns:
            Dict[str, object]: ``scrolled`` (the scroll OFFSET changed, never
            the extent), ``at_edge``, ``settled`` (the offset stopped moving
            within the budget), ``settle_seconds``, the requested
            ``direction``/``amount``/``smooth``, the six offsets, and
            ``scroller``/``scroller_is_document`` — see the tool's own
            docstring.

        Raises:
            ToolError: an invalid direction or a negative amount (both decided
            before any round trip), or an operational failure of the evaluate
            itself. A page with nothing to scroll is NOT one of these: a
            one-viewport document is a legitimate page, and it is reported.
        """
        try:
            # ONE pick per call, and it validates first: an invalid direction or
            # a negative amount must cost no round trip at all — not even this
            # one, which since F-878 is the first (F-875 guarded the before-read
            # by building the script first; the pick now stands in that place).
            on = await scroll_position.scroller(tab, direction, amount)
            scroll_js = scroll_position.script(direction, amount, smooth, on)
            before = (await scroll_position.read(tab, on)).position
            # ONE round trip: arms the end-of-scroll latch ON THE SCROLLER and
            # scrolls it.
            scrolled = await scroll_position.start(tab, scroll_js)
            # The page's own answer to "will anything move", not a guess from
            # the offsets: a scroll that moves nothing never fires `scrollend`,
            # so waiting for one would burn the whole budget. This is what keeps
            # a one-viewport page and an instant scroll on the fast path.
            settled = await scroll_position.settle(
                tab,
                before,
                awaiting_end=scrolled.moves and scrolled.supported,
                start_grace=None if scrolled.moves else 0.0,
                on=on,
            )
            after = settled.position
            return {
                # OFFSETS only, and only when both readings are about the same
                # element — ``Position.moved_from`` is the one home for that
                # comparison and its docstring is the why.
                "scrolled": after.moved_from(before),
                "at_edge": after.at_edge(direction),
                "settled": settled.settled,
                "settle_seconds": round(settled.seconds, 3),
                "direction": direction,
                "amount": amount,
                "smooth": smooth,
                "scroll_x_before": before.x,
                "scroll_y_before": before.y,
                "scroll_x_after": after.x,
                "scroll_y_after": after.y,
                "max_scroll_x": after.max_x,
                "max_scroll_y": after.max_y,
                # Both from the FINAL READ, never from the pick: a path that
                # went stale fell back to the document, and the record has to
                # name what it actually drove (F-878).
                "scroller": after.descriptor,
                "scroller_is_document": after.is_document,
            }

        except ToolError:
            # ``scroll_position.script``/``read`` already speak the error
            # convention and already name what went wrong. Re-wrapping them
            # doubled the message ("Failed to scroll page: Invalid scroll
            # direction: …") and dropped the cause.
            raise
        except Exception as e:
            raise ToolError(f"Failed to scroll page: {e!s}") from e
