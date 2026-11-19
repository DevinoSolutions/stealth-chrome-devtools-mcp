"""THE one home for "can this browser's page be trusted to be its host?" (T-19366).

The vault types a password only after checking that the page is ``https`` on a host
named in the entry. That proof means nothing if the browser itself lies about hosts
or certificates: with ``--host-resolver-rules="MAP accounts.example.com
127.0.0.1:443"`` and ``--ignore-certificate-errors`` the address bar says the right
host and an attacker's server answers. So a login is refused in a browser that was
started with anything that changes **name resolution, proxying, certificate
checking or the same-origin policy**, or that can run another party's code in the
page.

**Spellings.** A deny list of ``--flag`` strings is the wrong shape, because Chrome's
own parser (``base::CommandLine``) accepts a switch as ``--name``, ``-name`` or, on
Windows, ``/name``, with the name case-insensitive on Windows, and a value after
``=``. :func:`switch_name` reduces every argument to that name before it is looked
up, on every platform (a refusal is the safe error).

**Where the list comes from.** Not from memory: it is the set of switch names that
Chrome 158's own binary contains (extracted from ``chrome.dll`` with a string scan
and filtered by ``proxy``, ``cert``, ``ssl``, ``resolver``, ``insecure``,
``security``, ``extension`` and ``remote``), read against the switch documentation
(<https://peter.sh/experiments/chromium-command-line-switches/>) and Chromium's
``net/base/net_switches``, ``services/network/public/cpp/network_switches.cc``,
``content/public/common/content_switches.cc`` and
``chrome/common/chrome_switches.cc``. ``--no-proxy-server`` is deliberately absent
(it disables proxies) and so is ``--user-agent``.

**What an allow list would need.** Chrome's own argv holds a hundred benign switches
and a re-attached browser has nothing but that argv, so a list of the safe ones
cannot be kept; refusing the dangerous names is what can be.

**Beyond the command line**, three routes change the same things and are read
too: the environment of the browser process (``SSLKEYLOGFILE``; the proxy variables
on Linux and macOS, where Chrome honours them), the profile's ``Local State``
(``chrome://flags`` experiments are switches), and enterprise policy (Windows
registry, Linux ``policies/managed``): proxy, insecure-origin and force-installed
extension policies. A browser whose process environment cannot be read is refused.

**Not covered:** the operating system's own proxy and certificate store (a
machine-wide proxy with a trusted root can read any TLS session), and an extension
the person installed in the profile, which can read any page. The README says so.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

#: Switch names (no prefix, lower case) a login is refused under.
REFUSED_SWITCHES = frozenset(
    {
        # name resolution
        "host-resolver-rules",
        "host-rules",
        "dns-txt-resolver-url-prefix",
        # proxying
        "proxy-server",
        "proxy-pac-url",
        "proxy-auto-detect",
        "proxy-bypass-list",
        "winhttp-proxy-resolver",
        "use-system-proxy-resolver",
        # certificate checking and transport security
        "ignore-certificate-errors",
        "ignore-certificate-errors-spki-list",
        "ignore-urlfetcher-cert-requests",
        "allow-insecure-localhost",
        "unsafely-treat-insecure-origin-as-secure",
        "use-mock-cert-verifier-for-testing",
        "mock-cert-verifier-default-result-for-testing",
        "ssl-version-min",
        "ssl-version-max",
        "ssl-key-log-file",
        "cipher-suite-blacklist",
        "origin-to-force-quic-on",
        # same-origin policy and site isolation
        "disable-web-security",
        "allow-running-insecure-content",
        "allow-file-access-from-files",
        "disable-site-isolation-trials",
        "test-type",
        # another party's code in the page, or another client on the browser
        "load-extension",
        "disable-extensions-except",
        "load-component-extension",
        "remote-debugging-pipe",
        "remote-debugging-address",
        "remote-allow-origins",
        # a record of the traffic
        "log-net-log",
        "net-log-capture-mode",
    }
)

#: ``--name`` forms of the above, for messages and for callers that list them.
FORBIDDEN_FLAGS = tuple(f"--{name}" for name in sorted(REFUSED_SWITCHES))

#: Environment variables a browser process honours that change the same things.
_ALWAYS_ENV = ("sslkeylogfile",)
_POSIX_PROXY_ENV = ("http_proxy", "https_proxy", "all_proxy")

#: Enterprise policies that change them (names in Chrome's policy templates).
POLICIES = frozenset(
    {
        "proxymode",
        "proxyserver",
        "proxypacurl",
        "proxybypasslist",
        "proxysettings",
        "overridesecurityrestrictionsoninsecureorigin",
        "insecurecontentallowedforurls",
        "certificatetransparencyenforcementdisabledforurls",
        "certificatetransparencyenforcementdisabledforcas",
        "sslversionmin",
        "extensioninstallforcelist",
    }
)
_POLICY_DIRECTORIES = (
    Path("/etc/opt/chrome/policies/managed"),
    Path("/etc/chromium/policies/managed"),
)


def switch_name(argument: str) -> str | None:
    """The switch ``argument`` is, the way Chrome's parser reads it, or ``None``.

    One prefix (``--``, ``-`` or ``/``) is removed, the value after ``=`` is cut
    off, and the name is lower-cased. A bare prefix, or an argument with no name,
    is no switch."""
    text = argument.strip()
    for prefix in ("--", "-", "/"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    else:
        return None
    name = text.partition("=")[0].strip().lower()
    return name or None


def switch_refusal(options: object, cmdline: list[str]) -> str | None:
    """Why the browser's launch arguments forbid a login, or ``None``."""
    if getattr(options, "proxy", None):
        return "this browser was started with a proxy; vault logins are refused in it"
    for argument in cmdline:
        name = switch_name(argument)
        if name in REFUSED_SWITCHES:
            return (
                f"this browser was started with --{name}; "
                "vault logins are refused in it"
            )
    return None


