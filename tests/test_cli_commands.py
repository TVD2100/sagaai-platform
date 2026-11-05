"""Tests of the console commands.

Every test here runs the real ``python -m sagaai`` in a child process with an
isolated ``SAGAAI_DATA_DIR``. That is deliberate: the commands are a CONTRACT
with a script, and the part a script sees is the process - its exit code, its
stdout and the files it left behind. A test that called ``execute`` directly
would skip the argument parser, the error translation and the envelope, which
is where the contract actually lives.

The isolation is the same one the platform uses everywhere: a temporary data
folder and a temporary key file, so a run never touches the developer's own
installation or ``~/.sagaai``.
"""

import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ─── Harness ────────────────────────────────────────────────────────────────


@pytest.fixture()
def data_dir(tmp_path):
    """Return an isolated data folder with its own key file."""
    folder = tmp_path / "data"
    folder.mkdir()
    return folder


def run_cli(args, data_dir, extra_env=None, timeout=180, root=None):
    """Run the console in a child process against *data_dir*.

    ``root`` overrides the installation root. A test must never let a
    command that replaces files point at the checkout it imported, so every
    call here defaults the override to the isolated data folder.
    """
    env = dict(os.environ)
    env["SAGAAI_DATA_DIR"] = str(data_dir)
    env["SAGAAI_KEY_FILE"] = str(data_dir / "encryption_key")
    env["SAGAAI_INSTALL_ROOT"] = str(root if root is not None else data_dir)
    env.pop("SAGAAI_ENCRYPTION_KEY", None)
    env.update(extra_env or {})
    return subprocess.run([sys.executable, "-m", "sagaai", *args],
                          capture_output=True, text=True, timeout=timeout,
                          cwd=ROOT, env=env)


def envelope(result):
    """Parse the single JSON object a ``--json`` run prints."""
    assert result.stdout.strip(), "no output on stdout"
    return json.loads(result.stdout)


# ─── doctor ─────────────────────────────────────────────────────────────────


def test_doctor_reports_a_clean_installation_without_creating_anything(data_dir):
    """A diagnostic must describe the state, not build a new one."""
    result = run_cli(["doctor", "--json"], data_dir)
    assert result.returncode == 0, result.stderr
    payload = envelope(result)
    assert payload["ok"] is True
    assert payload["command"] == "doctor"
    data = payload["data"]
    assert data["status"] in ("ok", "warn")
    assert data["errors"] == []
    assert "databases" in data["checks"]
    assert "dependencies" in data["checks"]
    assert "encryption_key" in data["checks"]
    assert "tokens" in data["checks"]
    assert "scheduler" in data["checks"]
    assert not any(name.endswith(".db") for name in os.listdir(data_dir)), \
        "doctor created a database it was only asked about"


def test_doctor_names_the_not_initialized_state_as_a_warning(data_dir):
    result = run_cli(["doctor", "--json"], data_dir)
    data = envelope(result)["data"]
    databases = data["checks"]["databases"]
    assert databases["status"] == "warn"
    assert "sagaai.db" in databases["missing"]
    assert "migrate" in databases["summary"]


def test_doctor_verbose_renders_sections_a_person_can_read(data_dir):
    result = run_cli(["doctor", "--verbose"], data_dir)
    assert result.returncode == 0
    text = result.stdout
    assert "SagaAI doctor" in text
    assert "[warn]" in text or "[ ok ]" in text
    assert "checks" in text
    assert "{" not in text


def test_doctor_exit_code_tracks_errors_not_warnings(data_dir):
    """A fresh installation warns; that must not look like a failure."""
    result = run_cli(["doctor", "--json"], data_dir)
    assert result.returncode == 0
    assert envelope(result)["data"]["warnings"], \
        "the clean-installation warnings disappeared"


# ─── agents ─────────────────────────────────────────────────────────────────


def test_agents_lists_slugs_a_run_can_use(data_dir):
    result = run_cli(["agents", "--json"], data_dir)
    assert result.returncode == 0, result.stderr
    payload = envelope(result)
    assert payload["command"] == "agents"
    assert isinstance(payload["data"]["agents"], list)
    assert payload["data"]["count"] == len(payload["data"]["agents"])


