"""F-903: the suite-wide fence keeping a test run out of ``~/.stealth-mcp``.

Four things are pinned here and each answers a different way the fence could
rot.

* **The redirect reaches every binding** -- ten of them, across five modules,
  because four modules FROM-import names a fifth computed from ``Path.home()``
  at import time.
* **The enumeration cannot go stale.** ``derived_globals`` re-runs the probe the
  table was measured with, so a NEW global derived from the state dir fails a
  test instead of quietly escaping. This is the half that history says matters:
  a "pure move" of module-global paths once escaped the ``setattr(singleton,
  ...)`` fixtures and deleted the operator's live record.
* **The write guard stops a write the redirect missed**, and is not catchable by
  the product's fail-open handlers.
* **Reads still work**, because ``release_gate_harness._reserved_ports()``
  depends on reading the operator's real record.

Every node here works against a DECOY state dir: ``state_dir_fence`` reads
``REAL_STATE_DIR`` at call time, so pointing it at ``tmp_path`` makes the
already-installed guard guard the decoy instead. Nothing in this file can touch
the operator's directory even when it fails.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest

import state_dir_fence


class TestTheRedirectReachesEveryBinding:
    """The ten measured bindings, live in the running session."""

    def test_no_binding_still_names_the_real_state_dir(self):
        real = state_dir_fence.REAL_STATE_DIR
        escaped = []
        for dotted, attr, _ in state_dir_fence.STATE_DIR_BINDINGS:
            value = getattr(importlib.import_module(dotted), attr)
            if value == real or real in Path(value).parents:
                escaped.append(f"{dotted}.{attr} = {value}")
        assert not escaped, (
            "these bindings still point at the operator's real state dir: "
            + ", ".join(escaped)
        )

    def test_the_real_state_dir_is_the_home_convention(self):
        """THE one home for "the state dir is ``~/.stealth-mcp``".

        ``tests/test_clone_output_dir.py`` used to carry this claim implicitly,
        by asserting a derived default equalled ``Path.home()/".stealth-mcp"/
        "element_clones"``. Once the fence redirects the binding that node can no
        longer say it, and it should not: its subject is the CONVENTION (the
        default is ``<state dir>/element_clones``). The home-derivation is a
        different claim and this is its one home -- which also keeps the fence
        honest, since ``REAL_STATE_DIR`` is what the write guard designates and a
        drift there would silently designate the wrong directory.
        """
        assert Path.home() / ".stealth-mcp" == state_dir_fence.REAL_STATE_DIR

    def test_every_binding_lands_under_one_fence_root(self):
        """One root, so a test reading two of them sees one consistent dir."""
        roots = set()
        for dotted, attr, basename in state_dir_fence.STATE_DIR_BINDINGS:
            value = Path(getattr(importlib.import_module(dotted), attr))
            roots.add(value if basename is None else value.parent)
        assert len(roots) == 1, f"bindings are split across roots: {sorted(roots)}"

    def test_the_env_file_pydantic_actually_reads_is_fenced(self):
        """The global is documentation; ``model_config`` is the binding.

        pydantic copies ``_STATE_DIR_ENV_FILE``'s VALUE into ``model_config``
        when the class body runs, so redirecting the global alone leaves the
        suite reading the operator's own ``~/.stealth-mcp/.env`` -- their knobs,
        our tests, and ``extra="forbid"`` turning one stale key into a crash.
        """
        from stealth_chrome_devtools_mcp.settings import Settings

        configured = Path(Settings.model_config["env_file"])
        assert configured.parent != state_dir_fence.REAL_STATE_DIR


class TestTheEnumerationCannotGoStale:
    """A new global derived from the state dir must fail a test, not escape."""

    def test_no_package_global_is_left_under_the_real_state_dir(self):
        real = state_dir_fence.REAL_STATE_DIR
        leaked = {
            name: value
            for name, value in state_dir_fence.derived_globals().items()
            if Path(value) == real or real in Path(value).parents
        }
        assert not leaked, (
            "package globals derived from the real state dir that the fence "
            f"does not redirect: {leaked}. Add each to "
            "state_dir_fence.STATE_DIR_BINDINGS."
        )

    def test_the_pin_catches_a_global_the_table_does_not_name(self, monkeypatch):
        """The pin above, shown failing -- otherwise it proves only that today
        happens to be clean.

        A module global derived from the state dir is exactly the shape history
        produced: a "pure move" of module-global paths once escaped the
        ``setattr(singleton, ...)`` fixtures and deleted the operator's live
        record. Here one is introduced deliberately, on a module the table DOES
        name, and the sweep must see it.
        """
        from stealth_chrome_devtools_mcp.embedded import backend_registry

        monkeypatch.setattr(
            backend_registry,
            "_F903_NEW_DERIVED_GLOBAL",
            state_dir_fence.REAL_STATE_DIR / "newly-derived.json",
            raising=False,
        )
        real = state_dir_fence.REAL_STATE_DIR
        leaked = {
            name: value
            for name, value in state_dir_fence.derived_globals().items()
            if Path(value) == real or real in Path(value).parents
        }
        assert (
            "stealth_chrome_devtools_mcp.embedded.backend_registry._F903_NEW_DERIVED_GLOBAL"
            in leaked
        )

    def test_the_table_names_globals_that_still_exist(self):
        """A binding whose global was renamed is a fence with a hole in it."""
        for dotted, attr, _ in state_dir_fence.STATE_DIR_BINDINGS:
            module = importlib.import_module(dotted)
            assert hasattr(module, attr), (
                f"{dotted}.{attr} is in the fence table but no longer exists"
            )

    def test_the_sweep_never_imports_dunder_main(self):
        """``__main__.py`` RUNS the product; importing it starts a backend.

        MEASURED 2026-09-21: the census probe for this finding imported it and
        cold-started a real backend (pid 189088, port 64986) into the operator's
        live record. If someone guards that call behind ``if __name__ ==
        "__main__":`` this deny-list entry can go -- and this node is what will
        tell them, by failing on the second assertion.
        """
        assert "stealth_chrome_devtools_mcp.__main__" in state_dir_fence._NEVER_IMPORT

        import stealth_chrome_devtools_mcp as pkg

        source = (Path(pkg.__file__).parent / "__main__.py").read_text(encoding="utf-8")
        assert 'if __name__ == "__main__"' not in source, (
            "__main__.py now guards its main() call, so importing it is safe "
            "and state_dir_fence._NEVER_IMPORT no longer needs it"
        )


class TestTheWriteGuardStopsAWriteTheRedirectMissed:
    """RED/GREEN against a decoy: the guard is what stops the write.

    The pairing matters. A node asserting only that the write raises would still
    pass if ``pytest.raises`` were catching something else entirely; the
    companion node writes the SAME path with the decoy not designated, lands it,
    and reads it back -- so the difference between the two is exactly the guard.
    """

    @pytest.fixture()
    def decoy(self, tmp_path):
        target = tmp_path / "decoy-state"
        target.mkdir()
        return target

    def test_without_the_guard_the_write_lands(self, decoy):
        (decoy / "server.json").write_text("{}", encoding="utf-8")
        assert (decoy / "server.json").read_text(encoding="utf-8") == "{}"

    def test_write_text_under_the_designated_dir_raises(self, decoy, monkeypatch):
        monkeypatch.setattr(state_dir_fence, "REAL_STATE_DIR", decoy)
        with pytest.raises(state_dir_fence.RealStateDirWrite):
            (decoy / "server.json").write_text("{}", encoding="utf-8")

    # The RAW primitives on purpose. ``os.replace``/``os.remove``/``open`` are
    # the doors the guard actually wraps, and the ``pathlib`` spellings ruff
    # prefers here are the two cases already covered by their own params -- a
    # rewrite would test one door twice and leave two untested.
    @pytest.mark.parametrize(
        "act",
        [
            pytest.param(
                lambda d: open(d / "server.json", "w"),  # noqa: SIM115, PTH123  PERMANENT(F-903: builtins.open IS the door under test)
                id="builtins-open",
            ),
            pytest.param(lambda d: (d / "x").mkdir(), id="path-mkdir"),
            pytest.param(
                lambda d: (d / "logs").mkdir(parents=True), id="path-mkdir-parents"
            ),
            pytest.param(lambda d: (d / "lock").touch(), id="path-touch"),
            pytest.param(
                lambda d: os.replace(d / "a", d / "b"),  # noqa: PTH105  PERMANENT(F-903: os.replace IS the door under test)
                id="os-replace",
            ),
            pytest.param(
                lambda d: os.remove(d / "server.json"),  # noqa: PTH107  PERMANENT(F-903: os.remove IS the door under test)
                id="os-remove",
            ),
            pytest.param(lambda d: (d / "server.json").unlink(), id="path-unlink"),
        ],
    )
    def test_every_write_door_is_guarded(self, decoy, monkeypatch, act):
        """One node per primitive the product writes the state dir through."""
        monkeypatch.setattr(state_dir_fence, "REAL_STATE_DIR", decoy)
        with pytest.raises(state_dir_fence.RealStateDirWrite):
            act(decoy)

    def test_the_guard_survives_the_products_fail_open_handlers(
        self, decoy, monkeypatch
    ):
        """``BaseException``, so ``except Exception`` cannot swallow it.

        Not a style choice: ``backend_registry`` is a never-raise cache,
        ``proxy_selfheal`` never raises and ``observability`` never raises, so an
        ``Exception`` here is eaten at the first fail-open handler it meets and
        the node goes green over a real write. The F-900 branch's
        ``_RealStartupReached(Exception)`` was swallowed at
        ``proxy_selfheal.py:~325`` for exactly this reason.
        """
        monkeypatch.setattr(state_dir_fence, "REAL_STATE_DIR", decoy)
        with pytest.raises(state_dir_fence.RealStateDirWrite):
            try:
                (decoy / "server.json").write_text("{}", encoding="utf-8")
            # The product's own fail-open shape, reproduced deliberately.
            except Exception:
                pytest.fail("a fail-open handler swallowed the fence")

    def test_the_products_own_record_writer_is_stopped(self, decoy, monkeypatch):
        """The guard on the door the product actually writes through.

        Every node above drives a ``pathlib``/``os`` primitive directly, which
        proves the wrappers fire but not that the product reaches them.
        ``record_backend`` is the one writer of ``server.json`` and goes through
        ``backend_registry._commit_json``; pointing BOTH the designated dir and
        the record path at the decoy makes this the F-903 harm in miniature.
        """
        from stealth_chrome_devtools_mcp.embedded import backend_registry

        monkeypatch.setattr(state_dir_fence, "REAL_STATE_DIR", decoy)
        with pytest.raises(state_dir_fence.RealStateDirWrite):
            backend_registry.record_backend(
                decoy / "server.json",
                port=64986,
                version="2.1.11",
                pid=189088,
                source_fingerprint="e021a40",
                display_context="win-session-1",
            )
        assert not (decoy / "server.json").exists()

    def test_a_relative_path_from_inside_the_dir_is_still_caught(
        self, decoy, monkeypatch
    ):
        """The one case the fast substring gate deliberately skips.

        Membership is decided on a substring of the path for ABSOLUTE paths --
        exact, because the root's whole string is a prefix of anything under it.
        A RELATIVE path has no such guarantee: ``server.json`` opened from a cwd
        inside the state dir is under it and contains none of its name. The gate
        is therefore applied only to absolute paths and this node is what keeps
        it that way; widening it for speed would open a hole the size of a
        ``chdir``.
        """
        monkeypatch.setattr(state_dir_fence, "REAL_STATE_DIR", decoy)
        monkeypatch.chdir(decoy)
        with pytest.raises(state_dir_fence.RealStateDirWrite):
            Path("server.json").write_text("{}", encoding="utf-8")

    def test_a_sibling_directory_is_not_caught_by_prefix(self, tmp_path, monkeypatch):
        """``.stealth-mcp-browser-sessions`` must not read as inside the dir.

        A plain ``startswith`` on the directory name alone would swallow every
        sibling whose name merely begins with it -- including the browser-session
        root, which the suite writes to constantly.
        """
        designated = tmp_path / ".stealth-mcp"
        designated.mkdir()
        sibling = tmp_path / ".stealth-mcp-browser-sessions"
        sibling.mkdir()
        monkeypatch.setattr(state_dir_fence, "REAL_STATE_DIR", designated)
        (sibling / "profile.json").write_text("{}", encoding="utf-8")
        assert (sibling / "profile.json").is_file()


class TestReadsAreDeliberatelyNotGuarded:
    """One read of the operator's real record is REQUIRED.

    ``release_gate_harness._reserved_ports()`` reads it through ``Path.home()``
    so an isolated backend never binds a port a LIVE backend holds. Guarding
    reads would break the one mechanism protecting the live backends from a port
    collision, to prevent a harm that reading cannot do -- which is also why the
    fence redirects the module globals instead of ``HOME``.
    """

    def test_reading_under_the_designated_dir_is_allowed(self, tmp_path, monkeypatch):
        designated = tmp_path / ".stealth-mcp"
        designated.mkdir()
        record = designated / "server.json"
        record.write_text('{"schema": 3}', encoding="utf-8")
        monkeypatch.setattr(state_dir_fence, "REAL_STATE_DIR", designated)
        assert record.read_text(encoding="utf-8") == '{"schema": 3}'
        # builtins.open explicitly: it is the wrapped door, and a read through it
        # must pass. ``read_text`` above reaches ``io.open``, the other wrapper.
        with open(record, encoding="utf-8") as handle:  # noqa: PTH123  PERMANENT(F-903: builtins.open IS the door under test)
            assert handle.read() == '{"schema": 3}'

    def test_the_harness_still_reads_the_operators_real_record(self):
        """The mechanism itself, not a re-implementation of it."""
        from release_gate_harness import _reserved_ports

        assert isinstance(_reserved_ports(), frozenset)


class TestTheKillGuardProtectsALiveBackend:
    """A recorded pid may not be terminated from the suite.

    Separate from the write guard because a kill leaves no filesystem trace, and
    terminating a live backend is the harm F-886 exists to prevent -- reached
    from the suite instead of from a cold start.
    """

    def test_a_recorded_pid_cannot_be_terminated(self, monkeypatch):
        from stealth_chrome_devtools_mcp.embedded import backend_eviction

        state_dir_fence._install_kill_guard(frozenset({4242}))
        monkeypatch.setattr(
            backend_eviction,
            "terminate",
            backend_eviction.terminate,
            raising=False,
        )
        with pytest.raises(state_dir_fence.RealBackendTerminated):
            backend_eviction.terminate(4242)

    def test_an_unrecorded_pid_is_not_refused_by_the_guard(self):
        """The guard must not become a blanket ban on ``terminate``.

        Several files drive the real eviction act against fake pids; a guard
        that refused every pid would fence the suite by breaking it.
        """
        captured = []

        def fake(pid, *args, **kwargs):
            captured.append(pid)
            return True

        from stealth_chrome_devtools_mcp.embedded import backend_eviction

        original = backend_eviction.terminate
        try:
            backend_eviction.terminate = fake
            state_dir_fence._install_kill_guard(frozenset({4242}))
            backend_eviction.terminate(9999)
        finally:
            backend_eviction.terminate = original
        assert captured == [9999]
