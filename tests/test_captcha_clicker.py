"""The captcha auto-click engine and its three tools, with no browser and no network.

``scan`` finds widgets through CDP (``Target.getTargets`` / ``Page.getFrameTree``,
then ``DOM.getFrameOwner`` + ``DOM.getBoxModel``), so the double here,
:class:`CaptchaTab`, answers those commands with the JSON Chrome sends and lets
nodriver's OWN command generators parse it: a reply shape nodriver cannot read
fails here exactly as it would against Chrome. The real widgets, in a real
browser, are ``tests/test_captcha_integration.py``'s.
"""

from __future__ import annotations

import asyncio
import math
from types import GeneratorType
from typing import Any

import pytest
from nodriver.core.connection import ProtocolException

from fakes import FakeBrowser, FakeBrowserManager, FakeTab, fake_instance
from stealth_chrome_devtools_mcp.embedded import captcha_clicker
from stealth_chrome_devtools_mcp.embedded.captcha_clicker import (
    CHALLENGE,
    CHECKBOX_OFFSETS,
    FIRST_CLICK_AGE,
    RETRY_AFTER,
    CaptchaMonitor,
    classify,
    click_captcha,
    scan,
)
from stealth_chrome_devtools_mcp.embedded.tool_errors import (
    InstanceNotFoundError,
    ToolError,
)
from stealth_chrome_devtools_mcp.embedded.tool_registry import SECTION_TOOLS

#: Real frame URLs, as Chrome lists them (measured, Chrome 157).
TURNSTILE_SRC = (
    "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/f/"
    "av0/rch/1cuyo/3x00000000000000000000FF/auto/fbE/new/normal?lang=auto"
)
RECAPTCHA_SRC = (
    "https://www.google.com/recaptcha/api2/anchor?ar=1&k=6LeIxAcTAAAAAJcZVRqyHh71UMIEGNQ"
    "_MXjiZKhI&co=aHR0cDovLzEyNy4wLjAuMTo4MDAw&hl=en&v=x&size=normal&cb=y"
)
RECAPTCHA_BFRAME = "https://www.google.com/recaptcha/api2/bframe?hl=en&v=x&k=KEY"
HCAPTCHA_SRC = (
    "https://newassets.hcaptcha.com/captcha/v1/abc/static/hcaptcha.html"
    "#frame=checkbox&id=0p6j4drviy3d&host=127.0.0.1&sitekey=KEY"
)
HCAPTCHA_CHALLENGE = (
    "https://newassets.hcaptcha.com/captcha/v1/abc/static/hcaptcha.html"
    "#frame=challenge&id=0p6j4drviy3d&host=127.0.0.1&sitekey=KEY"
)
SRC = {
    "turnstile": TURNSTILE_SRC,
    "recaptcha-v2": RECAPTCHA_SRC,
    "hcaptcha": HCAPTCHA_SRC,
}

MAIN = "MAINFRAME000000000000000000000000"
VIEWPORT = (1280, 800)


def frame(
    kind="turnstile",
    frame_id=None,
    x=100,
    y=200,
    width=None,
    height=None,
    solved=False,
    url=None,
    in_process=False,
    parent=MAIN,
    box_error=None,
    scale=1.0,
    covered=False,
):
    """One iframe on the page, as the double serves it. ``scale`` is a CSS
    ``transform: scale()`` on it; ``covered``, an element on top of it."""
    default_size = (300, 65) if kind == "turnstile" else (304, 78)
    return {
        "id": frame_id or f"F-{kind}",
        "url": SRC.get(kind, "") if url is None else url,
        "x": x,
        "y": y,
        "width": default_size[0] if width is None else width,
        "height": default_size[1] if height is None else height,
        "solved": solved,
        "in_process": in_process,
        "parent": parent,
        "box_error": box_error,
        "scale": scale,
        "covered": covered,
    }


def _frame_json(frame_id, url, parent=None):
    """A ``Page.Frame`` as Chrome sends it."""
    base, _, fragment = url.partition("#")
    out = {
        "id": frame_id,
        "loaderId": f"L-{frame_id}",
        "url": base,
        "domainAndRegistry": "",
        "securityOrigin": base.split("/", 3)[0] + "//" + base.split("/", 3)[2]
        if "//" in base
        else "null",
        "mimeType": "text/html",
        "secureContextType": "Secure",
        "crossOriginIsolatedContextType": "NotIsolated",
        "gatedAPIFeatures": [],
    }
    if fragment:
        out["urlFragment"] = "#" + fragment
    if parent:
        out["parentId"] = parent
    return out


