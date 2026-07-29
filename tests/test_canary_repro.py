"""Pins for tools/canary_repro.py — the bounded W6 repro helper (plan_RELEASE 2.6).

The claim this file defends is a NEGATIVE one: the canary's repro dump cannot
contain DOM, screenshots, request headers or bodies, cookies, profile data, or
live-site content, and cannot land in the operator's home directory or in the
repository. A negative claim is exactly the kind that rots silently — nothing
fails when a helper quietly starts accepting a blob — so each refusal gets a test
that fails the moment the boundary moves.

Every test here is filesystem-local and Chrome-free: this is the unit lane.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_MOD_PATH = Path(__file__).resolve().parent.parent / "tools" / "canary_repro.py"
_spec = importlib.util.spec_from_file_location("canary_repro", _MOD_PATH)
assert _spec and _spec.loader
cr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cr)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _resolve(dest: Path, **overrides):
    kwargs = {"repo_root": REPO_ROOT, "home": Path.home(), "cwd": Path.cwd()}
    kwargs.update(overrides)
    return cr.resolve_destination(dest, **kwargs)


# ---------------------------------------------------------------------------
# The capture surface is CLOSED: a blob cannot get in.
# ---------------------------------------------------------------------------
def test_a_bounded_oracle_scalar_is_accepted():
    record = cr.build_record(
        call_ids=["call-0001"],
        fixture_refs=["http://127.0.0.1:8931/stealth_probe.html"],
        oracles=["webdriver=false"],
        runner_identity=None,
        chrome_identity=None,
    )
    assert record["schema"] == cr.SCHEMA
    assert record["fixture_call_ids"] == ["call-0001"]
    assert record["oracles"] == {"webdriver": "false"}


def test_the_record_has_exactly_the_allowlisted_keys():
    """A new top-level key is a widened capture surface — make it a red test."""
    record = cr.build_record(
        call_ids=[],
        fixture_refs=[],
        oracles=[],
        runner_identity=None,
        chrome_identity=None,
    )
    assert set(record) == {
        "schema",
        "fixture_call_ids",
        "fixture_refs",
        "oracles",
        "runner_identity",
        "chrome_identity",
    }


def test_a_dom_sized_value_is_refused():
    blob = "<div>" + ("x" * cr.MAX_VALUE_CHARS) + "</div>"
    with pytest.raises(cr.RefusedError, match="cap"):
        cr.build_record(
            call_ids=[],
            fixture_refs=[],
            oracles=[f"dom={blob}"],
            runner_identity=None,
            chrome_identity=None,
        )


def test_a_free_text_call_id_is_refused():
    """Call IDs are opaque ids; free text is the shape a leak arrives in."""
    with pytest.raises(cr.RefusedError, match="synthetic id"):
        cr.build_record(
            call_ids=["Cookie: session=abc; path=/"],
            fixture_refs=[],
            oracles=[],
            runner_identity=None,
            chrome_identity=None,
        )


@pytest.mark.parametrize(
    "ref",
    [
        "https://bot.incolumitas.com/",  # live site
        "https://abrahamjuliot.github.io/creepjs/",  # live site
        "/home/runner/.config/google-chrome/Default/Cookies",  # profile data
        "C:\\Users\\someone\\AppData\\Local\\Google\\Chrome",  # profile data
        "file:///etc/passwd",
    ],
)
def test_a_non_fixture_reference_is_refused(ref):
    with pytest.raises(cr.RefusedError, match="loopback fixture URL"):
        cr.build_record(
            call_ids=[],
            fixture_refs=[ref],
            oracles=[],
            runner_identity=None,
            chrome_identity=None,
        )


def test_a_nested_identity_value_is_refused(tmp_path):
    """An identity file is a flat scalar map — not a smuggling channel."""
    identity = tmp_path / "runner-identity.json"
    identity.write_text(json.dumps({"headers": {"cookie": "abc"}}), encoding="utf-8")
    with pytest.raises(cr.RefusedError, match="not a bounded scalar"):
        cr.build_record(
            call_ids=[],
            fixture_refs=[],
            oracles=[],
            runner_identity=identity,
            chrome_identity=None,
        )


def test_the_w2_identity_records_are_carried_verbatim(tmp_path):
    runner = tmp_path / "runner-identity.json"
    runner.write_text(json.dumps({"runner_label": "Linux/X64"}), encoding="utf-8")
    chrome = tmp_path / "chrome-identity.json"
    chrome.write_text(json.dumps({"version": "126.0.6478.126"}), encoding="utf-8")
    record = cr.build_record(
        call_ids=[],
        fixture_refs=[],
        oracles=[],
        runner_identity=runner,
        chrome_identity=chrome,
    )
    assert record["runner_identity"] == {"runner_label": "Linux/X64"}
    assert record["chrome_identity"] == {"version": "126.0.6478.126"}


def test_an_oversized_batch_is_refused():
    with pytest.raises(cr.RefusedError, match="over the"):
        cr.build_record(
            call_ids=[f"call-{i}" for i in range(cr.MAX_ENTRIES + 1)],
            fixture_refs=[],
            oracles=[],
            runner_identity=None,
            chrome_identity=None,
        )


# ---------------------------------------------------------------------------
# The destination is a throwaway: home, repo, cwd, overwrite, links all refused.
# ---------------------------------------------------------------------------
def test_the_home_directory_is_refused():
    with pytest.raises(cr.RefusedError, match="the home directory"):
        _resolve(Path.home())


def test_the_repository_is_refused(tmp_path):
    # cwd is overridden because pytest runs FROM the repository root, and the
    # cwd refusal would otherwise fire first and hide the property under test.
    with pytest.raises(cr.RefusedError, match="the repository"):
        _resolve(REPO_ROOT, cwd=tmp_path)


def test_a_path_inside_the_repository_is_refused():
    with pytest.raises(cr.RefusedError, match="inside the repository"):
        _resolve(REPO_ROOT / "canary-dump")


def test_the_current_working_directory_is_refused(tmp_path):
    with pytest.raises(cr.RefusedError, match="current working directory"):
        _resolve(tmp_path, cwd=tmp_path)


def test_an_existing_destination_is_refused(tmp_path):
    existing = tmp_path / "already-here"
    existing.mkdir()
    with pytest.raises(cr.RefusedError, match="already exists"):
        _resolve(existing)


@pytest.mark.skipif(
    sys.platform == "win32", reason="symlink creation needs privilege on Windows"
)
def test_a_symlinked_ancestor_is_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(cr.RefusedError, match="symlink"):
        _resolve(link / "dump")


def test_a_fresh_temporary_destination_is_accepted(tmp_path):
    assert _resolve(tmp_path / "dump") == (tmp_path / "dump").absolute()


# ---------------------------------------------------------------------------
# End to end through the CLI: a refusal writes NOTHING.
# ---------------------------------------------------------------------------
def test_main_writes_the_closed_record(tmp_path, capsys):
    out = tmp_path / "dump"
    code = cr.main(["--out", str(out), "--call-id", "call-1", "--oracle", "hits=3"])
    capsys.readouterr()
    assert code == 0
    written = json.loads((out / "canary-repro.json").read_text(encoding="utf-8"))
    assert written["fixture_call_ids"] == ["call-1"]
    assert written["oracles"] == {"hits": "3"}


def test_a_refusal_leaves_no_directory_behind(tmp_path, capsys):
    out = tmp_path / "dump"
    code = cr.main(["--out", str(out), "--call-id", "not a valid id"])
    assert code == 1
    assert "canary_repro" in capsys.readouterr().err
    assert not out.exists(), "a refused run must not create its destination"
