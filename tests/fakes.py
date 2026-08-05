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

import inspect
import json
from pathlib import Path
from types import GeneratorType, SimpleNamespace
from typing import Any

import nodriver.cdp.dom as cdp_dom
import nodriver.cdp.target as cdp_target

# ---------------------------------------------------------------------------
# Module signature guards (shared by the on-disk record modules)
# ---------------------------------------------------------------------------


def _public_functions(module: Any) -> list[tuple[str, Any]]:
    """Every function *module* defines itself, excluding privates and imports."""
    return [
        (name, obj)
        for name, obj in vars(module).items()
        if not name.startswith("_")
        and inspect.isfunction(obj)
        and obj.__module__ == module.__name__
    ]


def _is_path_param(param: Any) -> bool:
    """A parameter that selects an on-disk record, by name or by annotation.

    Name alone was enough while the only callers were the two registry modules,
    whose parameters are literally ``path``/``paths``. ``spawn_exhaustion``
    takes the same kind of argument under a domain name (``pid_file``), so the
    annotation counts too — otherwise the guard reads as vacuous there and
    fails for the wrong reason.
    """
    return param.name in ("path", "paths") or "Path" in str(param.annotation)


def _takes_a_path(func: Any) -> bool:
    return any(
        _is_path_param(param) for param in inspect.signature(func).parameters.values()
    )


def assert_no_default_paths(module: Any) -> None:
    """Assert no public function of *module* defaults a path parameter.

    Both on-disk record modules (``backend_registry``, ``browser_pid_registry``)
    state the same corollary in their docstrings, so the check belongs here
    rather than copied into each test file. The caller's binding is what selects
    the file: redirecting it at runtime is what the hermetic fixtures do, and it
    is the only thing keeping a test run from editing the developer's live
    ``~/.stealth-mcp``. A function that defaulted its path would bind its own
    module global at def-time and silently ignore that redirection.

    Three ways to offend, all caught: naming the parameter ``path``/``paths``
    and giving it a default, annotating ANY parameter as a Path and giving it a
    default (``pid_file: Path | None = None`` would escape a name-only check),
    and defaulting ANY parameter to a Path (a future
    ``record=SERVER_STATE_FILE`` would escape both).

    The companion assertion is that the sweep actually visited a path-taking
    function — a renamed module, a broken ``__module__`` filter, or an API that
    stopped taking paths would otherwise leave this passing vacuously.
    """
    functions = _public_functions(module)
    offenders = [
        f"{name}({param})"
        for name, func in functions
        for param in inspect.signature(func).parameters.values()
        if (_is_path_param(param) and param.default is not inspect.Parameter.empty)
        or isinstance(param.default, Path)
    ]
    assert offenders == [], (
        f"path parameters must stay required, but {offenders} default theirs"
    )
    visited = [name for name, func in functions if _takes_a_path(func)]
    assert visited, (
        f"vacuous guard: no public function of {module.__name__} takes a path "
        "parameter — has the module been renamed or its API changed?"
    )


# ---------------------------------------------------------------------------
# In-process tool invoker (THE one way to drive a tool in a test)
# ---------------------------------------------------------------------------


async def call_tool(server_mod: Any, name: str, /, **kwargs: Any) -> Any:
    """Invoke the registered tool ``name`` on ``server_mod`` in-process.

    Unwraps the FastMCP ``.fn`` seam (``getattr(fn, "fn", fn)`` — a no-op if the
    attribute is already the raw callable) and awaits only awaitable results, so
    the same call drives async and sync tools alike. No transport, no Chrome.
    """
    tool_obj = getattr(server_mod, name)
    fn = getattr(tool_obj, "fn", tool_obj)
    result = fn(**kwargs)
    if inspect.isawaitable(result):
        result = await result
    return result


def pretend_display_context(monkeypatch: Any, token: str) -> None:
    """State a test's display premise instead of inheriting the runner's desktop.

    ``spawn_browser`` refuses a headed spawn from a context that cannot show a
    window (F-808), so without this ANY hermetic headed-spawn test would pass on
    a developer's desktop and fail on a DISPLAY-less CI cell — the same test
    asserting two different things. Patch the one source token: everything else
    (``can_show_windows``) derives from it, so a test can never pin an impossible
    pair. Use ``display_context.HEADLESS`` / ``UNVERIFIED`` for the named cases.
    """
    from stealth_chrome_devtools_mcp.embedded import display_context

    monkeypatch.setattr(display_context, "display_context", lambda: token)


# ---------------------------------------------------------------------------
# Fake DOM tab — covers BOTH cloner seams (JS-eval + CDP)
# ---------------------------------------------------------------------------


