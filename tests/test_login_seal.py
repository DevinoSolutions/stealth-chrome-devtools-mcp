"""Sealed tabs and the refusals that keep every tool out of them (T-19366).

A vault login runs in a tab only the backend may touch. This file pins the one
fact ``login_seal`` owns (which ``(instance, target)`` pairs are sealed and which
instances have a seal in progress) and the three places it is enforced: the
``section_tool`` wrapper every tool passes through, ``list_tabs``, and the
``browser://`` resources.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from fakes import FakeBrowserManager, call_tool
from stealth_chrome_devtools_mcp.embedded import login_seal, tool_registry
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

INSTANCE = "inst-1"
OTHER_INSTANCE = "inst-2"
SEALED = "T-SEALED"
ELSEWHERE = "T-ELSEWHERE"


@pytest.fixture(autouse=True)
def _no_seal_left_behind():
    yield
    for instance in (INSTANCE, OTHER_INSTANCE):
        while login_seal.sealing(instance):
            login_seal.end(instance)
    login_seal._unclosed.clear()


@pytest.fixture
def sealed():
    login_seal.begin(INSTANCE)
    login_seal.seal_tab(INSTANCE, SEALED)


# ------------------------------------------------------------ the unit


def test_a_tab_that_would_not_close_stays_sealed_after_the_seal_ends():
    login_seal.begin(INSTANCE)
    login_seal.seal_tab(INSTANCE, SEALED)
    login_seal.end(INSTANCE, unclosed=SEALED)
    assert login_seal.sealing(INSTANCE) is False
    assert login_seal.is_sealed(INSTANCE, SEALED) is True
    assert login_seal.any_sealing() is True
    with pytest.raises(ToolError):
        login_seal.refuse_if_sealed(
            "execute_script", {"instance_id": INSTANCE, "tab_id": SEALED}
        )
    # no seal is in progress, so the instance-wide tools work again
    login_seal.refuse_if_sealed("execute_cdp_command", {"instance_id": INSTANCE})
    login_seal.refuse_if_sealed(
        "execute_script", {"instance_id": INSTANCE, "tab_id": ELSEWHERE}
    )
    login_seal.refuse_if_sealed(
        "execute_script", {"instance_id": OTHER_INSTANCE, "tab_id": SEALED}
    )


def test_a_tab_that_closed_is_not_kept():
    login_seal.begin(INSTANCE)
    login_seal.seal_tab(INSTANCE, SEALED)
    login_seal.end(INSTANCE)
    assert login_seal.is_sealed(INSTANCE, SEALED) is False
    assert login_seal.any_sealing() is False


def test_nothing_is_sealed_by_default():
    assert login_seal.sealing(INSTANCE) is False
    assert login_seal.is_sealed(INSTANCE, SEALED) is False
    assert login_seal.any_sealing() is False


def test_begin_marks_the_instance_as_sealing_before_any_tab_is_sealed():
    login_seal.begin(INSTANCE)
    assert login_seal.sealing(INSTANCE) is True
    assert login_seal.is_sealed(INSTANCE, SEALED) is False
    assert login_seal.any_sealing() is True


def test_seal_tab_seals_that_target_only(sealed):
    assert login_seal.is_sealed(INSTANCE, SEALED) is True
    assert login_seal.is_sealed(INSTANCE, ELSEWHERE) is False


def test_a_seal_belongs_to_its_instance(sealed):
    assert login_seal.sealing(OTHER_INSTANCE) is False
    assert login_seal.is_sealed(OTHER_INSTANCE, SEALED) is False


def test_no_target_is_never_sealed(sealed):
    assert login_seal.is_sealed(INSTANCE, None) is False
    assert login_seal.is_sealed(INSTANCE, "") is False


def test_end_drops_the_instance_and_its_tabs(sealed):
    login_seal.end(INSTANCE)
    assert login_seal.sealing(INSTANCE) is False
    assert login_seal.is_sealed(INSTANCE, SEALED) is False
    assert login_seal.any_sealing() is False


def test_end_without_begin_is_a_no_op():
    login_seal.end(INSTANCE)
    assert login_seal.sealing(INSTANCE) is False


def test_seal_tab_needs_a_login_in_progress():
    with pytest.raises(RuntimeError):
        login_seal.seal_tab(INSTANCE, SEALED)
    assert login_seal.sealing(INSTANCE) is False


def test_nested_logins_hold_the_seal_until_the_last_one_ends():
    """A provider entry runs the flow for its ``via`` entry inside its own."""
    login_seal.begin(INSTANCE)
    login_seal.seal_tab(INSTANCE, SEALED)
    login_seal.begin(INSTANCE)
    login_seal.end(INSTANCE)
    assert login_seal.sealing(INSTANCE) is True
    assert login_seal.is_sealed(INSTANCE, SEALED) is True
    login_seal.end(INSTANCE)
    assert login_seal.sealing(INSTANCE) is False


def test_the_instance_wide_set_is_the_two_that_can_reach_any_tab():
    assert frozenset({"execute_cdp_command", "switch_tab"}) == login_seal.INSTANCE_WIDE


def test_refuse_if_sealed_raises_a_tool_error_for_a_sealed_tab(sealed):
    with pytest.raises(ToolError) as caught:
        login_seal.refuse_if_sealed(
            "click_element", {"instance_id": INSTANCE, "tab_id": SEALED}
        )
    assert str(caught.value) == (
        "a vault login is running in this browser; retry when it finishes"
    )


def test_refuse_if_sealed_lets_another_tab_through(sealed):
    login_seal.refuse_if_sealed(
        "click_element", {"instance_id": INSTANCE, "tab_id": ELSEWHERE}
    )
    login_seal.refuse_if_sealed("click_element", {"instance_id": INSTANCE})


@pytest.mark.parametrize("tool", sorted(login_seal.INSTANCE_WIDE))
@pytest.mark.parametrize("tab_id", [None, ELSEWHERE, SEALED])
def test_refuse_if_sealed_refuses_an_instance_wide_tool_on_any_tab(
    sealed, tool, tab_id
):
    with pytest.raises(ToolError):
        login_seal.refuse_if_sealed(tool, {"instance_id": INSTANCE, "tab_id": tab_id})


def test_an_instance_wide_tool_is_refused_from_begin_not_from_the_first_tab():
    login_seal.begin(INSTANCE)
    with pytest.raises(ToolError):
        login_seal.refuse_if_sealed("switch_tab", {"instance_id": INSTANCE})


def test_refuse_if_sealed_ignores_other_instances(sealed):
    login_seal.refuse_if_sealed(
        "execute_cdp_command", {"instance_id": OTHER_INSTANCE, "tab_id": SEALED}
    )


@pytest.mark.parametrize("kwargs", [{}, {"instance_id": None}, {"instance_id": 7}])
def test_refuse_if_sealed_ignores_a_call_with_no_usable_instance(sealed, kwargs):
    login_seal.refuse_if_sealed("execute_cdp_command", kwargs)


def test_the_refusal_names_no_instance_and_no_tab(sealed):
    with pytest.raises(ToolError) as caught:
        login_seal.refuse_if_sealed(
            "click_element", {"instance_id": INSTANCE, "tab_id": SEALED}
        )
    assert INSTANCE not in str(caught.value)
    assert SEALED not in str(caught.value)


# ------------------------------------------------------------ the registry


def _tab_taking_tool_of_each_section():
    """One tool per section that takes ``tab_id``, from the live registry: a new
    section or tool is covered without anyone adding it to a list."""
    from stealth_chrome_devtools_mcp.embedded import server

    chosen = []
    for section, names in tool_registry.SECTION_TOOLS.items():
        for name in names:
            fn = getattr(server, name).fn
            if "tab_id" in inspect.signature(fn).parameters:
                chosen.append((section, name))
                break
    return chosen


def _dummy_arguments(name, **given):
    """Every required parameter filled, so the call reaches the guard."""
    from stealth_chrome_devtools_mcp.embedded import server

    parameters = inspect.signature(getattr(server, name).fn).parameters
    arguments = dict(given)
    for parameter in parameters.values():
        if parameter.name in arguments:
            continue
        if parameter.default is inspect.Parameter.empty and parameter.kind in (
            parameter.POSITIONAL_OR_KEYWORD,
            parameter.KEYWORD_ONLY,
        ):
            arguments[parameter.name] = "x"
    return arguments


async def _outcome(server_mod, name, **arguments):
    """The exception a call raised, or ``None``. A body may fail for its own
    reasons (there is no real browser); the question here is only whether the
    seal refused it."""
    try:
        await call_tool(server_mod, name, **arguments)
    except Exception as error:  # noqa: BLE001  PERMANENT(the test asks which error, any)
        return error
    return None


def _was_refused(error):
    return isinstance(error, ToolError) and str(error) == login_seal.REFUSAL


@pytest.fixture
def server_mod(patched_server):
    return patched_server(browser_manager=FakeBrowserManager())


def test_every_section_with_page_tools_is_represented():
    sections = {section for section, _ in _tab_taking_tool_of_each_section()}
    assert {
        "browser-management",
        "element-interaction",
        "network-debugging",
        "cookies-storage",
        "tabs",
        "element-extraction",
        "progressive-cloning",
        "file-extraction",
        "cdp-functions",
    } <= sections


async def test_a_tab_tool_of_every_section_is_refused_on_the_sealed_tab(
    server_mod, sealed
):
    for section, name in _tab_taking_tool_of_each_section():
        arguments = _dummy_arguments(name, instance_id=INSTANCE, tab_id=SEALED)
        error = await _outcome(server_mod, name, **arguments)
        assert _was_refused(error), f"{section}/{name} was not refused"


async def test_the_same_tools_are_not_refused_on_another_tab(server_mod, sealed):
    for section, name in _tab_taking_tool_of_each_section():
        if name in login_seal.INSTANCE_WIDE:
            continue
        arguments = _dummy_arguments(name, instance_id=INSTANCE, tab_id=ELSEWHERE)
        error = await _outcome(server_mod, name, **arguments)
        assert not _was_refused(error), f"{section}/{name} was refused"


async def test_the_same_tools_are_not_refused_in_another_instance(server_mod, sealed):
    for section, name in _tab_taking_tool_of_each_section():
        arguments = _dummy_arguments(name, instance_id=OTHER_INSTANCE, tab_id=SEALED)
        error = await _outcome(server_mod, name, **arguments)
        assert not _was_refused(error), f"{section}/{name} was refused"


@pytest.mark.parametrize("tab_id", [None, ELSEWHERE, SEALED])
async def test_execute_cdp_command_and_switch_tab_are_refused_on_any_tab(
    server_mod, sealed, tab_id
):
    for name in sorted(login_seal.INSTANCE_WIDE):
        given = {"instance_id": INSTANCE}
        if tab_id is not None:
            given["tab_id"] = tab_id
        arguments = _dummy_arguments(name, **given)
        if tab_id is None:
            arguments.pop("tab_id", None)
        error = await _outcome(server_mod, name, **arguments)
        assert _was_refused(error), f"{name} on {tab_id!r} was not refused"


async def test_a_positional_call_is_refused_too(server_mod, sealed):
    fn = server_mod.close_tab.fn
    with pytest.raises(ToolError) as caught:
        await fn(INSTANCE, SEALED)
    assert str(caught.value) == login_seal.REFUSAL


async def test_a_call_with_the_instance_only_is_not_refused(server_mod, sealed):
    error = await _outcome(server_mod, "list_instances")
    assert not _was_refused(error)


async def test_everything_works_again_after_end(server_mod, sealed):
    login_seal.end(INSTANCE)
    for section, name in _tab_taking_tool_of_each_section():
        arguments = _dummy_arguments(name, instance_id=INSTANCE, tab_id=SEALED)
        error = await _outcome(server_mod, name, **arguments)
        assert not _was_refused(error), f"{section}/{name} still refused"


async def test_a_refused_call_never_runs_the_body(server_mod, sealed, monkeypatch):
    calls = []

    class Recording(FakeBrowserManager):
        async def get_browser(self, instance_id):
            calls.append(instance_id)
            return await super().get_browser(instance_id)

    from stealth_chrome_devtools_mcp.embedded import tool_runtime

    monkeypatch.setattr(tool_runtime, "browser_manager", Recording())
    await _outcome(server_mod, "close_tab", instance_id=INSTANCE, tab_id=SEALED)
    assert calls == []


def test_a_registered_tool_keeps_its_signature_and_schema(server_mod):
    """The guard is plumbing: ``tab_id`` still appears once, and nothing else
    about a tool's schema changed."""
    fn = server_mod.click_element.fn
    names = list(inspect.signature(fn).parameters)
    assert names.count("tab_id") == 1
    assert names[0] == "instance_id"


