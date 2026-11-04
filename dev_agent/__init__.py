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
from .agent_loop import (  # noqa: F401
    AgentLoopState,
    AgentResult,
    approve_and_apply,
    classify_step_strength,
    discard,
    parse_tool_calls,
    run_agent_loop,
    step_agent_loop,
)

# Convenience re-exports — the public API of the dev_agent package.
from .backup_manager import BackupEntry, BackupManager  # noqa: F401
from .safe_writer import ApplyResult, DraftResult, ProtectedFileError, SafeWriter, render_diff  # noqa: F401
from .tool_executor import TOOL_CATALOG, ToolExecutor  # noqa: F401
from .universal_agent import UniversalDevAgent, build_skill_dict_from_config, load_system_prompt  # noqa: F401
from .workspace_tools import (  # noqa: F401
    assess_workspace,
    build_project_map,
    current_workspace,
    list_snapshots,
    read_doc,
    restore_all,
    scan_folder,
    set_target_file,
    set_workspace,
    snapshot_all,
    write_doc,
    write_project_map,
)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
