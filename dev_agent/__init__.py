# SagaAI DevAgent — the embedded developer that builds the platform itself.
#
# Phase 0 of the SagaAI roadmap. Created first because every later phase is
# implemented with its help. See SagaAI_Architecture_v3-3.md section 7 and 11.
#
# Public surface:
#   - backup_manager:  versioned backups with a manifest
#   - safe_writer:     protected writes, workspace drafts, diff rendering
#   - tool_executor:   the tool set DevAgent exposes to the LLM
#   - agent_loop:      provider-independent orchestrator loop
#   - universal_agent: unified dispatcher (core + workspace tools)
#   - workspace_tools: folder/file selection, inspection, docs, snapshots
#
# Every edit is a full-file rewrite staged via safe_writer; there is no
# fragment/patch engine.

__version__ = "0.1.0"
__phase__ = "0"

from . import config  # noqa: F401

# Convenience re-exports — the public API of the dev_agent package.
from .backup_manager import BackupManager, BackupEntry  # noqa: F401
from .safe_writer import SafeWriter, ProtectedFileError, render_diff, DraftResult, ApplyResult  # noqa: F401
from .tool_executor import ToolExecutor, TOOL_CATALOG  # noqa: F401
from .agent_loop import (  # noqa: F401
    step_agent_loop,
    run_agent_loop,
    parse_tool_calls,
    classify_step_strength,
    AgentLoopState,
    AgentResult,
    approve_and_apply,
    discard,
)
from .universal_agent import UniversalDevAgent, load_system_prompt, build_skill_dict_from_config  # noqa: F401
from .workspace_tools import (  # noqa: F401
    set_workspace,
    set_target_file,
    current_workspace,
    scan_folder,
    assess_workspace,
    build_project_map,
    write_project_map,
    write_doc,
    read_doc,
    snapshot_all,
    list_snapshots,
    restore_all,
)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