# ------------------------------------------------------------ list_tabs


async def test_list_tabs_omits_the_sealed_tab(patched_server, sealed):
    listing = [
        {
            "tab_id": ELSEWHERE,
            "url": "https://a.example/",
            "title": "a",
            "type": "page",
        },
        {
            "tab_id": SEALED,
            "url": "https://login.example/",
            "title": "l",
            "type": "page",
        },
    ]

    async def list_tabs(instance_id):
        return list(listing)

    server_mod = patched_server(browser_manager=SimpleNamespace(list_tabs=list_tabs))
    tabs = await call_tool(server_mod, "list_tabs", instance_id=INSTANCE)
    assert [tab["tab_id"] for tab in tabs] == [ELSEWHERE]


async def test_list_tabs_lists_everything_when_nothing_is_sealed(patched_server):
    listing = [
        {"tab_id": ELSEWHERE, "url": "u", "title": "a", "type": "page"},
        {"tab_id": SEALED, "url": "u", "title": "l", "type": "page"},
    ]

    async def list_tabs(instance_id):
        return list(listing)

    server_mod = patched_server(browser_manager=SimpleNamespace(list_tabs=list_tabs))
    tabs = await call_tool(server_mod, "list_tabs", instance_id=INSTANCE)
    assert [tab["tab_id"] for tab in tabs] == [ELSEWHERE, SEALED]