class CaptchaTab(FakeTab):
    """A page holding provider iframes, answering the CDP a scan sends.

    Every reply is the JSON Chrome sends, fed into nodriver's own command
    generator, so what the product reads is what nodriver parsed.
    """

    def __init__(
        self, *frames, target_id="T-1", main=MAIN, session_gone=False, scroll=(0, 0)
    ):
        super().__init__(target_id=target_id)
        self.frames = list(frames)
        self.main = main
        self.session_gone = session_gone
        self.scroll = scroll

    def methods(self):
        return [f["method"] for f in self.cdp_frames]

    def _by_id(self, frame_id):
        return next(f for f in self.frames if f["id"] == frame_id)

    def _reply(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self.session_gone:
            raise ProtocolException(
                {"code": -32001, "message": "Session with given id not found."}
            )
        if method == "Page.getFrameTree":
            children = []
            for f in self.frames:
                if not f["in_process"]:
                    continue
                node = {"frame": _frame_json(f["id"], f["url"], f["parent"])}
                if f["parent"] != self.main:  # inside a frame of its own
                    node = {
                        "frame": _frame_json(
                            f["parent"], "https://other.test/", self.main
                        ),
                        "childFrames": [node],
                    }
                children.append(node)
            tree: dict[str, Any] = {
                "frame": _frame_json(self.main, "https://site.test/")
            }
            if children:
                tree["childFrames"] = children
            return {"frameTree": tree}
        if method == "Target.getTargets":
            infos = [
                {
                    "targetId": self.main,
                    "type": "page",
                    "title": "site",
                    "url": "https://site.test/",
                    "attached": True,
                    "canAccessOpener": False,
                    "browserContextId": "C",
                }
            ]
            infos.extend(
                {
                    "targetId": f["id"],
                    "type": "iframe",
                    "title": f["url"],  # measured: an OOPIF's title is its URL
                    "url": f["url"],
                    "attached": False,
                    "canAccessOpener": False,
                    "browserContextId": "C",
                    "parentFrameId": f["parent"],
                }
                for f in self.frames
                if not f["in_process"]
            )
            return {"targetInfos": infos}
        if method == "Page.getLayoutMetrics":
            width, height = VIEWPORT
            scroll_x, scroll_y = self.scroll
            layout = {
                "pageX": scroll_x,
                "pageY": scroll_y,
                "clientWidth": width,
                "clientHeight": height,
            }
            visual = {
                "offsetX": 0,
                "offsetY": 0,
                "pageX": scroll_x,
                "pageY": scroll_y,
                "clientWidth": width,
                "clientHeight": height,
                "scale": 1,
                "zoom": 1,
            }
            size = {"x": 0, "y": 0, "width": width, "height": 3000}
            return {
                "layoutViewport": layout,
                "visualViewport": visual,
                "contentSize": size,
                "cssLayoutViewport": layout,
                "cssVisualViewport": visual,
                "cssContentSize": size,
            }
        if method == "DOM.getFrameOwner":
            index = self.frames.index(self._by_id(params["frameId"]))
            return {"backendNodeId": 1000 + index}
        if method == "DOM.getBoxModel":
            f = self.frames[params["backendNodeId"] - 1000]
            if f["box_error"]:
                raise ProtocolException({"code": -32000, "message": f["box_error"]})
            x, y, w, h = f["x"], f["y"], f["width"], f["height"]
            sw, sh = w * f["scale"], h * f["scale"]
            quad = [x, y, x + sw, y, x + sw, y + sh, x, y + sh]
            return {
                "model": {
                    "content": quad,
                    "padding": quad,
                    "border": quad,
                    "margin": quad,
                    "width": w,
                    "height": h,
                }
            }
        if method == "DOM.resolveNode":
            return {
                "object": {
                    "type": "object",
                    "subtype": "node",
                    "className": "HTMLIFrameElement",
                    "description": "iframe",
                    "objectId": f"obj-{int(params['backendNodeId'])}",
                }
            }
        if method == "DOM.getNodeForLocation":
            # DOCUMENT coordinates, as Chrome takes them (measured).
            x, y = params["x"] - self.scroll[0], params["y"] - self.scroll[1]
            for index, f in reversed(list(enumerate(self.frames))):
                w, h = f["width"] * f["scale"], f["height"] * f["scale"]
                if f["x"] <= x <= f["x"] + w and f["y"] <= y <= f["y"] + h:
                    if f["covered"]:
                        return {"backendNodeId": 9999, "frameId": self.main}
                    if f["in_process"]:
                        return {"backendNodeId": 5000 + index, "frameId": f["id"]}
                    return {"backendNodeId": 1000 + index, "frameId": self.main}
            raise ProtocolException(
                {"code": -32000, "message": "No node found at given location"}
            )
        if method == "Runtime.callFunctionOn":
            f = self.frames[int(params["objectId"].removeprefix("obj-")) - 1000]
            return {"result": {"type": "boolean", "value": f["solved"]}}
        return {}

    async def send(self, cdp_obj: Any, *args: Any, **kwargs: Any) -> Any:
        if not isinstance(cdp_obj, GeneratorType):
            return await super().send(cdp_obj, *args, **kwargs)
        request = next(cdp_obj)
        self.cdp_frames.append(request)
        self.send_calls.append(request["method"])
        reply = self._reply(request["method"], request.get("params", {}))
        try:
            cdp_obj.send(reply)
        except StopIteration as done:
            return done.value
        raise AssertionError(f"{request['method']} did not finish on its reply")


def mouse_events(tab):
    return [
        f["params"] for f in tab.cdp_frames if f["method"] == "Input.dispatchMouseEvent"
    ]


def presses(tab):
    return [e for e in mouse_events(tab) if e["type"] == "mousePressed"]


def released(tab):
    return [e for e in mouse_events(tab) if e["type"] == "mouseReleased"]


async def until(predicate, seconds=3.0):
    deadline = asyncio.get_running_loop().time() + seconds
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.005)


async def scans(tab, count):
    """Wait until ``tab`` has been scanned ``count`` more times."""
    start = tab.methods().count("Page.getFrameTree")
    await until(lambda: tab.methods().count("Page.getFrameTree") >= start + count)


@pytest.fixture()
def monitor():
    fresh = CaptchaMonitor(interval=0.01)
    yield fresh
    for instance_id in list(fresh._monitors):
        fresh.cleanup(instance_id)


@pytest.fixture(autouse=True)
def _quick(monkeypatch):
    """No waiting on the clock unless a test asks for it."""
    for kind in FIRST_CLICK_AGE:
        monkeypatch.setitem(FIRST_CLICK_AGE, kind, 0.0)
        monkeypatch.setitem(RETRY_AFTER, kind, 60.0)


