"""F-1025 follow-ups N1-N3 (T-6403): what the first deny-list and the first
header redaction missed, found by the #274 re-review.

N1  Win32 drops trailing dots and spaces from every path segment, so a
    not-yet-existing ``~/.kube.`` IS ``~/.kube`` and was created under a name
    the string comparison did not see.
N2  ``x_api_key``, ``api_key``, ``x-authorization``, ``cf-access-jwt-assertion``,
    ``x-session-id`` and ``password`` headers went out in an export in the clear.
N3  a Chrome profile ``spawn_browser(user_data_dir=<any path>)`` put outside the
    session root, ``~/.pgpass``, ``~/.vault-token`` and ``/proc/<pid>/environ``.

The pure-string halves run on every OS; the end-to-end halves are gated to the
OS whose file-system rule they pin.
"""

import os
import sys
from pathlib import Path

import pytest

from stealth_chrome_devtools_mcp.embedded import (
    backend_registry,
    network_export,
    path_confinement,
    profile_seed,
)
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

WINDOWS_ONLY = pytest.mark.skipif(
    sys.platform != "win32", reason="Win32 drops trailing dots and spaces"
)
POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="/proc is a Linux file system"
)
LINUX_ONLY = pytest.mark.skipif(
    not Path("/proc/self/environ").exists(), reason="needs a Linux /proc"
)

SECRET = "sk-live-0123456789abcdef"


@pytest.fixture()
def home(tmp_path, monkeypatch) -> Path:
    """A throwaway HOME, so ``~/.kube`` is ours and the real one is never named."""
    root = tmp_path / "home"
    root.mkdir()
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(root))
    monkeypatch.setattr(backend_registry, "STATE_DIR", root / ".stealth-mcp")
    return root


def _refused(path: Path, *, tool: str = "export_debug_logs") -> ToolError:
    with pytest.raises(ToolError) as err:
        path_confinement.refuse_secret_path(path, tool=tool, action="write to")
    return err.value


# ------------------------------------------------------------------- N1 -------


@pytest.mark.parametrize(
    ("spelled", "win32_reads"),
    [
        (r"C:\Users\u\.kube.\config", r"C:\Users\u\.kube\config"),
        (r"C:\Users\u\.gnupg.", r"C:\Users\u\.gnupg"),
        (r"C:\Users\u\.kube \config", r"C:\Users\u\.kube\config"),
        (r"C:\Users\u\.aws. .\credentials", r"C:\Users\u\.aws\credentials"),
        ("C:/Users/u/.ssh../id_rsa", "C:/Users/u/.ssh/id_rsa"),
        (r"C:\Users\u\.config\gcloud. \x", r"C:\Users\u\.config\gcloud\x"),
        # Already canonical, or navigation, or only dots: untouched.
        (r"C:\Users\u\.kube\config", r"C:\Users\u\.kube\config"),
        (r"C:\Users\u\.\..\v", r"C:\Users\u\.\..\v"),
        (r"C:\Users\u\...\v", r"C:\Users\u\...\v"),
        (r"C:\Users\u\notes.txt", r"C:\Users\u\notes.txt"),
    ],
)
def test_win_canonical_drops_trailing_dots_and_spaces_per_segment(spelled, win32_reads):
    assert path_confinement._win_canonical(spelled) == win32_reads


@WINDOWS_ONLY
@pytest.mark.parametrize(
    ("parts", "rule"),
    [
        ((".kube.", "config"), "~/.kube"),
        ((".kube ", "config"), "~/.kube"),
        ((".gnupg.",), "~/.gnupg"),
        ((".gnupg. .", "private-keys-v1.d", "k.key"), "~/.gnupg"),
        ((".ssh.", "id_ed25519"), "~/.ssh"),
        ((".aws. ", "credentials"), "~/.aws"),
        ((".docker.", "config.json"), "~/.docker"),
        ((".netrc.",), "netrc"),
        ((".env ",), ".env files"),
        ((".pgpass.",), "pgpass"),
    ],
)
def test_a_trailing_dot_or_space_does_not_hide_a_denied_location(home, parts, rule):
    target = home.joinpath(*parts)

    refusal = _refused(target)

    assert rule in str(refusal)
    assert not target.exists()


@WINDOWS_ONLY
def test_precreate_private_does_not_create_a_denied_directory_by_a_dotted_name(home):
    target = home / ".kube." / "config"

    with pytest.raises(ToolError):
        path_confinement.precreate_private(target, tool="take_screenshot")

    assert not (home / ".kube.").exists()
    assert not (home / ".kube").exists()


@WINDOWS_ONLY
def test_a_trailing_dot_on_an_ordinary_directory_is_still_allowed(home):
    target = home / "notes." / "a.txt"

    assert path_confinement.secret_location_rule(target) is None


# ------------------------------------------------------------------- N2 -------

REDACTED_NAMES = [
    "x_api_key",
    "X_API_KEY",
    "api_key",
    "Api-Key",
    "x-authorization",
    "X_Authorization",
    "cf-access-jwt-assertion",
    "CF_Access_JWT_Assertion",
    "x-session-id",
    "x_session_id",
    "password",
    "X-Password",
    "x-passwd",
    # The names the first rule already caught keep being caught, spelled both ways.
    "x-api-key",
    "x_auth_token",
    "x_csrf_token",
    "Cookie",
    "Set-Cookie",
    "Authorization",
    "Proxy-Authorization",
]
VISIBLE_NAMES = [
    "content-type",
    "Content-Type",
    "accept",
    "Accept",
    "user-agent",
    "User-Agent",
    "accept-language",
    "cache-control",
    "x-request-id",
    "content_length",
]


