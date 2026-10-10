"""F-962: a page tool acts on the CALLER's tab, never on another session's.

Several Claude Code sessions share one browser (the ``fleet`` session). Every
page tool used to read one slot per instance, which any session's
``switch_tab`` rewrote, so BioFlow's and uprank's reads landed on another
chat's GCP console and MinIO login tabs. These pins drive ``tab_binding``'s
decisions with two named callers and a hand-built browser; the real-wire
proof (two stdio proxies, real Chrome, two tabs at once) is
``test_e2e_two_sessions_own_tabs``.
"""

from __future__ import annotations

import asyncio
import contextvars
import importlib
import inspect
import pkgutil
from types import SimpleNamespace

import pytest

from stealth_chrome_devtools_mcp.embedded import tab_binding, tab_open
from stealth_chrome_devtools_mcp.embedded.tool_errors import (
    InstanceNotFoundError,
    ToolError,
    _require_tab,
)

IID = "inst-1"
#: Who is calling, per task — as the HTTP header is per request.
_CALLER: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "who", default=None
)


class FakeTab:
    def __init__(self, target_id: str) -> None:
        self.target = SimpleNamespace(target_id=target_id, type_="page")
        self.type_ = "page"


class FakeBrowser:
    def __init__(self, *ids: str) -> None:
        self.targets = [FakeTab(i) for i in ids]
        self.update_calls = 0

    @property
    def tabs(self) -> list[FakeTab]:
        return list(self.targets)

    async def update_targets(self) -> None:
        self.update_calls += 1

    def close(self, target_id: str) -> None:
        self.targets = [t for t in self.targets if t.target.target_id != target_id]


class FakeManager:
    """The surface ``tab_binding`` reads: the instance's tab, its browser, the
    armer, and ``navigate``."""

    def __init__(self, browser: FakeBrowser, main: str) -> None:
        self.browser = browser
        self.main = main
        self.armed: list[str] = []
        self.navigations: list[tuple[str, str | None]] = []

    async def get_tab(self, instance_id: str) -> FakeTab | None:
        if instance_id != IID:
            return None
        return tab_open.find(self.browser, self.main)

    async def get_browser(self, instance_id: str) -> FakeBrowser | None:
        return self.browser if instance_id == IID else None

    async def _arm_tracked_tab(self, instance_id: str, tab: FakeTab) -> None:
        self.armed.append(tab.target.target_id)

    async def navigate(
        self, instance_id, url, wait_until, timeout, referrer, pinned=None
    ):
        target = pinned.target.target_id if pinned is not None else None
        self.navigations.append((url, target))
        return {"url": url, "title": ""}


@pytest.fixture
def world(monkeypatch):
    """A browser with the instance's own tab ``main`` and two more, and a
    switchable 'who is calling' in place of the HTTP header."""
    tab_binding._bound.clear()
    who = {"value": None}
    monkeypatch.setattr(tab_binding, "caller", lambda: who["value"])
    browser = FakeBrowser("main", "t-a", "t-b")
    manager = FakeManager(browser, "main")
    yield SimpleNamespace(who=who, browser=browser, manager=manager)
    tab_binding._bound.clear()


def _as(world, caller: str | None) -> None:
    world.who["value"] = caller


async def _resolve(world) -> str:
    tab = await _require_tab(world.manager, IID)
    return tab.target.target_id


# --- the defect -----------------------------------------------------------


async def test_another_sessions_switch_does_not_move_my_tab(world):
    """THE field failure: A works in its tab, B switches the instance to B's
    tab, and A's next read must still land in A's tab."""
    _as(world, "A")
    tab_binding.bind(IID, "t-a")
    _as(world, "B")
    tab_binding.bind(IID, "t-b")
    world.manager.main = "t-b"  # what browser_manager.switch_to_tab does

    _as(world, "A")
    assert await _resolve(world) == "t-a"
    _as(world, "B")
    assert await _resolve(world) == "t-b"


