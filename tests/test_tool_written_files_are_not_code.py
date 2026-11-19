"""A file a tool writes is never code the machine would run (T-19366, fourth review F1b).

``export_debug_logs`` writes bytes an agent influenced (a failing tool echoes its
argument into the log ring, newline and all, and a pickle holds strings raw), and the
old deny-list only protected what a path HOLDS. A file named ``evil.pth`` in a
``site-packages`` directory is a line ``import ...`` that every Python on the machine
runs when it starts, the backend and ``stealthy login unlock`` included. The rule is in
``path_confinement`` (``refuse_write_path``), used by EVERY tool that writes a path the
caller named: these tests drive the tools and the one function, and check ordinary
files still work. A READ of a caller's path (``import_network_data``) takes the
secret-location rule too.

Nothing here executes the exported file except the control, which proves the harness
would notice if it did.
"""

from __future__ import annotations

import os
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest

from fakes import call_tool
from stealth_chrome_devtools_mcp.embedded import (
    backend_registry,
    path_confinement,
)
from stealth_chrome_devtools_mcp.embedded.debug_logger import DebugLogger
from stealth_chrome_devtools_mcp.embedded.network_interceptor import (
    NetworkInterceptor,
)
from stealth_chrome_devtools_mcp.embedded.tool_errors import ToolError

WINDOWS_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="Win32 name rules")


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """A throwaway HOME, so the person's real startup folder is never named."""
    root = tmp_path / "home"
    root.mkdir()
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(root))
    monkeypatch.setattr(backend_registry, "STATE_DIR", root / ".stealth-mcp")
    return root


@pytest.fixture
def usersite(tmp_path, monkeypatch) -> Path:
    """A scratch directory standing in for the user's site-packages."""
    scratch = tmp_path / "usersite"
    scratch.mkdir()
    monkeypatch.setattr(
        path_confinement.site, "getusersitepackages", lambda: str(scratch)
    )
    return scratch


def _refused(path, *, tool="export_debug_logs"):
    with pytest.raises(ToolError) as caught:
        path_confinement.refuse_write_path(path, tool=tool)
    return str(caught.value)


# -------------------------------------------------- the reviewer's steps 1-3


def _logger_that_holds(text: str) -> DebugLogger:
    log = DebugLogger()
    log.log_info("navigate", "go", text)
    return log


def _runs_site_dir(directory: Path) -> None:
    subprocess.run(  # noqa: S603  PERMANENT(our own interpreter on a scratch dir)
        [
            sys.executable,
            "-I",
            "-c",
            "import site, sys; site.addsitedir(sys.argv[1])",
            str(directory),
        ],
        check=True,
        timeout=60,
    )


def test_the_control_proves_a_pth_line_would_run(tmp_path):
    """If this fails the harness cannot tell, and the tests below prove nothing."""
    scratch, marker = tmp_path / "ctl", tmp_path / "ctl-marker"
    scratch.mkdir()
    (scratch / "evil.pth").write_text(
        f"import pathlib; pathlib.Path({str(marker)!r}).write_text('executed')\n"
    )
    _runs_site_dir(scratch)
    assert marker.exists()


@pytest.mark.parametrize("fmt", ["pickle", "json", "gzip-pickle"])
@pytest.mark.parametrize("name", ["evil.pth", "evil.txt"])
async def test_a_debug_log_cannot_be_written_into_a_site_directory(
    patched_server, home, usersite, tmp_path, fmt, name
):
    """Step 1: a ring entry carrying a line that starts with ``import``. Step 2:
    ``export_debug_logs`` to a site directory. Step 3: Python would run it at start.
    The export is refused by name (.pth) and by place (any file there)."""
    marker = tmp_path / "executed"
    payload = f"x\nimport pathlib; pathlib.Path({str(marker)!r}).write_text('1')\n"
    srv = patched_server(debug_logger=_logger_that_holds(payload))
    with pytest.raises(ToolError):
        await call_tool(
            srv,
            "export_debug_logs",
            filename=str(usersite / name),
            format=fmt,
            include_all=True,
        )
    assert not any(usersite.iterdir()), "a file was written into the site directory"
    _runs_site_dir(usersite)
    assert not marker.exists()


async def test_the_same_export_to_an_ordinary_folder_still_works(
    patched_server, home, tmp_path
):
    srv = patched_server(debug_logger=_logger_that_holds("hello"))
    for fmt, name in (("json", "log.json"), ("pickle", "log.pkl")):
        await call_tool(
            srv,
            "export_debug_logs",
            filename=str(tmp_path / name),
            format=fmt,
            include_all=True,
        )
        assert (tmp_path / name).stat().st_size > 0


async def test_a_pickle_named_as_code_is_refused_anywhere(
    patched_server, home, tmp_path
):
    srv = patched_server(debug_logger=_logger_that_holds("hello"))
    for name in ("evil.pth", "evil.PY", "evil.cmd", "profile.ps1"):
        with pytest.raises(ToolError):
            await call_tool(
                srv,
                "export_debug_logs",
                filename=str(tmp_path / name),
                format="pickle",
            )
        assert not (tmp_path / name).exists()


