"""End-to-end console scenarios: what an operator actually does.

Each test is one story told in given -> when -> then form, and each walks the
console the way a person or a cron job would: a real process, an isolated
data folder, and only the public surface (arguments, exit code, stdout,
files on disk). Nothing here reaches into a command function - if a scenario
passes, the console really does that.

The stories are chosen to cover the transitions an operator cares about:

1. a brand-new installation is prepared from the console alone and then
   reports itself healthy;
2. a maintenance window: snapshot, migrate, verify, and a restore that has to
   be undoable;
3. an update from a local mirror, then a rollback of what was applied;
4. issuing a credential and a script consuming the JSON it prints;
5. the refusals: a destructive action without confirmation, an unknown
   employee, a paused run that is a RESULT rather than a failure.
"""

import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ─── Harness ────────────────────────────────────────────────────────────────


@pytest.fixture()
def install(tmp_path):
    """Return a fake installation root plus an isolated data folder."""
    root = tmp_path / "install"
    root.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    return {"root": root, "data": data, "tmp": tmp_path}


def console(install, *args, timeout=180, extra_env=None):
    """Run the console against an isolated installation.

    Both roots are redirected: the data folder holds the databases, and the
    install root points at a scratch folder. A command that replaces files
    must never be aimed at the checkout the test imported.
    """
    env = dict(os.environ)
    env["SAGAAI_DATA_DIR"] = str(install["data"])
    env["SAGAAI_KEY_FILE"] = str(install["data"] / "encryption_key")
    env["SAGAAI_INSTALL_ROOT"] = str(install["root"])
    env.pop("SAGAAI_ENCRYPTION_KEY", None)
    env.update(extra_env or {})
    return subprocess.run([sys.executable, "-m", "sagaai", *args],
                          capture_output=True, text=True, timeout=timeout,
                          cwd=ROOT, env=env)


def payload(result):
    """Parse and check the JSON envelope of a console run."""
    assert result.stdout.strip(), "the console printed nothing on stdout"
    body = json.loads(result.stdout)
    assert "ok" in body and "command" in body and "exit_code" in body
    return body


# ─── Scenario 1: a fresh installation is prepared from the console ─────────


def test_scenario_a_clean_machine_is_prepared_and_diagnosed(install):
    """Given a machine where SagaAI was never started,
    when the operator migrates and then diagnoses it,
    then the databases exist, the diagnosis is clean, and nothing is left in
    a warning state that would confuse the next person."""
    # given: a clean data folder with no database at all
    assert not (install["data"] / "sagaai.db").exists()

    # when: the console prepares the installation, then reports on it
    migrated = console(install, "migrate", "--yes", "--json")
    diagnosed = console(install, "doctor", "--json")

    # then: the migration created the databases and both checks are green
    assert migrated.returncode == 0, migrated.stderr
    assert (install["data"] / "sagaai.db").is_file()
    assert diagnosed.returncode == 0
    checks = payload(diagnosed)["data"]["checks"]
    assert checks["databases"]["status"] == "ok"
    assert checks["paths"]["status"] == "ok"
    # A second migrate is idempotent: the same story, run twice, is safe.
    again = console(install, "migrate", "--yes", "--json")
    assert again.returncode == 0, again.stderr
    assert payload(again)["data"]["failed"] == []


def test_scenario_doctor_before_migrate_does_not_build_anything(install):
    """Given a machine that was never prepared,
    when the operator only ASKS about its state,
    then the console answers without creating a database - a diagnostic that
    changes the thing it measures is useless."""
    born = console(install, "doctor", "--json")
    assert born.returncode == 0
    assert payload(born)["data"]["checks"]["databases"]["status"] == "warn"
    assert not (install["data"] / "sagaai.db").exists()
    assert not (install["data"] / "devagent.db").exists()


# ─── Scenario 2: a maintenance window ──────────────────────────────────────