def manager_with(*tabs, instance_id="i1"):
    return FakeBrowserManager(
        instances=[fake_instance(instance_id)],
        tabs={instance_id: tabs[0]},
        browsers={instance_id: FakeBrowser(tabs=list(tabs))},
    )


class TestClassify:
    @pytest.mark.parametrize(
        ("url", "kind"),
        [
            (TURNSTILE_SRC, "turnstile"),
            (
                "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/g/"
                "turnstile/if/ov2/av0/rcv/abc/0x4AAAA/dark/fbE/new/normal",
                "turnstile",
            ),
            (RECAPTCHA_SRC, "recaptcha-v2"),
            ("https://www.recaptcha.net/recaptcha/api2/anchor?k=K", "recaptcha-v2"),
            ("https://www.google.com/recaptcha/enterprise/anchor?k=K", "recaptcha-v2"),
            (HCAPTCHA_SRC, "hcaptcha"),
            ("https://assets.hcaptcha.com/x/hcaptcha.html#frame=checkbox", "hcaptcha"),
            (RECAPTCHA_BFRAME, CHALLENGE),
            ("https://www.google.com/recaptcha/enterprise/bframe?k=K", CHALLENGE),
            (HCAPTCHA_CHALLENGE, CHALLENGE),
        ],
    )
    def test_names_each_provider_frame(self, url, kind):
        assert classify(url) == kind

    @pytest.mark.parametrize(
        "url",
        [
            "",
            "about:blank",
            "https://site.test/",
            # a provider's host, but no widget
            "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/orchestrate",
            "https://www.google.com/search?q=recaptcha/api2/anchor",
            "https://newassets.hcaptcha.com/captcha/v1/abc/static/hcaptcha.html",
            # the badge of an INVISIBLE reCAPTCHA has no checkbox
            "https://www.google.com/recaptcha/api2/anchor?k=K&size=invisible",
            # look-alikes: the host decides, not a substring
            "https://challenges.cloudflare.com.evil.test/turnstile/",
            "https://evil.test/challenges.cloudflare.com/turnstile/",
            "https://hcaptcha.com.evil.test/x.html#frame=checkbox",
            "https://evilhcaptcha.com/x.html#frame=checkbox",
            "https://evil.test/recaptcha/api2/anchor?k=K",
            # not https
            "http://challenges.cloudflare.com/cdn-cgi/challenge-platform/turnstile/x",
        ],
    )
    def test_anything_else_is_none(self, url):
        assert classify(url) is None

    def test_every_type_has_its_numbers(self):
        for table in (CHECKBOX_OFFSETS, FIRST_CLICK_AGE, RETRY_AFTER):
            assert set(table) == set(captcha_clicker.SUPPORTED_TYPES)


