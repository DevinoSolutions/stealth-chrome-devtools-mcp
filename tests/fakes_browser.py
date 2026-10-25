"""The fake browser, browser manager and instance factory.

Split out of :mod:`fakes` (T-5431); that module re-exports every name here, so
tests keep writing ``from fakes import ...``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from fakes_tab import FakeTab

# ---------------------------------------------------------------------------
# Fake browser + browser manager
# ---------------------------------------------------------------------------


class FakeBrowser(FakeTab):
    """A fake nodriver browser for the ``list_instances`` liveness path (F-611).

    ``browser_is_alive`` inspects ``_process.poll()`` first, then falls
    back to ``_process_pid`` (psutil). Model the cases:

    * ``FakeBrowser(alive=True)``  → ``_process.poll()`` returns ``None`` (alive)
    * ``FakeBrowser(alive=False)`` → ``_process.poll()`` returns ``0`` (exited)
    * ``FakeBrowser(alive=None, pid=<int>)`` → no ``_process``; psutil pid path
    * ``FakeBrowser(alive=None)`` → no ``_process``, no pid → defaults to alive

    ``tabs`` seeds the target-listing seam (``list_tabs``/``switch_to_tab``/
    ``close_tab`` read it after ``update_targets()``); seed it with
    :class:`FakeAttachedTab` / :class:`FakeDiscoveredTarget`.

    The browser IS its own BROWSER-level connection, as in nodriver 0.50
    (``Browser(Connection)``, ``core/browser.py``:32; ``Browser.connection`` is
    always ``None`` there) — the one every by-id Target-domain command travels
    over. So it is a :class:`FakeTab`: ``send_calls`` / ``cdp_frames`` record the
    command name AND its arguments, ``add_handler`` / ``_deliver`` model the
    event listeners, and ``aclose`` is the verb that closes OUR websocket.

    ``Target.createTarget`` over that connection opens a tab (F-940): the URL is
    recorded in ``opened``, so a test can assert that a code path opened NO extra
    tab (the F-775a leak). The tab joins ``targets`` when :meth:`update_targets`
    runs, which is where nodriver 0.50 registers every target (``Target.getTargets``,
    ``browser.py``:498); ``lists_new_targets=False`` is a Chrome that answers
    ``createTarget`` and then does not list the target (it closed again at once).
    ``unlisted`` holds targets Chrome has that nodriver has not listed yet (a
    popup), which the next ``update_targets`` registers. ``create_target_error``
    is a ``createTarget`` that Chrome refuses.

    ``main_tab`` is what an ATTACH hands back (F-888) — nodriver's
    ``Browser.main_tab``, which the adoption path reads to decide whether a
    re-attached browser is usable at all.
    """

    def __init__(
        self,
        alive: bool | None = True,
        pid: int | None = None,
        tabs: list[Any] | None = None,
        opened_tab: Any = None,
        update_targets_stalls: bool = False,
        main_tab: Any = None,
        lists_new_targets: bool = True,
        create_target_error: BaseException | None = None,
    ) -> None:
        if alive is None:
            self._process = None
        else:
            code = None if alive else 0
            self._process = SimpleNamespace(poll=lambda: code, returncode=code)
        self._process_pid = pid
        self.tabs = list(tabs or [])
        self.update_targets_calls = 0
        # nodriver's ``Browser.config`` — the host/port its tab URLs are built on.
        self.config = SimpleNamespace(host="127.0.0.1", port=9222)
        # nodriver 0.50 gives every ``Tab`` its parent's ``websocket_url``.
        self.websocket_url = "ws://127.0.0.1:9222/devtools/browser/fake"
        super().__init__(
            url="ws://fake.test/devtools/browser",
            cdp_responses={
                "create_target": self._create_target,
            },
        )
        # The browser's OWN target is the ``browser`` one, which is what makes
        # ``Tab.browser`` (``tab.py``:163) answer its parent.
        self.target = SimpleNamespace(url="https://fake.test/page", type_="browser")
        # nodriver's ``Connection.socket``: ``None`` until opened, a close code
        # once closed (``connection.py``:231, the test ``aopen`` itself makes).
        self.socket = SimpleNamespace(close_code=None)
        self.opened: list[str] = []
        self._created: dict[str, Any] = {}
        self._opened_tab = opened_tab
        self._lists_new_targets = lists_new_targets
        self.unlisted: list[Any] = []
        self.stopped = False
        self._create_target_error = create_target_error
        self._update_targets_stalls = update_targets_stalls
        # nodriver's ``Browser.main_tab`` — the tab an ATTACH hands back (F-888).
        # ``None`` unless a test seeds it, because a browser we connected to and
        # that reports no tab is a real case the adoption path must refuse rather
        # than register half an instance for.
        self.main_tab = main_tab
        # A browser that reports a main tab has opened that page (F-1003:
        # ``browser_connect.first_page`` waits on a ``page`` target).
        if main_tab is not None and main_tab not in self.tabs:
            self.tabs.append(main_tab)

    @property
    def targets(self) -> list[Any]:
        """nodriver's ``Browser.targets``. The fake holds pages only, so it is
        the very list ``tabs`` is — a tab registered in one is in the other."""
        return self.tabs

    def _create_target(self, _name: str) -> Any:
        """``Target.createTarget``.

        Seed ``opened_tab`` to say what the opened tab answers (e.g. a landing
        on a ``chrome-error://`` page); otherwise a plain tab at the requested
        URL is made. Either way the tab is stamped with its opener, so closing
        it removes it from ``tabs`` — the difference between "the failure path
        closed the tab" and "the failure path leaked it".
        """
        if self._create_target_error is not None:
            raise self._create_target_error
        url = self.cdp_frames[-1]["params"]["url"]
        self.opened.append(url)
        tab = self._opened_tab or FakeTab(
            url=url, target_id=f"T-opened-{len(self.opened)}"
        )
        tab.opened_by = self
        self._created[tab.target.target_id] = tab
        if self._lists_new_targets:
            self.unlisted.append(tab)
        return tab.target.target_id

    def stop(self) -> None:
        """nodriver's ``Browser.stop`` (terminates the process)."""
        self.stopped = True

    async def update_targets(self) -> None:
        """nodriver's target refresh. The real one rewrites every known
        ``target``'s metadata in place from a fresh ``Target.getTargets``, which
        is precisely why a metadata-listing loop has nothing left to await.

        Seed ``update_targets_stalls=True`` for a WEDGED browser: the refresh is
        a real CDP round trip, so a browser whose devtools websocket has stopped
        answering never completes it. That is the one case a listing over N
        instances must survive (F-874), and a fake that returned promptly could
        not express it.
        """
        self.update_targets_calls += 1
        if self._update_targets_stalls:
            await asyncio.Event().wait()
        # ``Target.getTargets``: every target Chrome has that is not listed yet.
        listed, self.unlisted = self.unlisted, []
        self.tabs.extend(tab for tab in listed if tab not in self.tabs)


class FakeBrowserManager:
    """Seedable stand-in for the module-global ``browser_manager`` singleton.

    ``get_tab``/``get_browser``/``list_instances`` are async (the tools await
    them). ``list_instances`` returns the seeded instance objects verbatim; seed
    with :func:`fake_instance`.

    ``get_instance`` answers the ENTRY — the dict the real manager keeps in
    ``_instances``, whose ``"options"`` is the only place a live instance's
    ``user_data_dir`` exists (F-898). ``BrowserInstance`` has no such field, so a
    double that offered one on the instance would let a reader pass here and
    find nothing in production; seed it with ``profiles=``.
    """

    def __init__(
        self,
        instances: list[Any] | None = None,
        tabs: dict[str, Any] | None = None,
        browsers: dict[str, Any] | None = None,
        spawn_instance: Any = None,
        spawn_diagnostics: dict[str, Any] | None = None,
        navigate_result: Any = None,
        profiles: dict[str, str] | None = None,
    ) -> None:
        self._instances = list(instances or [])
        self._tabs = dict(tabs or {})
        self._browsers = dict(browsers or {})
        self._profiles = dict(profiles or {})
        self._spawn_instance = spawn_instance
        self._spawn_diagnostics = (
            spawn_diagnostics if spawn_diagnostics is not None else {}
        )
        self._navigate_result = navigate_result
        self.spawn_calls: list[Any] = []
        self.navigate_calls: list[dict[str, Any]] = []
        self.armed_tabs: list[tuple[str, Any]] = []

    async def list_instances(self) -> list[Any]:
        return list(self._instances)

    async def get_tab(self, instance_id: str) -> Any:
        return self._tabs.get(instance_id)

    async def get_active_tab(self, instance_id: str) -> Any:
        """The real manager's ``get_active_tab`` is ``get_tab`` under another
        name (``browser_manager.py``: "the instance's stored active tab"), so the
        double delegates rather than growing a second seedable store that could
        disagree with ``tabs=``."""
        return self._tabs.get(instance_id)

    async def get_browser(self, instance_id: str) -> Any:
        return self._browsers.get(instance_id)

    async def _arm_tracked_tab(self, instance_id: str, tab: Any) -> None:
        """The real manager arms a tab it starts driving (F-935), and a tab
        ``new_tab`` opens is one (F-962). Recorded; there is nothing to arm."""
        self.armed_tabs.append((instance_id, tab))

    async def get_instance(self, instance_id: str) -> dict[str, Any] | None:
        """The instance ENTRY, shaped like the real manager's (F-898).

        Only the keys a reader of the entry actually needs are offered, and
        ``"options"`` is present only for an instance seeded with a profile —
        an under-specified double is worse than none, and a reader reaching for
        a key the product does not put there should fail here rather than in
        production (``test_extra_headers_cdp``'s rule, stated on its own
        ``clone_storage`` double).
        """
        known = {instance.instance_id for instance in self._instances}
        if instance_id not in known:
            return None
        entry: dict[str, Any] = {
            "browser": self._browsers.get(instance_id),
            "tab": self._tabs.get(instance_id),
        }
        directory = self._profiles.get(instance_id)
        if directory is not None:
            entry["options"] = SimpleNamespace(user_data_dir=directory)
        return entry

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
    last_navigated_url: str = "https://fake.test/page",
    last_navigated_title: str = "Fake Page",
) -> SimpleNamespace:
    """A minimal instance object with the attributes ``list_instances`` reads.

    The pair is named as ``BrowserInstance`` names it (F-874): it is what the
    last navigation reported, NOT what the instance is showing. Where a test
    wants the live answer it seeds a tab through ``tabs=``/``browsers=``, which
    is the only thing that can carry one.
    """
    return SimpleNamespace(
        instance_id=instance_id,
        state=state,
        last_navigated_url=last_navigated_url,
        last_navigated_title=last_navigated_title,
    )