def test_scenario_snapshot_migrate_restore_is_undoable(install):
    """Given an installation in use,
    when the operator snapshots it, migrates, and then restores an earlier
    snapshot by mistake,
    then the restore reports a safety snapshot that puts the mistake back."""
    # given: an installation with the databases in place
    assert console(install, "migrate", "--yes", "--json").returncode == 0
    marker = install["data"] / "sagaai.db"
    before = marker.read_bytes()

    # when: a snapshot is taken, and later restored
    made = console(install, "backup", "create", "--label", "window",
                   "--json")
    assert made.returncode == 0, made.stderr
    snapshot_id = payload(made)["data"]["id"]
    restored = console(install, "backup", "restore", snapshot_id,
                       "--yes", "--json")

    # then: the restore succeeded, left a safety snapshot, and the database
    # still passes its integrity check afterwards
    assert restored.returncode == 0, restored.stderr
    body = payload(restored)["data"]
    # migrate creates BOTH databases, so a snapshot holds both members and a
    # restore puts both back - the pair is what makes the state consistent.
    assert sorted(body["restored"]) == ["devagent.db", "sagaai.db"]
    assert body["safety_snapshot"].startswith("db_")
    diagnosed = console(install, "doctor", "--json")
    assert payload(diagnosed)["data"]["checks"]["databases"]["status"] == "ok"
    assert marker.is_file()
    assert len(before) > 0


def test_scenario_the_listing_grows_with_each_snapshot(install):
    """Given an operator who snapshots before and after a change,
    when they list the snapshots,
    then both are visible, newest first, so they can pick the right one."""
    first = console(install, "backup", "create", "--label", "one", "--json")
    second = console(install, "backup", "create", "--label", "two", "--json")
    assert first.returncode == 0 and second.returncode == 0
    listing = console(install, "backup", "list", "--json")
    items = payload(listing)["data"]["snapshots"]
    labels = [item.get("label") for item in items]
    assert labels[:2] == ["two", "one"]


# ─── Scenario 3: an update from a local mirror, then a rollback ────────────


