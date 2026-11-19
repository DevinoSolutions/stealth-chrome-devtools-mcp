"""The PowerShell script a delegated headed launch runs (T-19366, audit 5, A5).

A caller's ``browser_args`` end up inside it. Windows PowerShell 5.1 reads a script
without a BOM in the ANSI code page and treats U+2018, U+2019, U+201A and U+201B as
single quotes, so an argument holding one (or the UTF-8 bytes of a letter that is one
in cp1252) ended the literal and ran the rest as PowerShell, as the user. These tests
parse the generated script the way PowerShell's tokenizer does and never launch
anything.
"""

from __future__ import annotations

import codecs
import inspect
import subprocess
from pathlib import Path

import pytest

from stealth_chrome_devtools_mcp.embedded import desktop_launch

QUOTES = "'\u2018\u2019\u201a\u201b"


def _literal(text: str, start: int) -> tuple[str, int]:
    """Read the single-quoted literal that starts at ``text[start]`` as Windows
    PowerShell's tokenizer does: a quote character followed by ANY quote character is
    one literal character (the second); a quote followed by anything else ends it.
    Returns ``(value, index just after the closing quote)``."""
    assert text[start] in QUOTES, "a literal starts with a quote"
    out = []
    index = start + 1
    while True:
        char = text[index]
        if char in QUOTES:
            if index + 1 < len(text) and text[index + 1] in QUOTES:
                out.append(text[index + 1])
                index += 2
                continue
            return "".join(out), index + 1
        out.append(char)
        index += 1


HOSTILE = [
    "plain",
    "--user-agent=it's",
    "--user-agent=\u2018",
    "--user-agent=\u2019; calc; '",
    "--user-agent=\u201a\u201b",
    "x\u2019 -PassThru; Start-Process calc #",
    "$(Get-Process)",
    "`$(whoami)`",
    "$env:USERNAME ${x} @(1)",
    '"double" and \\"escaped\\"',
    "line one\nline two\r\nline three",
    "C:\\Users\\\u0412asya\\profile with space",  # a Cyrillic letter: D0 92 in UTF-8
    "\u2018\u2019\u201a\u201b'\u2018",
    "emoji \U0001f600 and \u00e9",
    "",
]


@pytest.mark.parametrize("value", HOSTILE)
def test_a_quoted_value_reads_back_as_itself_and_ends_where_it_should(value):
    quoted = desktop_launch._ps_quote(value)
    text, end = _literal(quoted, 0)
    assert text == value
    assert end == len(quoted)  # nothing follows the closing quote


@pytest.mark.parametrize("quote", list(QUOTES))
def test_every_quote_character_powershell_knows_is_doubled(quote):
    quoted = desktop_launch._ps_quote(f"a{quote}b")
    assert quoted == f"'a{quote}{quote}b'" if quote == "'" else True
    assert _literal(quoted, 0)[0] == f"a{quote}b"


@pytest.mark.parametrize("value", HOSTILE)
def test_the_launcher_has_the_shape_it_had_whatever_the_arguments_hold(value, tmp_path):
    args = ["--remote-debugging-port=9222", value, "--lang=en"]
    script = desktop_launch._launcher_script(
        "C:/Program Files/Google/Chrome/chrome.exe", args, tmp_path / "x.pid"
    )
    lines_before = "$ErrorActionPreference = 'Stop'\n$p = Start-Process -FilePath "
    assert script.startswith(lines_before)
    executable, index = _literal(script, len(lines_before))
    assert executable == "C:/Program Files/Google/Chrome/chrome.exe"
    marker = " -ArgumentList "
    assert script[index : index + len(marker)] == marker
    command_line, index = _literal(script, index + len(marker))
    assert command_line == subprocess.list2cmdline(args)  # byte for byte
    assert script[index:].startswith(" -PassThru\ntry { $p.PriorityClass = ")
    # and the pid file is the last literal: nothing smuggled in between
    tail = script[index:]
    assert tail.count("Start-Process") == 0
    assert tail.endswith("-Value $p.Id\n")


def test_the_script_is_written_with_a_byte_order_mark(tmp_path):
    """Without one, Windows PowerShell 5.1 reads the file in the ANSI code page."""
    path = tmp_path / "launch.ps1"
    text = "$x = 'C:\\Users\\\u0412asya'\n"
    desktop_launch.write_launcher_script(path, text)
    data = path.read_bytes()
    assert data.startswith(codecs.BOM_UTF8)
    assert data.decode("utf-8-sig") == text
    # the hazard the BOM removes: read as cp1252, the Cyrillic letter's second byte
    # is a right single quote
    assert "\u2019" in "\u0412".encode("utf-8").decode("cp1252")


def test_the_launch_writes_its_script_through_that_one_function():
    source = inspect.getsource(desktop_launch.launch_and_attach)
    assert "write_launcher_script" in source
    assert "write_text" not in source
    assert desktop_launch.write_launcher_script.__module__ == desktop_launch.__name__


def test_a_pid_file_path_with_a_typographic_quote_stays_a_literal(tmp_path):
    folder = Path(tmp_path) / "it\u2019s here"
    script = desktop_launch._launcher_script("chrome.exe", [], folder / "x.pid")
    marker = "Set-Content -LiteralPath "
    start = script.index(marker) + len(marker)
    value, end = _literal(script, start)
    assert value == str(folder / "x.pid")
    assert script[end:] == " -Value $p.Id\n"
