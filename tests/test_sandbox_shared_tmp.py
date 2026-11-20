"""Tests for the shared temporary tree grant (``shared_tmp``).

A tool that stages an intermediate file at a hardcoded path ignores the
child's TMPDIR: a scanner backend writes ``/tmp/<file>`` and a macOS
converter writes into the per-user temp directory the frameworks report.
Both fail inside a sandbox that granted only the workspace, so a profile that
allows writes has to state whether the shared tree is part of the grant.
The grant is opt-in: a read-only profile never gets it.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.sandbox import profiles
from core.sandbox.profiles import get_profile
from core.sandbox.providers import seatbelt

#: Profiles that promise writes and therefore may stage an intermediate file
#: outside the project.
WRITABLE_SHARED = (profiles.NETWORK, profiles.LEGACY_PROCESS, profiles.OFF)
#: Profiles that do not:
#: ``read_only`` writes nothing at all, and ``workspace_write`` exists precisely
#: to keep a generated program inside the project.
WRITABLE_CONFINED = (profiles.READ_ONLY, profiles.WORKSPACE_WRITE)


@pytest.mark.parametrize("name", WRITABLE_SHARED)
def test_writable_network_profiles_grant_the_shared_tree(name):
    """A profile that allows staging a file outside the project says so."""
    assert get_profile(name).shared_tmp is True


@pytest.mark.parametrize("name", WRITABLE_CONFINED)
def test_confined_profiles_do_not_grant_the_shared_tree(name):
    """Confining writes to the project must not silently widen to /tmp."""
    assert get_profile(name).shared_tmp is False


def test_shared_tmp_is_reported_in_the_profile_mapping():
    """The settings panel reads the flag from the profile, so it must be there."""
    assert "shared_tmp" in get_profile(profiles.NETWORK).to_dict()


def _names_shared_tree(policy: str) -> bool:
    """True when the policy grants the shared tree itself.

    A substring test is wrong on a runner whose own workspace lives under
    the shared tree: ``/tmp/pytest-of-runner/...`` contains ``/tmp`` without
    the policy ever granting the tree. The grant is a whole subpath entry,
    so the closing quote after the path is what tells the two apart.
    """
    return '(subpath "%s")' % os.path.realpath("/tmp") in policy


def test_network_profile_policy_allows_writing_to_tmp(tmp_path):
    """The generated policy names the shared tree for a writable profile."""
    policy = seatbelt.build_profile(get_profile(profiles.NETWORK), str(tmp_path), [])
    assert _names_shared_tree(policy)


def test_workspace_write_policy_does_not_name_the_shared_tree(tmp_path):
    """A confined profile keeps the shared tree out of its allow rule."""
    policy = seatbelt.build_profile(get_profile(profiles.WORKSPACE_WRITE), str(tmp_path), [])
    assert not _names_shared_tree(policy)


def test_read_only_policy_does_not_name_the_shared_tree(tmp_path):
    """A read-only profile writes nothing, so it never names the tree."""
    policy = seatbelt.build_profile(get_profile(profiles.READ_ONLY), str(tmp_path), [])
    assert not _names_shared_tree(policy)


def test_user_temp_dirs_reports_an_existing_directory():
    """Every returned path is a real directory, never a guess."""
    for path in seatbelt.user_temp_dirs():
        assert os.path.isabs(path)
        assert os.path.isdir(path)


def test_user_temp_dirs_is_empty_when_there_is_no_system_temp(monkeypatch):
    """An unresolvable location widens nothing."""
    monkeypatch.setattr(seatbelt.tempfile, "gettempdir", lambda: "/nowhere/at/all")
    assert seatbelt.user_temp_dirs() == []


# ── the Linux provider mounts the same grant ────────────────────────────────
#
# bubblewrap used to put an empty tmpfs over the shared tree. Two things
# break that: the snippet of an inline run is written there BEFORE the
# namespace exists, so the child could not open its own program, and the
# tree stops being a mount SOURCE as well - a workspace or HOME under it has
# nothing left to bind. The tree therefore stays visible through the
# read-only root bind, and only writes to it depend on the profile.


def _mounts(argv):
    """Return ``(flag, source, target)`` for every mount of a bwrap argv."""
    found = []
    index = 0
    while index < len(argv):
        flag = argv[index]
        if flag == "--tmpfs":
            found.append((flag, argv[index + 1], argv[index + 1]))
            index += 2
            continue
        if flag in ("--bind", "--ro-bind", "--dev-bind"):
            found.append((flag, argv[index + 1], argv[index + 2]))
            index += 3
            continue
        index += 1
    return found


def _bwrap_argv(name, workspace, env=None):
    from core.sandbox.providers import bwrap

    return bwrap.build_argv("bwrap", get_profile(name), str(workspace), env or {})


def test_the_linux_provider_keeps_the_temporary_tree_private():
    """The files of other processes on this machine are not part of a grant.

    The workspace is named outside the shared tree on purpose: a project that
    lives inside it keeps the tree visible (see the mount-source test below),
    and this test is about the ordinary case.
    """
    argv = _bwrap_argv(profiles.WORKSPACE_WRITE, "/opt/project")
    targets = [target for flag, _source, target in _mounts(argv) if flag == "--tmpfs"]
    assert "/tmp" in targets
    assert ("--ro-bind", "/", "/") in _mounts(argv)


def test_a_tree_holding_what_the_run_needs_is_not_replaced():
    """A replaced tree cannot serve as the source of a later mount.

    ``bwrap`` applies the mounts in order inside one namespace, so after an
    empty tmpfs is put over ``/tmp`` a ``--bind`` of a path under it has
    nothing left to open, and the whole namespace is refused with
    ``Can't mkdir``. On a CI runner the workspace and the child's HOME live
    exactly there, so such a tree is left visible.
    """
    from core.sandbox.providers import bwrap

    assert "/tmp" not in bwrap._private_trees(["/tmp/pytest-of-runner/work"])
    assert "/tmp" not in bwrap._private_trees(["/tmp/pytest-of-runner/work/home"])
    assert "/tmp" in bwrap._private_trees(["/opt/project", "/home/runner"])


def test_a_workspace_under_the_shared_tree_keeps_it_visible(tmp_path, monkeypatch):
    """The namespace is built, not refused, when the project lives in /tmp."""
    from core.sandbox.providers import bwrap

    monkeypatch.setattr(bwrap, "_shared_tmp_paths", lambda: [str(tmp_path)])
    workspace = tmp_path / "work"
    workspace.mkdir()
    argv = bwrap.build_argv("bwrap", get_profile(profiles.WORKSPACE_WRITE),
                            str(workspace), {})

    targets = [target for flag, _source, target in _mounts(argv) if flag == "--tmpfs"]
    assert str(tmp_path) not in targets
    assert ("--bind", str(workspace), str(workspace)) in _mounts(argv)


def test_the_linux_provider_mounts_back_what_the_run_owns(tmp_path):
    """A private tree hides the snippet and the child's home unless restored."""
    home = tmp_path / "home"
    home.mkdir()
    snippet = home / "probe.py"
    snippet.write_text("print(1)", encoding="utf-8")
    from core.sandbox.providers import bwrap

    argv = bwrap.build_argv("bwrap", get_profile(profiles.WORKSPACE_WRITE), str(tmp_path),
                            {"HOME": str(home), "TMPDIR": str(home)},
                            [str(snippet)])
    mounts = _mounts(argv)

    assert ("--bind", str(home), str(home)) in mounts
    assert ("--ro-bind", str(snippet), str(snippet)) in mounts


