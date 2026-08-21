"""Platform-specific utility functions for browser automation."""

from __future__ import annotations

import asyncio
import ctypes
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from nodriver import cdp

from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.settings import get_settings

if TYPE_CHECKING:  # pragma: no cover - typing only
    from nodriver import Tab


def is_running_as_root() -> bool:
    """
    Check if the current process is running with elevated privileges.

    Returns:
        bool: True if running as root (Linux/macOS) or administrator (Windows)
    """
    system = platform.system().lower()

    if system in ("linux", "darwin"):  # Linux or macOS
        try:
            return os.getuid() == 0
        except AttributeError:
            return False
    elif system == "windows":
        try:
            return ctypes.windll.shell32.IsUserAnAdmin() != 0
        except (AttributeError, OSError):
            return False
    else:
        return False


def is_running_in_container() -> bool:
    """
    Check if the process is running inside a container (Docker, etc.).

    Returns:
        bool: True if likely running in a container
    """

    def _check_cgroup_for_docker() -> bool:
        """
        Check /proc/1/cgroup for docker indicators.

        Returns:
            bool: True if docker indicator found in cgroup file.
        """
        try:
            cgroup = Path("/proc/1/cgroup")
            if not cgroup.exists():
                return False
            with cgroup.open() as f:
                return "docker" in f.read()
        except (OSError, PermissionError):
            return False

    container_indicators = [
        Path("/.dockerenv").exists(),
        _check_cgroup_for_docker(),
        get_settings().container is not None,
        get_settings().kubernetes_service_host is not None,
    ]

    return any(container_indicators)


def get_required_sandbox_args() -> list[str]:
    """
    Get the required browser arguments for sandbox handling based on current
    environment.

    Returns:
        List[str]: List of browser arguments needed for current environment
    """
    args = []

    if is_running_as_root():
        args.extend(["--no-sandbox", "--disable-setuid-sandbox"])

    if is_running_in_container():
        args.extend(
            [
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--single-process",
            ]
        )

    seen = set()
    unique_args = []
    for arg in args:
        if arg not in seen:
            seen.add(arg)
            unique_args.append(arg)

    return unique_args


def _stealth_blocked_args() -> dict:
    """
    Returns a dict mapping blocked Chrome flag prefixes to the reason they
    compromise stealth.  Keys are lowercase and checked with startswith()
    so ``--disable-gpu-sandbox`` is caught by ``--disable-gpu``.
    """
    return {
        # ── direct automation signals ──
        "--enable-automation": "sets navigator.webdriver=true",
        "--test-type": "enables Chrome test mode",
        "--enable-blink-features=automationcontrolled": "explicit automation marker",
        "--auto-open-devtools-for-tabs": "DevTools on start is detectable",
        "--remote-debugging-port": "DevTools port exposure",
        "--remote-debugging-pipe": "DevTools pipe exposure",
        # ── fingerprint-altering flags ──
        "--no-sandbox": "missing sandbox detectable via process topology",
        "--disable-gpu": "GPU absence detectable via WebGL probes",
        "--disable-dev-shm-usage": "signals headless container environment",
        "--disable-software-rasterizer": (
            "alters rendering pipeline (canvas fingerprint)"
        ),
        "--disable-webgl": "WebGL absence is a strong bot signal",
        "--disable-webgl2": "WebGL2 absence is a strong bot signal",
        "--disable-extensions": "real users have extensions",
        "--disable-default-apps": "app list mismatch",
        "--disable-popup-blocking": "behavior differs from real user",
        "--disable-notifications": "permission API behaves differently",
        "--single-process": "detectable process architecture",
        "--headless": "headless detection via window/navigator properties",
        "--mute-audio": "audio context fingerprint affected",
        "--force-device-scale-factor": "DPI/scale mismatch detectable",
        "--disable-background-networking": "network behavior differs from real browser",
        # ── Puppeteer / Playwright signature flags ──
        "--disable-backgrounding-occluded-windows": "Puppeteer default",
        "--disable-renderer-backgrounding": "Puppeteer/Playwright default",
        "--disable-ipc-flooding-protection": "Puppeteer default",
        "--password-store=basic": "Playwright default",
        "--use-mock-keychain": "Playwright default",
        "--export-tagged-pdf": "Playwright default",
        "--disable-hang-monitor": "automation default",
        "--disable-prompt-on-repost": "automation default",
        "--disable-client-side-phishing-detection": "automation default",
        "--disable-domain-reliability": "automation default",
        "--metrics-recording-only": "automation telemetry flag",
        "--safebrowsing-disable-auto-update": "automation default",
        "--disable-sync": "common in automation, absent in real profiles",
        "--disable-component-extensions-with-background-pages": (
            "component fingerprint mismatch"
        ),
        "--no-first-run": "automation convenience flag",
        "--no-default-browser-check": "automation convenience flag",
        "--disable-setuid-sandbox": "sandbox mismatch detectable",
    }