def environment_refusal(environment: dict[str, str] | None) -> str | None:
    """Why the browser process's environment forbids a login, or ``None``.

    ``None`` as ``environment`` means it could not be read, which is a refusal."""
    if environment is None:
        return (
            "the browser's environment could not be read, so it cannot be shown "
            "free of proxy and key-logging overrides; vault logins are refused"
        )
    names = {key.lower(): key for key in environment}
    watched = _ALWAYS_ENV + (() if sys.platform == "win32" else _POSIX_PROXY_ENV)
    for name in watched:
        if name in names and environment[names[name]].strip():
            return (
                f"this browser's environment sets {names[name]}; vault logins are "
                "refused in it"
            )
    return None


def profile_refusal(user_data_dir: str | None) -> str | None:
    """Why the profile's ``Local State`` forbids a login (``chrome://flags``
    experiments are command-line switches by another name), or ``None``."""
    if not user_data_dir:
        return None
    try:
        state = json.loads(
            (Path(user_data_dir) / "Local State").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    browser = state.get("browser") if isinstance(state, dict) else None
    enabled = (
        browser.get("enabled_labs_experiments") if isinstance(browser, dict) else None
    )
    if enabled:
        return (
            "this browser's profile has chrome://flags experiments turned on; "
            "vault logins are refused in it"
        )
    return None


def policy_refusal(
    windows_policy_names: set[str] | None = None,
    directories: tuple[Path, ...] = _POLICY_DIRECTORIES,
) -> str | None:
    """Why an enterprise policy forbids a login, or ``None``.

    ``windows_policy_names`` is injected by tests; on Windows it is read from the
    registry, elsewhere from the managed-policy JSON files."""
    found = windows_policy_names
    if found is None:
        found = _windows_policy_names() if sys.platform == "win32" else set()
    for directory in directories:
        try:
            files = sorted(directory.glob("*.json"))
        except OSError:
            continue
        for file in files:
            try:
                document = json.loads(file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(document, dict):
                found |= {str(key).lower() for key in document}
    hit = sorted(found & POLICIES)
    if hit:
        return (
            f"this machine's browser policy sets {hit[0]}; vault logins are "
            "refused in it"
        )
    return None


def _windows_policy_names() -> set[str]:
    if sys.platform != "win32":
        return set()
    import winreg

    found: set[str] = set()
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for path in (r"SOFTWARE\Policies\Google\Chrome", r"SOFTWARE\Policies\Chromium"):
            try:
                key = winreg.OpenKey(hive, path)
            except OSError:
                continue
            with key:
                index = 0
                while True:
                    try:
                        found.add(winreg.EnumValue(key, index)[0].lower())
                    except OSError:
                        break
                    index += 1
                index = 0
                while True:
                    try:
                        found.add(winreg.EnumKey(key, index).lower())
                    except OSError:
                        break
                    index += 1
    return found


def process_environment(pid: int) -> dict[str, str] | None:
    """The environment of process ``pid``, or ``None`` when it cannot be read."""
    import psutil

    try:
        return dict(psutil.Process(pid).environ())
    except (psutil.Error, OSError):
        return None


def user_data_dir_of(cmdline: list[str]) -> str | None:
    """The ``--user-data-dir`` a browser was started with, read from its argv."""
    for argument in cmdline:
        if switch_name(argument) == "user-data-dir":
            value = argument.partition("=")[2].strip().strip('"')
            return os.path.expandvars(value) or None
    return None