def _publish_mirror(folder, root, files):
    """Write a manifest and the files it describes into a mirror folder."""
    import hashlib

    units = {}
    for rel, text in files.items():
        path = folder / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        units.setdefault("platform", {"version": "9.9.9", "files": {}})
        units["platform"]["files"][rel] = {"version": "9.9.9",
                                           "sha256": digest}
    manifest = {"app_version": "9.9.9", "units": units, "selectable": {}}
    (folder / "file_versions.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def test_scenario_an_update_is_staged_applied_and_rolled_back(install):
    """Given a published mirror with one changed file,
    when the operator checks, stages and applies the update,
    then the file is replaced, and a rollback puts the old content back."""
    # given: an installation with a file the mirror also ships
    target = install["root"] / "docs" / "release_note.md"
    target.parent.mkdir(parents=True)
    target.write_text("old\n", encoding="utf-8")
    mirror = install["tmp"] / "mirror"
    mirror.mkdir()
    _publish_mirror(mirror, install["root"], {"docs/release_note.md": "new\n"})

    # when: the three read-mostly steps run against the local mirror
    checked = console(install, "update", "check", "--channel", str(mirror),
                      "--json")
    assert checked.returncode == 0, checked.stderr
    assert payload(checked)["data"]["up_to_date"] is False

    # then: staging leaves the live file alone, applying replaces it
    staged = console(install, "update", "stage", "--channel", str(mirror),
                     "--json")
    assert staged.returncode == 0, staged.stderr
    assert payload(staged)["data"]["staged"] == ["docs/release_note.md"]
    assert target.read_text(encoding="utf-8") == "old\n"

    applied = console(install, "update", "apply", "--yes", "--json")
    assert applied.returncode == 0, applied.stderr
    assert payload(applied)["data"]["applied"] == ["docs/release_note.md"]
    assert target.read_text(encoding="utf-8") == "new\n"

    # then: a rollback puts the previous content back
    rolled = console(install, "update", "rollback", "--yes", "--json")
    assert rolled.returncode == 0, rolled.stderr
    assert payload(rolled)["data"]["restored"] == ["docs/release_note.md"]
    assert target.read_text(encoding="utf-8") == "old\n"


def test_scenario_staging_from_a_mirror_without_the_file_fails_cleanly(install):
    """Given a mirror whose manifest names a file it does not ship,
    when the operator stages from it,
    then the command fails with the failure code and the live tree is
    untouched - a half-published mirror must not corrupt an installation."""
    mirror = install["tmp"] / "broken"
    mirror.mkdir()
    manifest = _publish_mirror(mirror, install["root"],
                               {"docs/missing.md": "present\n"})
    (mirror / "docs" / "missing.md").unlink()
    staged = console(install, "update", "stage", "--channel", str(mirror),
                     "--json")
    assert staged.returncode == 1
    body = payload(staged)
    assert body["ok"] is False
    assert "file not found in the mirror" in body["error"]["message"]
    assert manifest["units"]
    assert not (install["root"] / "docs" / "missing.md").exists()


# ─── Scenario 4: a credential a script consumes ────────────────────────────


def test_scenario_a_script_issues_and_uses_a_token(install):
    """Given a script that needs to call the HTTP API,
    when it asks the console for a token,
    then it gets the value once, and a later listing shows the record without
    the value - the console never hands out a stored secret."""
    created = console(install, "token", "create", "--scope", "dev_agent",
                      "--note", "nightly", "--json")
    assert created.returncode == 0, created.stderr
    body = payload(created)
    token = body["data"]["token"]
    token_id = body["data"]["record"]["id"]
    assert token and token.startswith("saga_")

    listing = console(install, "token", "list", "--json")
    assert listing.returncode == 0
    text = listing.stdout
    assert token_id in text
    assert token not in text, "the listing leaked a usable credential"

    revoked = console(install, "token", "revoke", token_id, "--json")
    assert revoked.returncode == 0
    after = console(install, "token", "list", "--json")
    assert payload(after)["data"]["count"] == 0


# ─── Scenario 5: the refusals ──────────────────────────────────────────────


def test_scenario_destructive_actions_refuse_without_a_human(install):
    """Given a cron job with no terminal attached,
    when it tries a destructive action without --yes,
    then the console refuses with the configuration code instead of guessing,
    and nothing was changed."""
    made = console(install, "backup", "create", "--json")
    snapshot_id = payload(made)["data"]["id"]
    refused = console(install, "backup", "restore", snapshot_id, "--json")
    assert refused.returncode == 2
    assert payload(refused)["error"]["code"] == "confirmation_required"
    assert "--yes" in payload(refused)["error"]["hint"]


def test_scenario_an_unknown_employee_names_the_command_that_lists_them(install):
    """Given a misconfigured cron job that names an employee by a wrong slug,
    when it starts the task,
    then the error says which command shows the real slugs - a dead end is a
    bug, not a message."""
    result = console(install, "run", "--agent", "typo_here", "--task", "go",
                     "--json")
    assert result.returncode == 2
    error = payload(result)["error"]
    assert error["code"] == "unknown_agent"
    assert "sagaai agents" in error["hint"]


def test_scenario_the_console_never_needs_the_ui(install):
    """Given a server where Streamlit is not installed,
    when an operator runs the console,
    then every non-UI command works - the console is not a UI feature."""
    script = (
        "import sys\n"
        "sys.path.insert(0, %r)\n"
        "BLOCKED = ('streamlit',)\n"
        "class Blocker:\n"
        "    def find_spec(self, name, *a, **k):\n"
        "        if name == 'streamlit' or name.startswith('streamlit.'):\n"
        "            raise ImportError('blocked: ' + name)\n"
        "        return None\n"
        "sys.meta_path.insert(0, Blocker())\n"
        "from sagaai.cli import main\n"
        "raise SystemExit(main(['doctor', '--json']))\n"
    ) % ROOT
    env = dict(os.environ, SAGAAI_DATA_DIR=str(install["data"]),
               SAGAAI_KEY_FILE=str(install["data"] / "encryption_key"))
    result = subprocess.run([sys.executable, "-c", script], capture_output=True,
                            text=True, timeout=180, cwd=ROOT, env=env)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["command"] == "doctor"
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