def filter_stealth_args(user_args: list[str]) -> tuple:
    """
    Strip browser args that would compromise stealth and return
    (clean_args, stripped_warnings).

    Each warning is a string like:
        ``"--no-sandbox stripped: missing sandbox detectable via process topology"``
    """
    blocked = _stealth_blocked_args()
    clean: list[str] = []
    warnings: list[str] = []

    for arg in user_args:
        lower = arg.lower().strip()
        matched = False
        for prefix, reason in blocked.items():
            if lower.startswith(prefix):
                warnings.append(f"{arg} stripped: {reason}")
                matched = True
                break
        if not matched:
            clean.append(arg)

    return clean, warnings


# ── The masked default User-Agent (F-770) ───────────────────────────────────
# Headless Chrome advertises itself in its own User-Agent:
#     Mozilla/5.0 (...) HeadlessChrome/<major>.0.0.0 Safari/537.36
# That single substring is the cheapest bot check in existence — one server-side
# test, before a byte of JavaScript runs — so a default headless spawn must not
# ship it.
#
# The mask is CONSTRUCTED rather than guessed because Chrome froze its
# User-Agent in the reduced-UA rollout: the platform token, the WebKit build and
# the Safari token are constants, and the only varying part is the browser's
# MAJOR version, always rendered ``<major>.0.0.0``. The string built here is
# therefore byte-identical to what the same binary emits headed — i.e. exactly
# Chrome's own User-Agent with ``HeadlessChrome`` replaced by ``Chrome``. That
# equality is pinned as a product-vs-control differential in tests/test_stealth.py:
# consistency is itself a tell, and a UA that disagrees with ``sec-ch-ua`` or with
# the real OS would be a WORSE signal than the honest headless one.
_REDUCED_UA_PLATFORM_TOKEN = {
    "Windows": "Windows NT 10.0; Win64; x64",
    "Darwin": "Macintosh; Intel Mac OS X 10_15_7",
    "Linux": "X11; Linux x86_64",
}

_USER_AGENT_ARG_PREFIX = "--user-agent="
_BROWSER_VERSION_RE = re.compile(r"(\d+)\.\d+\.\d+\.\d+")
# How long we will wait to learn a binary's version, over EITHER transport: the
# pre-launch `<exe> --version` subprocess and the post-launch CDP
# `Browser.getVersion` ask one question, so they share one number (F-806).
_VERSION_PROBE_TIMEOUT_SECONDS = 10.0
_VERSION_MEMO_MAX_ENTRIES = 8