class TestScan:
    @pytest.mark.parametrize("kind", ["turnstile", "recaptcha-v2", "hcaptcha"])
    async def test_finds_an_out_of_process_widget_where_its_iframe_is(self, kind):
        tab = CaptchaTab(frame(kind, x=10, y=20))
        (hit,) = (await scan(tab)).hits
        assert (hit.type, hit.iframe_x, hit.iframe_y) == (kind, 10, 20)
        assert (hit.src, hit.frame_id) == (SRC[kind], f"F-{kind}")
        assert (hit.viewport_width, hit.viewport_height) == VIEWPORT
        assert hit.already_checked is False

    async def test_finds_an_in_process_widget_with_its_fragment(self):
        tab = CaptchaTab(frame("hcaptcha", in_process=True))
        (hit,) = (await scan(tab)).hits
        assert (hit.type, hit.src) == ("hcaptcha", HCAPTCHA_SRC)

    async def test_a_widget_inside_another_frame_is_left_alone(self):
        """Its box model would be in that frame's coordinates, not the page's."""
        tab = CaptchaTab(
            frame("turnstile", parent="SOMEOTHERFRAME"),
            frame("recaptcha-v2", in_process=True, parent=MAIN),
        )
        hits = (await scan(tab)).hits
        assert [hit.type for hit in hits] == ["recaptcha-v2"]

    async def test_an_in_process_widget_inside_another_frame_is_left_alone(self):
        tab = CaptchaTab(
            frame("hcaptcha", in_process=True, parent="SOMEOTHERFRAME"),
            frame("recaptcha-v2", frame_id="F-r"),
        )
        hits = (await scan(tab)).hits
        assert [hit.type for hit in hits] == ["recaptcha-v2"]

    async def test_a_page_without_a_captcha_has_no_hits_and_costs_two_calls(self):
        tab = CaptchaTab(frame(url="https://ads.test/frame.html"))
        result = await scan(tab)
        assert result.hits == [] and result.challenge_showing is False
        assert tab.methods() == ["Page.getFrameTree", "Target.getTargets"]

    async def test_a_fake_widget_is_never_located(self):
        """An attacker's iframe in a ``cf-turnstile`` div is still not on a provider."""
        tab = CaptchaTab(frame(url="https://evil.test/turnstile/fake"))
        assert (await scan(tab)).hits == []
        assert "DOM.getFrameOwner" not in tab.methods()

    async def test_never_resets_the_sessions_node_ids(self):
        """F-884: ``DOM.getDocument`` would, under every other DOM tool's feet."""
        tab = CaptchaTab(frame("turnstile"), frame("hcaptcha", frame_id="F-h"))
        await scan(tab)
        assert "DOM.getDocument" not in tab.methods()
        assert "DOM.performSearch" not in tab.methods()

    async def test_a_solved_widget_is_not_clickable(self):
        tab = CaptchaTab(frame(solved=True))
        result = await scan(tab)
        (hit,) = result.hits
        assert hit.already_checked is True
        assert result.clickable() == []

    @pytest.mark.parametrize(
        ("x", "y", "width", "height"),
        [
            (100, 5000, 300, 65),
            (100, -10119, 300, 65),
            (5000, 10, 300, 65),
            (100, 10, 0, 0),
        ],
    )
    async def test_a_widget_off_screen_or_unsized_is_not_clickable(
        self, x, y, width, height
    ):
        tab = CaptchaTab(frame(x=x, y=y, width=width, height=height))
        result = await scan(tab)
        assert len(result.hits) == 1
        assert result.clickable() == []
        assert "Runtime.callFunctionOn" not in tab.methods()

    @pytest.mark.parametrize(
        ("url", "y", "showing"),
        [
            (RECAPTCHA_BFRAME, 150, True),
            (HCAPTCHA_CHALLENGE, 150, True),
            (RECAPTCHA_BFRAME, -10119, False),  # parked, as both providers keep it
            (HCAPTCHA_CHALLENGE, -10119, False),
        ],
    )
    async def test_a_puzzle_on_screen_is_reported(self, url, y, showing):
        tab = CaptchaTab(
            frame("recaptcha-v2"),
            frame(url=url, frame_id="F-puzzle", y=y, width=400, height=580),
        )
        result = await scan(tab)
        assert result.challenge_showing is showing
        assert [hit.type for hit in result.hits] == ["recaptcha-v2"]

    async def test_a_frame_that_cannot_be_measured_is_skipped_alone(self):
        tab = CaptchaTab(
            frame("turnstile", box_error="Could not compute box model."),
            frame("hcaptcha", frame_id="F-h"),
        )
        hits = (await scan(tab)).hits
        assert [hit.type for hit in hits] == ["hcaptcha"]

    async def test_releases_what_it_resolved(self):
        tab = CaptchaTab(frame("turnstile"), frame("hcaptcha", frame_id="F-h"))
        await scan(tab)
        groups = {
            f["params"].get("objectGroup")
            for f in tab.cdp_frames
            if f["method"] == "DOM.resolveNode"
        }
        released = [
            f["params"]["objectGroup"]
            for f in tab.cdp_frames
            if f["method"] == "Runtime.releaseObjectGroup"
        ]
        assert groups == {captcha_clicker._OBJECT_GROUP}
        assert released == [captcha_clicker._OBJECT_GROUP]
        assert tab.methods()[-1] == "Runtime.releaseObjectGroup"

    async def test_releases_even_when_the_scan_fails_midway(self, monkeypatch):
        class Breaks(CaptchaTab):
            def _reply(self, method, params):
                if method == "Runtime.callFunctionOn":
                    raise RuntimeError("tab went away")
                return super()._reply(method, params)

        tab = Breaks(frame())
        with pytest.raises(RuntimeError):
            await scan(tab)
        assert tab.methods()[-1] == "Runtime.releaseObjectGroup"

    async def test_a_hung_tab_is_cut_off(self, monkeypatch):
        monkeypatch.setattr(captcha_clicker, "SEND_TIMEOUT", 0.05)

        class Hung(CaptchaTab):
            async def send(self, *args, **kwargs):
                await asyncio.Event().wait()

        with pytest.raises(TimeoutError):
            await scan(Hung(frame()))


async def hit_on(tab):
    """The one widget ``tab`` shows, as a scan places it."""
    (hit,) = (await scan(tab)).hits
    return hit


