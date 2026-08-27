# -*- coding: utf-8 -*-
"""
Scenario tests for the Connectors feature (core.connectors + GitHub tools + orchestrator binding).

Walk the feature through its public entry points the way a user would use it:
  1. Happy path: create a connection, list it, test it, update it, delete it.
  2. Token safety: secrets never appear in public manifests / connections list.
  3. Validation errors: bad service / empty name / empty token are rejected.
  4. Orchestrator binding: enabled_connections round-trip and prompt extension.
  5. GitHub tools: list-repos tool returns repo metadata; errors become ok=False dicts.
  6. Dispatcher integration: the LLM-facing tool names resolve through UniversalDevAgent.

These scenarios run without network access: PyGithub is mocked at the connector layer.
"""
import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest import mock

import pytest

import core.paths
from core import connectors


@pytest.fixture()
def isolated_data_dir():
    """Point DATA_DIR and DB paths at a throwaway temp directory."""
    tmp = tempfile.mkdtemp(prefix="sagaai_conn_scenario_")
    old_env = os.environ.get("SAGAAI_DATA_DIR")
    os.environ["SAGAAI_DATA_DIR"] = tmp

    old_attrs = {}
    for attr in ("DATA_DIR", "DB_PATH", "DEVAGENT_DB_PATH", "HISTORY_DIR", "SYSTEM_PROMPTS_DIR"):
        old_attrs[attr] = getattr(core.paths, attr, None)
    core.paths.DATA_DIR = tmp
    core.paths.DB_PATH = os.path.join(tmp, "sagaai.db")
    core.paths.DEVAGENT_DB_PATH = os.path.join(tmp, "devagent.db")
    core.paths.HISTORY_DIR = os.path.join(tmp, "history")
    core.paths.SYSTEM_PROMPTS_DIR = os.path.join(tmp, "system_prompts")

    from storage.db import reset_engine, reset_devagent_engine
    reset_engine()
    reset_devagent_engine()
    yield tmp
    reset_engine()
    reset_devagent_engine()

    if old_env:
        os.environ["SAGAAI_DATA_DIR"] = old_env
    else:
        os.environ.pop("SAGAAI_DATA_DIR", None)
    for attr, value in old_attrs.items():
        if value is not None:
            setattr(core.paths, attr, value)
    shutil.rmtree(tmp, ignore_errors=True)


def _github_connection(isolated_data_dir, name="GitHub-CI", token="ghp_scenario_secret"):
    """Create a GitHub connection for use in scenarios, return its public manifest."""
    return connectors.create_connection("github", name, token, account="TVD2100")


# 1. Happy path ---------------------------------------------------------------
def test_scenario_connection_lifecycle(isolated_data_dir):
    """A user creates, lists, updates and deletes a connection through the public API."""
    created = _github_connection(isolated_data_dir, name="Prod-GitHub", token="ghp_prod_token")
    conn_id = created["id"]
    assert created["name"] == "Prod-GitHub"
    assert created["service"] == "github"
    assert created["has_token"] is True

    # The connection is visible in the list, sorted by name.
    items = connectors.list_connections()
    assert [c["name"] for c in items] == ["Prod-GitHub"]
    assert items[0]["id"] == conn_id

    # Test-connection at the connector layer updates the stored account.
    from core import github_connector as gh
    with mock.patch("core.github_connector._ensure_github") as ensure:
        import github
        ensure.return_value = github

        class FakeUser:
            login = "tvd-ci"
            name = "TVD CI"
            id = 42
            html_url = "https://github.com/tvd-ci"

        class FakeGithub:
            def __init__(self, token):
                self.token = token

            def get_user(self):
                return FakeUser()

        with mock.patch.object(github, "Github", FakeGithub):
            result = gh.test_connection(conn_id)
    assert result["ok"] is True
    assert result["login"] == "tvd-ci"
    assert connectors.get_connection(conn_id)["account"] == "tvd-ci"

    # Update name and rotate the token.
    updated = connectors.update_connection(conn_id, name="Prod-GitHub-2", token="ghp_new_token")
    assert updated["name"] == "Prod-GitHub-2"
    assert connectors.decrypt_token(conn_id) == "ghp_new_token"

    # Delete removes the folder and the connection disappears.
    assert connectors.delete_connection(conn_id) is True
    assert connectors.get_connection(conn_id) == {}
    assert connectors.list_connections() == []


# 2. Token safety -------------------------------------------------------------
def test_scenario_token_never_leaks_to_public_views(isolated_data_dir):
    """Plaintext and even encrypted tokens never reach public manifests."""
    secret = "ghp_super_secret_scenario_token"
    created = _github_connection(isolated_data_dir, token=secret)
    conn_id = created["id"]

    for view in (connectors.list_connections(), [connectors.get_connection(conn_id)], [created]):
        raw = json.dumps(view)
        assert secret not in raw
        assert "token_encrypted" not in raw

    # The manifest on disk is encrypted; the raw secret is absent.
    manifest_path = Path(isolated_data_dir) / "connectors" / conn_id / "manifest.json"
    raw = manifest_path.read_text(encoding="utf-8")
    assert secret not in raw
    assert connectors.decrypt_token(conn_id) == secret


# 3. Validation errors --------------------------------------------------------
def test_scenario_validation_rejects_bad_input(isolated_data_dir):
    """Invalid service / name / token produce clean user-facing errors."""
    with pytest.raises(ValueError):
        connectors.create_connection("slack", "Slack", "xoxb-token")
    with pytest.raises(ValueError):
        connectors.create_connection("github", "", "ghp_token")
    with pytest.raises(ValueError):
        connectors.create_connection("github", "Valid", "   ")
    assert connectors.list_connections() == []