# ── The browser-version memo (F-806) ────────────────────────────────────────
# The mask above is only coherent while the version it was built from is the
# version that actually launches. Two things have to hold for that, and F-806
# was each of them failing in turn:
#
#   * the PROBE must read the binary that will run — not something beside it.
#     See `_windows_file_version`: guessing from the newest version-named
#     sibling directory answers with a staged update days before the browser
#     will run it.
#   * the MEMO must expire when that binary changes. The probe is memoized
#     because it runs on the spawn path, so it is keyed on the executable's
#     on-disk IDENTITY, never on its path alone: Chrome auto-updates IN PLACE,
#     so a path-keyed memo in a long-lived backend keeps advertising the version
#     the machine had when the process started.
#
# Either failure ends the same way: a masked UA claiming Chrome/150 while the
# browser (and its `sec-ch-ua` client hints) say 151 — a self-contradicting UA,
# i.e. exactly the WORSE tell this mask exists to avoid.
#
# ``None`` is memoized too: an executable whose version cannot be resolved must
# not re-pay for a failed subprocess on every spawn.
_ExecutableIdentity = tuple[int, int]
_BROWSER_VERSION_MEMO: dict[tuple[str, _ExecutableIdentity | None], str | None] = {}


def _executable_identity(executable: str) -> _ExecutableIdentity | None:
    """The executable's on-disk identity: ``(mtime_ns, size)``.

    One ``stat`` call, no subprocess — microseconds, so it can run on every spawn
    where the version probe itself cannot. The executable's own bytes are the
    WHOLE key because every branch of the probe answers from the executable
    itself: POSIX runs ``<exe> --version``, Windows reads ``<exe>``'s version
    resource. An earlier revision also hashed the parent directory's mtime, to
    chase the Windows sibling-directory scan; that scan is now only the fallback,
    and even there the directory component bought nothing but re-probes. During a
    pending update the staged directory is a version the browser will not run
    until its launcher stub is swapped — and swapping the stub moves this key
    anyway — so re-probing on the directory could only re-derive the same wrong
    guess, sooner. Off Windows it was pure cost: ``/usr/bin`` changes mtime on any
    package install, putting a blocking subprocess back on the spawn path.

    An unstattable executable yields ``None``, which degrades to the old
    path-only memo rather than failing the spawn.
    """
    try:
        exe_stat = Path(executable).stat()
    except OSError as error:
        debug_logger.log_debug("platform_utils", "_executable_identity", str(error))
        return None
    return (exe_stat.st_mtime_ns, exe_stat.st_size)


def _remember_browser_major_version(
    key: tuple[str, _ExecutableIdentity | None], major: str | None
) -> None:
    """Write *major* into the memo, evicting the oldest entry past the bound."""
    if (
        key not in _BROWSER_VERSION_MEMO
        and len(_BROWSER_VERSION_MEMO) >= _VERSION_MEMO_MAX_ENTRIES
    ):
        _BROWSER_VERSION_MEMO.pop(next(iter(_BROWSER_VERSION_MEMO)))
    _BROWSER_VERSION_MEMO[key] = major


def reset_browser_version_memo() -> None:
    """Forget every memoized browser version.

    THE way to invalidate by hand (tests, and ops after an out-of-band swap).
    Routine in-place upgrades need no call: the identity key expires itself.
    """
    _BROWSER_VERSION_MEMO.clear()


class _VsFixedFileInfo(ctypes.Structure):
    """The fixed (numeric) part of a Win32 ``VS_VERSIONINFO`` resource.

    Only the ``dwFileVersion*`` words are wanted, so nothing here has to touch
    the string table or its code pages.
    """

    _fields_ = (
        ("dwSignature", ctypes.c_uint32),
        ("dwStrucVersion", ctypes.c_uint32),
        ("dwFileVersionMS", ctypes.c_uint32),
        ("dwFileVersionLS", ctypes.c_uint32),
        ("dwProductVersionMS", ctypes.c_uint32),
        ("dwProductVersionLS", ctypes.c_uint32),
        ("dwFileFlagsMask", ctypes.c_uint32),
        ("dwFileFlags", ctypes.c_uint32),
        ("dwFileOS", ctypes.c_uint32),
        ("dwFileType", ctypes.c_uint32),
        ("dwFileSubtype", ctypes.c_uint32),
        ("dwFileDateMS", ctypes.c_uint32),
        ("dwFileDateLS", ctypes.c_uint32),
    )


_VS_FFI_SIGNATURE = 0xFEEF04BD