async def test_two_sessions_resolve_concurrently_to_their_own_tabs(world, monkeypatch):
    """Calls from both sessions in flight at once never cross over."""
    for who, tab in (("A", "t-a"), ("B", "t-b")):
        _as(world, who)
        tab_binding.bind(IID, tab)

    monkeypatch.setattr(tab_binding, "caller", _CALLER.get)

    seen: dict[str, list[str]] = {"A": [], "B": []}

    async def one(who: str) -> None:
        token = _CALLER.set(who)
        try:
            for _ in range(20):
                await asyncio.sleep(0)
                tab = await tab_binding.tab_for_caller(world.manager, IID)
                seen[who].append(tab.target.target_id)
        finally:
            _CALLER.reset(token)

    await asyncio.gather(one("A"), one("B"))
    assert set(seen["A"]) == {"t-a"}
    assert set(seen["B"]) == {"t-b"}


# --- a session with no tab of its own ---------------------------------------


async def test_an_unbound_session_claims_the_free_instance_tab(world):
    _as(world, "A")
    assert await _resolve(world) == "main"
    assert tab_binding._bound[("A", IID)] == "main"


async def test_an_unbound_session_is_refused_another_sessions_tab(world):
    _as(world, "A")
    tab_binding.bind(IID, "main")
    _as(world, "B")
    with pytest.raises(ToolError, match="no tab of its own"):
        await _resolve(world)


async def test_an_in_process_call_keeps_the_instance_tab(world):
    """No caller at all (the hermetic lanes, the CLI in-process) is exactly
    the pre-F-962 behaviour, even when sessions hold tabs."""
    _as(world, "A")
    tab_binding.bind(IID, "main")
    _as(world, None)
    assert await _resolve(world) == "main"


async def test_a_missing_instance_is_still_instance_not_found(world):
    _as(world, "A")
    with pytest.raises(InstanceNotFoundError):
        await _require_tab(world.manager, "nope")


# --- tab_id ------------------------------------------------------------------


async def test_tab_id_beats_the_binding_and_does_not_rebind(world):
    _as(world, "A")
    tab_binding.bind(IID, "t-a")
    token = tab_binding._requested.set("t-b")
    try:
        assert await _resolve(world) == "t-b"
    finally:
        tab_binding._requested.reset(token)
    assert await _resolve(world) == "t-a"


async def test_an_unknown_tab_id_is_named_and_asks_chrome_once(world):
    _as(world, "A")
    token = tab_binding._requested.set("gone")
    try:
        with pytest.raises(ToolError, match="Tab gone is not open"):
            await _resolve(world)
    finally:
        tab_binding._requested.reset(token)
    assert world.browser.update_calls == 1


async def test_a_closed_bound_tab_is_reported_not_swapped_for_another(world):
    """Falling back to the instance's tab here is exactly the misdirected
    read: it may be another session's."""
    _as(world, "A")
    tab_binding.bind(IID, "t-a")
    world.browser.close("t-a")
    with pytest.raises(ToolError, match="no longer open"):
        await _resolve(world)
    assert ("A", IID) not in tab_binding._bound


def test_forget_tab_drops_every_binding_to_it(world):
    for who in ("A", "B"):
        _as(world, who)
        tab_binding.bind(IID, "t-a")
    tab_binding.forget_tab(IID, "t-a")
    assert tab_binding._bound == {}


# --- spawn_browser's claim ----------------------------------------------------


async def test_claim_gives_each_session_its_own_tab(world, monkeypatch):
    opened: list[str] = []

    async def open_tab(browser, url):
        tab = FakeTab(f"new-{len(opened)}")
        browser.targets.append(tab)
        opened.append(url)
        return tab

    monkeypatch.setattr(tab_open, "open_tab", open_tab)

    _as(world, "A")
    first = await tab_binding.claim(world.manager, IID)
    _as(world, "B")
    second = await tab_binding.claim(world.manager, IID)
    again = await tab_binding.claim(world.manager, IID)

    assert first == {"tab_id": "main"}
    assert second == {"tab_id": "new-0"}
    assert again == second, "a second spawn by the same session reuses its tab"
    assert opened == ["about:blank"]
    assert world.manager.armed == ["new-0"], "the new tab is armed like the spawn tab"