class TestClick:
    @pytest.mark.parametrize("kind", ["turnstile", "recaptcha-v2", "hcaptcha"])
    async def test_clicks_the_checkbox_for_its_kind(self, kind):
        tab = CaptchaTab(frame(kind))
        hit = await hit_on(tab)
        result = await click_captcha(tab, hit)

        offset_x, offset_y = CHECKBOX_OFFSETS[kind]
        events = mouse_events(tab)
        assert [event["type"] for event in events] == [
            "mouseMoved",
            "mousePressed",
            "mouseReleased",
        ]
        assert {(event["x"], event["y"]) for event in events} == {
            (result["clicked_at"]["x"], result["clicked_at"]["y"])
        }
        assert abs(events[0]["x"] - (100 + offset_x)) <= captcha_clicker.JITTER_PX
        assert abs(events[0]["y"] - (200 + offset_y)) <= captcha_clicker.JITTER_PX
        assert (result["type"], result["src"]) == (kind, hit.src)

    async def test_presses_the_left_button_once(self):
        tab = CaptchaTab(frame())
        await click_captcha(tab, await hit_on(tab))
        moved, pressed, released = mouse_events(tab)
        assert (moved["button"], moved["buttons"]) == ("none", 0)
        assert (pressed["button"], pressed["buttons"], pressed["clickCount"]) == (
            "left",
            1,
            1,
        )
        assert (released["button"], released["buttons"], released["clickCount"]) == (
            "left",
            0,
            1,
        )

    async def test_jitter_never_leaves_the_band(self):
        tab = CaptchaTab(frame(x=0, y=0))
        hit = await hit_on(tab)
        for _ in range(50):
            await click_captcha(tab, hit)
        offset_x, offset_y = CHECKBOX_OFFSETS["turnstile"]
        for event in mouse_events(tab):
            assert abs(event["x"] - offset_x) <= captcha_clicker.JITTER_PX
            assert abs(event["y"] - offset_y) <= captcha_clicker.JITTER_PX

    async def test_a_covered_widget_is_not_clicked(self):
        """A sticky header or a banner over it would take the click instead."""
        tab = CaptchaTab(frame(covered=True))
        assert await click_captcha(tab, await hit_on(tab)) is None
        assert mouse_events(tab) == []
        assert "DOM.getNodeForLocation" in tab.methods()

    async def test_the_hit_test_is_in_document_coordinates(self):
        """Measured: Chrome's hit test takes the viewport point PLUS the scroll."""
        tab = CaptchaTab(frame(x=100, y=200), scroll=(0, 250))
        hit = await hit_on(tab)
        assert (hit.iframe_y, hit.scroll_y) == (200, 250)
        result = await click_captcha(tab, hit)
        assert result is not None
        (probe,) = [
            f["params"]
            for f in tab.cdp_frames
            if f["method"] == "DOM.getNodeForLocation"
        ]
        assert abs(probe["y"] - (200 + 28 + 250)) <= captcha_clicker.JITTER_PX + 1
        # the mouse events themselves are in viewport coordinates
        assert abs(mouse_events(tab)[0]["y"] - (200 + 28)) <= captcha_clicker.JITTER_PX

    async def test_an_in_process_widget_is_hit_through_its_own_frame(self):
        tab = CaptchaTab(frame("hcaptcha", in_process=True))
        assert await click_captcha(tab, await hit_on(tab)) is not None

    async def test_a_scaled_widget_is_clicked_on_its_scaled_checkbox(self):
        """``.g-recaptcha { transform: scale(.6) }``: the offsets scale with it."""
        tab = CaptchaTab(frame("recaptcha-v2", x=100, y=200, scale=0.6))
        hit = await hit_on(tab)
        assert hit.scale == pytest.approx(0.6)
        assert hit.iframe_width == pytest.approx(304 * 0.6)
        await click_captcha(tab, hit)
        offset_x, offset_y = CHECKBOX_OFFSETS["recaptcha-v2"]
        event = mouse_events(tab)[0]
        assert abs(event["x"] - (100 + offset_x * 0.6)) <= captcha_clicker.JITTER_PX
        assert abs(event["y"] - (200 + offset_y * 0.6)) <= captcha_clicker.JITTER_PX

    async def test_a_failed_press_is_still_released(self, monkeypatch):
        class PressFails(CaptchaTab):
            def _reply(self, method, params):
                if params.get("type") == "mousePressed":
                    raise RuntimeError("renderer went away")
                return super()._reply(method, params)

        tab = PressFails(frame())
        with pytest.raises(RuntimeError):
            await click_captcha(tab, await hit_on(tab))
        assert [e["type"] for e in mouse_events(tab)] == [
            "mouseMoved",
            "mousePressed",
            "mouseReleased",
        ]

    async def test_a_hung_send_is_cut_off(self, monkeypatch):
        monkeypatch.setattr(captcha_clicker, "SEND_TIMEOUT", 0.05)
        hit = await hit_on(CaptchaTab(frame()))

        class Hung(FakeTab):
            async def send(self, *args, **kwargs):
                await asyncio.Event().wait()

        with pytest.raises(TimeoutError):
            await click_captcha(Hung(), hit)