def _windows_file_version(executable: str) -> str | None:
    """Read ``executable``'s own embedded file-version resource, e.g.
    ``"150.0.7871.186"`` — or ``None`` if it has none / cannot be read.

    This is the executable answering for ITSELF, which is the whole point: it is
    the one Windows reading that tracks the binary that will actually run rather
    than what happens to be lying next to it (F-806). Non-Windows raises
    ``AttributeError`` on ``ctypes.windll`` and degrades to ``None``.
    """
    try:
        version_dll = ctypes.windll.version
        size = version_dll.GetFileVersionInfoSizeW(ctypes.c_wchar_p(executable), None)
        if not size:
            return None
        block = ctypes.create_string_buffer(size)
        loaded = version_dll.GetFileVersionInfoW(
            ctypes.c_wchar_p(executable), 0, size, block
        )
        fixed = ctypes.c_void_p()
        fixed_size = ctypes.c_uint()
        if not loaded or not version_dll.VerQueryValueW(
            block,
            ctypes.c_wchar_p("\\"),
            ctypes.byref(fixed),
            ctypes.byref(fixed_size),
        ):
            return None
        if fixed_size.value < ctypes.sizeof(_VsFixedFileInfo):
            return None
        info = ctypes.cast(fixed, ctypes.POINTER(_VsFixedFileInfo)).contents
        if info.dwSignature != _VS_FFI_SIGNATURE:
            return None
        high, low = info.dwFileVersionMS, info.dwFileVersionLS
    except (AttributeError, OSError, ValueError) as error:
        debug_logger.log_debug("platform_utils", "_windows_file_version", str(error))
        return None
    return f"{high >> 16}.{high & 0xFFFF}.{low >> 16}.{low & 0xFFFF}"


def _windows_newest_sibling_version_directory(executable: str) -> str | None:
    """FALLBACK: the newest version-named directory beside the binary.

    Every Chromium install keeps its build in a version-named directory next to
    the launcher stub, so this answers when the version resource cannot be read.
    It is a GUESS, and a knowably wrong one during an update: Chrome's updater
    lands the new directory days before it swaps the stub, so this reports a
    version the browser will not run until it next restarts. That is exactly the
    F-806 skew ``_windows_file_version`` exists to close — but it is kept as the
    floor so no machine gets a worse answer than it had before.
    """
    try:
        names = [
            entry.name for entry in Path(executable).parent.iterdir() if entry.is_dir()
        ]
    except OSError as error:
        debug_logger.log_debug(
            "platform_utils", "_windows_newest_sibling_version_directory", str(error)
        )
        return None
    majors = [m.group(1) for m in map(_BROWSER_VERSION_RE.fullmatch, names) if m]
    return max(majors, key=int) if majors else None


