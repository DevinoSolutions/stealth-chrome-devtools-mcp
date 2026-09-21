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

Every node here works against a DECOY state dir: ``operator_fence`` reads
``REAL_STATE_DIR`` at call time, so pointing it at ``tmp_path`` makes the
already-installed guard guard the decoy instead. Nothing in this file can touch
the operator's directory even when it fails.
"""

from __future__ import annotations

import contextlib
import importlib
import os
import sys
from pathlib import Path

import pytest

import operator_fence


class TestTheRedirectReachesEveryBinding:
    """The ten measured bindings, live in the running session."""

    def test_no_binding_still_names_the_real_state_dir(self):
        real = operator_fence.REAL_STATE_DIR
        escaped = []
        for dotted, attr, _ in operator_fence.STATE_DIR_BINDINGS:
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
        assert Path.home() / ".stealth-mcp" == operator_fence.REAL_STATE_DIR

    def test_every_binding_lands_under_one_fence_root(self):
        """One root, so a test reading two of them sees one consistent dir."""
        roots = set()
        for dotted, attr, basename in operator_fence.STATE_DIR_BINDINGS:
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
        assert configured.parent != operator_fence.REAL_STATE_DIR


class TestTheEnumerationCannotGoStale:
    """A new global derived from the state dir must fail a test, not escape."""

    def test_no_package_global_is_left_under_the_real_state_dir(self):
        real = operator_fence.REAL_STATE_DIR
        leaked = {
            name: value
            for name, value in operator_fence.derived_globals().items()
            if Path(value) == real or real in Path(value).parents
        }
        assert not leaked, (
            "package globals derived from the real state dir that the fence "
            f"does not redirect: {leaked}. Add each to "
            "operator_fence.STATE_DIR_BINDINGS."
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
            operator_fence.REAL_STATE_DIR / "newly-derived.json",
            raising=False,
        )
        real = operator_fence.REAL_STATE_DIR
        leaked = {
            name: value
            for name, value in operator_fence.derived_globals().items()
            if Path(value) == real or real in Path(value).parents
        }
        assert (
            "stealth_chrome_devtools_mcp.embedded.backend_registry._F903_NEW_DERIVED_GLOBAL"
            in leaked
        )

    def test_the_table_names_globals_that_still_exist(self):
        """A binding whose global was renamed is a fence with a hole in it."""
        for dotted, attr, _ in operator_fence.STATE_DIR_BINDINGS:
            module = importlib.import_module(dotted)
            assert hasattr(module, attr), (
                f"{dotted}.{attr} is in the fence table but no longer exists"
            )

    def test_the_sweep_covers_the_whole_package_with_no_exclusions(self):
        """The census may not have a blind spot, and no longer does.

        This node was the inverse until F-903's second round: the sweep carried a
        ``_NEVER_IMPORT`` deny-list because ``__main__.py`` called ``main()`` at
        module level, and importing it cold-started a real backend (pid 189088,
        port 64986) into the operator's live record -- the finding reproducing
        itself inside its own census. The product is guarded now, the deny-list
        is deleted, and what this asserts is the property that made deleting it
        safe: the sweep reaches EVERY module, ``__main__`` included, and reaching
        it runs nothing.

        The two halves live where they belong -- that importing it is inert is
        ``tests/test_package_entrypoints.py``'s, and so is the whole-package
        rule that keeps any other module body from becoming the next one.
        """
        assert not hasattr(operator_fence, "_NEVER_IMPORT"), (
            "the deny-list is back; a census with an exclusion list is a census "
            "with a blind spot -- fix the module that does work instead"
        )

        operator_fence.derived_globals()

        assert "stealth_chrome_devtools_mcp.__main__" in sys.modules

    def test_the_sweep_sees_a_path_a_singleton_captured_on_itself(self):
        """F-903 review S3: a module GLOBAL is not the only import-time capture.

        ``process_cleanup``'s module-level singleton keeps
        ``self.pid_file = STATE_DIR / RECORD_NAME``, built in its own module
        body. The reviewer measured that an attributes-only probe reported
        neither it nor ``file_based_element_cloner``'s ``output_dir``, so the
        "a new derived path fails a test" promise had a hole precisely where
        the redirect is saved by the ORDER of its own table (the singleton is
        constructed by row 4's import, after row 3 rebinds the global it
        reads). The sweep walks one level into the package's own instances now,
        and this is the node that keeps it doing so.
        """
        found = operator_fence.derived_globals()
        assert (
            "stealth_chrome_devtools_mcp.embedded.process_cleanup"
            ".process_cleanup.pid_file" in found
        ), "the sweep no longer sees a Path a module-level singleton captured"


class TestTheFenceReportsWhatItIsProtecting:
    """``conftest`` keeps the live pids the kill guard was armed with.

    F-903 review S6: the binding was assigned and never read, and the kill
    guard's own nodes all use a hand-made decoy set -- so on a machine where
    ``recorded_backend_pids()`` silently answered ``frozenset()`` every one of
    them would still pass. These two are the anti-vacuous half: the reader is
    proven against a record that HAS entries, and the conftest global is proven
    to be that reader's answer for this session.
    """

    def test_the_reader_finds_the_pids_a_record_names(self, tmp_path, monkeypatch):
        state = tmp_path / ".stealth-mcp"
        state.mkdir()
        (state / "server.json").write_text(
            '{"schema_version": 3, "backends": ['
            '{"port": 1, "pid": 4242}, {"port": 2, "pid": 4243}]}',
            encoding="utf-8",
        )
        monkeypatch.setattr(operator_fence, "REAL_STATE_DIR", state)
        assert operator_fence.recorded_backend_pids() == frozenset({4242, 4243})

    def test_the_conftest_holds_what_the_fence_protects(self):
        import conftest

        live = operator_fence.recorded_backend_pids()
        assert live == conftest._FENCED_LIVE_BACKEND_PIDS


class TestEveryTripwireIsUnswallowable:
    """The one property all four share, and the one home they share it in.

    A tripwire that a ``except Exception`` backstop can eat is decoration, and
    this codebase is fail-open by design -- ``proxy_selfheal.heal_backend`` has
    a ``PERMANENT`` one. So the ``BaseException`` base is asserted for EVERY
    tripwire rather than for the one someone remembered, and ``Exception`` is
    excluded explicitly, because inheriting it is the exact regression.
    """

    @pytest.mark.parametrize(
        "name",
        [
            "RealStateDirWrite",
            "RealSessionRootAccess",
            "RealBackendTerminated",
            "RealStartupReached",
        ],
    )
    def test_the_tripwire_is_a_baseexception_and_not_an_exception(self, name):
        tripwire = getattr(operator_fence, name)

        assert issubclass(tripwire, BaseException)
        assert not issubclass(tripwire, Exception), (
            f"{name} inherits Exception, so the product's fail-open handlers "
            "will swallow it -- that is the bug F-900 review M2 measured"
        )

    def test_the_bridge_test_uses_the_shared_tripwire(self):
        """F-900 declared its own; F-903 made it one symbol in one home.

        Asserted by IDENTITY rather than by reading the source, because two
        classes that merely look alike is the state this replaced -- and an
        ``except operator_fence.RealStartupReached`` elsewhere would not catch a
        locally redeclared twin.
        """
        import test_proxy_bridge_transport as bridge

        assert bridge._RealStartupReached is operator_fence.RealStartupReached


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
        monkeypatch.setattr(operator_fence, "REAL_STATE_DIR", decoy)
        with pytest.raises(operator_fence.RealStateDirWrite):
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
            # F-903 review M2: both reached a designated root and did not
            # raise. `os.truncate` emptied the fenced file; `os.utime` is the
            # fast path `Path.touch()` takes when the file already exists, so a
            # touch bumped a fenced mtime while `path-touch` above (which goes
            # through `os.open`) looked like it covered touching.
            pytest.param(lambda d: os.truncate(d / "server.json", 0), id="os-truncate"),
            pytest.param(lambda d: os.utime(d / "server.json"), id="os-utime"),
            # F-903 review M4: the SAME doors in keyword form, which walked
            # straight through an args-only guard.
            pytest.param(
                lambda d: os.remove(path=d / "server.json"),  # noqa: PTH107  PERMANENT(F-903: os.remove IS the door under test)
                id="os-remove-keyword",
            ),
            pytest.param(
                lambda d: os.mkdir(path=d / "x"),  # noqa: PTH102  PERMANENT(F-903: os.mkdir IS the door under test)
                id="os-mkdir-keyword",
            ),
            pytest.param(
                lambda d: os.rmdir(path=d / "x"),  # noqa: PTH106  PERMANENT(F-903: os.rmdir IS the door under test)
                id="os-rmdir-keyword",
            ),
        ],
    )
    def test_every_write_door_is_guarded(self, decoy, monkeypatch, act):
        """One node per primitive the product writes the state dir through."""
        (decoy / "server.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(operator_fence, "REAL_STATE_DIR", decoy)
        with pytest.raises(operator_fence.RealStateDirWrite):
            act(decoy)

    def test_a_keyword_call_the_guard_missed_really_deleted_the_file(self, decoy):
        """The measurement behind M4, kept as the RED half of the pair above.

        With the decoy NOT designated, ``os.remove(path=…)`` deletes it -- which
        is precisely what it did while designated, under the args-only guard.
        """
        target = decoy / "server.json"
        target.write_text("{}", encoding="utf-8")
        os.remove(path=target)  # noqa: PTH107  PERMANENT(F-903: os.remove IS the door under test)
        assert not target.exists()

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
        monkeypatch.setattr(operator_fence, "REAL_STATE_DIR", decoy)
        with pytest.raises(operator_fence.RealStateDirWrite):
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

        monkeypatch.setattr(operator_fence, "REAL_STATE_DIR", decoy)
        with pytest.raises(operator_fence.RealStateDirWrite):
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
        monkeypatch.setattr(operator_fence, "REAL_STATE_DIR", decoy)
        monkeypatch.chdir(decoy)
        with pytest.raises(operator_fence.RealStateDirWrite):
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
        monkeypatch.setattr(operator_fence, "REAL_STATE_DIR", designated)
        (sibling / "profile.json").write_text("{}", encoding="utf-8")
        assert (sibling / "profile.json").is_file()


class TestEverySpellingOfARootIsFenced:
    """F-903 review S1: one directory, several names the OS answers to.

    The hot guard compares strings, so a root known by one spelling is fenced
    by one spelling. ``_root_spellings`` resolves the alternatives ONCE per
    table rebuild -- realpath for a junction or symlink, ``GetShortPathName``
    for the 8.3 name, and the extended-length prefix -- and this is what keeps
    them in the table.
    """

    @pytest.fixture()
    def decoy(self, tmp_path, monkeypatch):
        target = tmp_path / "stealth-mcp-decoy-directory"
        target.mkdir()
        monkeypatch.setattr(operator_fence, "REAL_STATE_DIR", target)
        return target

    @pytest.mark.skipif(os.name != "nt", reason="8.3 short names are Windows'")
    def test_the_short_name_spelling_is_refused(self, decoy):
        import ctypes

        buffer = ctypes.create_unicode_buffer(32768)
        assert ctypes.windll.kernel32.GetShortPathNameW(str(decoy), buffer, 32768)
        short = Path(buffer.value)
        # The premise: it really is a DIFFERENT string for the same directory.
        assert str(short).casefold() != str(decoy).casefold()
        with pytest.raises(operator_fence.RealStateDirWrite):
            (short / "server.json").write_text("{}", encoding="utf-8")

    @pytest.mark.skipif(os.name != "nt", reason="UNC admin shares are Windows'")
    def test_the_local_admin_share_spelling_is_refused(self, decoy):
        """No I/O is attempted, so this needs no privilege: the guard refuses
        on the STRING, before the original primitive is called."""
        if decoy.drive[1:2] != ":":
            pytest.skip("not a drive-letter path")
        unc = f"\\\\localhost\\{decoy.drive[0]}$" + str(decoy)[2:]
        with pytest.raises(operator_fence.RealStateDirWrite):
            Path(unc + "\\server.json").write_text("{}", encoding="utf-8")

    @pytest.mark.skipif(os.name != "nt", reason=r"\\?\ is Windows'")
    def test_the_extended_length_spelling_is_refused(self, decoy):
        with pytest.raises(operator_fence.RealStateDirWrite):
            Path("\\\\?\\" + str(decoy) + "\\server.json").write_text(
                "{}", encoding="utf-8"
            )

    def test_a_link_to_the_root_is_still_not_the_root(self, tmp_path, decoy):
        """What S1 does NOT close, pinned so the claim stays honest.

        Realpathing the ROOT canonicalises the root. A junction or symlink used
        MID-PATH in the TARGET is a spelling of a fenced file that no root
        string is a prefix of, and closing it would need a resolve on every
        open -- the cost this guard exists to avoid. The redirect covers it.
        """
        link = tmp_path / "link"
        try:
            link.symlink_to(decoy, target_is_directory=True)
        except (OSError, NotImplementedError):  # no privilege on this machine
            pytest.skip("cannot create a directory symlink here")
        (link / "server.json").write_text("{}", encoding="utf-8")
        assert (decoy / "server.json").is_file()


class TestTheBrowserSessionRootIsFencedToo:
    """The operator's profiles: the ``master`` a human is logged into, and every
    named session copied from it.

    The residue that proves this was reachable is still on the machine that
    found it -- ``e2e-warmup``, ``ci-warmup``, ``ci-cycle-0/1/2``,
    ``tree-kill-test``, ``integration-test-profile`` and ``ci-basic-test`` in the
    operator's real ``sessions/``, beside 87 real ones.
    """

    def test_the_product_resolves_every_root_inside_the_fence(self):
        """All FOUR roots, not just the one env name.

        ``clone_storage`` derives master / clone root / snapshot from the
        session root only when their own env names are unset, so an inherited
        ``BROWSER_MASTER_USER_DATA_DIR`` would name the operator's real master
        profile underneath a redirected root. The fence clears all three.
        """
        from stealth_chrome_devtools_mcp.embedded import clone_storage

        resolved = [
            clone_storage.default_session_root(),
            clone_storage.master_profile_dir(),
            clone_storage.clone_root_dir(),
            clone_storage.master_snapshot_dir(),
        ]
        for forbidden in operator_fence.REAL_SESSION_ROOTS:
            for path in resolved:
                assert forbidden not in [path, *path.parents], (
                    f"{path} resolves inside the operator's real {forbidden}"
                )

    def test_the_real_root_is_designated(self):
        """The fence must actually be guarding something.

        A regression that left ``REAL_SESSION_ROOTS`` empty would make every
        node in this class vacuously green.
        """
        assert operator_fence.REAL_SESSION_ROOTS

    def test_the_spelled_default_matches_the_products_own(self, monkeypatch):
        """``_product_session_root`` re-spells a default it may not import.

        It runs before the product is importable, so it cannot ask
        ``clone_storage``. This node asks, with the env cleared, and fails if the
        two ever drift -- which is what would silently designate the wrong
        directory and fence nothing.
        """
        from stealth_chrome_devtools_mcp.embedded import clone_storage
        from stealth_chrome_devtools_mcp.settings import get_settings

        monkeypatch.delenv(operator_fence.SESSION_ROOT_ENV, raising=False)
        get_settings.cache_clear()
        assert clone_storage.default_session_root() == (
            operator_fence._product_session_root()
        )

    def test_a_write_under_the_real_session_root_raises(self, tmp_path, monkeypatch):
        decoy = tmp_path / "operator-sessions"
        decoy.mkdir()
        monkeypatch.setattr(operator_fence, "REAL_SESSION_ROOTS", (decoy,))
        with pytest.raises(operator_fence.RealSessionRootAccess):
            (decoy / "master" / "Cookies").parent.mkdir(parents=True)

    def test_reading_under_the_real_session_root_also_raises(
        self, tmp_path, monkeypatch
    ):
        """The asymmetry with the state dir, pinned.

        Reading is the harm here: copying the operator's ``master`` profile is
        how a test would take their logged-in cookies into a clone. The state
        dir's rows deliberately allow reads, and the node below proves the two
        policies do not bleed into each other.
        """
        decoy = tmp_path / "operator-sessions"
        (decoy / "master").mkdir(parents=True)
        cookies = decoy / "master" / "Cookies"
        cookies.write_bytes(b"sqlite")
        monkeypatch.setattr(operator_fence, "REAL_SESSION_ROOTS", (decoy,))
        with pytest.raises(operator_fence.RealSessionRootAccess):
            cookies.read_bytes()

    def test_listing_the_real_session_root_raises(self, tmp_path, monkeypatch):
        """``_copy_profile_tree`` walks before it opens a single file."""
        decoy = tmp_path / "operator-sessions"
        decoy.mkdir()
        monkeypatch.setattr(operator_fence, "REAL_SESSION_ROOTS", (decoy,))
        with pytest.raises(operator_fence.RealSessionRootAccess):
            list(decoy.iterdir())

    def test_the_state_dir_still_allows_reads_while_this_is_installed(
        self, tmp_path, monkeypatch
    ):
        """One table, two policies -- and they must not bleed.

        The session root's read ban is per-ROW, so designating it must not make
        the state dir's reads raise; ``_reserved_ports()`` depends on that.
        """
        state = tmp_path / ".stealth-mcp"
        state.mkdir()
        (state / "server.json").write_text('{"schema": 3}', encoding="utf-8")
        sessions = tmp_path / "operator-sessions"
        sessions.mkdir()
        monkeypatch.setattr(operator_fence, "REAL_STATE_DIR", state)
        monkeypatch.setattr(operator_fence, "REAL_SESSION_ROOTS", (sessions,))
        assert (state / "server.json").read_text(encoding="utf-8") == '{"schema": 3}'

    def test_a_filesystem_root_is_never_designated(self, monkeypatch):
        """A misconfigured root would otherwise fence the suite off the disk."""
        monkeypatch.setattr(
            operator_fence, "REAL_SESSION_ROOTS", (Path(Path.cwd().anchor),)
        )
        rows = operator_fence._designated_roots()
        assert all(mark for _root, mark, _reads, _error in rows)


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
        monkeypatch.setattr(operator_fence, "REAL_STATE_DIR", designated)
        assert record.read_text(encoding="utf-8") == '{"schema": 3}'
        # builtins.open explicitly: it is the wrapped door, and a read through it
        # must pass. ``read_text`` above reaches ``io.open``, the other wrapper.
        with open(record, encoding="utf-8") as handle:  # noqa: PTH123  PERMANENT(F-903: builtins.open IS the door under test)
            assert handle.read() == '{"schema": 3}'

    def test_the_harness_still_reads_the_operators_real_record(self):
        """The mechanism itself, not a re-implementation of it."""
        from release_gate_harness import _reserved_ports

        assert isinstance(_reserved_ports(), frozenset)


# A pid the guard will be told to protect. NEVER one of the operator's: every
# node below installs its own guard over a DECOY set, and the wrapper is taken
# off again in a `finally` (F-903 review S2 -- the first version of these pins
# installed a layer per node and left every one of them on the class for the
# rest of the session).
_DECOY_PID = 424242
_UNPROTECTED_PID = 424243


class _FakeProcess:
    """As much of ``psutil.Process`` as the guard reads: ``.pid``.

    The guard is a wrapper on the CLASS, so calling it unbound with this is the
    real wrapper on a real method lookup -- and no real process is involved,
    which is the point. The refusing path never reaches the original method;
    the allowing path reaches the recorder installed under it.
    """

    def __init__(self, pid: int) -> None:
        self.pid = pid


@contextlib.contextmanager
def _kill_guard(live: frozenset[int], *, record: list | None = None):
    """Install the kill guard over optional recorders, then take it off again.

    Restoring is not tidiness: ``_install_kill_guard`` sets attributes on
    ``psutil.Process`` and on ``os``, which are process-global, so a node that
    installs and walks away leaves its DECOY pid protected for every later test
    in the session (F-903 review S2).
    """
    import psutil

    names = ("terminate", "kill", "send_signal")
    saved = {name: getattr(psutil.Process, name) for name in names}
    saved_os_kill = os.kill
    try:
        if record is not None:
            for name in names:
                setattr(
                    psutil.Process,
                    name,
                    lambda self, *a, _n=name, **k: record.append((_n, self.pid)),
                )
            os.kill = lambda pid, *a, **k: record.append(("os.kill", pid))
        operator_fence._install_kill_guard(live)
        yield
    finally:
        for name, original in saved.items():
            setattr(psutil.Process, name, original)
        os.kill = saved_os_kill


class TestTheKillGuardProtectsALiveBackend:
    """A recorded pid may not be ended from the suite.

    Separate from the write guard because a kill leaves no filesystem trace, and
    terminating a live backend is the harm F-886 exists to prevent -- reached
    from the suite instead of from a cold start.

    **These nodes are about the PID, deliberately** (F-903 review M1). The guard
    they replace wrapped ``backend_eviction.terminate`` and searched its
    ARGUMENTS for a protected int, and the pins matched it: they passed
    ``terminate(4242)`` and read the refusal as proof. It was not. That
    function's first parameter is a PORT and its kill target is a local
    (``pid = pid_on_port(port)``), so the shipped guard ALLOWED a call that
    would have ended the operator's pid 47424 and REFUSED a call whose port
    merely equalled a protected pid -- measured, both ways. What every kill in
    the tree does have in common is the two doors below.
    """

    def test_the_decoy_is_not_one_of_the_operators(self):
        """The safety premise of every node here, asserted rather than assumed."""
        assert _DECOY_PID not in operator_fence.recorded_backend_pids()
        assert _UNPROTECTED_PID not in operator_fence.recorded_backend_pids()

    @pytest.mark.parametrize("method", ["terminate", "kill", "send_signal"])
    def test_a_protected_pid_cannot_be_ended_through_psutil(self, method):
        import psutil

        with _kill_guard(frozenset({_DECOY_PID})):
            with pytest.raises(operator_fence.RealBackendTerminated):
                getattr(psutil.Process, method)(_FakeProcess(_DECOY_PID))

    def test_a_protected_pid_cannot_be_ended_through_os_kill(self):
        """``browser_manager``'s teardown is the tree's one ``os.kill``."""
        with _kill_guard(frozenset({_DECOY_PID})):
            with pytest.raises(operator_fence.RealBackendTerminated):
                os.kill(_DECOY_PID, 9)

    @pytest.mark.parametrize("method", ["terminate", "kill", "send_signal"])
    def test_an_unprotected_pid_still_goes_through(self, method):
        """The guard must not become a blanket ban on ending a process.

        Several files drive the real eviction act and the real orphan reaper
        against pids of their own; a guard that refused every pid would fence
        the suite by breaking it.
        """
        import psutil

        record: list = []
        with _kill_guard(frozenset({_DECOY_PID}), record=record):
            getattr(psutil.Process, method)(_FakeProcess(_UNPROTECTED_PID))
            os.kill(_UNPROTECTED_PID, 9)
        assert record == [(method, _UNPROTECTED_PID), ("os.kill", _UNPROTECTED_PID)]

    def test_a_port_that_equals_a_protected_pid_is_not_refused(self):
        """The false POSITIVE the argument-walking guard had, pinned.

        ``backend_eviction.terminate``'s first argument is a port. Ports and
        pids share the integer space, so on the shipped guard a test-owned
        backend that happened to bind TCP port 47424 was refused about a
        process the operator never owned. Here nothing is killed at all --
        ``pid_on_port`` finds nobody and ``is_ours`` claims nobody -- so a
        refusal could only come from reading the port as a pid.
        """
        from stealth_chrome_devtools_mcp.embedded import backend_eviction

        with _kill_guard(frozenset({_DECOY_PID})):
            answered = backend_eviction.terminate(
                _DECOY_PID,
                pid_on_port=lambda _port: None,
                recorded_pid=None,
                is_ours=lambda _pid: False,
                is_healthy=lambda _port: False,
            )
        assert answered is False