def cdp_command_name(cdp_obj: Any) -> str:
    """Stable key for a nodriver CDP command.

    nodriver's ``uc.cdp.<domain>.<command>(...)`` returns a *generator*; its
    ``gi_code.co_name`` is the command name (``get_document``, ``enable``, …).
    Falls back to ``__name__`` / type name for any non-generator command object.
    """
    code = getattr(cdp_obj, "gi_code", None)
    if code is not None:
        return code.co_name
    return getattr(cdp_obj, "__name__", type(cdp_obj).__name__)


class FakeTab:
    """A fake nodriver tab recording ``evaluate``/``send`` and returning canned
    responses. Instantaneous returns → ``_with_cdp_timeout``'s ``wait_for`` never
    fires (hermetic + zero-flake).

    Args:
        url: value for both ``.url`` and ``.target.url`` (engines read either).
        evaluate_result: default value returned by ``evaluate`` for any JS.
        evaluate_map: optional {substring: value}; first substring found in the
            JS expression wins over ``evaluate_result``.
        cdp_responses: {command_name: value_or_callable}; a callable is invoked
            with the command name and returns the response.
    """

    def __init__(
        self,
        url: str = "https://fake.test/page",
        evaluate_result: Any = None,
        evaluate_map: dict[str, Any] | None = None,
        cdp_responses: dict[str, Any] | None = None,
        select_result: Any = None,
        target_id: str = "T-faketab",
    ) -> None:
        self.url = url
        # ``fake_target`` (defined below) — a Tab's ``.target`` is a real
        # ``TargetInfo``, so the double carries a real ``TargetID`` too.
        self.target = fake_target(target_id=target_id, url=url)
        self.awaited = 0
        self._evaluate_result = evaluate_result
        self._evaluate_map = evaluate_map or {}
        self._cdp_responses = cdp_responses or {}
        self._select_result = select_result
        self.evaluate_calls: list[str] = []
        self.send_calls: list[str] = []
        self.get_calls: list[str] = []
        self.select_calls: list[str] = []
        self.cdp_frames: list[dict[str, Any]] = []
        self.handlers: list[tuple[Any, Any]] = []

    def __await__(self) -> Any:
        """nodriver's ``Tab.__await__`` (→ ``Tab.wait()``). Only ``Tab`` defines
        it — see :class:`FakeDiscoveredTarget`, which deliberately does not."""

        async def _wait() -> None:
            self.awaited += 1

        return _wait().__await__()

    async def evaluate(self, expression: str, *args: Any, **kwargs: Any) -> Any:
        self.evaluate_calls.append(expression)
        for needle, resp in self._evaluate_map.items():
            if needle in expression:
                return resp
        return self._evaluate_result

    async def get(self, url: str, *args: Any, **kwargs: Any) -> FakeTab:
        """nodriver's ``Tab.get`` — the navigation seam.

        Updates ``.url`` the way a real navigation does, so a caller that reads
        the tab back after navigating sees where it went. Returns ``self``, as
        ``Tab.get`` returns the tab it navigated.
        """
        self.get_calls.append(url)
        self.url = url
        return self

    async def select(self, selector: str, *args: Any, **kwargs: Any) -> Any:
        """The nodriver element-resolution seam used by the CDP styles path and
        ``clone_element_complete``. Returns the configured ``select_result``
        (e.g. a ``node_id``-carrying element), or ``None`` for the not-found path.
        """
        self.select_calls.append(selector)
        return self._select_result

    def add_handler(self, event_type: Any, handler: Any) -> None:
        """The nodriver CDP-event subscription seam (``Fetch.RequestPaused``, …).

        Records the (event_type, handler) pair so a test can assert *how many*
        handlers a code path registered, not just that it sent a command.
        """
        self.handlers.append((event_type, handler))

    async def send(self, cdp_obj: Any, *args: Any, **kwargs: Any) -> Any:
        name = cdp_command_name(cdp_obj)
        self.send_calls.append(name)
        if isinstance(cdp_obj, GeneratorType):
            # Advancing once yields the request frame ({"method", "params"}), so a
            # test can assert the *arguments* of a CDP command.
            #
            # Nothing here is swallowed, deliberately. A nodriver command builds
            # its frame WHILE being advanced, so an argument it cannot serialize
            # (a raw dict where a ``to_json()``-bearing type is required) raises on
            # exactly this line — and a bare ``except Exception: pass`` around it
            # is what let Sentry STEALTH-…-1P ship green through 43 tests. Only
            # StopIteration is tolerated, and only because a command that yields no
            # frame has nothing to record.
            #
            # The ``isinstance`` gate is the whole tolerance: a non-generator
            # double (a Mock, which answers ``callable(obj.close)`` truthfully)
            # is skipped outright rather than advanced and forgiven.
            try:
                self.cdp_frames.append(next(cdp_obj))
            except StopIteration:
                pass
            cdp_obj.close()  # never leave the generator un-iterated
        resp = self._cdp_responses.get(name, None)
        return resp(name) if callable(resp) else resp


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