def _probe_browser_major_version(executable: str) -> str | None:
    """Ask the executable on disk what version it is. Uncached — see the memo.

    Windows deliberately does NOT shell out — ``chrome.exe --version`` hands the
    argument to an already-running Chrome ("Opening in existing browser
    session.") instead of printing anything — so it reads the binary's embedded
    version resource, falling back to the sibling-directory scan only when that
    resource is unreadable. Elsewhere ``<exe> --version`` prints e.g.
    ``Google Chrome 150.0.7871.186``, which likewise reports the binary that
    will actually run.
    """
    if platform.system() == "Windows":
        match = _BROWSER_VERSION_RE.fullmatch(_windows_file_version(executable) or "")
        # A zeroed resource is a stripped or repacked binary, not a browser
        # anyone ships — treat it as unreadable rather than mask as Chrome/0.
        if match and match.group(1) != "0":
            return match.group(1)
        return _windows_newest_sibling_version_directory(executable)

    try:
        completed = subprocess.run(  # noqa: S603  RELEASE-FIX-D (F-770)
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=_VERSION_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        debug_logger.log_debug(
            "platform_utils", "_probe_browser_major_version", str(error)
        )
        return None
    match = _BROWSER_VERSION_RE.search(completed.stdout or "")
    return match.group(1) if match else None


def resolve_browser_major_version(executable: str) -> str | None:
    """Return the major version of a Chromium-family executable, or ``None``.

    The probe runs on the spawn path, so it is memoized — but on the
    executable's on-disk identity, so an in-place upgrade expires the memo
    instead of surviving it (F-806). Cost per spawn is two ``stat`` calls on the
    hit path and one subprocess only when the binary has actually changed.
    """
    key = (executable, _executable_identity(executable))
    if key in _BROWSER_VERSION_MEMO:
        return _BROWSER_VERSION_MEMO[key]
    major = _probe_browser_major_version(executable)
    _remember_browser_major_version(key, major)
    return major


def record_launched_browser_major_version(
    executable: str, product: str | None
) -> str | None:
    """Reconcile the memo against the browser that ACTUALLY launched (F-806).

    ``Browser.getVersion``'s ``product`` field reports the running binary's own
    version and is NOT rewritten by ``--user-agent=`` — measured on Chrome 150:
    launching with ``--user-agent=…Chrome/1.0.0.0…`` still reported
    ``product: "Chrome/150.0.7871.186"`` while ``userAgent`` carried the
    override. It is therefore the one authoritative post-launch reading, and the
    only way to *know* — rather than predict — which browser the mask is
    describing.

    A disagreement means the pre-launch probe was wrong for this executable: an
    upgrade that landed between probe and launch, a second install, or a
    launcher that chose a different binary. Whatever the cause, the launched
    browser is the truth, so it is written back and every later spawn masks
    correctly. The disagreement is logged rather than silently repaired: the
    instance already running kept the flag it launched with, and a UA that
    contradicts its own ``sec-ch-ua`` is a stealth defect, not a cosmetic one.
    """
    match = _BROWSER_VERSION_RE.search(product or "")
    if not match:
        return None
    major = match.group(1)
    key = (executable, _executable_identity(executable))
    previous = _BROWSER_VERSION_MEMO.get(key)
    if previous and previous != major:
        debug_logger.log_warning(
            "platform_utils",
            "record_launched_browser_major_version",
            f"version skew: the masked User-Agent was built for Chrome/{previous} "
            f"but {executable!r} launched as {product!r}, so this instance "
            f"advertises {previous} while running {major} — later spawns will use "
            f"{major} (F-806)",
        )
    _remember_browser_major_version(key, major)
    return major


async def reconcile_launched_browser_version(tab: Tab, executable: str) -> str | None:
    """Read the launched browser's real version over CDP and reconcile the memo.

    Guarded the way ``window_sizing`` guards its measurement: a spawn must not
    fail because a diagnostic probe did, so a failure degrades to ``None``
    (leaving the memo exactly as the pre-launch probe left it) rather than
    taking the browser down with it. The guard covers the WRITE-BACK as well as
    the CDP call, because the write-back can raise too — ``_executable_identity``
    only swallows ``OSError`` and the skew warning is not guarded at all — and a
    contract that says "never fails a spawn" has to mean the whole reconciliation.

    The CDP read is BOUNDED, which is the condition this fix's re-land carried:
    it is the first await in ``_apply_post_launch``, on every spawn, and against
    a stale or dead connection an unguarded ``tab.send`` never returns — so
    unbounded, a diagnostic probe could wedge the spawn it must not even fail.
    ``server.py``'s ``_with_cdp_timeout`` is not reachable from here (convention
    1: no module under ``embedded/`` imports ``server``), so the bound is a local
    ``asyncio.wait_for`` on ``_VERSION_PROBE_TIMEOUT_SECONDS`` — the same 10s the
    pre-launch subprocess probe already waits for the same answer. Expiry cancels
    the pending send and degrades to ``None`` through the guard below, leaving
    the memo exactly as the pre-launch probe left it.

    The unbounded awaits that FOLLOW this one in ``_apply_post_launch`` are
    untouched here: they are pre-existing and belong to their own finding.
    """
    try:
        version = await asyncio.wait_for(
            tab.send(cdp.browser.get_version()), _VERSION_PROBE_TIMEOUT_SECONDS
        )
        product = version[1] if version and len(version) > 1 else None
        return record_launched_browser_major_version(executable, product)
    except Exception as error:  # noqa: BLE001  PERMANENT(a diagnostic probe must never fail a spawn - F-806)
        debug_logger.log_warning(
            "platform_utils",
            "reconcile_launched_browser_version",
            f"reconciliation failed ({type(error).__name__}: {error}); the "
            "masked User-Agent could not be checked against the running browser "
            f"(the CDP read is bounded at {_VERSION_PROBE_TIMEOUT_SECONDS:.0f}s)",
        )
        return None


def build_reduced_user_agent(executable: str) -> str | None:
    """Build this executable's own reduced User-Agent, without the headless token.

    Returns ``None`` (meaning "do not mask") on an unrecognized platform or when
    the version cannot be resolved — masking with a wrong version would be worse
    than not masking at all.
    """
    platform_token = _REDUCED_UA_PLATFORM_TOKEN.get(platform.system())
    if not platform_token:
        return None
    major = resolve_browser_major_version(executable)
    if not major:
        return None
    agent = (
        f"Mozilla/5.0 ({platform_token}) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{major}.0.0.0 Safari/537.36"
    )
    if "edge" in Path(executable).name.lower():
        # Edge appends its own token after Safari/537.36. Dropping it while
        # sec-ch-ua still advertises the "Microsoft Edge" brand would be a
        # sharper tell than the headless token this mask removes.
        agent += f" Edg/{major}.0.0.0"
    return agent


def _apply_default_user_agent(args: list[str]) -> list[str]:
    """Append the masked default ``--user-agent=`` unless the caller supplied one.

    An explicit caller ``user_agent`` reaches here already rendered as a
    ``--user-agent=`` arg, so the presence check is what makes an explicit value
    win. The flag is process-wide: unlike a per-target CDP override it covers
    every tab, worker and subresource of the launched browser — including tabs
    the page itself opens — and it reaches the real HTTP request header, which is
    the vector a server-side bot check reads first.
    """
    if any(arg.lower().startswith(_USER_AGENT_ARG_PREFIX) for arg in args):
        return args
    executable = check_browser_executable()
    agent = build_reduced_user_agent(executable) if executable else None
    if not agent:
        debug_logger.log_warning(
            "platform_utils",
            "default_user_agent",
            f"could not derive a masked User-Agent for {executable!r}; a headless "
            "launch will advertise HeadlessChrome (F-770)",
        )
        return args
    return [*args, f"{_USER_AGENT_ARG_PREFIX}{agent}"]


def merge_browser_args(user_args: list[str] | None = None) -> tuple:
    """
    Merge user-provided browser arguments with platform-specific required arguments.
    Strips any args that would compromise stealth detection, and supplies the
    masked default User-Agent (F-770) when the caller did not choose one.

    Args:
        user_args: User-provided browser arguments

    Returns:
        tuple: (combined_args, stealth_warnings) — warnings list may be empty
    """
    user_args = user_args or []
    clean_args, stealth_warnings = filter_stealth_args(user_args)
    required_args = get_required_sandbox_args()

    combined_args = list(clean_args)

    for arg in required_args:
        if arg not in combined_args:
            combined_args.append(arg)

    return _apply_default_user_agent(combined_args), stealth_warnings


def get_platform_info() -> dict:
    """
    Get comprehensive platform information for debugging.

    Returns:
        dict: Platform information including OS, architecture, privileges, etc.
    """
    return {
        "system": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "architecture": platform.architecture(),
        "python_version": sys.version,
        "is_root": is_running_as_root(),
        "is_container": is_running_in_container(),
        "required_sandbox_args": get_required_sandbox_args(),
        "user_id": getattr(os, "getuid", lambda: "N/A")(),
        "effective_user_id": getattr(os, "geteuid", lambda: "N/A")(),
        "environment_vars": {
            "DISPLAY": get_settings().display,
            "container": get_settings().container,
            "KUBERNETES_SERVICE_HOST": get_settings().kubernetes_service_host,
            "USER": get_settings().user,
            "USERNAME": get_settings().username,
        },
    }


def check_browser_executable() -> str | None:
    """
    Find a compatible browser executable on the system.
    Searches for Chrome, Chromium, and Microsoft Edge in order of preference.

    Returns:
        Optional[str]: Path to browser executable or None if not found
    """
    system = platform.system().lower()

    if system == "windows":
        possible_paths = [
            # Chrome paths
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"C:\Users\{}\AppData\Local\Google\Chrome\Application\chrome.exe".format(
                get_settings().username or ""
            ),
            # Chromium paths
            r"C:\Program Files\Chromium\Application\chromium.exe",
            # Microsoft Edge paths
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Users\{}\AppData\Local\Microsoft\Edge\Application\msedge.exe".format(
                get_settings().username or ""
            ),
        ]
    elif system == "darwin":
        possible_paths = [
            # Chrome paths
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            # Chromium paths
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            # Microsoft Edge paths
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ]
    else:
        possible_paths = [
            # Chrome paths
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            # Chromium paths
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/snap/bin/chromium",
            "/usr/local/bin/chrome",
            # Microsoft Edge paths
            "/usr/bin/microsoft-edge-stable",
            "/usr/bin/microsoft-edge",
            "/usr/bin/microsoft-edge-beta",
            "/usr/bin/microsoft-edge-dev",
            "/snap/bin/microsoft-edge",
            "/opt/microsoft/msedge/msedge",
        ]

    # First check static paths
    for path in possible_paths:
        try:
            if Path(path).is_file() and os.access(path, os.X_OK):
                return path
        except (OSError, PermissionError):
            # Handle potential permission issues on certain systems
            continue

    # Fallback: search using 'which' command
    browser_names = [
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "chrome",
        "microsoft-edge-stable",
        "microsoft-edge",
        "msedge",
    ]
    for name in browser_names:
        try:
            found_path = shutil.which(name)
            if (
                found_path
                and Path(found_path).is_file()
                and os.access(found_path, os.X_OK)
            ):
                return found_path
        except Exception as e:  # noqa: BLE001  plan_M10a (F-181 row 14)
            debug_logger.log_debug("platform_utils", "check_browser_executable", str(e))
            continue

    return None