async def test_claim_never_fails_a_spawn(world, monkeypatch):
    async def broken(browser, url):
        raise RuntimeError("no tab for you")

    monkeypatch.setattr(tab_open, "open_tab", broken)
    _as(world, "A")
    tab_binding.bind(IID, "main")
    _as(world, "B")
    answer = await tab_binding.claim(world.manager, IID)
    assert answer["tab_id"] is None
    assert "no tab for you" in answer["tab_error"]


async def test_claim_in_process_reports_the_instance_tab_and_binds_nothing(world):
    _as(world, None)
    assert await tab_binding.claim(world.manager, IID) == {"tab_id": "main"}
    assert tab_binding._bound == {}


# --- navigate --------------------------------------------------------------------


async def test_navigate_pins_a_session_tab_and_leaves_the_instance_tab(world):
    _as(world, "A")
    tab_binding.bind(IID, "t-a")
    await tab_binding.navigate_callers_tab(
        world.manager,
        IID,
        url="https://a.example/",
        wait_until="load",
        timeout=1000,
        referrer=None,
    )
    assert world.manager.navigations == [("https://a.example/", "t-a")]


async def test_navigate_on_the_instance_tab_follows_its_replacement(world):
    """The instance's tab keeps its recycle/recovery; a session bound to it
    follows it to the replacement instead of losing its tab."""
    _as(world, "A")
    tab_binding.bind(IID, "main")

    async def recycling(instance_id, url, wait_until, timeout, referrer, pinned=None):
        world.browser.targets.append(FakeTab("fresh"))
        world.manager.main = "fresh"
        return {"url": url}

    world.manager.navigate = recycling
    await tab_binding.navigate_callers_tab(
        world.manager, IID, url="u", wait_until="load", timeout=1000, referrer=None
    )
    assert tab_binding._bound[("A", IID)] == "fresh"


# --- every page tool takes tab_id --------------------------------------------------


def test_scoped_adds_tab_id_and_its_doc_line_only_to_page_tools():
    async def page_tool(instance_id: str, script: str) -> dict:
        """
        Run.

        Args:
            instance_id (str): Browser instance ID.
            script (str): Code.
        """
        await _require_tab(None, instance_id)
        return {}

    async def instance_tool(instance_id: str) -> bool:
        """Close."""
        return True

    wrapped = tab_binding.scoped(page_tool)
    params = inspect.signature(wrapped).parameters
    assert list(params) == ["instance_id", "script", "tab_id"]
    assert params["tab_id"].default is None
    lines = inspect.getdoc(wrapped).splitlines()
    at = next(i for i, line in enumerate(lines) if "instance_id (str)" in line)
    assert lines[at + 1].strip().startswith("tab_id (Optional[str])")
    assert tab_binding.scoped(instance_tool) is instance_tool


async def test_scoped_hands_the_tab_id_to_the_resolver_for_that_call_only():
    seen: list[str | None] = []

    async def page_tool(instance_id: str) -> None:
        _require_tab  # noqa: B018  PERMANENT(the name that makes it a page tool, F-962)
        seen.append(tab_binding._requested.get())

    wrapped = tab_binding.scoped(page_tool)
    await asyncio.gather(
        wrapped("i", tab_id="x"), wrapped("i", tab_id="y"), wrapped("i")
    )
    assert sorted(seen, key=str) == sorted(["x", "y", None], key=str)
    assert tab_binding._requested.get() is None


