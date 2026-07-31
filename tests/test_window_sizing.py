"""F-804 — a spawn's window size must be applied headed, and REPORTED truthfully.

Two tiers in one file, because the defect had two halves:

* **unit** (no Chrome) — the ``--window-size=W,H`` launch arg is actually built,
  for both modes, and an explicit caller ``--window-size`` still wins;
* **integration** (real Chrome, both modes) — a request that fits the desktop is
  honoured *exactly*, an oversized request is clamped by the window manager, and
  in BOTH cases the size the spawn reports equals the size the window really has.

The truthfulness pins are the load-bearing ones. Matching the request exactly is
best-effort by nature: headed Chrome clamps to the desktop work area, so on a
1024x768 desktop a 1920x1080 request lands near 1044x788. Before this fix the
spawn result echoed ``1920x1080`` back regardless — a window size that never
existed. The "fits" cases therefore use a deliberately modest 900x600, which
fits any desktop Chrome will realistically run on, so an exact-match assertion
is a real assertion and not a screen-size lottery.
"""

from __future__ import annotations

import pytest

from e2e_helpers import CAN_RUN, eval_js, get_fn, sandbox_kwargs, warmup_once
from stealth_chrome_devtools_mcp.embedded import window_sizing
from stealth_chrome_devtools_mcp.embedded.models import BrowserOptions

# A size that fits every plausible desktop work area (the smallest Chrome is
# realistically launched on is 1024x768), so "actual == requested" is testable.
FITS_W, FITS_H = 900, 600
# Deliberately larger than any real screen, so the window manager MUST clamp.
OVERSIZED_W, OVERSIZED_H = 9000, 7000
# CDP window bounds vs. window.outerWidth can round apart by a pixel on a
# fractional-DPI display; anything beyond this is a genuine disagreement.
TOLERANCE_PX = 2


# ---------------------------------------------------------------------------
# Unit tier — launch-arg construction (no browser)
# ---------------------------------------------------------------------------


class TestWindowSizeLaunchArg:
    def test_arg_is_appended_for_headed_spawn(self):
        args = window_sizing.append_size_arg(
            [],
            BrowserOptions(headless=False, viewport_width=1200, viewport_height=800),
        )
        assert "--window-size=1200,800" in args

    def test_arg_is_appended_for_headless_spawn(self):
        args = window_sizing.append_size_arg(
            [],
            BrowserOptions(headless=True, viewport_width=1200, viewport_height=800),
        )
        assert "--window-size=1200,800" in args

    def test_default_options_carry_the_default_size(self):
        args = window_sizing.append_size_arg([], BrowserOptions())
        assert "--window-size=1920,1080" in args

    def test_explicit_caller_arg_wins(self):
        args = window_sizing.append_size_arg(
            ["--window-size=640,480"],
            BrowserOptions(viewport_width=1200, viewport_height=800),
        )
        assert args == ["--window-size=640,480"]

    def test_existing_args_are_preserved(self):
        args = window_sizing.append_size_arg(
            ["--lang=en-US"],
            BrowserOptions(viewport_width=800, viewport_height=600),
        )
        assert args == ["--lang=en-US", "--window-size=800,600"]

    def test_resolve_launch_args_emits_the_arg_for_a_headed_spawn(self, monkeypatch):
        """The arg must reach the REAL launch-arg pipeline, not just the helper.

        ``--window-size`` is not on the stealth block list, so it has to survive
        ``merge_browser_args`` — that is the property this pins.
        """
        from stealth_chrome_devtools_mcp.embedded import browser_manager as bm_mod

        monkeypatch.setattr(
            bm_mod, "check_browser_executable", lambda: "/usr/bin/google-chrome"
        )
        manager = bm_mod.BrowserManager()
        launch_args, _executable, _warnings = manager._resolve_launch_args(
            BrowserOptions(headless=False, viewport_width=1200, viewport_height=800),
            None,
            {"system": "Linux", "is_root": False, "is_container": False},
        )
        assert "--window-size=1200,800" in launch_args


# ---------------------------------------------------------------------------
# Integration tier — real Chrome, both modes
# ---------------------------------------------------------------------------


async def _measured_outer_size(iid: str) -> tuple[int, int]:
    """The window's real outer size, read from the page itself."""
    width = await eval_js(iid, "window.outerWidth")
    height = await eval_js(iid, "window.outerHeight")
    return int(width), int(height)