def validate_browser_environment() -> dict:
    """
    Validate the browser environment and return status information.
    Checks for Chrome, Chromium, and Microsoft Edge availability.

    Returns:
        dict: Environment validation results
    """
    browser_path = check_browser_executable()
    platform_info = get_platform_info()

    issues = []
    warnings = []
    recommendations = []

    if not browser_path:
        issues.append(
            "Compatible browser executable not found "
            "(Chrome, Chromium, or Microsoft Edge)"
        )
        recommendations.append(
            "Install a compatible browser (Chrome, Chromium, or Microsoft Edge)"
        )
    else:
        # Identify which browser was found
        browser_type = "Unknown"
        if "chrome" in browser_path.lower():
            browser_type = "Google Chrome"
        elif "chromium" in browser_path.lower():
            browser_type = "Chromium"
        elif "edge" in browser_path.lower() or "msedge" in browser_path.lower():
            browser_type = "Microsoft Edge"

        # Add Edge-specific warnings if applicable
        if browser_type == "Microsoft Edge" and platform_info["system"] == "Linux":
            warnings.append(
                "Microsoft Edge on Linux detected - ensure all "
                "dependencies are installed"
            )

    if platform_info["is_root"]:
        warnings.append("Running as root/administrator - sandbox will be disabled")

    if platform_info["is_container"]:
        warnings.append("Running in container - additional arguments will be added")

    if platform_info["system"] not in ["Windows", "Linux", "Darwin"]:
        warnings.append(f"Untested platform: {platform_info['system']}")

    return {
        "browser_executable": browser_path,
        "browser_type": browser_type if browser_path else None,
        "platform_info": platform_info,
        "issues": issues,
        "warnings": warnings,
        "recommendations": recommendations,
        "is_ready": len(issues) == 0,
        "recommended_args": get_required_sandbox_args(),
    }