def test_a_script_outside_the_workspace_is_mounted_read_only(tmp_path):
    """Running a script is not a reason to make its directory writable."""
    from core.sandbox.providers import bwrap

    script = tmp_path / "helper.py"
    script.write_text("print(1)", encoding="utf-8")
    workspace = tmp_path / "work"
    workspace.mkdir()
    argv = bwrap.build_argv("bwrap", get_profile(profiles.WORKSPACE_WRITE), str(workspace),
                            {}, [str(script)])

    assert ("--ro-bind", str(script), str(script)) in _mounts(argv)
    assert ("--bind", str(script), str(script)) not in _mounts(argv)


def test_an_inline_snippet_is_written_where_the_child_can_open_it(tmp_path):
    """A snippet in the system tree would vanish inside a private one.

    The file is created BEFORE the namespace exists. Written to the system
    temporary directory it would be hidden by the private tree the sandbox
    mounts over it, and the interpreter would fail with "can't open file"
    instead of running the code.
    """
    from dev_agent.tools.exec_support import inline_command

    home = tmp_path / "home"
    home.mkdir()
    cleanup, argv = inline_command("print('probe')", "utf-8",
                                  env={"HOME": str(home), "TMPDIR": str(home)})
    try:
        assert Path(cleanup).parent == home
        assert argv[-1] == cleanup
        assert Path(cleanup).read_text(encoding="utf-8") == "print('probe')"
    finally:
        Path(cleanup).unlink()


def test_an_inline_snippet_falls_back_to_the_system_tree(tmp_path):
    """A run without its own TMPDIR still gets a file."""
    from dev_agent.tools.exec_support import inline_command

    cleanup, _argv = inline_command("print(1)", "utf-8", env={})
    try:
        assert Path(cleanup).is_file()
    finally:
        Path(cleanup).unlink()


def test_the_linux_provider_widens_the_shared_tree_only_on_request(tmp_path):
    """A profile without the grant keeps the tree read-only."""
    confined = _mounts(_bwrap_argv(profiles.WORKSPACE_WRITE, tmp_path))
    shared = _mounts(_bwrap_argv(profiles.NETWORK, tmp_path))

    assert ("--bind", "/tmp", "/tmp") not in confined
    assert ("--bind", "/tmp", "/tmp") in shared


def test_the_linux_provider_mounts_the_child_home_for_writing(tmp_path):
    """The interpreter writes its caches and temporary files into its HOME."""
    home = tmp_path / "home"
    home.mkdir()
    argv = _bwrap_argv(profiles.WORKSPACE_WRITE, tmp_path,
                       {"HOME": str(home), "TMPDIR": str(home)})
    mounts = _mounts(argv)

    assert ("--bind", str(home), str(home)) in mounts
    # Applied after the workspace bind, so the home wins on an overlapping
    # path and a home under a read-only tree stays writable.
    workspace_index = mounts.index(next(item for item in mounts
                                        if item[2] == str(tmp_path)))
    assert mounts.index(("--bind", str(home), str(home))) > workspace_index


def test_the_linux_provider_mounts_the_workspace_read_only_when_confined(tmp_path):
    """A read-only profile can still read the project it was given."""
    argv = _bwrap_argv(profiles.READ_ONLY, tmp_path)
    assert ("--ro-bind", str(tmp_path), str(tmp_path)) in _mounts(argv)
