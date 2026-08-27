# -*- coding: utf-8 -*-
"""Tests for UniversalDevAgent connection tool registration."""
import os
import sys
import tempfile
import shutil
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage.db import reset_engine, reset_devagent_engine


@pytest.fixture
def isolated_data_dir():
    tmp = tempfile.mkdtemp(prefix='sagaai_test_disp_')
    old_data_dir = os.environ.get('SAGAAI_DATA_DIR')
    os.environ['SAGAAI_DATA_DIR'] = tmp

    import core.paths
    old_values = {}
    for attr in ('DATA_DIR', 'DB_PATH', 'DEVAGENT_DB_PATH', 'HISTORY_DIR', 'SYSTEM_PROMPTS_DIR'):
        old_values[attr] = getattr(core.paths, attr, None)

    core.paths.DATA_DIR = tmp
    core.paths.DB_PATH = os.path.join(tmp, 'sagaai.db')
    core.paths.DEVAGENT_DB_PATH = os.path.join(tmp, 'devagent.db')
    core.paths.HISTORY_DIR = os.path.join(tmp, 'history')
    core.paths.SYSTEM_PROMPTS_DIR = os.path.join(tmp, 'system_prompts')

    reset_engine()
    reset_devagent_engine()
    yield tmp
    reset_engine()
    reset_devagent_engine()

    if old_data_dir:
        os.environ['SAGAAI_DATA_DIR'] = old_data_dir
    else:
        os.environ.pop('SAGAAI_DATA_DIR', None)

    for attr in ('DATA_DIR', 'DB_PATH', 'DEVAGENT_DB_PATH', 'HISTORY_DIR', 'SYSTEM_PROMPTS_DIR'):
        if old_values.get(attr) is not None:
            setattr(core.paths, attr, old_values[attr])

    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def orch_slug(isolated_data_dir):
    from core.orchestrators import create_orchestrator
    slug = 'conn_disp'
    create_orchestrator(slug, 'Conn Disp', 'Test')
    return slug


class TestDispatcherConnectionTools:

    def test_no_connections_no_github_tools(self, orch_slug):
        from dev_agent.universal_agent import UniversalDevAgent
        agent = UniversalDevAgent()
        agent.attach_orchestrator(orch_slug)
        names = ('github_list_repos', 'github_create_repo', 'github_upload_file', 'github_update_file', 'github_read_file')
        for name in names:
            assert name not in agent._extra

    def test_enabled_connections_register_github_tools(self, orch_slug):
        from dev_agent.universal_agent import UniversalDevAgent
        from core.orchestrators import set_enabled_connections
        set_enabled_connections(orch_slug, ['conn_github'])
        agent = UniversalDevAgent()
        agent.attach_orchestrator(orch_slug)
        names = ('github_list_repos', 'github_create_repo', 'github_upload_file', 'github_update_file', 'github_read_file')
        for name in names:
            assert name in agent._extra

    def test_dispatch_github_tool_missing_connector_id(self, orch_slug):
        from dev_agent.universal_agent import UniversalDevAgent
        from core.orchestrators import set_enabled_connections
        set_enabled_connections(orch_slug, ['conn_github'])
        agent = UniversalDevAgent()
        agent.attach_orchestrator(orch_slug)
        result = agent.dispatch('github_list_repos', {})
        assert result.get('ok') is False
        assert 'connector_id' in result.get('error', '')

    def test_disable_connections_removes_github_tools(self, orch_slug):
        from dev_agent.universal_agent import UniversalDevAgent
        from core.orchestrators import set_enabled_connections
        set_enabled_connections(orch_slug, ['conn_github'])
        agent = UniversalDevAgent()
        agent.attach_orchestrator(orch_slug)
        assert 'github_list_repos' in agent._extra
        set_enabled_connections(orch_slug, [])
        agent.attach_orchestrator(orch_slug)
        assert 'github_list_repos' not in agent._extra