# ---------------------------------------------------------------------------
# Fake browser + browser manager
# ---------------------------------------------------------------------------


class FakeBrowser:
    """A fake nodriver browser for the ``list_instances`` liveness path (F-611).

    ``_browser_process_is_alive`` inspects ``_process.poll()`` first, then falls
    back to ``_process_pid`` (psutil). Model the cases:

    * ``FakeBrowser(alive=True)``  → ``_process.poll()`` returns ``None`` (alive)
    * ``FakeBrowser(alive=False)`` → ``_process.poll()`` returns ``0`` (exited)
    * ``FakeBrowser(alive=None, pid=<int>)`` → no ``_process``; psutil pid path
    * ``FakeBrowser(alive=None)`` → no ``_process``, no pid → defaults to alive

    ``tabs`` seeds the target-listing seam (``list_tabs``/``switch_to_tab``/
    ``close_tab`` read it after ``update_targets()``); seed it with
    :class:`FakeAttachedTab` / :class:`FakeDiscoveredTarget`.

    ``connection`` is the BROWSER-level websocket (nodriver ``Browser.connection``,
    ``core/browser.py:437``) — the one every by-id Target-domain command travels
    over. It is a :class:`FakeTab`, so ``connection.send_calls`` /
    ``connection.cdp_frames`` record the command name AND its arguments.

    ``get(url, new_tab=True)`` appends the tab it creates to ``tabs`` and records
    the call in ``get_calls``, so a test can assert that a code path opened NO
    extra tab (the F-775a leak).
    """

    def __init__(
        self,
        alive: bool | None = True,
        pid: int | None = None,
        tabs: list[Any] | None = None,
    ) -> None:
        if alive is None:
            self._process = None
        else:
            code = None if alive else 0
            self._process = SimpleNamespace(poll=lambda: code, returncode=code)
        self._process_pid = pid
        self.target = SimpleNamespace(url="https://fake.test/page")
        self.tabs = list(tabs or [])
        self.update_targets_calls = 0
        self.connection = FakeTab(url="ws://fake.test/devtools/browser")
        self.get_calls: list[tuple[str, bool]] = []

    async def get(self, url: str, new_tab: bool = False) -> FakeTab:
        self.get_calls.append((url, new_tab))
        tab = FakeTab(url=url, target_id=f"T-opened-{len(self.get_calls)}")
        if new_tab:
            self.tabs.append(tab)
        return tab

    async def update_targets(self) -> None:
        """nodriver's target refresh. The real one rewrites every known
        ``target``'s metadata in place from a fresh ``Target.getTargets``, which
        is precisely why a metadata-listing loop has nothing left to await."""
        self.update_targets_calls += 1


class FakeBrowserManager:
    """Seedable stand-in for the module-global ``browser_manager`` singleton.

    ``get_tab``/``get_browser``/``list_instances`` are async (the tools await
    them). ``list_instances`` returns the seeded instance objects verbatim; seed
    with :func:`fake_instance`.
    """

    def __init__(
        self,
        instances: list[Any] | None = None,
        tabs: dict[str, Any] | None = None,
        browsers: dict[str, Any] | None = None,
        spawn_instance: Any = None,
        spawn_diagnostics: dict[str, Any] | None = None,
        navigate_result: Any = None,
    ) -> None:
        self._instances = list(instances or [])
        self._tabs = dict(tabs or {})
        self._browsers = dict(browsers or {})
        self._spawn_instance = spawn_instance
        self._spawn_diagnostics = (
            spawn_diagnostics if spawn_diagnostics is not None else {}
        )
        self._navigate_result = navigate_result
        self.spawn_calls: list[Any] = []
        self.navigate_calls: list[dict[str, Any]] = []

    async def list_instances(self) -> list[Any]:
        return list(self._instances)

    async def get_tab(self, instance_id: str) -> Any:
        return self._tabs.get(instance_id)

    async def get_browser(self, instance_id: str) -> Any:
        return self._browsers.get(instance_id)

    async def spawn_browser(self, options: Any) -> Any:
        """Record the ``BrowserOptions`` the tool built (to assert param
        forwarding) and return the seeded fake instance."""
        self.spawn_calls.append(options)
        if self._spawn_instance is None:
            raise AssertionError(
                "seed spawn_instance to use FakeBrowserManager.spawn_browser"
            )
        return self._spawn_instance

    async def navigate(self, **kwargs: Any) -> Any:
        """Record the navigation the tool requested and return the seeded
        result verbatim — the seam for pinning what ``navigate`` does with a
        manager payload it cannot influence (e.g. an error-page URL, F-802)."""
        self.navigate_calls.append(kwargs)
        if self._navigate_result is None:
            raise AssertionError(
                "seed navigate_result to use FakeBrowserManager.navigate"
            )
        return self._navigate_result

    async def get_spawn_diagnostics(self, instance_id: str) -> dict[str, Any]:
        return dict(self._spawn_diagnostics)


