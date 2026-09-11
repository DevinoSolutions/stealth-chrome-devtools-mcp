"""Pins for F-861: a caller's JSON reaches a typed CDP parameter as the type.

Callers of ``execute_cdp_command`` send what the CDP docs show — an integer
``windowId``, a ``{"width": ..}`` bounds object, a string ``targetId``. nodriver's
generated wrappers do not take JSON: every typed parameter is a class with
``from_json``/``to_json`` (``WindowID(int)``, ``Bounds`` dataclass, ``TargetID(str)``,
enums), and the wrapper body calls ``.to_json()`` on whatever it was handed. A raw
value therefore crashed INSIDE the wrapper — ``'int' object has no attribute
'to_json'`` (Sentry STEALTH-CHROME-DEVTOOLS-MCP-4T, 74 events; -79) — and the
caller learned nothing about which parameter or what type it wanted.

``cdp_params.typed`` turns each argument into the type the wrapper's own signature
declares, from the wrapper's resolved type hints: ``from_json`` for the generated
classes (dataclasses, enums, ``int``/``str`` newtypes), recursively through
``Optional[..]`` and ``List[..]``, and leaves primitives and already-typed values
untouched. The frame Chrome receives is the assertion, exactly as F-816's pins do.
Hermetic: the wrappers are pure generators, so building the call never touches a
browser.
"""

import nodriver as uc
import pytest

from fakes import FakeTab
from stealth_chrome_devtools_mcp.embedded.cdp_function_executor import (
    CDPFunctionExecutor,
    build_cdp_call,
)
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError


def _frame(call) -> dict:
    return next(call)


def test_an_int_newtype_and_a_dataclass_arrive_typed():
    """THE Sentry shape: ``Browser.setWindowBounds`` with a plain int and a dict."""
    call = build_cdp_call(
        uc.cdp.browser.set_window_bounds,
        {
            "windowId": 7,
            "bounds": {"width": 800, "height": 600, "windowState": "normal"},
        },
    )

    assert _frame(call) == {
        "method": "Browser.setWindowBounds",
        "params": {
            "windowId": 7,
            "bounds": {"width": 800, "height": 600, "windowState": "normal"},
        },
    }


def test_a_str_newtype_inside_optional_arrives_typed():
    """``Optional[TargetID]``: the string is coerced, ``None`` stays ``None``."""
    call = build_cdp_call(uc.cdp.browser.get_window_for_target, {"targetId": "ABC"})

    assert _frame(call)["params"] == {"targetId": "ABC"}

    call = build_cdp_call(uc.cdp.browser.get_window_for_target, {"targetId": None})

    assert _frame(call)["params"] == {}


def test_a_list_of_dataclasses_arrives_typed():
    """``List[CookieParam]``: every element is coerced, not just the list."""
    call = build_cdp_call(
        uc.cdp.network.set_cookies,
        {"cookies": [{"name": "a", "value": "1"}, {"name": "b", "value": "2"}]},
    )

    assert _frame(call)["params"] == {
        "cookies": [{"name": "a", "value": "1"}, {"name": "b", "value": "2"}]
    }


def test_an_enum_inside_optional_arrives_typed():
    """``Optional[TransitionType]``: the wire string becomes the enum member."""
    call = build_cdp_call(
        uc.cdp.page.navigate, {"url": "https://x/", "transitionType": "typed"}
    )

    assert _frame(call)["params"] == {"url": "https://x/", "transitionType": "typed"}


def test_an_already_typed_value_is_passed_through():
    """A caller who did the typing already is not re-typed (and cannot be broken)."""
    bounds = uc.cdp.browser.Bounds(width=1)
    call = build_cdp_call(
        uc.cdp.browser.set_window_bounds,
        {"window_id": uc.cdp.browser.WindowID(3), "bounds": bounds},
    )

    assert _frame(call)["params"] == {"windowId": 3, "bounds": {"width": 1}}


def test_primitives_are_untouched():
    """Nothing is coerced for ``str``/``bool``/``float`` params — the F-816 frames hold."""
    call = build_cdp_call(
        uc.cdp.emulation.set_device_metrics_override,
        {"width": 390, "height": 844, "deviceScaleFactor": 3.0, "mobile": True},
    )

    assert _frame(call)["params"] == {
        "width": 390,
        "height": 844,
        "deviceScaleFactor": 3.0,
        "mobile": True,
    }


def test_a_value_the_type_cannot_take_names_the_param_and_the_type():
    """An un-coercible value is a caller mistake: an EXPECTED failure, by name.

    A ToolError (which ``before_send`` drops by type), not the wrapper's own
    AttributeError one frame deeper — and it says WHICH param wanted WHAT.
    """
    with pytest.raises(ToolError) as caught:
        build_cdp_call(
            uc.cdp.browser.set_window_bounds, {"windowId": 7, "bounds": "big"}
        )

    assert "bounds" in str(caught.value)
    assert "Bounds" in str(caught.value)


@pytest.mark.asyncio
async def test_the_executor_sends_the_typed_frame():
    """End to end through the executor: the frame on the wire is the typed one."""
    tab = FakeTab(cdp_responses={"enable": None, "set_window_bounds": None})

    result = await CDPFunctionExecutor().execute_cdp_command(
        tab, "Browser.setWindowBounds", {"windowId": 1, "bounds": {"width": 640}}
    )

    assert result["success"] is True, result
    assert tab.cdp_frames[-1] == {
        "method": "Browser.setWindowBounds",
        "params": {"windowId": 1, "bounds": {"width": 640}},
    }


def test_the_74_event_shape_a_bare_enum_string_arrives_typed():
    """Sentry -4T: ``Input.dispatchMouseEvent`` with ``button: "left"``."""
    call = build_cdp_call(
        uc.cdp.input_.dispatch_mouse_event,
        {"type": "mousePressed", "x": 10, "y": 20, "button": "left", "clickCount": 1},
    )

    assert _frame(call)["params"] == {
        "type": "mousePressed",
        "x": 10,
        "y": 20,
        "button": "left",
        "clickCount": 1,
    }


def test_the_list_of_enums_shape_arrives_typed():
    """Sentry -79: ``Browser.grantPermissions`` with ``["geolocation", ..]``."""
    call = build_cdp_call(
        uc.cdp.browser.grant_permissions,
        {"permissions": ["geolocation", "notifications"], "origin": "https://x/"},
    )

    assert _frame(call)["params"] == {
        "permissions": ["geolocation", "notifications"],
        "origin": "https://x/",
    }
