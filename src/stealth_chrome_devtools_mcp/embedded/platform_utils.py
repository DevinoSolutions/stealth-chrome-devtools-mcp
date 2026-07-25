"""Platform-specific utility functions for browser automation."""

import ctypes
import os
import platform
import re
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

from stealth_chrome_devtools_mcp.embedded.debug_logger import debug_logger
from stealth_chrome_devtools_mcp.settings import get_settings


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
_VERSION_PROBE_TIMEOUT_SECONDS = 10.0


@lru_cache(maxsize=8)
def resolve_browser_major_version(executable: str) -> str | None:
    """Return the major version of a Chromium-family executable, or ``None``.

    Cached per executable path: this runs on the spawn path, so an uncached
    subprocess per spawn would be a real performance regression.

    Windows deliberately does NOT shell out — ``chrome.exe --version`` hands the
    argument to an already-running Chrome ("Opening in existing browser
    session.") instead of printing anything, so the version is read from the
    version-named directory every Chromium install keeps beside its binary.
    Elsewhere ``<exe> --version`` prints e.g. ``Google Chrome 150.0.7871.186``.
    """
    if platform.system() == "Windows":
        try:
            names = [
                entry.name
                for entry in Path(executable).parent.iterdir()
                if entry.is_dir()
            ]
        except OSError as error:
            debug_logger.log_debug(
                "platform_utils", "resolve_browser_major_version", str(error)
            )
            return None
        majors = [m.group(1) for m in map(_BROWSER_VERSION_RE.fullmatch, names) if m]
        return max(majors, key=int) if majors else None

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
            "platform_utils", "resolve_browser_major_version", str(error)
        )
        return None
    match = _BROWSER_VERSION_RE.search(completed.stdout or "")
    return match.group(1) if match else None


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