class TestMonitor:
    async def test_clicks_a_captcha_it_finds(self, monitor):
        tab = CaptchaTab(frame())
        monitor.start("i1", manager_with(tab))
        await until(lambda: monitor.status("i1")["clicks"])

        (click,) = monitor.status("i1")["clicks"]
        assert click["type"] == "turnstile"
        assert click["src"] == TURNSTILE_SRC
        assert click["attempt"] == 1
        assert set(click["clicked_at"]) == {"x", "y"}
        assert click["timestamp"].endswith("+00:00")

    async def test_an_unsolved_widget_is_not_clicked_again_before_its_retry(
        self, monitor
    ):
        tab = CaptchaTab(frame())
        monitor.start("i1", manager_with(tab))
        await until(lambda: monitor.status("i1")["clicks"])
        await scans(tab, 5)
        assert len(monitor.status("i1")["clicks"]) == 1
        assert len(presses(tab)) == 1

    async def test_an_unsolved_widget_is_retried_up_to_the_cap(
        self, monitor, monkeypatch
    ):
        monkeypatch.setitem(RETRY_AFTER, "turnstile", 0.0)
        tab = CaptchaTab(frame())
        monitor.start("i1", manager_with(tab))
        cap = captcha_clicker.MAX_ATTEMPTS
        await until(lambda: len(monitor.status("i1")["clicks"]) == cap)
        await scans(tab, 5)
        clicks = monitor.status("i1")["clicks"]
        assert [click["attempt"] for click in clicks] == list(range(1, cap + 1))
        assert len(presses(tab)) == cap

    async def test_a_retry_waits_for_its_kinds_delay(self, monitor, monkeypatch):
        monkeypatch.setitem(RETRY_AFTER, "turnstile", 0.15)
        tab = CaptchaTab(frame())
        monitor.start("i1", manager_with(tab))
        await until(lambda: len(monitor.status("i1")["clicks"]) == 2)
        first, second = (monitor_click_times(tab))[:2]
        # The delay runs from the attempt's start; each press then comes after
        # the 50 ms move-to-press sleep, whose overshoot varies by a fraction
        # of a millisecond (CI py3.11 measured 0.14987 s). A retry ignoring the
        # delay comes one 0.01 s sweep plus one click (~0.08 s) later.
        assert second - first >= 0.15 - 0.01

    async def test_a_solved_widget_is_not_clicked(self, monitor):
        tab = CaptchaTab(frame(solved=True))
        monitor.start("i1", manager_with(tab))
        await scans(tab, 5)
        assert monitor.status("i1")["clicks"] == []

    async def test_a_widget_solved_after_its_click_is_not_retried(
        self, monitor, monkeypatch
    ):
        monkeypatch.setitem(RETRY_AFTER, "turnstile", 0.0)
        tab = CaptchaTab(frame())
        monitor.start("i1", manager_with(tab))
        await until(lambda: monitor.status("i1")["clicks"])
        tab.frames[0]["solved"] = True
        clicked = len(presses(tab))
        await scans(tab, 5)
        assert len(presses(tab)) == clicked

    async def test_the_first_click_waits_for_its_kinds_age(self, monitor, monkeypatch):
        monkeypatch.setitem(FIRST_CLICK_AGE, "recaptcha-v2", 0.2)
        tab = CaptchaTab(frame("recaptcha-v2"))
        loop = asyncio.get_running_loop()
        monitor.start("i1", manager_with(tab))
        await until(lambda: "Page.getFrameTree" in tab.methods())
        seen = loop.time()
        await until(lambda: monitor.status("i1")["clicks"])
        assert loop.time() - seen >= 0.19

    async def test_a_widget_due_soon_does_not_wait_a_whole_interval(self, monkeypatch):
        """The loop wakes for the due time, not the next interval."""
        monkeypatch.setitem(FIRST_CLICK_AGE, "recaptcha-v2", 0.1)
        slow = CaptchaMonitor(interval=5.0)
        tab = CaptchaTab(frame("recaptcha-v2"))
        loop = asyncio.get_running_loop()
        try:
            slow.start("i1", manager_with(tab))
            await until(lambda: "Page.getFrameTree" in tab.methods())
            seen = loop.time()
            await until(lambda: slow.status("i1")["clicks"], seconds=2.0)
            assert loop.time() - seen < 1.0
        finally:
            slow.cleanup("i1")

    async def test_nothing_is_clicked_while_a_puzzle_shows(self, monitor):
        tab = CaptchaTab(
            frame("recaptcha-v2"),
            frame(
                url=RECAPTCHA_BFRAME, frame_id="F-puzzle", y=100, width=400, height=580
            ),
        )
        monitor.start("i1", manager_with(tab))
        await scans(tab, 5)
        assert mouse_events(tab) == []

    async def test_a_re_rendered_widget_is_a_new_widget(self, monitor):
        """A reset puts the widget in a NEW frame, which gets its own first click."""
        tab = CaptchaTab(frame(frame_id="F-1"))
        monitor.start("i1", manager_with(tab))
        await until(lambda: len(monitor.status("i1")["clicks"]) == 1)
        tab.frames = [frame(frame_id="F-2")]
        await until(lambda: len(monitor.status("i1")["clicks"]) == 2)
        assert [c["attempt"] for c in monitor.status("i1")["clicks"]] == [1, 1]

    async def test_a_widget_that_is_gone_is_forgotten(self, monitor):
        tab = CaptchaTab(frame(frame_id="F-1"))
        monitor.start("i1", manager_with(tab))
        await until(lambda: monitor.status("i1")["clicks"])
        tab.frames = []
        await scans(tab, 3)
        assert monitor._widgets["i1"] == {}

    async def test_a_closed_tab_nodriver_still_lists_does_not_block_forgetting(
        self, monitor
    ):
        """Measured: such a tab answers -32001 to everything, every sweep."""
        tab = CaptchaTab(frame(frame_id="F-1"), target_id="T-1")
        gone = CaptchaTab(target_id="T-gone", session_gone=True)
        monitor.start("i1", manager_with(tab, gone))
        await until(lambda: monitor.status("i1")["clicks"])
        tab.frames = []
        await scans(tab, 3)
        assert monitor._widgets["i1"] == {}

    async def test_two_tabs_with_the_same_widget_are_both_clicked(self, monitor):
        first = CaptchaTab(frame(), target_id="T-1")
        second = CaptchaTab(frame(), target_id="T-2")
        monitor.start("i1", manager_with(first, second))
        await until(lambda: len(monitor.status("i1")["clicks"]) == 2)
        assert mouse_events(first) and mouse_events(second)

    async def test_every_tab_is_scanned(self, monitor):
        quiet = CaptchaTab(target_id="T-1")
        busy = CaptchaTab(frame("hcaptcha"), target_id="T-2")
        monitor.start("i1", manager_with(quiet, busy))
        await until(lambda: monitor.status("i1")["clicks"])
        assert monitor.status("i1")["clicks"][0]["type"] == "hcaptcha"
        assert not mouse_events(quiet)

    async def test_a_tab_that_fails_does_not_stop_the_others(self, monitor):
        class Broken(CaptchaTab):
            def _reply(self, method, params):
                raise RuntimeError("tab went away")

        busy = CaptchaTab(frame(), target_id="T-2")
        monitor.start("i1", manager_with(Broken(target_id="T-1"), busy))
        await until(lambda: monitor.status("i1")["clicks"])
        assert monitor.status("i1")["active"]

    async def test_a_raw_connection_is_skipped(self, monitor):
        class RawConnection:
            """A rediscovered target: no ``send``."""

        busy = CaptchaTab(frame())
        manager = FakeBrowserManager(
            instances=[fake_instance("i1")],
            browsers={"i1": FakeBrowser(tabs=[RawConnection(), busy])},
        )
        monitor.start("i1", manager)
        await until(lambda: monitor.status("i1")["clicks"])

    async def test_a_covered_widget_waits_and_keeps_its_attempts(self, monitor):
        tab = CaptchaTab(frame(covered=True))
        monitor.start("i1", manager_with(tab))
        await scans(tab, 5)
        assert mouse_events(tab) == []
        tab.frames[0]["covered"] = False
        await until(lambda: monitor.status("i1")["clicks"])
        assert monitor.status("i1")["clicks"][0]["attempt"] == 1

    async def test_a_press_that_fails_still_uses_an_attempt(self, monitor, monkeypatch):
        monkeypatch.setitem(RETRY_AFTER, "turnstile", 0.0)

        class PressFails(CaptchaTab):
            def _reply(self, method, params):
                if params.get("type") == "mousePressed":
                    raise RuntimeError("renderer went away")
                return super()._reply(method, params)

        tab = PressFails(frame())
        monitor.start("i1", manager_with(tab))
        cap = captcha_clicker.MAX_ATTEMPTS
        await until(lambda: len(presses(tab)) == cap)
        await scans(tab, 5)
        assert len(presses(tab)) == cap
        assert len(released(tab)) == cap

    async def test_a_frame_that_cannot_be_measured_keeps_its_attempts(
        self, monitor, monkeypatch
    ):
        """A modal closed and reopened must not hand its widget fresh clicks."""
        monkeypatch.setitem(RETRY_AFTER, "turnstile", 60.0)
        tab = CaptchaTab(frame(frame_id="F-1"))
        monitor.start("i1", manager_with(tab))
        await until(lambda: monitor.status("i1")["clicks"])
        tab.frames[0]["box_error"] = "Could not compute box model."
        await scans(tab, 3)
        tab.frames[0]["box_error"] = None
        await scans(tab, 3)
        assert len(presses(tab)) == 1

    async def test_a_stuck_tab_holds_up_no_other_tab(self, monitor, monkeypatch):
        monkeypatch.setattr(captcha_clicker, "SWEEP_WAIT", 0.05)

        class Stuck(CaptchaTab):
            async def send(self, *args, **kwargs):
                await asyncio.Event().wait()

        busy = CaptchaTab(frame(frame_id="F-1"), target_id="T-2")
        monitor.start("i1", manager_with(Stuck(target_id="T-1"), busy))
        await until(lambda: monitor.status("i1")["clicks"], seconds=1.0)
        stuck_scan = monitor._scans["i1"]["T-1"]
        # a widget appearing later on the healthy tab is not held up either
        busy.frames.append(frame("hcaptcha", frame_id="F-2", x=600))
        await until(lambda: len(monitor.status("i1")["clicks"]) == 2, seconds=1.0)
        # ...and the stuck tab still has its ONE scan, not one per sweep
        assert monitor._scans["i1"]["T-1"] is stuck_scan
        assert not stuck_scan.done()

    async def test_a_failing_tab_does_not_stop_another_tab_forgetting(self, monitor):
        class Broken(CaptchaTab):
            def _reply(self, method, params):
                raise RuntimeError("tab went away")

        busy = CaptchaTab(frame(frame_id="F-1"), target_id="T-2")
        monitor.start("i1", manager_with(Broken(target_id="T-1"), busy))
        await until(lambda: monitor.status("i1")["clicks"])
        busy.frames = []
        await scans(busy, 3)
        assert monitor._widgets["i1"] == {}

    async def test_stop_cancels_the_scans_still_running(self, monitor, monkeypatch):
        monkeypatch.setattr(captcha_clicker, "SWEEP_WAIT", 0.01)

        class Stuck(CaptchaTab):
            async def send(self, *args, **kwargs):
                await asyncio.Event().wait()

        monitor.start("i1", manager_with(Stuck()))
        await until(lambda: monitor._scans.get("i1"))
        (running,) = monitor._scans["i1"].values()
        monitor.stop("i1")
        await asyncio.sleep(0)
        assert running.cancelled() or running.cancelling()
        assert "i1" not in monitor._scans

    async def test_status_before_and_after_start_and_stop(self, monitor):
        assert monitor.status("i1") == {"active": False, "clicks": [], "interval": None}

        tab = CaptchaTab(frame())
        monitor.start("i1", manager_with(tab), interval=0.02)
        assert monitor.status("i1")["active"] is True
        assert monitor.status("i1")["interval"] == 0.02

        await until(lambda: monitor.status("i1")["clicks"])
        assert monitor.stop("i1") is True
        stopped = monitor.status("i1")
        assert stopped["active"] is False
        assert stopped["interval"] is None
        assert len(stopped["clicks"]) == 1
        assert monitor.stop("i1") is False

    async def test_a_stopped_monitor_scans_no_more(self, monitor):
        tab = CaptchaTab()
        monitor.start("i1", manager_with(tab))
        await until(lambda: tab.cdp_frames)
        monitor.stop("i1")
        await asyncio.sleep(0.05)
        sent = len(tab.cdp_frames)
        await asyncio.sleep(0.1)
        assert len(tab.cdp_frames) == sent

    async def test_starting_again_replaces_the_loop(self, monitor):
        tab = CaptchaTab()
        manager = manager_with(tab)
        monitor.start("i1", manager, interval=0.01)
        first = monitor._monitors["i1"]
        monitor.start("i1", manager, interval=0.05)
        await asyncio.sleep(0)
        assert first.cancelled() or first.cancelling()
        assert monitor._monitors["i1"] is not first
        assert monitor.status("i1")["interval"] == 0.05

    async def test_instances_are_kept_apart(self, monitor):
        mine = CaptchaTab(frame(), target_id="T-1")
        theirs = CaptchaTab(target_id="T-2")
        manager = FakeBrowserManager(
            instances=[fake_instance("i1"), fake_instance("i2")],
            browsers={"i1": FakeBrowser(tabs=[mine]), "i2": FakeBrowser(tabs=[theirs])},
        )
        monitor.start("i1", manager)
        monitor.start("i2", manager)
        await until(lambda: monitor.status("i1")["clicks"])
        assert monitor.status("i2")["clicks"] == []

    async def test_an_instance_that_is_gone_ends_its_loop_and_is_forgotten(
        self, monitor
    ):
        tab = CaptchaTab(frame())
        manager = manager_with(tab)
        monitor.start("i1", manager)
        await until(lambda: monitor.status("i1")["clicks"])

        manager._instances.clear()
        await until(lambda: not monitor.status("i1")["active"])
        assert monitor.status("i1")["clicks"] == []
        assert "i1" not in monitor._widgets

    async def test_cleanup_stops_and_forgets(self, monitor):
        tab = CaptchaTab(frame())
        monitor.start("i1", manager_with(tab))
        await until(lambda: monitor.status("i1")["clicks"])

        monitor.cleanup("i1")
        assert monitor.status("i1") == {"active": False, "clicks": [], "interval": None}
        assert "i1" not in monitor._widgets
        assert "i1" not in monitor._next_due
        assert "i1" not in monitor._scans

    async def test_the_module_singleton_is_a_monitor(self):
        assert isinstance(captcha_clicker.monitor, CaptchaMonitor)