# ─── token ──────────────────────────────────────────────────────────────────


def test_token_create_returns_the_plaintext_once_and_stores_only_a_hash(data_dir):
    created = run_cli(["token", "create", "--scope", "dev_agent",
                       "--note", "ci", "--json"], data_dir)
    assert created.returncode == 0, created.stderr
    payload = envelope(created)
    token = payload["data"]["token"]
    assert token.startswith("saga_")
    assert payload["data"]["record"]["slugs"] == ["dev_agent"]
    assert payload["data"]["record"]["note"] == "ci"

    listing = run_cli(["token", "list", "--json"], data_dir)
    assert listing.returncode == 0
    records = envelope(listing)["data"]["tokens"]
    assert len(records) == 1
    assert records[0]["id"] == payload["data"]["record"]["id"]
    assert "sha256" not in records[0]
    assert token not in json.dumps(records)


def test_the_cli_token_is_accepted_by_the_token_store(data_dir):
    """The credential the console issues is the one the API verifies."""
    created = run_cli(["token", "create", "--scope", "dev_agent", "--json"],
                      data_dir)
    token = envelope(created)["data"]["token"]
    script = (
        "import sys, json\n"
        "sys.path.insert(0, %r)\n"
        "from core import api_token_store as store\n"
        "record = store.verify_token(%r, store.load_records())\n"
        "print(json.dumps({'matched': bool(record), 'slugs': store.scope_of(record or {})}))\n"
    ) % (ROOT, token)
    env = dict(os.environ, SAGAAI_DATA_DIR=str(data_dir),
               SAGAAI_KEY_FILE=str(data_dir / "encryption_key"))
    result = subprocess.run([sys.executable, "-c", script], capture_output=True,
                            text=True, timeout=120, cwd=ROOT, env=env)
    assert result.returncode == 0, result.stderr
    answer = json.loads(result.stdout)
    assert answer["matched"] is True
    assert answer["slugs"] == ["dev_agent"]


def test_token_revoke_removes_the_record(data_dir):
    created = run_cli(["token", "create", "--json"], data_dir)
    token_id = envelope(created)["data"]["record"]["id"]
    revoked = run_cli(["token", "revoke", token_id, "--json"], data_dir)
    assert revoked.returncode == 0, revoked.stderr
    assert envelope(revoked)["data"]["revoked"] is True
    listing = run_cli(["token", "list", "--json"], data_dir)
    assert envelope(listing)["data"]["count"] == 0