@pytest.mark.parametrize("name", REDACTED_NAMES)
def test_every_credential_spelling_is_redacted(name):
    assert network_export.is_credential_header(name) is True, name
    out = network_export.redacted_headers({name: SECRET, "Accept": "*/*"}, True)
    assert out[name] == network_export.REDACTED
    assert SECRET not in repr(out)


@pytest.mark.parametrize("name", VISIBLE_NAMES)
def test_harmless_headers_stay_visible(name):
    assert network_export.is_credential_header(name) is False, name
    assert network_export.redacted_headers({name: "v"}, True) == {name: "v"}


def test_hide_false_keeps_even_the_newly_recognised_names():
    headers = dict.fromkeys(REDACTED_NAMES, SECRET)

    assert network_export.redacted_headers(headers, False) == headers


# ------------------------------------------------------------------- N3 -------


@pytest.mark.parametrize(
    "witness", [profile_seed.MARKER_NAME, *profile_seed.LOGIN_WITNESSES]
)
def test_a_chrome_profile_outside_the_session_root_is_refused(home, tmp_path, witness):
    profile = tmp_path / "my-own-profile-dir"
    (profile / witness).parent.mkdir(parents=True)
    (profile / witness).write_text("x", encoding="utf-8")

    # The directory itself, a file in it, a file in a sub-folder, and a
    # destination that does not exist yet: all are "in the profile".
    for target in (
        profile,
        profile / "Default" / "Preferences",
        profile / "Default" / "Deep" / "er" / "file.json",
        profile / "new-export.json",
    ):
        assert "Chrome profile" in str(_refused(target)), target


def test_the_clone_marker_a_real_clone_gets_marks_it_as_a_profile(home, tmp_path):
    profile = tmp_path / "elsewhere" / "named-profile"
    profile.mkdir(parents=True)
    profile_seed.write_marker(
        profile, source=tmp_path, source_kind="explicit", seeded_from="default"
    )

    assert "Chrome profile" in str(_refused(profile / "Cookies"))


def test_a_directory_with_no_marker_or_login_store_is_not_a_profile(home, tmp_path):
    folder = tmp_path / "exports"
    folder.mkdir()
    (folder / "Default").mkdir()
    (folder / "Default" / "notes.txt").write_text("x", encoding="utf-8")

    assert path_confinement.secret_location_rule(folder / "log.json") is None
    assert (
        path_confinement.secret_location_rule(folder / "Default" / "notes.txt") is None
    )


@pytest.mark.parametrize(
    ("parts", "rule"),
    [
        ((".pgpass",), "pgpass"),
        ((".vault-token",), "vault-token"),
    ],
)
def test_dot_files_that_hold_passwords_and_tokens_are_denied(home, parts, rule):
    target = home.joinpath(*parts)
    target.write_text(SECRET, encoding="utf-8")

    assert rule in str(_refused(target))
    # An ordinary neighbour is untouched by the new entries.
    assert path_confinement.secret_location_rule(home / ".pgpass-notes.txt") is None
    assert path_confinement.secret_location_rule(home / "vault-token.txt") is None


def test_the_windows_postgres_password_file_location_is_denied(home):
    target = home / "AppData" / "Roaming" / "postgresql" / "pgpass.conf"

    assert "postgresql" in str(_refused(target))


@pytest.mark.parametrize(
    ("real", "is_environ"),
    [
        ("/proc/self/environ", True),
        ("/proc/thread-self/environ", True),
        ("/proc/1/environ", True),
        ("/proc/4242/environ", True),
        ("/proc/4242/task/4243/environ", True),
        ("/proc/self/status", False),
        ("/proc/self/environment", False),
        ("/proc/self/environ.bak", False),
        ("/proc/self/fd/environ", False),
        ("/proc/environ", False),
        ("/home/u/proc/self/environ", False),
        ("/tmp/environ", False),
    ],
)
def test_proc_environ_path_names_only_process_environments(real, is_environ):
    assert path_confinement.proc_environ_path(real) is is_environ


@pytest.mark.parametrize(
    "spelling",
    ["/proc/self/environ", "/proc/4242/environ", "/proc/12/task/34/environ"],
)
def test_the_deny_list_applies_the_proc_environ_rule_on_every_os(monkeypatch, spelling):
    # Resolution is what differs by OS (a Windows path has a drive); with it out
    # of the way the rule's WIRING into the deny-list is checkable anywhere.
    monkeypatch.setattr(path_confinement, "_real", os.fspath)

    assert "environ" in str(path_confinement.secret_location_rule(spelling))


@LINUX_ONLY
@POSIX_ONLY
@pytest.mark.parametrize(
    "spelling", ["/proc/self/environ", "/proc/thread-self/environ"]
)
def test_a_live_proc_environ_is_refused_under_every_spelling(spelling):
    refusal = _refused(Path(spelling))

    assert "environ" in str(refusal)
    assert SECRET not in str(refusal)


@LINUX_ONLY
@POSIX_ONLY
def test_the_pid_spelling_of_proc_environ_is_refused_too():
    assert "environ" in str(_refused(Path(f"/proc/{os.getpid()}/environ")))


@LINUX_ONLY
@POSIX_ONLY
def test_other_proc_files_are_not_swept_up_by_the_environ_rule():
    assert path_confinement.secret_location_rule("/proc/self/status") is None