def monitor_click_times(tab):
    """When each press reached the tab, by the fake's own record order."""
    return tab.press_times


@pytest.fixture(autouse=True)
def _press_clock(monkeypatch):
    """Stamp every press a CaptchaTab receives with the loop's clock."""
    original = CaptchaTab.send

    async def send(self, cdp_obj, *args, **kwargs):
        result = await original(self, cdp_obj, *args, **kwargs)
        if self.cdp_frames and self.cdp_frames[-1].get("params", {}).get("type") == (
            "mousePressed"
        ):
            self.__dict__.setdefault("press_times", []).append(
                asyncio.get_running_loop().time()
            )
        return result

    monkeypatch.setattr(CaptchaTab, "send", send)


class TestTools:
    @pytest.fixture()
    def wired(self, patched_server, monkeypatch):
        fresh = CaptchaMonitor(interval=0.01)
        monkeypatch.setattr(captcha_clicker, "monitor", fresh)
        tab = CaptchaTab(frame())
        server = patched_server(browser_manager=manager_with(tab))
        yield server, fresh, tab
        fresh.cleanup("i1")

    def test_the_section_registers_three_tools(self, patched_server):
        patched_server()
        assert SECTION_TOOLS["captcha"] == [
            "enable_captcha_auto_click",
            "disable_captcha_auto_click",
            "get_captcha_status",
        ]

    async def test_enable_then_status_then_disable(self, wired, call_tool):
        server, _, _ = wired
        enabled = await call_tool(
            server, "enable_captcha_auto_click", instance_id="i1", interval_seconds=0.5
        )
        assert enabled == {
            "enabled": True,
            "instance_id": "i1",
            "interval_seconds": 0.5,
        }
        status = await call_tool(server, "get_captcha_status", instance_id="i1")
        assert status["active"] is True
        assert status["interval_seconds"] == 0.5
        assert status["supported_types"] == ["turnstile", "recaptcha-v2", "hcaptcha"]

        disabled = await call_tool(
            server, "disable_captcha_auto_click", instance_id="i1"
        )
        assert disabled == {"enabled": False, "instance_id": "i1"}
        status = await call_tool(server, "get_captcha_status", instance_id="i1")
        assert status["active"] is False
        assert status["interval_seconds"] is None

    async def test_the_default_interval_is_a_quarter_second(self, wired, call_tool):
        server, _, _ = wired
        enabled = await call_tool(server, "enable_captcha_auto_click", instance_id="i1")
        assert enabled["interval_seconds"] == 0.25

    async def test_status_reports_the_clicks_made(self, wired, call_tool):
        server, fresh, _ = wired
        await call_tool(
            server, "enable_captcha_auto_click", instance_id="i1", interval_seconds=0.1
        )
        await until(lambda: fresh.status("i1")["clicks"])
        status = await call_tool(server, "get_captcha_status", instance_id="i1")
        (click,) = status["clicks"]
        assert click["type"] == "turnstile"
        assert set(click) == {"type", "clicked_at", "src", "attempt", "timestamp"}

    @pytest.mark.parametrize("seconds", [0.09, 30.01, 0, -1, math.nan])
    async def test_an_interval_out_of_range_is_refused(self, wired, call_tool, seconds):
        server, fresh, _ = wired
        with pytest.raises(ToolError, match="interval_seconds"):
            await call_tool(
                server,
                "enable_captcha_auto_click",
                instance_id="i1",
                interval_seconds=seconds,
            )
        assert fresh.status("i1")["active"] is False

    @pytest.mark.parametrize(
        "name",
        [
            "enable_captcha_auto_click",
            "disable_captcha_auto_click",
            "get_captcha_status",
        ],
    )
    async def test_an_unknown_instance_is_refused(self, wired, call_tool, name):
        server, fresh, _ = wired
        with pytest.raises(InstanceNotFoundError, match="Instance not found: nope"):
            await call_tool(server, name, instance_id="nope")
        assert fresh.status("nope")["active"] is False