async def test_list_tabs_in_another_instance_is_not_filtered(patched_server, sealed):
    listing = [{"tab_id": SEALED, "url": "u", "title": "l", "type": "page"}]

    async def list_tabs(instance_id):
        return list(listing)

    server_mod = patched_server(browser_manager=SimpleNamespace(list_tabs=list_tabs))
    tabs = await call_tool(server_mod, "list_tabs", instance_id=OTHER_INSTANCE)
    assert [tab["tab_id"] for tab in tabs] == [SEALED]


# ------------------------------------------------------------ browser:// resources


class _ManagerWithoutPageState(FakeBrowserManager):
    """The fake has no page state to read; ``None`` is the real manager's answer
    for an instance with no tab, which a resource turns into instance-not-found."""

    async def get_page_state(self, instance_id):
        return None


def _stored_tab(target_id):
    return SimpleNamespace(target=SimpleNamespace(target_id=target_id))


@pytest.mark.parametrize(
    "resource",
    ["get_browser_state_resource", "get_cookies_resource", "get_console_resource"],
)
async def test_a_resource_over_the_sealed_tab_is_refused(
    patched_server, sealed, resource
):
    manager = _ManagerWithoutPageState(tabs={INSTANCE: _stored_tab(SEALED)})
    server_mod = patched_server(browser_manager=manager)
    with pytest.raises(ToolError) as caught:
        await getattr(server_mod, resource)(INSTANCE)
    assert str(caught.value) == login_seal.REFUSAL


@pytest.mark.parametrize(
    "resource",
    ["get_browser_state_resource", "get_cookies_resource", "get_console_resource"],
)
async def test_a_resource_over_another_tab_is_not_refused(
    patched_server, sealed, resource
):
    manager = _ManagerWithoutPageState(tabs={INSTANCE: _stored_tab(ELSEWHERE)})
    server_mod = patched_server(browser_manager=manager)
    try:
        await getattr(server_mod, resource)(INSTANCE)
    except Exception as error:  # noqa: BLE001  PERMANENT(the fake has no page state; only the seal matters)
        assert not _was_refused(error)
