# -*- coding: utf-8 -*-
"""
scripts/regenerate_project_map.py - CANONICAL generator of PROJECT_MAP.md.

The map is a generated artifact: never edit PROJECT_MAP.md by hand (no
apply_patch / propose_file / direct writes). Update the descriptions below and
re-run this script; the file list is taken from file_versions.json so the map
covers exactly the files published on GitHub.
"""
import json
import os
import sys
from pathlib import Path

MANIFEST_NAME = "file_versions.json"
SCOPE_NOTE = ("только файлы, публикуемые на GitHub "
              "(units + selectable из file_versions.json)")


def build_publish_set(manifest: dict) -> dict:
    """Return the files a repository publishes: units[*].files + selectable.

    Mirrors the coverage rules of scripts/verify_manifest.py. Tolerates
    malformed sections. Returns {"paths": sorted list, "count": int}.
    """
    paths = set()
    units = manifest.get("units") if isinstance(manifest, dict) else None
    if isinstance(units, dict):
        for unit in units.values():
            files = (unit or {}).get("files") if isinstance(unit, dict) else None
            if isinstance(files, dict):
                paths.update(str(p) for p in files.keys())
    selectable = manifest.get("selectable") if isinstance(manifest, dict) else None
    if isinstance(selectable, dict):
        paths.update(str(p) for p in selectable.keys())
    return {"paths": sorted(paths), "count": len(paths)}


def main() -> dict:
    """Regenerate PROJECT_MAP.md for the workspace's published file set."""
    from dev_agent import config as dev_config
    root = Path(dev_config.PROJECT_ROOT).resolve()
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.exists():
        out = {"ok": False, "error": "%s not found in %s" % (MANIFEST_NAME, root)}
        print(out)
        return out
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    publish = build_publish_set(manifest)
    res = wt.write_project_map(
        roles,
        include_paths=publish["paths"],
        scope_note=SCOPE_NOTE,
    )
    print(res)
    return res


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dev_agent import workspace_tools as wt

