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


def test_network_profile_policy_allows_writing_to_tmp(tmp_path):
    """The generated policy names the shared tree for a writable profile."""
    policy = seatbelt.build_profile(get_profile(profiles.NETWORK), str(tmp_path), [])
    assert os.path.realpath("/tmp") in policy


def test_workspace_write_policy_does_not_name_the_shared_tree(tmp_path):
    """A confined profile keeps the shared tree out of its allow rule."""
    policy = seatbelt.build_profile(get_profile(profiles.WORKSPACE_WRITE), str(tmp_path), [])
    assert os.path.realpath("/tmp") not in policy


def test_read_only_policy_does_not_name_the_shared_tree(tmp_path):
    """A read-only profile writes nothing, so it never names the tree."""
    policy = seatbelt.build_profile(get_profile(profiles.READ_ONLY), str(tmp_path), [])
    assert os.path.realpath("/tmp") not in policy


def test_user_temp_dirs_reports_an_existing_directory():
    """Every returned path is a real directory, never a guess."""
    for path in seatbelt.user_temp_dirs():
        assert os.path.isabs(path)
        assert os.path.isdir(path)


def test_user_temp_dirs_is_empty_when_there_is_no_system_temp(monkeypatch):
    """An unresolvable location widens nothing."""
    monkeypatch.setattr(seatbelt.tempfile, "gettempdir", lambda: "/nowhere/at/all")
    assert seatbelt.user_temp_dirs() == []