def fake_instance(
    instance_id: str = "i1",
    state: str = "active",
    current_url: str = "https://fake.test/page",
    title: str = "Fake Page",
) -> SimpleNamespace:
    """A minimal instance object with the attributes ``list_instances`` reads."""
    return SimpleNamespace(
        instance_id=instance_id,
        state=state,
        current_url=current_url,
        title=title,
    )


# ---------------------------------------------------------------------------
# Fake in-memory storage (mirrors the real singleton's public surface)
# ---------------------------------------------------------------------------


class FakeStorage:
    """In-memory double for ``in_memory_storage``.

    Mirrors the real public API (``get``/``set``/``list_instances``/``clear_all``/
    ``remove_instance``/``get_instance``/``store_instance``) so it can stand in
    for the shared singleton without mutating real cross-test state.
    """

    def __init__(
        self,
        instances: dict[str, Any] | None = None,
        kv: dict[str, Any] | None = None,
    ) -> None:
        self._instances = dict(instances or {})
        self._kv = dict(kv or {})

    def list_instances(self) -> dict[str, Any]:
        return {"instances": dict(self._instances)}

    def get(self, key: str, default: Any = None) -> Any:
        return self._kv.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._kv[key] = value

    def clear_all(self) -> None:
        self._instances.clear()
        self._kv.clear()

    def remove_instance(self, instance_id: str) -> None:
        del self._instances[instance_id]

    def get_instance(self, instance_id: str) -> Any:
        return self._instances.get(instance_id)

    def store_instance(self, instance_id: str, data: Any) -> None:
        self._instances[instance_id] = data


# ---------------------------------------------------------------------------
# Golden normalisation (one documented home for the volatile-field policy)
# ---------------------------------------------------------------------------

# Keys whose VALUES are non-deterministic across runs/machines (wall-clock time,
# absolute paths, random ids). A golden that embedded a real one of these would
# be a flake/portability bug, so both capture and compare replace the value with
# a fixed ``<KEY>`` sentinel. The set is passed per-call because e.g. a seeded
# progressive store uses a FIXED element_id (deterministic — do not normalise).
DEFAULT_VOLATILE_KEYS: tuple[str, ...] = ("timestamp", "file_path")


def normalize_golden(
    obj: Any, volatile_keys: tuple[str, ...] = DEFAULT_VOLATILE_KEYS
) -> Any:
    """Recursively replace volatile dict values with ``<KEY>`` sentinels.

    Applied identically at capture and compare time (see
    :func:`load_or_capture_golden`). Non-dict/list scalars pass through.
    """
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for key, value in obj.items():
            if key in volatile_keys:
                out[key] = f"<{key.upper()}>"
            else:
                out[key] = normalize_golden(value, volatile_keys)
        return out
    if isinstance(obj, list):
        return [normalize_golden(item, volatile_keys) for item in obj]
    return obj


def as_jsonable(obj: Any) -> Any:
    """Round-trip through JSON so tuples/SimpleNamespace/etc. compare equal to a
    loaded golden (tuples become lists, non-serialisable objects become str)."""
    return json.loads(json.dumps(obj, sort_keys=True, default=str))


def load_or_capture_golden(path: Any, obj: Any) -> Any:
    """Load the committed golden at ``path``, or capture ``obj`` as the golden on
    first run (when the file does not yet exist).

    Characterization goldens are *defined* by the current tree, so the first
    capture is authoritative; thereafter the committed file is the reference an
    intentional M5a/M5b change updates via a reviewed diff. ``obj`` must already
    be normalised + jsonable so capture and compare are byte-consistent.
    """
    from pathlib import Path

    p = Path(path)
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(obj, indent=2, sort_keys=True), encoding="utf-8")
    return json.loads(p.read_text(encoding="utf-8"))