roles = {
    "__init__.py": "Package marker",
    "app.py": "Entry point for the platform",
    "apply_methods.py": "Method finder helper used by apply_orch_patch",
    "apply_orch_patch.py": "Patch helpers for the orchestrator UI page",
    "fix_tool_executor.py": "One-off fix helper for tool executor",
    "new_propose_desc.txt": "Draft description for propose_file tool",
    "new_propose_file.txt": "Draft full-file proposal example",
    "new_read_file.txt": "Draft read_file tool description",
    "pytest.ini": "Pytest configuration",
    "requirements.txt": "Python dependencies",
    "update_catalog.py": "Tool catalog update helper",
    "update_read_desc.py": "Read-description update helper",
    "ui/__init__.py": "Package marker",
    "ui/app.py": "Main Streamlit app: sidebar navigation and page dispatch; assistants use assistant terminology; one-time workspace-meta migration on startup",
    "ui/components/__init__.py": "Package marker",
    "ui/components/workspace_picker.py": "Workspace picker component",
    "ui/pages/__init__.py": "Package marker",
    "ui/pages/assistants.py": "Assistants management page (create/edit/delete assistant profiles, files, tools)",
    "ui/pages/chat.py": "Chat page for AI assistants: selector, history, send form",
    "ui/pages/history.py": "Unified dialogue history page (assistants + employees)",
    "ui/pages/orchestrator.py": "Reusable orchestrator page (chat/history/settings incl. skills tab; no employee export/import UI; step events attach to the user message when the first LLM call fails; per-message download/copy controls on plain-prose answers; generated image visible immediately in the feed, outside the collapsed tool-result block; empty-state reset on new/reset dialog; thread workspace meta persistence; workspace-side attachment writer removed - uploads live only in history/<tid>/files)",
    "ui/pages/orchestrator_settings.py": "Orchestrator settings entry page",
    "ui/pages/orchestrators.py": "Employees (orchestrators) management page (create/open/settings/delete; export/import deferred)",
    "ui/pages/settings.py": "LLM provider settings page",
    "ui/pages/skills.py": "DEPRECATED shim -> ui/pages/assistants.py (page_assistants)",
    "ui/pages/skills_library.py": "Skills library page (install ZIP/GitHub/folder, edit metadata, delete)",
    "ui/pages/welcome.py": "Welcome / about page",
    "core/__init__.py": "Package marker",
    "core/api_errors.py": "API error hierarchy and user messages",
    "core/api_layer.py": "HTTP requests to AI providers; send_request(assistant=...) with legacy skill= alias; vision/image transports (send_vision_request with bearer/yandex_iam/deepseek_responses + detail=original, send_image_generation_request)",
    "core/assistant_creator.py": "Validation and linting helpers for assistant prompts",
    "core/assistants.py": "CRUD for AI assistant profiles and their attachment files",
    "core/auth.py": "Optional password authentication gate",
    "core/bootstrap.py": "First-run provisioning: Assistant/Employee Creator instructions, DevAgent settings, legacy skill_creator migration",
    "core/config.py": "Configuration load/save with secret encryption and env overlay",
    "core/contracts.py": "Typed dict contracts (AssistantDict etc.)",
    "core/crypto.py": "Encryption key handling and Fernet helpers",
    "core/dangerous.py": "Dangerous-code assessment for run_code/run_test",
    "core/env_loader.py": "Loads API keys from shell profiles",
    "core/files.py": "File upload helpers, token estimation, context checks",
    "core/fs.py": "Filesystem helpers (json/text read/write, ensure_dir, combine_nonempty)",
    "core/i18n.py": "Language discovery and translation helper t()",
    "core/instructions.py": "CRUD for internal instructions (Assistant Creator, Employee Creator)",
    "core/orchestrator_folders.py": "Per-orchestrator folders: bundles, functions, instructions",
    "core/orchestrators.py": "Orchestrator API; build_assistant_dicts (legacy alias build_skill_dicts); enabled_skills for orchestrator skills",
    "core/paths.py": "Base directories and thread paths",
    "core/prompt_guard.py": "Prompt-injection protection and sanitization",
    "core/recent_assistants.py": "Tracks recently used assistant IDs in session_state",
    "core/recent_skills.py": "DEPRECATED shim -> core/recent_assistants.py",
    "core/recent_workspaces.py": "Recent workspaces tracking, scoped per orchestrator (slug-keyed dict seeded from own threads; platform paths hidden; legacy flat list read as dev_agent)",
    "core/render.py": "Markdown rendering / clipboard helpers",
    "core/services.py": "Service definitions discovery (services/*.json)",
    "core/skill_creator.py": "DEPRECATED shim -> core/assistant_creator.py",
    "core/skills.py": "DEPRECATED shim -> core/assistants.py (legacy aliases)",
    "core/skills_library.py": "Standardized skills library: registry skills.json, ZIP/GitHub/folder imports, metadata for orchestrator system prompts",
    "core/threads.py": "Chat thread persistence for assistants",
    "core/threads_devagent.py": "DevAgent/orchestrator thread persistence (devagent.db) and the thread-search service layer (search_thread_messages, list_threads_filtered, read_thread_window) with access control; thread workspace meta (last folder) and the legacy-meta cleanup migration",
    "core/tools_utils.py": "Tool definitions list for the Skills/Assistants pages",
    "tests/__init__.py": "Package marker",
    "tests/_st_mock.py": "Streamlit mock for tests",
    "tests/conftest.py": "Pytest fixture bootstrap",
    "tests/patch_api_layer.py": "Test patches for api_layer",
    "tests/patch_api_layer_v2.py": "Test patches for api_layer (v2)",
    "tests/patch_orchestrator.py": "Test patches for orchestrator",
    "tests/test_app_imports.py": "Importability tests",
    "tests/test_backup_and_safewriter.py": "Backup/safe-writer tests",
    "tests/test_core_api_layer.py": "Pure api_layer unit tests",
    "tests/test_core_api_send.py": "send_request integration tests (mocked HTTP)",
    "tests/test_core_files.py": "File helpers tests",
    "tests/test_crypto.py": "Crypto tests",
    "tests/test_deepseek_responses.py": "DeepSeek Responses API tests",
    "tests/test_devagent_thread_workspace.py": "DevAgent thread workspace persistence tests",
    "tests/test_employee_management_ui.py": "UI regression tests: employee management pages render and expose no export/import employee UI",
    "tests/test_orchestrator_folders.py": "Orchestrator folder tests",
    "tests/test_orchestrator_message_controls.py": "Per-message download/copy controls tests (plain-prose answers)",
    "tests/test_orchestrator_prompt_settings.py": "UI tests: prompt settings tab - save, reset button and edited-prompt notice for the built-in orchestrator",
    "tests/test_orchestrator_image_result.py": "UI tests: generated image shown in the chat with a download button",
    "tests/test_phase1_agent_loop.py": "Agent loop tests",
    "tests/test_phase1_core_pure.py": "Pure core tests",
    "tests/test_phase1_storage.py": "Storage layer tests (assistants table + legacy aliases)",
    "tests/test_platform_bootstrap.py": "Bootstrap tests",
    "tests/test_prompt_guard_strict.py": "Prompt guard strict-mode tests",
    "tests/test_propose_file.py": "propose_file tool tests",
    "tests/test_propose_file_scenarios.py": "propose_file edge-case tests",
    "tests/test_protect_history.py": "History protection tests",
    "tests/test_recent_workspaces.py": "Recent workspaces tests",
    "tests/test_render_token_line.py": "Token line renderer tests",
    "tests/test_safety_mode.py": "Safety-mode gate tests",
    "tests/test_sanitized_approval_flow.py": "Sanitized-content approval flow tests",
    "tests/test_skills_library.py": "Skills library tests",
    "tests/test_tmp_simple.py": "Temporary placeholder test",
    "tests/test_ui_pages.py": "UI page tests",
    "tests/test_universal_developer.py": "UniversalDevAgent tests",
    "tests/scenarios/test_loop_stuck_protection_scenarios.py": "Loop-stuck protection scenarios: per-tool failure counter hints, duplicate-call flood compaction, prose loop_status continue",
    "tests/test_yandex_responses.py": "Yandex Responses API tests",
    "tests/smoke/test_app_smoke.py": "App smoke tests",
    "storage/__init__.py": "Package marker",
    "storage/db.py": "SQLAlchemy engines; auto-migration skills->assistants, skill_*->assistant_* columns",
    "storage/models.py": "ORM models: Assistant (assistants), Thread (assistant_id/assistant_name), Message, ConfigKV, Instruction, Orchestrator",
    "storage/repository.py": "High-level CRUD for assistants/threads/config/orchestrators + legacy repo_*_skill wrappers",
    "storage/repository_devagent.py": "DevAgent thread CRUD (devagent.db) plus thread-search query helpers (filtered listing, message counts, batched/window message loads); thread workspace-meta clearing for the legacy-meta cleanup migration",
    "orchestrators/autoid/instructions.json": "Test orchestrator instructions",
    "orchestrators/all_test/functions/f1.py": "Test function",
    "orchestrators/all_test/functions/f2.py": "Test function",
    "orchestrators/instr_test/instructions.json": "Test orchestrator instructions",
    "orchestrators/attached/functions/custom_tool.py": "Custom tool example",
    "orchestrators/del_test/functions/b.py": "Test function",
    "orchestrators/calc/functions/add.py": "Calculator add function example",
    "orchestrators/func_test/functions/do_thing.py": "Test function",
    "orchestrators/imported/instructions.json": "Test orchestrator instructions",
    "orchestrators/imported/orchestrator.json": "Test orchestrator bundle",
    "orchestrators/imported/functions/f1.py": "Test function",
    "orchestrators/bundle_test/orchestrator.json": "Test orchestrator bundle",
    "orchestrators/export_test/instructions.json": "Test orchestrator instructions",
    "orchestrators/export_test/orchestrator.json": "Test orchestrator bundle",
    "orchestrators/export_test/functions/f1.py": "Test function",
    "langs/en.json": "English UI strings",
    "langs/en_guide.md": "English user guide",
    "langs/ru.json": "Russian UI strings",
    "langs/ru_guide.md": "Russian user guide",
    "langs/zh-CN.json": "Simplified Chinese UI strings",
    "langs/zh_CN_guide.md": "Chinese user guide",
    "certs/russian_trusted_root_ca.pem": "Russian Trusted Root CA certificate for GigaChat TLS",
    "scripts/mig1_tool_executor.py": "Migration helper: skill->assistant in tool_executor/agent_loop",
    "scripts/mig2_fix_tool_executor.py": "Migration helper: cleanup + legacy aliases in tool_executor",
    "scripts/mig3_api_layer.py": "Migration helper: skil->assistant in api_layer",
    "scripts/mig4_repo_aliases.py": "Migration helper: repo legacy aliases",
    "scripts/mig5_orch_ui.py": "Migration helper: orchestrators/UI",
    "scripts/mig6_fix_bootstrap.py": "Migration helper: bootstrap fixes",
    "scripts/mig7_skill_tools.py": "Migration helper: add skills-library tools (list_skills_library/get_skill_folder/get_skill_prompt/get_skill_file)",
    "scripts/regenerate_project_map.py": "Regenerates PROJECT_MAP.md with assistant terminology",
    "scripts/_remove_export_import_ui.py": "One-off helper: remove employee export/import UI from orchestrator pages and language files",
    "dev_agent/README.md": "DevAgent module readme",
    "dev_agent/__init__.py": "Package marker",
    "dev_agent/agent_loop.py": "Provider-independent agent loop (single-model routing, economy mode, strength-classified tools; per-tool failure counter, duplicate-call flood compaction, cascading JSON repair)",
    "dev_agent/assistant_detector.py": "Assistant detection/creation helpers (renamed from skill_detector)",
    "dev_agent/assistant_model_resolver.py": "Auto model resolution for assistant creation",
    "dev_agent/backup_manager.py": "Per-file backup/restore manager",
    "dev_agent/config.py": "DevAgent runtime config and protected path policy; empty-state neutral root (NEUTRAL_ROOT / WORKSPACE_SELECTED / ensure_neutral_root, apply_paths selected flag)",
    "dev_agent/i18n_keys.json": "i18n keys placeholder",
    "dev_agent/llm_utils.py": "Unified LLM-call helper (assistant dict contract, legacy skill alias)",
    "dev_agent/safe_writer.py": "Safe full-file rewrite with diff/verification",
    "dev_agent/skill_detector.py": "DEPRECATED shim -> dev_agent/assistant_detector.py",
    "dev_agent/skill_model_resolver.py": "DEPRECATED shim -> dev_agent/assistant_model_resolver.py",
    "dev_agent/system_prompt.md": "DevAgent system prompt (assistant tool names, skills vs assistants section, skills-invocation tools; v3.19 AGENT.md docs-first (legacy SPEC.md removed); v3.17 compact task-state digest, read_file windows, PRAGMA-first; v3.16 empty-state Stage 0, task-journal and thread-files rules)",
    "dev_agent/task_state.py": "Per-thread task-state journal (plan/progress/handoff) with the thread-files fallback folder when no workspace is selected; compact digest (read_task_state compact=True) and budgeted injection",
    "dev_agent/tool_executor.py": "DevAgent tool set; assistant tools, legacy skill aliases, skills-library and multimodal tools; empty-state workspace guard (workspace_not_selected, neutral cwd for code=)",
    "dev_agent/universal_agent.py": "Universal dispatcher (core + workspace + orchestrator tools, incl. thread search: search_in_threads / list_threads / read_thread; empty-state guard and thread workspace-meta persistence)",
    "dev_agent/workspace_binding.py": "Per-thread workspace binding registry (RLock, thread_context, ensure_thread_active) with the empty-state neutral root",
    "dev_agent/workspace_tools.py": "Workspace layer: folders, project map, docs, snapshots; workspace_selected reporting and per-orchestrator recent workspaces",
    "services/deepseek.json": "DeepSeek service definition (vision_base_url + vision_models catalog)",
    "services/gigachat.json": "GigaChat service definition",
    "services/yandex.json": "YandexAI service definition",
    "core/ssh_connector.py": "SSH/SFTP service layer for the ssh connector (paramiko, lazy import)",
    "core/ssh_tools.py": "Orchestrator ssh_* tools for the SSH connector",
    "core/multimodal.py": "Vision/image model resolution + analyze_image/generate_image wrappers (core.multimodal)",
    "tests/test_ssh_connector.py": "Unit tests for core/ssh_connector (fake paramiko)",
    "tests/test_ssh_tools.py": "Unit tests for core/ssh_tools (SSH connector tool layer)",
    "tests/test_ui_connectors_ssh.py": "UI tests for the connectors page SSH support",
    "tests/scenarios/test_ssh_connector_scenarios.py": "Scenario tests for the SSH connector feature",
    "tests/scenarios/test_yandex_models_catalog_scenario.py": "Scenario tests for the YandexAI model catalog update",
    "tests/test_single_model_config.py": "Single-model config tests (legacy weak_* tolerance, vision/image keys, no weak_* persistence)",
    "tests/test_service_model_catalogs.py": "Vision/image model catalog tests for service profiles",
    "tests/test_orchestrator_models_settings.py": "Orchestrator settings UI tests (main model + optional vision/image selectors)",
    "defaults/instructions/multimodal_mode.md": "Global multimodal_mode instruction: analyze_image/generate_image usage rules and fallbacks",
    "defaults/instructions/github_connector.md": "Global github_connector instruction: ghr_* REST tool usage, batch publishing, and the mandatory pre-publish secret & personal-data check (STOP and ask the user on a hit)",
    "defaults/orchestrators/dev_agent/instructions/github_connector.md": "DevAgent orchestrator copy of the github_connector instruction (kept identical to the global one)",
    "tests/test_multimodal.py": "Unit tests for core.multimodal and the image request transports",
    "tests/test_multimodal_tools.py": "Unit tests for the analyze_image/generate_image tools",
    "tests/test_multimodal_instruction.py": "Tests for the multimodal_mode instruction seeding and content",
    "tests/scenarios/test_multimodal_scenarios.py": "Scenario tests for the multimodal tools (vision analysis, not_assigned, generation, provider hint)",
    "tests/scenarios/test_generated_image_feed_scenario.py": "Scenario tests for the generated image shown immediately in the chat feed (outside the collapsed tool-result block)",
    "tests/scenarios/test_orchestrator_tool_gating.py": "Gating scenarios: system-prompt version pins (v3.19/v2.11), empty-state prompt invariants and thread-search tool invariants",
    "tests/scenarios/test_orchestrator_thread_files.py": "Scenario tests: uploads land in thread files; legacy workspace manifests still re-announced",
    "tests/scenarios/test_empty_state_artifacts_scenario.py": "Scenario tests: task journal and uploads never create files in a foreign project (empty state)",
    "tests/scenarios/test_new_dialog_empty_workspace_scenario.py": "Scenario tests: a new dialog starts with no folder and never inherits a neighbour's workspace",
    "tests/scenarios/test_recent_workspace_scopes_scenario.py": "Scenario tests: per-orchestrator recent-folder menu (isolation, platform paths hidden, scoped writes)",
    "tests/scenarios/test_thread_workspace_meta_scenario.py": "Scenario tests: thread 'last folder' meta (restore, missing folder opens empty, neighbour isolation)",
    "tests/test_task_state.py": "Task-state journal tests (dialog-folder fallback in the empty state)",
    "tests/test_workspace_binding.py": "Workspace-binding registry tests (empty state, per-thread isolation)",
    "tests/test_workspace_empty_state.py": "Empty-state tests: neutral root, WORKSPACE_SELECTED flag, snapshot/restore round-trip",
    "tests/test_workspace_guard.py": "Workspace-guard tests: file tools blocked with workspace_not_selected; neutral cwd for code= runs",
    "tests/test_threads_devagent_search.py": "Unit tests for the thread-search storage/service layer: filtered listing, counts, message windows, access control",
    "tests/test_universal_agent_thread_search.py": "Unit tests for the thread-search tools in the universal dispatcher: defaults, access control, argument coercion",
    "tests/scenarios/test_thread_search_scenarios.py": "Scenario tests for the thread-search tools: current-dialog search without lists, explicit thread_id, orchestrator scope with period, search->read around a match, access control, list pagination",
    "core/github_connector_rest.py": "GitHub REST connector: batch commits and file ops; GET requests retry on network errors (Timeout/ConnectionError, up to 3 attempts, 0.5/1.5s backoff)",
    "tests/test_github_connector_rest.py": "Unit tests for the GitHub REST connector (mock sessions)",
    "tests/test_github_connector_retry.py": "Unit tests for the GitHub connector GET retry policy (network errors retried; writes not retried)",
    "tests/scenarios/test_state_recovery_scenario.py": "Scenario tests: compact task-state digest and budgeted CURRENT TASK STATE injection for large journals",
    "tests/scenarios/test_github_read_retry_scenario.py": "Scenario tests: GitHub read retries via the public API (get_user_info/create_repo), no token leaks",
}