def test_every_registered_page_tool_takes_tab_id():
    """Derived, so a new page tool cannot be missed: a tool that resolves a
    tab takes ``tab_id``, and no tool reads the instance's tab directly
    except the instance-level answers that report it."""
    from stealth_chrome_devtools_mcp.embedded import tool_sections

    direct = []
    for info in pkgutil.iter_modules(tool_sections.__path__):
        module = importlib.import_module(f"{tool_sections.__name__}.{info.name}")
        for tool in getattr(module, "TOOLS", ()):
            names = set(tool.__code__.co_names)
            if "get_tab" in names or "get_active_tab" in names:
                direct.append(tool.__name__)
            if names & tab_binding.RESOLVERS:
                scoped = tab_binding.scoped(tool)
                assert "tab_id" in inspect.signature(scoped).parameters, tool.__name__
    # spawn_browser reads the new instance's own tab to arm it; nothing else may.
    assert direct == ["spawn_browser"], direct


# --- over real streamable HTTP: who the caller is ------------------------------


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_over_http_each_proxy_is_its_own_caller_and_tab_id_arrives(monkeypatch):
    """No Chrome, real transport: a tool registered through the ONE registry
    sees the proxy's caller header (``backend_client.http_client`` stamps it),
    two proxies are two callers, a client without the header is keyed by its
    MCP session, and ``tab_id`` reaches the resolver for that call."""
    from collections import defaultdict

    import uvicorn
    from fastmcp import Client, FastMCP
    from fastmcp.client.transports import StreamableHttpTransport
    from sse_starlette.sse import AppStatus

    from stealth_chrome_devtools_mcp.embedded import backend_client, tool_registry
    from stealth_chrome_devtools_mcp.embedded.tool_registry import ToolRegistry

    monkeypatch.setattr(AppStatus, "should_exit", False)
    # The registry's section table is process-global: this throwaway tool must
    # not join the live surface the count tripwires read.
    monkeypatch.setattr(tool_registry, "SECTION_TOOLS", defaultdict(list))
    app = FastMCP("f962-wire")

    async def who_and_tab(instance_id: str) -> dict:
        """
        Echo.

        Args:
            instance_id (str): Browser instance ID.
        """
        _require_tab  # noqa: B018  PERMANENT(the name that makes it a page tool, F-962)
        return {"caller": tab_binding.caller(), "tab": tab_binding._requested.get()}

    ToolRegistry(app).section_tool("f962")(who_and_tab)
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            app.http_app(path="/mcp/"), host="127.0.0.1", port=port, log_level="error"
        )
    )
    serve = asyncio.create_task(server.serve())
    url = f"http://127.0.0.1:{port}/mcp/"

    def proxy(caller_id: str | None) -> Client:
        headers = {backend_client.CALLER_HEADER: caller_id} if caller_id else None
        return Client(StreamableHttpTransport(url, headers=headers))

    try:
        for _ in range(50):
            if server.started:
                break
            await asyncio.sleep(0.1)
        assert server.started
        async with proxy("proxy-a") as a, proxy("proxy-b") as b, proxy(None) as raw:
            seen_a = (await a.call_tool("who_and_tab", {"instance_id": "i"})).data
            seen_b = (
                await b.call_tool("who_and_tab", {"instance_id": "i", "tab_id": "T9"})
            ).data
            seen_raw = (await raw.call_tool("who_and_tab", {"instance_id": "i"})).data
        assert seen_a == {"caller": "proxy-a", "tab": None}
        assert seen_b == {"caller": "proxy-b", "tab": "T9"}
        assert seen_raw["caller"], "no header: keyed by the MCP session id"
        assert seen_raw["caller"] not in {"proxy-a", "proxy-b"}
    finally:
        server.should_exit = True
        await asyncio.wait_for(serve, timeout=10)
        AppStatus.should_exit = False


def test_the_proxy_transport_stamps_one_caller_id_per_process():
    from stealth_chrome_devtools_mcp.embedded import backend_client

    first = backend_client.http_client(None)
    second = backend_client.http_client(5.0)
    try:
        stamped = {
            first.headers[backend_client.CALLER_HEADER],
            second.headers[backend_client.CALLER_HEADER],
        }
        assert stamped == {backend_client.CALLER_ID}
    finally:
        import asyncio as _asyncio

        _asyncio.run(first.aclose())
        _asyncio.run(second.aclose())