# 4. Orchestrator binding -----------------------------------------------------
def test_scenario_orchestrator_binding(isolated_data_dir):
    """An orchestrator can be bound to connections and its prompt then advertises them."""
    from core.orchestrators import (
        create_orchestrator,
        get_enabled_connections,
        set_enabled_connections,
        _extend_prompt_with_connections,
    )

    slug = "scenario_conn_orch"
    create_orchestrator(slug, "Conn Scenario", "Test")
    assert get_enabled_connections(slug) == []

    conn = _github_connection(isolated_data_dir, name="Scenario GitHub")
    assert set_enabled_connections(slug, [conn["id"], conn["id"], " "]) is True
    assert get_enabled_connections(slug) == [conn["id"]]

    fake_tools = [{"name": "github_list_repos", "desc": "List repositories."}]
    with mock.patch("core.connectors.get_connection", return_value=conn), mock.patch(
        "core.github_tools.get_tools", return_value=fake_tools
    ):
        prompt = _extend_prompt_with_connections("Base prompt", slug)
    assert "## Available service connections" in prompt
    assert conn["id"] in prompt
    assert conn["name"] in prompt
    assert "github_list_repos" in prompt

    # Cleanup: reset DB state for this test process.
    set_enabled_connections(slug, [])


# 5. GitHub tools -------------------------------------------------------------
def test_scenario_github_tools_return_clean_dicts(isolated_data_dir):
    """Orchestrator tools return plain ok/result or ok/error dicts (dispatcher-friendly)."""
    from core import github_tools

    conn = _github_connection(isolated_data_dir, name="Tools GH")

    class FakeRepo:
        full_name = "tvd/scenario-repo"
        name = "scenario-repo"
        private = False
        description = "Scenario repo"
        html_url = "https://github.com/tvd/scenario-repo"
        default_branch = "main"

    class FakeRepos:
        def __iter__(self):
            return iter([FakeRepo()])

    class FakeUser:
        login = "tvd"

        def get_repos(self, sort="updated"):
            return FakeRepos()

    class FakeGithub:
        def __init__(self, token):
            self.token = token

        def get_user(self):
            return FakeUser()

    class FakeModule:
        Github = FakeGithub
        GithubException = Exception

    with mock.patch("core.github_connector._ensure_github", return_value=FakeModule()):
        result = github_tools.github_list_repos(connector_id=conn["id"])
    assert result["ok"] is True
    assert result["result"][0]["full_name"] == "tvd/scenario-repo"

    # Missing connector_id is a clean error dict, not an exception.
    result = github_tools.github_list_repos()
    assert result["ok"] is False
    assert "connector_id" in result["error"]

    # A connector failure (unknown repo) is wrapped into an error dict.
    with mock.patch("core.github_connector._ensure_github", side_effect=Exception("boom")):
        result = github_tools.github_create_repo(connector_id=conn["id"], name="x")
    assert result["ok"] is False
    assert "failed" in result["error"].lower() or "boom" in result["error"]


# 6. Dispatcher integration ---------------------------------------------------
def test_scenario_github_tool_available_through_dispatcher(isolated_data_dir):
    """The orchestration loop can call a GitHub tool through UniversalDevAgent.

    The user story: connect GitHub, enable the connection on an orchestrator,
    then let the orchestrator's dispatcher answer a 'list my repos' request.
    The LLM-facing tool name (github_list_repos) must resolve to a real callable.
    """
    from core.orchestrators import create_orchestrator, set_enabled_connections

    conn = _github_connection(isolated_data_dir, name="Dispatcher GH")
    slug = "scenario_dispatch_orch"
    create_orchestrator(slug, "Dispatch Scenario", "Test")
    assert set_enabled_connections(slug, [conn["id"]]) is True

    from dev_agent.universal_agent import UniversalDevAgent

    class FakeRepo:
        full_name = "tvd/dispatcher-repo"
        name = "dispatcher-repo"
        private = False
        description = "Dispatcher repo"
        html_url = "https://github.com/tvd/dispatcher-repo"
        default_branch = "main"

    class FakeRepos:
        def __iter__(self):
            return iter([FakeRepo()])

    class FakeUser:
        login = "tvd"

        def get_repos(self, sort="updated"):
            return FakeRepos()

    class FakeGithub:
        def __init__(self, token):
            self.token = token

        def get_user(self):
            return FakeUser()

    class FakeModule:
        Github = FakeGithub
        GithubException = Exception

    agent = UniversalDevAgent()
    agent.attach_orchestrator(slug)

    # The tool is registered and reaches the (mocked) connector layer.
    with mock.patch("core.github_connector._ensure_github", return_value=FakeModule()):
        result = agent.dispatch("github_list_repos", {"connector_id": conn["id"]})
    assert result["ok"] is True
    assert result["result"][0]["full_name"] == "tvd/dispatcher-repo"

    # Without connector_id the tool returns a clean error dict.
    result = agent.dispatch("github_list_repos", {})
    assert result["ok"] is False
    assert "connector_id" in result["error"]

    # After disabling the connection the tool is no longer callable.
    set_enabled_connections(slug, [])
    agent.attach_orchestrator(slug)
    result = agent.dispatch("github_list_repos", {"connector_id": conn["id"]})
    assert result["ok"] is False
    assert "unknown tool" in result.get("error", "").lower()