# --------------------------------------------- every tool that writes a path


async def test_export_network_data_takes_the_same_rule(patched_server, home, tmp_path):
    srv = patched_server()
    with pytest.raises(ToolError) as caught:
        await call_tool(
            srv,
            "export_network_data",
            instance_id="i",
            filepath=str(tmp_path / "net.pth"),
        )
    assert "refuses to write" in str(caught.value)
    assert not (tmp_path / "net.pth").exists()


def test_take_screenshot_precreate_takes_the_same_rule(home, usersite, tmp_path):
    for target in (usersite / "shot.png", tmp_path / "shot.cmd"):
        with pytest.raises(ToolError):
            path_confinement.precreate_private(target, tool="take_screenshot")
        assert not target.exists()


def test_the_three_writers_share_the_one_rule():
    """No second way: every writer goes through ``refuse_write_path``."""
    import inspect

    for writer in (
        path_confinement.open_text,
        path_confinement.open_binary,
        path_confinement.precreate_private,
    ):
        assert "refuse_write_path" in inspect.getsource(writer), writer.__name__


async def test_import_network_data_takes_the_secret_location_rule(home, tmp_path):
    secret = home / ".ssh" / "export.json"
    secret.parent.mkdir()
    secret.write_text("{}", encoding="utf-8")
    with pytest.raises(ToolError, match=r"~/\.ssh"):
        await NetworkInterceptor().import_from_json("i", str(secret))
    ordinary = tmp_path / "ok.json"
    ordinary.write_text("{}", encoding="utf-8")
    assert await NetworkInterceptor().import_from_json("i", str(ordinary)) is True


# ------------------------------------------------------- the refused places


def _places(home: Path, usersite: Path) -> dict[str, Path]:
    places = {
        "the user site-packages": usersite / "data.json",
        "the venv site-packages": Path(sysconfig.get_paths()["purelib"]) / "data.json",
        "the python prefix": Path(sys.prefix) / "data.json",
        "this package": Path(path_confinement.__file__).parent / "data.json",
        "a shell profile": home / ".bashrc",
        "zshrc": home / ".zshrc",
        "profile": home / ".profile",
        "fish config": home / ".config" / "fish" / "config.json",
        "xdg autostart": home / ".config" / "autostart" / "x.json",
        "systemd user": home / ".config" / "systemd" / "user" / "x.json",
        "powershell profile folder": home / "Documents" / "PowerShell" / "x.json",
        "windows powershell folder": home
        / "Documents"
        / "WindowsPowerShell"
        / "x.json",
        "onedrive powershell folder": home
        / "OneDrive"
        / "Documents"
        / "PowerShell"
        / "x.json",
        "windows startup": home
        / "AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup"
        / "x.json",
        "the state directory": home / ".stealth-mcp" / "x.json",
    }
    if sys.platform != "win32":
        places["etc"] = Path("/etc/cron.d/x.json")
    return places


@pytest.mark.parametrize(
    "label",
    [
        "the user site-packages",
        "the venv site-packages",
        "the python prefix",
        "this package",
        "a shell profile",
        "zshrc",
        "profile",
        "fish config",
        "xdg autostart",
        "systemd user",
        "powershell profile folder",
        "windows powershell folder",
        "onedrive powershell folder",
        "windows startup",
        "the state directory",
    ],
)
def test_a_place_code_is_loaded_or_started_from_is_refused(home, usersite, label):
    (home / "OneDrive" / "Documents").mkdir(parents=True)
    target = _places(home, usersite)[label]
    assert "refuses to write" in _refused(target)


@pytest.mark.skipif(sys.platform == "win32", reason="/etc is a POSIX place")
def test_etc_is_refused_on_posix(home, usersite):
    assert "refuses to write" in _refused(Path("/etc/cron.d/x.json"))


def test_every_sys_path_folder_but_the_working_directory_is_refused(
    home, tmp_path, monkeypatch
):
    elsewhere = tmp_path / "on-the-path"
    elsewhere.mkdir()
    monkeypatch.syspath_prepend(str(elsewhere))
    assert "folder code is loaded" in _refused(elsewhere / "a.json")
    # ...but a working directory that python -m put on the path is a person's folder
    cwd = tmp_path / "work"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.syspath_prepend(str(cwd))
    assert path_confinement.refuse_write_path(cwd / "a.json", tool="t")
    # while a file named like code is refused there too
    assert "type the machine runs" in _refused(cwd / "a.pth")


def test_a_working_directory_that_is_a_site_directory_stays_refused(
    home, usersite, monkeypatch
):
    """The cwd exception is for the ``sys.path`` loop only: a backend that happens to
    run in a site-packages directory still refuses to write there."""
    monkeypatch.chdir(usersite)
    monkeypatch.syspath_prepend(str(usersite))
    assert "folder code is loaded" in _refused(usersite / "data.json")


