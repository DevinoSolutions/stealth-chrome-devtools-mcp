"""The captcha auto-click engine and its three tools, with no browser and no network.

``DETECTION_JS`` runs in a page, so what is pinned here is everything around it:
how its answer is read (``scan``), where and how the click lands
(``click_captcha``), and the per-instance loop (``CaptchaMonitor``). The script
itself meets real Turnstile and reCAPTCHA widgets in
``tests/test_captcha_integration.py``.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from fakes import FakeBrowser, FakeBrowserManager, FakeTab, fake_instance
from stealth_chrome_devtools_mcp.embedded import captcha_clicker
from stealth_chrome_devtools_mcp.embedded.captcha_clicker import (
    CHECKBOX_OFFSETS,
    DETECTION_JS,
    CaptchaMonitor,
    click_captcha,
    scan,
)
from stealth_chrome_devtools_mcp.embedded.tool_errors import (
    InstanceNotFoundError,
    ToolError,
)
from stealth_chrome_devtools_mcp.embedded.tool_registry import SECTION_TOOLS

TURNSTILE_SRC = "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/turnstile"
RECAPTCHA_SRC = "https://www.google.com/recaptcha/api2/anchor?k=KEY"
HCAPTCHA_SRC = (
    "https://newassets.hcaptcha.com/captcha/v1/abc/static/hcaptcha.html#frame=checkbox"
)


def row(kind="turnstile", x=100, y=200, width=300, height=65, src=None, **extra):
    default_src = {
        "turnstile": TURNSTILE_SRC,
        "recaptcha-v2": RECAPTCHA_SRC,
        "hcaptcha": HCAPTCHA_SRC,
    }[kind]
    return {
        "type": kind,
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "src": default_src if src is None else src,
        "already_checked": False,
        "viewport_width": 1280,
        "viewport_height": 800,
        **extra,
    }


def page(*rows):
    """What the page answers: DETECTION_JS returns a JSON string."""
    return json.dumps(list(rows))


def mouse_events(tab):
    return [
        frame["params"]
        for frame in tab.cdp_frames
        if frame["method"] == "Input.dispatchMouseEvent"
    ]


async def until(predicate, seconds=3.0):
    deadline = asyncio.get_running_loop().time() + seconds
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


@pytest.fixture()
def monitor():
    fresh = CaptchaMonitor(interval=0.01)
    yield fresh
    for instance_id in list(fresh._monitors):
        fresh.cleanup(instance_id)


def manager_with(tab, instance_id="i1"):
    browser = FakeBrowser(tabs=[tab])
    return FakeBrowserManager(
        instances=[fake_instance(instance_id)],
        tabs={instance_id: tab},
        browsers={instance_id: browser},
    )


class TestDetectionScript:
    def test_names_every_supported_widget(self):
        for needle in (
            "challenges.cloudflare.com",
            "google.com/recaptcha/api2/anchor",
            "hcaptcha.com",
            ".cf-turnstile",
            ".g-recaptcha",
            ".h-captcha",
            "getBoundingClientRect",
            "JSON.stringify",
        ):
            assert needle in DETECTION_JS

    def test_every_type_has_a_checkbox_offset(self):
        assert set(CHECKBOX_OFFSETS) == set(captcha_clicker.SUPPORTED_TYPES)


class TestScan:
    @pytest.mark.parametrize("kind", ["turnstile", "recaptcha-v2", "hcaptcha"])
    async def test_reads_each_widget_kind(self, kind):
        tab = FakeTab(evaluate_result=page(row(kind, x=10, y=20)))
        (hit,) = await scan(tab)
        assert (hit.type, hit.iframe_x, hit.iframe_y) == (kind, 10, 20)
        assert hit.src == row(kind)["src"]

    async def test_a_page_without_a_captcha_has_no_hits(self):
        assert await scan(FakeTab(evaluate_result=page())) == []

    async def test_a_solved_widget_is_left_alone(self):
        tab = FakeTab(evaluate_result=page(row(already_checked=True)))
        assert await scan(tab) == []

    async def test_an_invisible_widget_is_left_alone(self):
        tab = FakeTab(evaluate_result=page(row(width=0, height=0)))
        assert await scan(tab) == []

    async def test_a_widget_below_the_fold_waits_for_the_page_to_scroll(self):
        tab = FakeTab(evaluate_result=page(row(y=5000)))
        assert await scan(tab) == []

    @pytest.mark.parametrize(
        "answer",
        [
            None,
            5,
            "not json",
            "{}",
            '["x", 3, {"type": "other"}]',
            '[{"type": "turnstile"}]',
        ],
    )
    async def test_an_answer_it_cannot_read_is_no_hit(self, answer):
        assert await scan(FakeTab(evaluate_result=answer)) == []

    async def test_a_hung_tab_is_cut_off(self, monkeypatch):
        monkeypatch.setattr(captcha_clicker, "SEND_TIMEOUT", 0.05)

        class Hung(FakeTab):
            async def evaluate(self, *args, **kwargs):
                await asyncio.Event().wait()

        with pytest.raises(TimeoutError):
            await scan(Hung())


class TestClick:
    @pytest.mark.parametrize("kind", ["turnstile", "recaptcha-v2", "hcaptcha"])
    async def test_clicks_the_checkbox_for_its_kind(self, kind):
        tab = FakeTab()
        (hit,) = await scan(FakeTab(evaluate_result=page(row(kind, x=100, y=200))))
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
        tab = FakeTab()
        (hit,) = await scan(FakeTab(evaluate_result=page(row())))
        await click_captcha(tab, hit)
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
        (hit,) = await scan(FakeTab(evaluate_result=page(row(x=0, y=0))))
        tab = FakeTab()
        for _ in range(50):
            await click_captcha(tab, hit)
        offset_x, offset_y = CHECKBOX_OFFSETS["turnstile"]
        for event in mouse_events(tab):
            assert abs(event["x"] - offset_x) <= captcha_clicker.JITTER_PX
            assert abs(event["y"] - offset_y) <= captcha_clicker.JITTER_PX

    async def test_a_hung_send_is_cut_off(self, monkeypatch):
        monkeypatch.setattr(captcha_clicker, "SEND_TIMEOUT", 0.05)

        class Hung(FakeTab):
            async def send(self, *args, **kwargs):
                await asyncio.Event().wait()

        (hit,) = await scan(FakeTab(evaluate_result=page(row())))
        with pytest.raises(TimeoutError):
            await click_captcha(Hung(), hit)


class TestMonitor:
    async def test_clicks_a_captcha_it_finds(self, monitor):
        tab = FakeTab(evaluate_result=page(row()))
        monitor.start("i1", manager_with(tab))
        await until(lambda: monitor.status("i1")["clicks"])

        (click,) = monitor.status("i1")["clicks"]
        assert click["type"] == "turnstile"
        assert click["src"] == TURNSTILE_SRC
        assert set(click["clicked_at"]) == {"x", "y"}
        assert click["timestamp"].endswith("+00:00")

    async def test_the_same_widget_is_clicked_once(self, monitor):
        tab = FakeTab(evaluate_result=page(row()))
        monitor.start("i1", manager_with(tab))
        await until(lambda: monitor.status("i1")["clicks"])
        scans = len(tab.evaluate_calls)
        await until(lambda: len(tab.evaluate_calls) >= scans + 5)

        assert len(monitor.status("i1")["clicks"]) == 1
        assert [e["type"] for e in mouse_events(tab)].count("mousePressed") == 1

    async def test_a_widget_that_comes_back_is_clicked_again(self, monitor):
        tab = FakeTab(evaluate_result=page(row()))
        monitor.start("i1", manager_with(tab))
        await until(lambda: len(monitor.status("i1")["clicks"]) == 1)

        tab._evaluate_result = page()
        scans = len(tab.evaluate_calls)
        await until(lambda: len(tab.evaluate_calls) >= scans + 3)
        tab._evaluate_result = page(row())
        await until(lambda: len(monitor.status("i1")["clicks"]) == 2)

    async def test_two_tabs_with_the_same_widget_are_both_clicked(self, monitor):
        first = FakeTab(evaluate_result=page(row()), target_id="T-1")
        second = FakeTab(evaluate_result=page(row()), target_id="T-2")
        manager = FakeBrowserManager(
            instances=[fake_instance("i1")],
            browsers={"i1": FakeBrowser(tabs=[first, second])},
        )
        monitor.start("i1", manager)
        await until(lambda: len(monitor.status("i1")["clicks"]) == 2)
        assert mouse_events(first) and mouse_events(second)

    async def test_every_tab_is_scanned(self, monitor):
        quiet = FakeTab(evaluate_result=page(), target_id="T-1")
        busy = FakeTab(evaluate_result=page(row("hcaptcha")), target_id="T-2")
        manager = FakeBrowserManager(
            instances=[fake_instance("i1")],
            browsers={"i1": FakeBrowser(tabs=[quiet, busy])},
        )
        monitor.start("i1", manager)
        await until(lambda: monitor.status("i1")["clicks"])
        assert monitor.status("i1")["clicks"][0]["type"] == "hcaptcha"
        assert not mouse_events(quiet)

    async def test_a_tab_that_fails_does_not_stop_the_others(self, monitor):
        class Broken(FakeTab):
            async def evaluate(self, *args, **kwargs):
                raise RuntimeError("tab went away")

        busy = FakeTab(evaluate_result=page(row()), target_id="T-2")
        manager = FakeBrowserManager(
            instances=[fake_instance("i1")],
            browsers={"i1": FakeBrowser(tabs=[Broken(target_id="T-1"), busy])},
        )
        monitor.start("i1", manager)
        await until(lambda: monitor.status("i1")["clicks"])
        assert monitor.status("i1")["active"]

    async def test_a_raw_connection_is_skipped(self, monitor):
        class RawConnection:
            """A rediscovered target: no ``send``."""

        busy = FakeTab(evaluate_result=page(row()))
        manager = FakeBrowserManager(
            instances=[fake_instance("i1")],
            browsers={"i1": FakeBrowser(tabs=[RawConnection(), busy])},
        )
        monitor.start("i1", manager)
        await until(lambda: monitor.status("i1")["clicks"])

    async def test_status_before_and_after_start_and_stop(self, monitor):
        assert monitor.status("i1") == {"active": False, "clicks": [], "interval": None}

        tab = FakeTab(evaluate_result=page(row()))
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
        tab = FakeTab(evaluate_result=page())
        monitor.start("i1", manager_with(tab))
        await until(lambda: tab.evaluate_calls)
        monitor.stop("i1")
        await asyncio.sleep(0.05)
        scans = len(tab.evaluate_calls)
        await asyncio.sleep(0.1)
        assert len(tab.evaluate_calls) == scans

    async def test_starting_again_replaces_the_loop(self, monitor):
        tab = FakeTab(evaluate_result=page())
        manager = manager_with(tab)
        monitor.start("i1", manager, interval=0.01)
        first = monitor._monitors["i1"]
        monitor.start("i1", manager, interval=0.05)
        await asyncio.sleep(0)
        assert first.cancelled() or first.cancelling()
        assert monitor._monitors["i1"] is not first
        assert monitor.status("i1")["interval"] == 0.05

    async def test_instances_are_kept_apart(self, monitor):
        mine = FakeTab(evaluate_result=page(row()), target_id="T-1")
        theirs = FakeTab(evaluate_result=page(), target_id="T-2")
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
        tab = FakeTab(evaluate_result=page(row()))
        manager = manager_with(tab)
        monitor.start("i1", manager)
        await until(lambda: monitor.status("i1")["clicks"])

        manager._instances.clear()
        await until(lambda: not monitor.status("i1")["active"])
        assert monitor.status("i1")["clicks"] == []
        assert "i1" not in monitor._dedup

    async def test_cleanup_stops_and_forgets(self, monitor):
        tab = FakeTab(evaluate_result=page(row()))
        monitor.start("i1", manager_with(tab))
        await until(lambda: monitor.status("i1")["clicks"])

        monitor.cleanup("i1")
        assert monitor.status("i1") == {"active": False, "clicks": [], "interval": None}
        assert "i1" not in monitor._dedup

    async def test_the_module_singleton_is_a_monitor(self):
        assert isinstance(captcha_clicker.monitor, CaptchaMonitor)


class TestTools:
    @pytest.fixture()
    def wired(self, patched_server, monkeypatch):
        fresh = CaptchaMonitor(interval=0.01)
        monkeypatch.setattr(captcha_clicker, "monitor", fresh)
        tab = FakeTab(evaluate_result=page(row()))
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

    async def test_the_default_interval_is_two_seconds(self, wired, call_tool):
        server, _, _ = wired
        enabled = await call_tool(server, "enable_captcha_auto_click", instance_id="i1")
        assert enabled["interval_seconds"] == 2.0

    async def test_status_reports_the_clicks_made(self, wired, call_tool, monkeypatch):
        server, fresh, tab = wired
        monkeypatch.setattr(captcha_clicker, "MIN_INTERVAL", 0.01)
        await call_tool(
            server, "enable_captcha_auto_click", instance_id="i1", interval_seconds=0.01
        )
        await until(lambda: fresh.status("i1")["clicks"])
        status = await call_tool(server, "get_captcha_status", instance_id="i1")
        (click,) = status["clicks"]
        assert click["type"] == "turnstile"
        assert set(click) == {"type", "clicked_at", "src", "timestamp"}

    @pytest.mark.parametrize("seconds", [0.49, 30.01, 0, -1, float("nan")])
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