async def _spawn(tmp_path, *, headless: bool, width: int, height: int) -> dict:
    """Spawn through the real tool, on a throwaway absolute profile dir.

    An absolute ``user_data_dir`` outside the clone root is the one spawn shape
    that never reads or copies the master profile, so these tests cost a Chrome
    launch and nothing else.
    """
    spawn = get_fn("spawn_browser")
    return await spawn(
        headless=headless,
        viewport_width=width,
        viewport_height=height,
        user_data_dir=str(tmp_path / f"f804-{'less' if headless else 'ed'}"),
        **sandbox_kwargs(),
    )


def _assert_report_matches_reality(result: dict, measured: tuple[int, int]) -> None:
    """The reported size must BE the window's size — the F-804 invariant."""
    metrics = result["spawn_diagnostics"]["window_size"]
    assert metrics["measured"] is True, metrics
    assert result["viewport"] == metrics["actual"], result
    assert abs(metrics["actual"]["width"] - measured[0]) <= TOLERANCE_PX, (
        metrics,
        measured,
    )
    assert abs(metrics["actual"]["height"] - measured[1]) <= TOLERANCE_PX, (
        metrics,
        measured,
    )


@pytest.mark.integration
@pytest.mark.skipif(not CAN_RUN, reason="Chrome not available or server failed to load")
class TestRealChromeWindowSize:
    @pytest.fixture(autouse=True)
    async def _warmup(self):
        """Absorb the cold-launch cost once per session (shared E2E mechanism)."""
        await warmup_once()
        yield

    async def test_headed_spawn_honours_a_size_that_fits(self, tmp_path):
        """A headed window asked for a size the desktop can hold must GET it."""
        close = get_fn("close_instance")
        result = await _spawn(tmp_path, headless=False, width=FITS_W, height=FITS_H)
        iid = result["instance_id"]
        try:
            measured = await _measured_outer_size(iid)
            assert abs(measured[0] - FITS_W) <= TOLERANCE_PX, measured
            assert abs(measured[1] - FITS_H) <= TOLERANCE_PX, measured
            _assert_report_matches_reality(result, measured)
            metrics = result["spawn_diagnostics"]["window_size"]
            assert metrics["requested"] == {"width": FITS_W, "height": FITS_H}
            assert metrics["clamped"] is False, metrics
            assert (
                f"--window-size={FITS_W},{FITS_H}"
                in result["spawn_diagnostics"]["effective_browser_args"]
            )
        finally:
            await close(instance_id=iid)

    async def test_headed_spawn_reports_a_clamped_size_truthfully(self, tmp_path):
        """THE F-804 pin: an unhonourable request is not echoed back as applied.

        The window manager clamps a 9000x7000 request to the work area. What the
        spawn reports must be that clamped reality, flagged as clamped — never
        the request.
        """
        close = get_fn("close_instance")
        result = await _spawn(
            tmp_path, headless=False, width=OVERSIZED_W, height=OVERSIZED_H
        )
        iid = result["instance_id"]
        try:
            measured = await _measured_outer_size(iid)
            assert measured[0] < OVERSIZED_W, measured  # the clamp really happened
            _assert_report_matches_reality(result, measured)
            metrics = result["spawn_diagnostics"]["window_size"]
            assert metrics["requested"] == {
                "width": OVERSIZED_W,
                "height": OVERSIZED_H,
            }
            assert metrics["clamped"] is True, metrics
            assert result["viewport"] != metrics["requested"], result
        finally:
            await close(instance_id=iid)

    async def test_headless_spawn_honours_a_size_that_fits(self, tmp_path):
        """Headless already worked — this pins that the fix did not regress it."""
        close = get_fn("close_instance")
        result = await _spawn(tmp_path, headless=True, width=FITS_W, height=FITS_H)
        iid = result["instance_id"]
        try:
            measured = await _measured_outer_size(iid)
            assert abs(measured[0] - FITS_W) <= TOLERANCE_PX, measured
            assert abs(measured[1] - FITS_H) <= TOLERANCE_PX, measured
            _assert_report_matches_reality(result, measured)
            assert result["spawn_diagnostics"]["window_size"]["clamped"] is False
        finally:
            await close(instance_id=iid)
