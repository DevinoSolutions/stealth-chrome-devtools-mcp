"""Platform-specific utility functions for browser automation."""

import ctypes
import os
import platform
import shutil
import sys
from pathlib import Path

from debug_logger import debug_logger

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


def merge_browser_args(user_args: list[str] | None = None) -> tuple:
    """
    Merge user-provided browser arguments with platform-specific required arguments.
    Strips any args that would compromise stealth detection.

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

    return combined_args, stealth_warnings


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
