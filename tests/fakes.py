"""Canonical hermetic test harness for the M6 characterization suite.

ONE home (dedup / conventions lens) for the fake DOM/tab/browser/BrowserManager
doubles, a fake in-memory storage, the single in-process tool invoker, and the
golden-normalisation helpers. **No test logic lives here** — only reusable
mechanism. Every M6 test module imports from here; a second hand-rolled tab mock
in a test module is a defect.

Two tab-interaction seams the cloners use are both faked:

* ``FakeTab.evaluate(js)`` — the **JS-eval path** (the canonical engine's
  structure/events/animations/assets/related_files aspects). Returns a canned
  value; a substring→value map lets one tab answer several distinct ``evaluate``
  calls.
* ``FakeTab.send(cdp_obj)`` — the **CDP path** (``cdp_element_cloner`` styles).
  nodriver CDP commands are *generators*; the
  canned response is keyed by the generator's ``co_name`` (e.g. ``get_document``)
  which is stable and call-order-independent. The generator is closed so it is
  never left un-iterated.

The in-process invoker follows the repo's established FastMCP seam: a registered
tool is a ``FunctionTool`` whose original coroutine (or plain function, for the 5
sync hook-doc tools) is ``.fn``. ``call_tool`` unwraps it and awaits only when
the result is awaitable, so it drives both the 89 async and 5 sync tools
identically.
"""

from __future__ import annotations

from fakes_browser import (
    FakeBrowser,
    FakeBrowserManager,
    fake_instance,
)
from fakes_consts import (
    _UNSET,
    ABORTED_NAVIGATION,
    ABORTED_SUPERSESSION,
    CHROME_ERROR_PAGE,
    IFRAME,
    MAIN_FRAME,
    SUPERSEDED_MARK,
    TARGET_SWAPPED_ERROR,
)
from fakes_elements import (
    FakeAttachedTab,
    FakeClickTarget,
    FakeDiscoveredTarget,
    FakeFileInput,
    FakeSelect,
    FakeTextField,
    fake_element,
    fake_target,
)
from fakes_harness import (
    DEFAULT_VOLATILE_KEYS,
    FakeStorage,
    _is_path_param,
    _public_functions,
    _takes_a_path,
    as_jsonable,
    assert_no_default_paths,
    call_tool,
    live_tools,
    load_or_capture_golden,
    normalize_golden,
    pretend_display_context,
    v2_record,
)
from fakes_js import (
    ANIMATION_JS_MARKER,
    JsPromise,
    animation_evaluate_map,
    js_aspect_answer,
    js_promise,
    js_result,
    js_threw,
)
from fakes_process import (
    FakeChrome,
    LaunchedBrowser,
    ProcessTable,
    a_zombie,
    forbid_real_process_kills,
    held_profile,
    nodriver_registry,
    write_singleton,
)
from fakes_tab import (
    FakeTab,
    ScrollingTab,
    cdp_command_name,
)

#: Every name that moved to a sibling module stays importable from here (T-5431).
__all__ = [
    "ABORTED_NAVIGATION",
    "ABORTED_SUPERSESSION",
    "ANIMATION_JS_MARKER",
    "CHROME_ERROR_PAGE",
    "DEFAULT_VOLATILE_KEYS",
    "IFRAME",
    "MAIN_FRAME",
    "SUPERSEDED_MARK",
    "TARGET_SWAPPED_ERROR",
    "_UNSET",
    "FakeAttachedTab",
    "FakeBrowser",
    "FakeBrowserManager",
    "FakeChrome",
    "FakeClickTarget",
    "FakeDiscoveredTarget",
    "FakeFileInput",
    "FakeSelect",
    "FakeStorage",
    "FakeTab",
    "FakeTextField",
    "JsPromise",
    "LaunchedBrowser",
    "ProcessTable",
    "ScrollingTab",
    "_is_path_param",
    "_public_functions",
    "_takes_a_path",
    "a_zombie",
    "animation_evaluate_map",
    "as_jsonable",
    "assert_no_default_paths",
    "call_tool",
    "cdp_command_name",
    "fake_element",
    "fake_instance",
    "fake_target",
    "forbid_real_process_kills",
    "held_profile",
    "js_aspect_answer",
    "js_promise",
    "js_result",
    "js_threw",
    "live_tools",
    "load_or_capture_golden",
    "nodriver_registry",
    "normalize_golden",
    "pretend_display_context",
    "v2_record",
    "write_singleton",
]