def test_token_revoke_of_an_unknown_id_is_a_configuration_error(data_dir):
    result = run_cli(["token", "revoke", "deadbeef", "--json"], data_dir)
    assert result.returncode == 2
    payload = envelope(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "unknown_token"


def test_token_expiry_accepts_a_date_and_means_end_of_that_day(data_dir):
    created = run_cli(["token", "create", "--expires", "2099-01-01",
                       "--json"], data_dir)
    assert created.returncode == 0, created.stderr
    record = envelope(created)["data"]["record"]
    assert record["expires_at"].startswith("2099-01-02")
    assert record["expired"] is False


def test_a_malformed_expiry_is_refused_with_the_configuration_code(data_dir):
    result = run_cli(["token", "create", "--expires", "not-a-date", "--json"],
                     data_dir)
    assert result.returncode == 2
    assert envelope(result)["error"]["code"] == "invalid_request"


# ─── backup и migrate ───────────────────────────────────────────────────────


def test_backup_create_writes_a_manifest_with_a_hash_per_member(data_dir):
    result = run_cli(["backup", "create", "--label", "before", "--json"],
                     data_dir)
    assert result.returncode == 0, result.stderr
    data = envelope(result)["data"]
    assert data["id"].startswith("db_")
    assert os.path.isdir(data["path"])
    manifest_path = os.path.join(data["path"], "manifest.json")
    assert os.path.isfile(manifest_path)
    with open(manifest_path, encoding="utf-8") as handle:
        manifest = json.load(handle)
    assert manifest["label"] == "before"
    assert manifest["members"], "a manifest without members is not a snapshot"


def test_backup_list_shows_the_snapshot_that_was_created(data_dir):
    created = run_cli(["backup", "create", "--json"], data_dir)
    snapshot_id = envelope(created)["data"]["id"]
    listing = run_cli(["backup", "list", "--json"], data_dir)
    assert listing.returncode == 0
    ids = [item["id"] for item in envelope(listing)["data"]["snapshots"]]
    assert snapshot_id in ids


def test_backup_restore_needs_confirmation_when_nobody_can_answer(data_dir):
    """No ``--yes`` and no terminal: refuse with a configuration error."""
    created = run_cli(["backup", "create", "--json"], data_dir)
    snapshot_id = envelope(created)["data"]["id"]
    result = run_cli(["backup", "restore", snapshot_id, "--json"], data_dir)
    assert result.returncode == 2
    payload = envelope(result)
    assert payload["error"]["code"] == "confirmation_required"
    assert "--yes" in payload["error"]["hint"]


def test_backup_restore_with_the_flag_puts_the_database_back(data_dir):
    first = run_cli(["migrate", "--yes", "--json"], data_dir)
    assert first.returncode == 0, first.stderr
    created = run_cli(["backup", "create", "--json"], data_dir)
    snapshot_id = envelope(created)["data"]["id"]
    result = run_cli(["backup", "restore", snapshot_id, "--yes", "--json"],
                     data_dir)
    assert result.returncode == 0, result.stderr
    data = envelope(result)["data"]
    assert data["restored"]
    assert data["safety_snapshot"].startswith("db_")
    # The databases are the same pair migrate created, so both come back.
    assert sorted(data["restored"]) == ["devagent.db", "sagaai.db"]


def test_backup_restore_of_an_unknown_snapshot_is_a_configuration_error(data_dir):
    result = run_cli(["backup", "restore", "db_20000101T000000_abcdef",
                      "--yes", "--json"], data_dir)
    assert result.returncode == 2
    assert envelope(result)["error"]["code"] == "unknown_snapshot"


def test_migrate_creates_the_databases_on_a_clean_machine(data_dir):
    result = run_cli(["migrate", "--yes", "--json"], data_dir)
    assert result.returncode == 0, result.stderr
    data = envelope(result)["data"]
    assert data["failed"] == []
    assert os.path.isfile(os.path.join(str(data_dir), "sagaai.db"))
    assert data["integrity"]["sagaai.db"] == "ok"


def test_doctor_after_migrate_reports_the_databases_as_current(data_dir):
    assert run_cli(["migrate", "--yes", "--json"], data_dir).returncode == 0
    result = run_cli(["doctor", "--json"], data_dir)
    databases = envelope(result)["data"]["checks"]["databases"]
    assert databases["status"] == "ok", databases["summary"]


# ─── run ────────────────────────────────────────────────────────────────────


def test_run_of_an_unknown_employee_is_a_configuration_error(data_dir):
    result = run_cli(["run", "--agent", "no_such_employee", "--task", "hi",
                      "--json"], data_dir)
    assert result.returncode == 2
    payload = envelope(result)
    assert payload["error"]["code"] == "unknown_agent"
    assert "agents" in payload["error"]["hint"]


def test_run_requires_an_agent_and_a_task(data_dir):
    missing_task = run_cli(["run", "--agent", "dev_agent", "--json"], data_dir)
    assert missing_task.returncode == 2
    assert "--task" in (missing_task.stdout + missing_task.stderr)


def test_run_rejects_a_workspace_that_does_not_exist(data_dir):
    result = run_cli(["run", "--agent", "dev_agent", "--task", "hi",
                      "--workspace", "/nowhere/at/all", "--json"], data_dir)
    assert result.returncode == 2
    assert envelope(result)["error"]["code"] == "workspace_not_found"


# ─── update ─────────────────────────────────────────────────────────────────


def test_update_check_reports_an_unreachable_channel_as_a_failure(data_dir,
                                                                 tmp_path):
    result = run_cli(["update", "check", "--channel",
                      str(tmp_path / "missing"), "--json"], data_dir)
    assert result.returncode == 1
    payload = envelope(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "check_failed"


def test_update_apply_needs_confirmation(data_dir):
    result = run_cli(["update", "apply", "--json"], data_dir)
    assert result.returncode == 2
    assert envelope(result)["error"]["code"] == "confirmation_required"
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