# Descriptions migrated from the previous PROJECT_MAP.md revision (files
# whose roles were only ever present in the map) plus refreshed entries for
# files changed by the AST / fingerprint / published-scope update.
roles.update({
    "AGENT.md": "Key project information for agents (requirements, conventions, constraints); managed doc, replaces SPEC.md",
    "tests/scenarios/test_agent_md_doc_scenario.py": "Scenario tests: AGENT.md managed doc (scaffold with mandatory conventions, legacy 'spec' alias, assess states)",
    "file_versions.json": "Update manifest: app_version/channel/release_note and per-file semver + sha256 for units/selectable",
    "core/context_guard.py": "Pre-flight context guard (M4/M5): soft trim at 0.5 window, hard cap 0.8, capped output reserve min(max_out, max(4096, 0.25*window)), economy head protection, history_cap() for budget callers",
    "defaults/langs/en.json": "UI strings (English, incl. token_line_* keys)",
    "defaults/langs/ru.json": "UI strings (Russian, incl. token_line_* keys)",
    "defaults/langs/zh-CN.json": "UI strings (Chinese, incl. token_line_* keys)",
    "defaults/services/deepseek.json": "DeepSeek service definition (defaults copy; vision_base_url + vision_models catalog; declared default output limit ~32k)",
    "tests/scenarios/test_context_overflow_protection.py": "Scenario: context-overflow protection - spill of a huge tool result and the failed-spill hard error",
    "tests/scenarios/test_gigachat_orchestrator_scenario.py": "Scenario: orchestrator run on a GigaChat-like payload (leading-system fold, error visibility)",
    "tests/scenarios/test_resume_identity_scenarios.py": "Scenario: resume identity - stored thread messages round-trip byte-for-byte after reload",
    "tests/scenarios/test_yandex_responses_failures_scenario.py": "Scenario: Responses API failure handling (failed-status surfacing, blank input filtering)",
    "tests/test_agent_loop_snapshot_chain.py": "Unit: persistent TS/TC snapshot chain in the agent loop",
    "tests/test_agent_loop_snapshot_persistence.py": "Unit: snapshot chain round-trip through the thread store",
    "tests/test_agent_loop_thread_context.py": "Agent-loop thread-context tests: TS/TC snapshot chain, service-block marking, persistence round-trip",
    "tests/test_context_window_guard.py": "Context-guard tests: output-reserve formula, 32k window, economy head protection, history_cap",
    "tests/test_economy_history_budget.py": "Economy budget tests: guard-aligned budget, low-watermark trim, economy_anchor hysteresis (frozen front)",
    "tests/test_gigachat_messages.py": "GigaChat payload tests: single leading system, mid-history system blocks keep the user role, role merging",
    "tests/test_orchestrator_economy_cache.py": "Orchestrator economy-cache tests: cache-friendly prefix and anchor behaviour",
    "tests/test_token_line_cache.py": "Token-line cache tests: language in the cache key, re-render on language switch, localized labels",
    "tests/test_tool_result_size_cap.py": "Tool-result spill policy tests: head+tail preview, hard-cap fallback, UniversalDevAgent path",
    "tests/test_tool_result_storage_summary.py": "Storage-summary tests: verbatim small documents (wire==stored), batch splitter, both persist paths",
    "dev_agent/workspace_tools.py": "Workspace layer: folders, project map (AST symbols, content fingerprint, GitHub-published scope filter), docs, snapshots; workspace_selected reporting and per-orchestrator recent workspaces",
    "dev_agent/system_prompt.md": "DevAgent system prompt (assistant tool names, skills vs assistants section, skills-invocation tools; v3.19 AGENT.md docs-first (legacy SPEC.md removed); v3.18 generated-PROJECT_MAP rule; v3.17 compact task-state digest, read_file windows, PRAGMA-first; v3.16 empty-state Stage 0, task-journal and thread-files rules)",
    "scripts/regenerate_project_map.py": "CANONICAL generator of PROJECT_MAP.md: publish scope from file_versions.json (units + selectable) + responsibility dictionary; run it to regenerate the map (never hand-edit it)",
    "scripts/verify_manifest.py": "Manifest validator/maintainer: schema, coverage of git-tracked files, sha256 freshness, app_version consistency; prompt versions come from file headers on --init/--add; modes --init/--add/--fix-hashes/--json/--strict",
    "tests/scenarios/test_orchestrator_tool_gating.py": "Gating scenarios: system-prompt version pins (v3.19/v2.11), empty-state prompt invariants, generated-document rule and thread-search tool invariants",
    "tests/test_project_map_ast.py": "Unit: project map - AST symbol extraction (methods/async/nested, line ranges), regex fallback, per-file symbol cap, include-paths scope, content fingerprint, enriched header",
    "tests/scenarios/test_project_map_freshness_scenario.py": "Scenario: PROJECT_MAP regeneration - GitHub-published scope only, recorded fingerprint and staleness detection, reproducible output",
})


if __name__ == "__main__":
    main()