def test_a_symlink_into_a_site_directory_is_refused_under_its_real_name(
    home, usersite, tmp_path
):
    link = tmp_path / "innocent"
    try:
        link.symlink_to(usersite, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("no symlink privilege")
    assert "folder code is loaded" in _refused(link / "data.json")


# --------------------------------------------------------- the refused names


def _named_as_code(directory: Path, name: str) -> bool:
    """The name rule alone: it is pure string work, while the secret-location rule
    around it walks the file system (a second or more per path)."""
    real = path_confinement._real(directory / name)
    return path_confinement.code_name_rule(real) is not None


@pytest.mark.parametrize("extension", sorted(path_confinement.CODE_EXTENSIONS))
def test_every_code_extension_is_refused_in_any_case(tmp_path, extension):
    for spelled in (extension, extension.upper(), extension.title()):
        assert _named_as_code(tmp_path, f"data{spelled}"), spelled


@pytest.mark.parametrize("name", ["evil.pth", "evil.PY", "run.cmd", "profile.ps1"])
def test_the_whole_refusal_names_the_rule_for_a_code_name(tmp_path, name):
    assert "type the machine runs" in _refused(tmp_path / name)


@pytest.mark.parametrize(
    "extension",
    [
        *(".pth", ".py", ".pyc", ".pyd", ".cmd", ".bat", ".ps1", ".psm1", ".sh"),
        *(".lnk", ".exe", ".dll", ".vbs", ".js", ".wsf", ".scr", ".desktop"),
        ".plist",
    ],
)
def test_the_listed_extensions_are_all_in_the_set(extension):
    assert extension in path_confinement.CODE_EXTENSIONS


@WINDOWS_ONLY
@pytest.mark.parametrize("name", ["evil.pth.", "evil.pth ", "evil.pth. .", "EVIL.PTH "])
def test_win32_drops_trailing_dots_and_spaces_so_the_name_is_still_code(tmp_path, name):
    assert _named_as_code(tmp_path, name)


@WINDOWS_ONLY
@pytest.mark.parametrize("name", ["note.txt:evil.pth", "note.txt:s", "x.pth::$DATA"])
def test_a_windows_alternate_data_stream_is_refused(tmp_path, name):
    assert _named_as_code(tmp_path, name)


@WINDOWS_ONLY
def test_an_8_3_short_name_of_a_site_directory_is_refused(usersite):
    import ctypes

    buffer = ctypes.create_unicode_buffer(260)
    got = ctypes.windll.kernel32.GetShortPathNameW(str(usersite), buffer, 260)
    if not got or buffer.value.lower() == str(usersite).lower():
        pytest.skip("no 8.3 name for this folder")
    assert "refuses to write" in _refused(Path(buffer.value) / "data.json")


# ------------------------------------------------- ordinary uses still work

ORDINARY_NAMES = [
    "log.json",
    "shot.png",
    "page.html",
    "export.txt",
    "log.pkl",
    "log.pkl.gz",
    "data.csv",
    "net.har",
    "photo.JPG",
    "readme.md",
    "noextension",
    "dotted.name.json",
    "evil.pth.json",
]


def _ordinary_folders(home, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    folders = [
        tmp_path,
        Path.cwd(),
        home / "Downloads",
        home / "Desktop",
        home / "Documents",
        Path(os.environ.get("TEMP") or os.environ.get("TMPDIR") or tmp_path),
    ]
    for folder in folders:
        folder.mkdir(parents=True, exist_ok=True)
    return folders


@pytest.mark.parametrize("name", ORDINARY_NAMES)
def test_ordinary_names_are_not_code_in_any_ordinary_folder(
    home, tmp_path, name, monkeypatch
):
    for folder in _ordinary_folders(home, tmp_path, monkeypatch):
        real = str(path_confinement._real(folder / name))
        assert path_confinement.code_name_rule(real) is None, (folder, name)
        assert path_confinement.code_location_rule(real) is None, (folder, name)


def test_the_whole_rule_lets_an_ordinary_file_through_in_each_ordinary_folder(
    home, tmp_path, monkeypatch
):
    for folder in _ordinary_folders(home, tmp_path, monkeypatch):
        real = path_confinement.refuse_write_path(folder / "log.json", tool="t")
        assert real.name == "log.json"


def test_an_ordinary_file_is_actually_written_by_the_real_writers(home, tmp_path):
    with path_confinement.open_text(tmp_path / "a.json", tool="t") as handle:
        handle.write("{}")
    with path_confinement.open_binary(tmp_path / "a.png", tool="t") as handle:
        handle.write(b"\x89PNG")
    _real_path, created = path_confinement.precreate_private(
        tmp_path / "s.png", tool="t"
    )
    assert created
    assert (tmp_path / "a.json").read_text() == "{}"
