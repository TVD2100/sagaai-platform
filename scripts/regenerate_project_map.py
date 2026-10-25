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
    "ui/pages/orchestrator.py": "Reusable orchestrator page (chat/history/settings incl. skills tab; no employee export/import UI; step events attach to the user message when the first LLM call fails; per-message download/copy controls on plain-prose answers; generated image visible immediately in the feed, outside the collapsed tool-result block; empty-state reset on new/reset dialog; thread workspace meta persistence; chat folder picker (Выбор папки): Авто + recent folders + native dialog, applied on send; file upload built into st.chat_input(accept_file=\"multiple\") - no separate dropzone; uploads live only in history/<tid>/files)",
    "ui/pages/orchestrator_settings.py": "Orchestrator settings page: tabbed sections, the active tab is kept across reruns and pinned after a save",
    "ui/pages/orchestrators.py": "Employees (orchestrators) management page (create/open/settings/delete; export/import deferred)",
    "ui/pages/settings.py": "LLM provider settings page",
    "ui/pages/skills.py": "DEPRECATED shim -> ui/pages/assistants.py (page_assistants)",
    "ui/pages/skills_library.py": "Skills library page (install ZIP/GitHub/folder, edit metadata, delete)",
    "ui/pages/welcome.py": "Welcome / about page",
    "core/__init__.py": "Package marker",
    "core/api_errors.py": "API error hierarchy and user messages",
    "core/api_layer.py": "HTTP requests to AI providers; send_request(assistant=...) with legacy skill= alias and optional incomplete_callback (truncated Responses detection); vision/image transports (send_vision_request with bearer/yandex_iam/deepseek_responses + detail=original, send_image_generation_request)",
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
    "core/orchestrators.py": "Orchestrator API; build_assistant_dicts (legacy alias build_skill_dicts); enabled_skills for orchestrator skills; save keeps DB write when folder mirror fails",
    "core/paths.py": "Base directories and thread paths",
    "core/prompt_guard.py": "Prompt-injection protection and sanitization",
    "core/recent_assistants.py": "Tracks recently used assistant IDs in session_state",
    "core/recent_skills.py": "DEPRECATED shim -> core/recent_assistants.py",
    "core/recent_workspaces.py": "Recent workspaces tracking, scoped per orchestrator (slug-keyed dict seeded from own threads; up to 10 entries with an optional limit; platform paths hidden; legacy flat list read as dev_agent)",
    "core/render.py": "Markdown rendering / clipboard helpers",
    "core/services.py": "Service definitions discovery (services/*.json)",
    "core/skill_creator.py": "DEPRECATED shim -> core/assistant_creator.py",
    "core/skills.py": "DEPRECATED shim -> core/assistants.py (legacy aliases)",
    "core/skills_library.py": "Standardized skills library: registry skills.json, ZIP/GitHub/folder imports, metadata for orchestrator system prompts",
    "core/threads.py": "Chat thread persistence for assistants",
    "core/threads_devagent.py": "DevAgent/orchestrator thread persistence (devagent.db) and the thread-search service layer (search_thread_messages, list_threads_filtered, read_thread_window) with access control; thread workspace meta (last folder) and the legacy-meta cleanup migration",
    "core/tools_utils.py": "Tool definitions list for the Skills/Assistants pages",
    "tests/__init__.py": "Package marker",
    "tests/_st_mock.py": "Streamlit mock for tests (records widget calls, controllable session_state; chat_input returns a seeded ChatInputValue-like object with .text/.files)",
    "tests/conftest.py": "Pytest fixture bootstrap",
    "tests/patch_api_layer.py": "Test patches for api_layer",
    "tests/patch_api_layer_v2.py": "Test patches for api_layer (v2)",
    "tests/patch_orchestrator.py": "Test patches for orchestrator",
    "tests/test_app_imports.py": "Importability tests",
    "tests/test_backup_and_safewriter.py": "Backup/safe-writer tests",
    "tests/test_cold_start_deps.py": "UI tests for the cold-start dependency warning in ui.app: missing/outdated reports render deps_missing/deps_outdated, no warning when up to date, audit runs once per session, probe errors are swallowed",
    "tests/test_dependency_check.py": "Unit tests for core.dependency_check: version parsing/comparison, registry contract, check report with stubbed importlib.metadata, and registry<->requirements.txt consistency",
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
    "tests/test_orchestrator_settings_tabs.py": "UI tests: settings tabs - stable tab key, section pinning after a save, language-change fallback",
    "tests/test_save_orchestrator_folder_sync.py": "Unit tests: save_orchestrator keeps a successful database write when the folder mirror fails",
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
    "tests/test_agent_loop_incomplete.py": "Unit: truncated (incomplete) Responses recovery in the agent loop - one bounded retry on lowered reasoning effort, event, file-context preservation",
    "tests/scenarios/test_incomplete_responses_recovery_scenario.py": "Scenarios: incomplete-responses recovery - truncation then success, partial text kept, persistent truncation terminates",
    "tests/test_agent_loop_connection_retry.py": "Connection-resilience tests for the agent loop (retrying_llm events, user message persisted before the LLM call)",
    "tests/test_sanitized_approval_flow.py": "Sanitized-content approval flow tests (loop stops, user approves, protected payload not re-triggered)",
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
    "dev_agent/agent_loop.py": "Provider-independent agent loop (single-model routing, economy mode, strength-classified tools; per-tool failure counter, duplicate-call flood compaction, cascading JSON repair; one bounded recovery retry with lowered reasoning effort when a Responses body is incomplete/truncated)",
    "dev_agent/assistant_detector.py": "Assistant detection/creation helpers (renamed from skill_detector)",
    "dev_agent/assistant_model_resolver.py": "Auto model resolution for assistant creation",
    "dev_agent/backup_manager.py": "Per-file backup/restore manager",
    "dev_agent/config.py": "DevAgent runtime config and protected path policy; empty-state neutral root (NEUTRAL_ROOT / WORKSPACE_SELECTED / ensure_neutral_root, apply_paths selected flag)",
    "dev_agent/i18n_keys.json": "i18n keys placeholder",
    "dev_agent/llm_utils.py": "Unified LLM-call helper (assistant dict contract, legacy skill alias)",
    "dev_agent/safe_writer.py": "Safe full-file rewrite with diff/verification",
    "dev_agent/skill_detector.py": "DEPRECATED shim -> dev_agent/assistant_detector.py",
    "dev_agent/skill_model_resolver.py": "DEPRECATED shim -> dev_agent/assistant_model_resolver.py",
    "dev_agent/system_prompt.md": "DevAgent system prompt (assistant tool names, skills vs assistants section, skills-invocation tools; v3.20 AGENT.md docs-first (legacy SPEC.md removed); v3.17 compact task-state digest, read_file windows, PRAGMA-first; v3.16 empty-state Stage 0, task-journal and thread-files rules)",
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
    "tests/scenarios/test_orchestrator_tool_gating.py": "Gating scenarios: system-prompt version pins (v3.20/v2.11), empty-state prompt invariants and thread-search tool invariants",
    "tests/scenarios/test_orchestrator_thread_files.py": "Scenario tests: uploads land in thread files; legacy workspace manifests still re-announced",
    "tests/scenarios/test_empty_state_artifacts_scenario.py": "Scenario tests: task journal and uploads never create files in a foreign project (empty state); files arrive via st.chat_input",
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
    "core/context_guard.py": "Pre-flight context guard (M4/M5/B4): unified input budget input_ceiling=window-max_out and input_budget=0.75*ceiling-prompt, soft trim at 0.75 window, hard cap 0.9, capped output reserve min(max_out, max(4096, 0.25*window)), economy head protection, history_cap() delegates to input_budget",
    "tests/scenarios/test_unified_context_budget_scenario.py": "Scenario: unified context budget (B4) - economy payload passes the guard unchanged, token overflow resets the front to tail, worst case stays below the hard cap",
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
    "tests/test_context_window_guard.py": "Context-guard tests: output-reserve formula, 32k window, economy head protection, unified input_ceiling/input_budget/history_cap (B4)",
    "tests/test_economy_history_budget.py": "Economy budget tests: unified (B4) input budget, tail reset on token overflow, low-watermark fallback, economy_anchor hysteresis (frozen front)",
    "tests/test_gigachat_messages.py": "GigaChat payload tests: single leading system, mid-history system blocks keep the user role, role merging",
    "tests/test_orchestrator_economy_cache.py": "Orchestrator economy-cache tests: cache-friendly prefix and anchor behaviour",
    "tests/test_token_line_cache.py": "Token-line cache tests: language in the cache key, re-render on language switch, localized labels",
    "tests/test_tool_result_size_cap.py": "Tool-result spill policy tests: head+tail preview, hard-cap fallback, UniversalDevAgent path",
    "tests/test_tool_result_storage_summary.py": "Storage-summary tests: verbatim small documents (wire==stored), batch splitter, both persist paths",
    "dev_agent/workspace_tools.py": "Workspace layer: folders, project map (AST symbols, content fingerprint, GitHub-published scope filter), docs, snapshots; workspace_selected reporting and per-orchestrator recent workspaces (top 10)",
    "dev_agent/system_prompt.md": "DevAgent system prompt (assistant tool names, skills vs assistants section, skills-invocation tools; v3.23 self-sufficiency rule for comments/docs/tests (no origin attribution); v3.21 Stage 0 recent-folders menu for a concrete project and the chat folder selector; v3.20 AGENT.md docs-first (legacy SPEC.md removed); v3.18 generated-PROJECT_MAP rule; v3.17 compact task-state digest, read_file windows, PRAGMA-first; v3.16 empty-state Stage 0, task-journal and thread-files rules)",
    "scripts/regenerate_project_map.py": "CANONICAL generator of PROJECT_MAP.md: publish scope from file_versions.json (units + selectable) + responsibility dictionary; run it to regenerate the map (never hand-edit it)",
    "scripts/verify_manifest.py": "Manifest validator/maintainer: schema, coverage of git-tracked files, sha256 freshness, app_version consistency; prompt versions come from file headers on --init/--add; modes --init/--add/--fix-hashes/--json/--strict",
    "tests/scenarios/test_orchestrator_tool_gating.py": "Gating scenarios: system-prompt version pins (v3.23/v2.11), empty-state prompt invariants, generated-document rule, Stage 0 recent-folders menu and thread-search tool invariants",
    "tests/test_project_map_ast.py": "Unit: project map - AST symbol extraction (methods/async/nested, line ranges), regex fallback, per-file symbol cap, include-paths scope, content fingerprint, enriched header",
    "tests/scenarios/test_project_map_freshness_scenario.py": "Scenario: PROJECT_MAP regeneration - GitHub-published scope only, recorded fingerprint and staleness detection, reproducible output",
    "core/rag_bm25.py": "Own BM25F lexical index of a RAG base: bm25_terms/bm25_docs tables with a field dimension in index.db (schema v3); body and chunk-header indexed as SEPARATE fields with their own df/avgdl, so a repeated header does not collapse the body IDF while a header-only product name stays searchable; conservative Cyrillic stemmer, ru/en stop-words, coordination factor, lazy rebuild of older indexes/legacy tables, incremental hooks, no API keys",
    "core/rag.py": "RAG base CRUD: folder and manifest (search_mode bm25/vector/hybrid, default bm25), explicit-slug create_base (Latin, normalised), files, chunk stats and the provider-credentials activity flag",
    "core/rag_search.py": "Base search dispatcher by mode: own BM25, vector cosine over BYOK embeddings, hybrid RRF (k=60) with bm25 degradation; hits carry mode; LLM chat-context builder",
    "core/rag_index.py": "Local SQLite RAG index: chunks plus optional vectors, BM25 sync hooks (add/update/delete/reset), fetch_chunks, cached counters",
    "core/rag_indexer.py": "Indexing pipeline: chunking and chunks always, BM25 structures kept in sync; embeddings only in vector/hybrid modes; status and progress",
    "core/assistant_tools.py": "Assistant Responses-API tool loop (local function_call execution incl. rag_search/rag_get_chunks honoring per-base search modes; assistant folder ACL)",
    "ui/pages/storage.py": "RAG storage page: bases, files, indexing, test search, per-base search-mode selector (bm25/vector/hybrid), chunk browser",
    "tests/test_rag_bm25.py": "Unit: own BM25F index (tokenization, stemming, stop-words, fielded body/meta separation, header-only product findable, repeated-header IDF kept, legacy unfielded tables rebuild, coordination, schema version, manual ranking check, lazy rebuild, incremental hooks, stats/drop)",
    "scripts/rag_eval.py": "Offline search-quality harness: indexes the benchmark corpus with the real core.rag_bm25 and scores a ground-truth query set with Recall@k / MRR / nDCG@k",
    "tests/test_rag_eval.py": "Unit: rag_eval - metrics on hand-checked rankings, fixture loading errors, end-to-end run, the recorded quality baseline and the multi-product BM25F benchmark (product-scoped queries at recall@5 = MRR = 1.0)",
    "tests/fixtures/rag_eval/corpus.json": "Benchmark corpus for rag_eval: Russian marketing chunks carrying the RAG Base Creator header (reproduces the IDF-collapse defect)",
    "tests/fixtures/rag_eval/queries.json": "Ground-truth query set for rag_eval: morphology, multi-word coordination, stop-word phrasing and exact-phrase probes",
    "tests/fixtures/rag_eval/multiproduct_corpus.json": "Benchmark corpus for the BM25F fielded index: several products whose bodies all mention the generic term while the product name lives ONLY in the header block",
    "tests/fixtures/rag_eval/multiproduct_queries.json": "Ground-truth queries for multiproduct_corpus.json: product-scoped queries that a body-only index cannot resolve",
    "tests/scenarios/test_rag_search_quality_scenario.py": "Scenarios: search quality - morphology across word forms, repeated-header IDF, stop-words/coordination, transparent upgrade of a previous-release index, and multi-product ranking where the product name lives only in the header (BM25F)",
    "tests/test_skill_build_base.py": "Unit: Rag Base Creator build_base.py - dry-run plan, bm25 build with zero embedding calls, and the --slug contract (override, normalisation, refusal without Latin, replace)",
    "tests/test_rag_create_base_slug.py": "Unit: rag.create_base explicit slug - normalisation to [a-z0-9_.-], refusal without Latin, duplicate slug, name-derived fallback",
    "tests/scenarios/test_rag_search_modes_scenario.py": "Scenarios: offline bm25 base end-to-end, vector->hybrid switch with RRF and bm25 degradation, error paths",
    "tests/test_storage_page_ui.py": "UI tests: storage base card - search-mode selectbox (current mode preselected) and Apply persisting the mode",
    "tests/test_rag_with_stats.py": "Unit: with_stats opt-out for RAG hot paths; mode-aware base activity (bm25/hybrid active without provider keys)",
    "core/tools_utils.py": "Tool definitions list for the Skills/Assistants pages; RAG tool descriptions carry the per-base search mode",
    "defaults/skills/rag_base_creator/SKILL.md": "Rag Base Creator skill: create and index RAG bases; Search modes section (bm25 default without keys - fielded BM25F over body+header, vector, hybrid), --search-mode",
    "defaults/skills/rag_base_creator/scripts/build_base.py": "Rag Base Creator helper: builds a base offline (default bm25, no embedding calls) or with embeddings for vector/hybrid",
    "tests/scenarios/test_license_consistency_scenario.py": "Scenario: license migration to PolyForm Noncommercial 1.0.0 - canonical LICENSE text, no MIT SPDX identifier among published files, README/UI/AGENT.md agreement, 2.x version and manifest consistency, documented licensing history",
    "core/folder_dialog.py": "Native OS folder picker for the Streamlit UI (no built-in chooser): tkinter filedialog in a child process with an osascript fallback on macOS, verdict via a temp UTF-8 JSON file, timeout, graceful degradation without a GUI (SAGAAI_DISABLE_FOLDER_DIALOG)",
    "tests/test_folder_dialog.py": "Unit tests for core.folder_dialog: availability probe, tkinter/osascript backends, timeout and cancel handling, non-ASCII paths, disable switch",
    "tests/scenarios/test_folder_picker_scenario.py": "Scenario tests for the chat folder picker (Выбор папки): applied on first send, mid-dialog switch, agent detour does not change the reported folder, native browse button records and stages the folder",
    "core/feed_autoscroll.py": "Client-side auto-scroll for the orchestrator chat feed: follows the agent output, pauses on a manual scroll-up (sessionStorage flag), resumes at the bottom; adds no bottom padding of its own (Streamlit 1.66 already reserves room for the pinned bottom bar)",
    "core/dependency_check.py": "Cold-start dependency audit: DEPENDENCIES registry (import name -> pip name, minimum version, required flag) kept in sync with requirements.txt; parse_version/version_lt; check_dependencies() probes importlib.metadata and reports missing/outdated packages",
    "tests/test_feed_autoscroll.py": "Unit + integration tests for core.feed_autoscroll and its wiring into _render_chat_tab: JS markers, force flag, zero-height component, idle/active/send behaviour",
    "tests/scenarios/test_feed_autoscroll_scenario.py": "Scenario tests for chat feed auto-scroll (given->when->then): forced scroll on send, follow on each agent step, pause on scroll-up / resume at bottom, idle chat untouched",
    "tests/scenarios/test_settings_tab_persists_scenario.py": "Scenario tests: a settings save keeps its own tab (given->when->then) - save pin, repeated save, chat rerun keeps the section",
    "tests/scenarios/test_inline_chat_upload_scenario.py": "Scenario tests for the inline file upload built into st.chat_input (given->when->then): text+file together, files-only submission, plain text creates no files, oversized attachment rejected",
    "tests/test_usability_fixes.py": "UI regression tests for the usability fixes (two-step delete confirmations, neutral employee caption, datetime captions, inline chat-input file upload: accept_file=\"multiple\", raw-byte persistence, size cap)",
    "ui/components/flash.py": "Deferred UI notifications: a page queues a message in session_state (flash/flash_success/flash_error/flash_warning/flash_info) and render_flash() paints and clears the queue on the NEXT render pass, so a confirmation shown right before st.rerun() is not discarded with the current pass",
    "tests/test_ui_flash.py": "Unit tests for ui.components.flash: queueing of every kind, missing/garbage session state tolerated, render_flash paints once and clears, pending queue capped",
    "tests/scenarios/test_save_confirmation_scenario.py": "Scenario tests for save confirmations (given->when->then): clicking Save on the prompt, connector, skill and employee pages paints nothing in the saving pass and shows the queued confirmation exactly once on the next pass (render_flash)",
})


roles.update({
    "core/connectors.py": "Connector storage & CRUD: encrypted credential manifest per connection (DATA_DIR/connectors/<id>/manifest.json), pending drafts without credentials (credentials_status), per-field SSH secrets, per-connection memory file (64 KiB cap), services registry",
    "core/orchestrators.py": "Orchestrator API; _extend_prompt_with_connections advertises enabled connections (per-service tool catalogs, credentials-pending marker, connection-memory quick notes); enabled_connections binding; disabled_tools blacklist; save keeps the DB write when the folder mirror fails",
    "dev_agent/universal_agent.py": "Universal dispatcher (core + workspace + orchestrator tools, incl. thread search and connection tools: list/get/create/update/delete/test connections, enabled bindings, connector services, per-connection memory); empty-state guard and thread workspace-meta persistence",
    "ui/pages/connectors.py": "Connectors page: list/create/update/delete connections, test them, fill credentials, pending-draft warning; per-service forms (GitHub REST token, SSH host/port/username + secrets)",
    "tests/test_connectors.py": "Unit tests for core.connectors: CRUD, encrypted token/secrets storage, public-view leak guard, pending drafts, connection memory",
    "tests/test_orchestrator_connections.py": "Unit tests for the connection prompt extension: enabled binding, per-service tool catalogs, credentials-pending marker, memory quick notes",
    "tests/test_ui_connectors_ssh.py": "UI tests for the connectors page SSH support and the pending-credentials warning",
    "tests/test_universal_developer.py": "UniversalDevAgent tests incl. connection-tool dispatch and disabled_tools gating",
    "tests/scenarios/test_connectors_scenarios.py": "Scenario tests for the connectors feature: lifecycle, token safety, validation, orchestrator binding/prompt, GitHub tools, dispatcher integration, draft flow",
    "tests/test_devagent_instructions.py": "Invariant tests for the canonical DevAgent instruction files (connector_creator, ssh_connector, github_connector): structure, draft flow, memory discipline, no-secrets rules",
    "defaults/orchestrators/dev_agent/instructions/connector_creator.md": "DevAgent instruction: creating service connections via pending drafts (no credentials in the chat), Connectors page fill, verification, enabling for orchestrators, memory seeding",
    "defaults/orchestrators/dev_agent/instructions/ssh_connector.md": "DevAgent instruction for the ssh_* tools: first-connection server identification, tool usage, mandatory per-connection memory file",
    "defaults/orchestrators/dev_agent/instructions/github_connector.md": "DevAgent orchestrator copy of the github_connector instruction (kept identical to the global one)",
    "defaults/instructions/ssh_connector.md": "Global ssh_connector instruction (identical to the DevAgent copy): ssh_* tools, first-connection rules, connection memory file",
    "defaults/instructions/github_connector.md": "Global github_connector instruction: ghr_* REST tool usage, batch publishing, the mandatory pre-publish secret & personal-data check (STOP and ask the user on a hit), connection memory file",
})


roles.update({
    "core/mcp_client.py": "MCP stdio client (stdlib JSON-RPC 2.0): Popen without shell, initialize/initialized handshake, tools/list with a per-process cache, text-only tools/call (non-text results rejected), 30/60s timeouts with process termination and connection lockout, per-connection process registry, stderr to log, inputSchema translation into platform tool parameters",
    "core/mcp_tools.py": "MCP tools for orchestrators: static mcp_list_tools/mcp_call_tool and dynamic mcp__<conn>__<tool> tools with a reverse registry and a 60s discovery cache; env_json decrypted only for the server process; untrusted server data sanitized",
    "core/connectors.py": "Connector storage & CRUD: encrypted credential manifest per connection (DATA_DIR/connectors/<id>/manifest.json), pending drafts without credentials (credentials_status), per-field SSH secrets, mcp service (command whitelist, args, env_json secret), per-connection memory file (64 KiB cap), services registry",
    "core/orchestrator_tools.py": "Tool catalog helpers: _connection_catalog builds per-service catalogs (github_rest ghr_*, ssh ssh_*, mcp mcp_list_tools/mcp_call_tool + dynamic per-tool mcp__ entries) from enabled connections; collect_catalog_entries(slug)",
    "dev_agent/universal_agent.py": "Universal dispatcher (core + workspace + orchestrator tools, incl. thread search and connection tools: list/get/create/update/delete/test connections, enabled bindings, connector services, per-connection memory; MCP static and dynamic tools attach for enabled mcp connections and detach when disabled); empty-state guard and thread workspace-meta persistence",
    "ui/pages/connectors.py": "Connectors page: list/create/update/delete connections, test them, fill credentials, pending-draft warning; per-service forms (GitHub REST token, SSH host/port/username + secrets, MCP command/args/env_json) with an MCP test-connection showing the server tools",
    "tests/test_mcp_client.py": "MCP client tests with a stdlib mini JSON-RPC server fixture: handshake and bad-version start, tools/list with caching, call success/isError/rpc-error, non-text result rejection, broken command, dead process, timeout with termination and lockout, process registry, stderr, schema translation",
    "tests/test_mcp_catalog.py": "MCP catalog tests: the catalog branch with an enabled connection, disabled_tools gating on mcp__ tools, a startup/list failure keeps the catalog healthy, secrets do not leak",
    "tests/scenarios/test_mcp_e2e_scenario.py": "Scenario: MCP e2e demo - a connector (python + stdlib server) is created and enabled, mcp_list_tools lists its tools, the dynamic mcp__conn__echo tool dispatches and returns echo:hello, a failing tool surfaces is_error",
    "tests/fixtures/mcp_demo_server.py": "Stdlib MCP demo server fixture for tests: one-line JSON-RPC 2.0 over stdio with echo/fail/sleep tools",
    "tests/test_devagent_instructions.py": "Invariant tests for the canonical DevAgent instruction files (connector_creator, ssh_connector, github_connector, mcp_connector): structure, draft flow, memory discipline, no-secrets rules",
    "defaults/orchestrators/dev_agent/instructions/mcp_connector.md": "DevAgent instruction for the mcp connector: creating/enabling a local stdio MCP server connection, mcp_list_tools/mcp_call_tool and dynamic tool names, error/timeout behaviour and disabling, security rules for untrusted server data",
})


roles.update({
    "core/connector_presets.py": "MCP connection presets: list_presets/resolve_preset/instantiate_preset over secret-free templates from defaults/connectors/mcp/*.json plus the per-user preset dir; the required icon renders on the MCP-tab tiles and sort_order orders them (vkusvill shipped, github_mcp kept with hidden=true); the optional hidden flag drops a preset from the catalogue while list_presets(include_hidden=True) and resolve_preset still find it by id (invalid values mean visible), so connections created from a hidden preset keep their preset fields and the tile returns once the flag is cleared; instantiate_preset takes account/secrets/pending - the default pending=True snaps the preset config into a credential-less draft and seeds the connection memory from memory_seed, while the Connectors page passes pending=False with the typed credentials so a user-created connection is active at once",
    "core/defaults.py": "Defaults layout helpers: paths of the shipped defaults tree, incl. connector_presets_dir() for the bundled connector presets (defaults/connectors)",
    "core/mcp_client.py": "MCP client, dual transport (stdlib only, no official SDK): McpClient (stdio) - Popen without shell, initialize/initialized handshake, per-process tools/list cache, text-only tools/call, 30/60s timeouts with termination and connection lockout, process registry, stderr to log; McpHttpClient (http) - POST requests with JSON or SSE responses, Mcp-Session-Id replay, Authorization header from an auth_header template resolved with the decrypted secrets per request (empty secrets skip the header), client cache dropped after secret updates; shared inputSchema translation into platform tool parameters",
    "core/mcp_tools.py": "MCP tools for orchestrators: static mcp_list_tools/mcp_call_tool and dynamic mcp__<conn>__<tool> tools with a reverse registry and a 60s discovery cache; env_json decrypted only for the server process; http transport branches; untrusted server data sanitized",
    "core/connectors.py": "Connector storage & CRUD: encrypted credential manifest per connection (DATA_DIR/connectors/<id>/manifest.json), pending drafts without credentials (credentials_status), per-field SSH secrets, mcp service (transport stdio/http, command whitelist, args, url scheme check, auth_header, preset, secret_env_fields; env_json secret plus env_pairs merge), per-connection memory file (64 KiB cap), services registry",
    "dev_agent/universal_agent.py": "Universal dispatcher (core + workspace + orchestrator tools, incl. thread search and connection tools: list/get/create/update/delete/test connections, enabled bindings, connector services, per-connection memory; list_connection_presets and the preset kwarg of create_connection; MCP static and dynamic tools attach for enabled mcp connections and detach when disabled); empty-state guard and thread workspace-meta persistence",
    "ui/pages/connectors.py": "Connectors page built as list <-> form (the assistants-page pattern): a page-level view key connectors_view = list | create | edit, and the form REPLACES the list instead of opening in an expander or on another tab. Tiles only open the create form - a GitHub/SSH service tile, an MCP preset tile (rows of three, preset icon) or the '+ Custom MCP server' tile write nothing; the connection is persisted solely by the Create button through create_connection / instantiate_preset(pending=False) with the typed credentials, so it is active from the start, and Cancel / back return to the list without writing (a failed save keeps the form open). The edit form is a full page too and resolves the preset by id (catalogue first, then resolve_preset), so a connection created from a preset hidden from the tile list keeps its preset fields; pending chat-created drafts are completed through it with a Fill credentials button. The tab strip is stateful - st.tabs(on_change='rerun', key=..., default=<label>) - because without on_change='rerun' the widget keeps no state at all, so the stored tab was ignored and every rerun fell back to the first tab; after a save the list reopens on Connections. Connections holds a card per saved connection (service icon, credential state, pending warning, test, edit, delete); test connection shows the server tools",
    "tests/test_mcp_client.py": "MCP client tests with a stdlib mini JSON-RPC server fixture: handshake and bad-version start, tools/list with caching, call success/isError/rpc-error, non-text result rejection, broken command, dead process, timeout with termination and lockout, process registry, stderr, schema translation, http auth-header resolution (empty placeholders skip the header), HTTP code hints and a 403 message that exposes no secrets",
    "tests/test_ui_connectors_mcp.py": "UI tests for the Connectors page: stateful tabs (on_change='rerun' + default), preset tiles with icons, a tile click opens the full-page create form and writes nothing, submitting that form creates an already-active connection with the typed token, Cancel creates nothing, a preset-based edit form shows only the credential fields and saves env_pairs, MCP cards render on the Connections tab, mcp is excluded from the general service tiles, and a preset hidden from the catalogue offers no tile while its connection keeps the preset edit form (resolved by id)",
    "tests/test_connector_presets.py": "Preset catalogue tests: hidden presets drop out of list_presets but stay resolvable by id (and with include_hidden=True), the hidden flag coerces booleans/strings/numbers and treats unknown values as visible, an invalid flag never breaks loading, instantiate_preset still works for a hidden preset, and the bundled catalogue ships vkusvill only while github_mcp stays resolvable with hidden=true",
    "tests/scenarios/test_mcp_preset_e2e_scenario.py": "Scenario: MCP preset e2e - a local fake Vkusvill HTTP MCP server on 127.0.0.1; the user path preset -> pending draft -> token via env_pairs -> private HTTP tool call; asserts the Authorization header reaches the server, session-id replay, tokenless public tools and a clean auth error without credentials",
    "tests/scenarios/test_mcp_tab_ui_scenario.py": "Scenario tests for the Connectors page user path (given->when->then): three tabs render, an MCP preset tile opens the create form without touching the DB, submitting it stores an ACTIVE connection whose token is decrypted on read and lands on the Connections tab, a GitHub service tile opens the form, Cancel writes nothing and a repeated open + create stores an active connection while the token never renders",
    "defaults/connectors/mcp/vkusvill.json": "Vkusvill MCP preset (http, https://mcp.vkusvill.ru/mcp, icon for the MCP tab tile): catalog and recipe tools work without credentials, the optional VKUSVILL_TOKEN unlocks personal data; secret_env_fields describe the token field",
    "defaults/connectors/mcp/github_mcp.json": "GitHub MCP preset (http, https://api.githubcopilot.com/mcp/, icon for the MCP tab tile) shipped with hidden=true: the Copilot endpoint rejects classic ghp_* PATs with 403 access denied and requires a fine-grained github_pat_* token carrying the account permission Copilot Requests, sent as Authorization Bearer; the preset is off the tile list while staying resolvable by id",
    "defaults/orchestrators/dev_agent/instructions/mcp_connector.md": "DevAgent instruction for the mcp connector: preset flow (tiles on the MCP tab open the create form with the preset config applied and write nothing until Create, so a page-created connection is active at once; the agent itself keeps the draft-first flow and the user fills only the credentials via the Fill credentials button), why GitHub is not offered as an MCP preset (Copilot endpoint needs a fine-grained PAT with Copilot Requests; github_rest is the supported path, a local GitHub MCP server goes through the custom tile), the '+ Custom MCP server' tile, stdio and http transports, authorization errors 401/403 (token type incl. the ghp_*/github_pat_* distinction, auth_header template, no retry loops), mcp_list_tools/mcp_call_tool and dynamic tool names, error/timeout behaviour and disabling, security rules for untrusted server data",
    "AGENT.md": "Key project information for agents (requirements, conventions, constraints); FR11 documents the connector architecture incl. the list <-> form Connectors page (tiles only open the form, Create stores an active connection) and the stateful tab strip; managed doc, replaces SPEC.md",
    "ARCHITECTURE.md": "Architecture description: platform layers and flows, incl. the connector system with the dual-transport MCP client, the preset subsystem, the list <-> form Connectors UI with stateful tabs and MCP 401/403 diagnosis",
    "README.md": "User-facing documentation: installation, usage, dependencies; the Connectors section covers the list <-> form page (tiles open a form, Create stores an active connection) and the MCP presets (Vkusvill tile plus the custom-server tile; GitHub is handled through the github_rest connection)",
})


roles.update({
    "core/orchestration/__init__.py": "Package marker for the multi-agent orchestration layer",
    "core/orchestration/contracts.py": "Run contract types for delegated runs: AgentBrief (goal, constraints, artifacts_in as a name->path dict, acceptance criteria), SpawnRequest (agent_slug, brief, parent_run_id, depth), InvokeRequest (system, user, context_files, parent_run_id), Artifact, AgentRunResult (run_id, status, stop_reason, summary, artifacts_out, steps_used, error_code), CancelReport plus the status/stop-reason/error-code constants and to_dict serialization for every structure",
    "core/orchestration/invoke.py": "invoke_once: exactly one model call for an assistant - no tool loop, no RAG, no file access; service and model resolve from the parent configuration, context_files are rendered as '### File: <name>' blocks",
    "core/orchestration/service.py": "OrchestrationService: run registry and run tree, ensure_run/run/release_tree, cascade cancel with a CancelledByParent reason, subscriber feed, slot pool (max_active_subagents), max_depth, total_subagent_runs accounting, per-run timeout and step cap, derived child thread ids ({parent}__s{run_id[:8]}) and the child tool bans",
    "core/orchestration/tools.py": "spawn_agent and invoke_assistant tool wrappers: argument validation, allowlist rights (agent_not_allowed, unknown_agent, assistant_not_allowed, unknown_assistant), invalid_args and not_implemented, catalog entries whose descriptions state the boundaries",
    "tests/test_orchestration_contracts.py": "Unit tests for the run contract types: defaults, serialization and the status/stop-reason vocabulary",
    "tests/test_orchestration_invoke.py": "Unit tests for invoke_once: one call only, no tool loop, context_files rendering, service/model resolution and error mapping",
    "tests/test_orchestration_service.py": "Unit tests for OrchestrationService: run registry and tree, slot pool, depth and total-run limits, timeout, cascade cancellation, child tool bans, invoke slot accounting",
    "tests/test_orchestration_tools.py": "Unit tests for the spawn_agent/invoke_assistant wrappers: rights checks, unknown targets, argument validation and catalog entries",
    "tests/test_delegation_fanout.py": "Unit tests for fan-out in the executing phase: whole-batch validation before the pool, parallel child runs, per-run event attribution and journal/binding isolation",
    "tests/test_orchestrator_delegation_settings.py": "UI tests for the Delegation settings tab: per-entity checkboxes, stale ids shown with a warning, six limit inputs with real tooltips, save/failure paths",
    "tests/scenarios/test_delegation_catalog_scenario.py": "Scenario tests for what the model sees: spawn_agent always offered, invoke_assistant only with a live assistant, disabled tools removed, limits clamped, catalog and runtime rights read the same allowlist",
    "tests/scenarios/test_delegation_run_scenario.py": "Scenario tests for one delegated run: two children reporting into the dialog feed, distinct threads and journals, Stop cascading to children, the run budget with a readable refusal, tree release at dialog end",
})

roles.update({
    "core/url_fetch.py": "URL-fetch transport: fetch_endpoint resolves fetch_base_url (or derives the sibling /v1/tools/fetch from base_url, never duplicating the version segment), validate_fetch_url enforces http/https, the 8192-character cap, no embedded credentials and no local/private hosts (SSRF guard, literal addresses only), fetch_page_content posts {url} with the provider bearer key and returns {title, content} in Markdown, extract_pdf_markdown turns PDF bytes into '### Page N' sections via pdfplumber with a pypdf fallback, selenium_page_text renders a page in headless Chrome and returns its visible text; every failure is raised through the APIError contract (ApiKeyMissing/ProviderHTTP/RequestTimeout/Network/ProviderResponse) or SeleniumUnavailableError",
    "core/services.py": "Service definitions discovery (services/*.json, defaults win) plus capability catalogs: reasoning-effort options (service-wide and per-model), embedding/rag models, vision and image-generation models and the URL-fetch models (get_fetch_models / svc_supports_fetch); a profile without fetch_models does not support provider-side fetching",
    "core/orchestrator_tools.py": "Tool catalog for orchestrators: core + workspace + connection + custom functions, the English '## Available tools' prompt block and the system-functions list; capability-gated tools (_CONDITIONAL_TOOLS, is_conditional_tool_enabled) keep url_fetch out of the catalog, the prompt block and the settings list until a fetch model is assigned or Selenium is enabled, while disabled_tools stays a plain blacklist",
    "core/orchestrators.py": "Orchestrator API: CRUD, assistant dicts, get_web_search_config/get_fetch_config (service, model, selenium flag, main-model fallback for the Selenium branch), economy config, export/import, prompt extension with skills, instructions, connections, RAG bases and the tool catalog",
    "dev_agent/tool_executor.py": "DevAgent tool dispatcher: file/patch/verify tools, run_code/run_test with the danger gate, assistant and skill tools, web_search with the DeepSeek Anthropic route, url_fetch (PDF download and page extraction, Selenium render plus main-model Markdown conversion, or the provider Search/Fetch API; URL validation, [DATA_FROM_URL_FETCH] labelling, 30k-char cap, configuration gate, page_fetch legacy alias), multimodal analyze_image/generate_image, RAG and history tools",
    "ui/pages/orchestrator.py": "Orchestrator page: chat feed, history and settings sections (models incl. the optional url-fetch selector fed only by fetch-capable providers and the 'Use Selenium' checkbox, prompt, economy, functions, instructions, skills, RAG bases, connections, other); builds the dispatcher and hands it the web-search and url-fetch configs",
    "defaults/services/deepseek.json": "DeepSeek service profile (defaults): responses endpoint, the Anthropic web-search endpoint, the vision chat/completions endpoint, the URL-fetch endpoint fetch_base_url (https://api.deepseek.com/v1/tools/fetch) with the fetch_models catalog (deepseek-flash, deepseek-v4-pro), models and reasoning-effort options",
    "tests/test_url_fetch.py": "Unit tests for the optional URL-fetch capability: catalog helpers and the shipped DeepSeek profile, endpoint derivation, URL validation, the transport (request shape, bearer key, nested envelopes, APIError mapping), sanitizer labelling and cap, the tool gate and its restricted arguments, get_fetch_config resolution, the catalog/prompt/system-tools gate and the Models tab UI",
    "tests/test_url_fetch_transport.py": "Unit tests for the URL-fetch transport extras: PDF text extraction (page sections, empty scans, pdfplumber-to-pypdf fallback), the SSRF guard on literal private addresses, Selenium text rendering with a mocked driver and the unavailable-dependency error",
    "tests/scenarios/test_url_fetch_scenario.py": "Scenario tests (given->when->then) for the URL-fetch user path: no source means no tool and a structured refusal, saving DeepSeek/deepseek-flash or enabling Selenium alone switches url_fetch on in the prompt and dispatch (page labelled as untrusted data), a PDF link is downloaded and extracted, clearing both sources or a vanished catalog entry switches it off, and the Models tab offers only fetch-capable providers while persisting the pair",
    "AGENT.md": "Key project information for agents (requirements, conventions, constraints); FR6 documents the optional URL-fetch model (fetch-capable providers only), the Selenium switch and the url_fetch tool with its PDF branch; managed doc, replaces SPEC.md",
    "ARCHITECTURE.md": "Architecture description: platform layers and flows, incl. the connector system, and the optional URL-fetch capability (core/url_fetch transport, PDF extraction, Selenium branch, capability-gated url_fetch in the tool catalog)",
    "README.md": "User-facing documentation: installation, usage, dependencies; the Employees section covers the optional page-fetch model, the Use Selenium checkbox and the url_fetch tool (incl. PDF links), alongside the connectors and MCP pages",
})


roles.update({
    "core/orchestrators.py": "Orchestrator API: CRUD, assistant dicts, get_web_search_config/get_fetch_config, economy config, export/import, prompt extension with skills, instructions, connections, RAG bases and the tool catalog; delegation rights - get_allowed_agents/set_allowed_agents (the own slug is never stored), get_allowed_assistants/set_allowed_assistants (validated against live assistant cards) and get_delegation_limits/set_delegation_limits (six limits clamped on write)",
    "core/orchestrator_tools.py": "Tool catalog for orchestrators: core + workspace + delegation + connection + custom functions, the English '## Available tools' prompt block and the system-functions list; capability-gated tools (_CONDITIONAL_TOOLS) keep url_fetch out of the catalog until a fetch model is assigned or Selenium is enabled, invoke_assistant is offered only with a non-empty assistant allowlist while spawn_agent is always offered, and disabled_tools stays a plain blacklist",
    "dev_agent/agent_loop.py": "Provider-independent agent loop (single-model routing, economy mode, strength-classified tools); carries the run identity (run_id/parent_run_id/depth) in state and fans out independent spawn_agent calls in the executing phase - the whole batch is validated for rights and arguments before the slot pool, children run in parallel and emit events tagged with their run_id",
    "dev_agent/task_state.py": "Per-thread task journal (TASK_STATE__<thread_id>.md) with section budgets and the compact digest; the thread id is an explicit parameter (current_thread_id, ensure_task_state_file, archive_and_start_task) with config.ACTIVE_THREAD_ID as a fallback, and thread_scope pins a child thread in the current Python thread so parallel children never overwrite each other's journal",
    "dev_agent/workspace_binding.py": "Per-thread workspace binding registry under an RLock; thread_context applies and restores a thread's state, bind_child_thread gives a child run the parent's workspace with its own derived thread ({parent}__s{run_id[:8]}, no target_file) and unbind_thread releases it when the run ends",
    "dev_agent/universal_agent.py": "Tool dispatcher for orchestrators: validate_tool_call resolves the run's disabled_tools first, then the argument specs, then the rights, without touching process globals; WORKSPACE_TOOL_ARGS describes the spawn_agent and invoke_assistant arguments",
    "ui/pages/orchestrator.py": "Orchestrator page: chat feed, history and settings sections (models, prompt, economy, functions, instructions, skills, RAG bases, connections, delegation, other); the Delegation tab holds the agent/assistant allowlists and the six limits, stale ids are shown with a warning, run events are grouped by run_id and the Stop button cancels the run tree",
    "ui/pages/orchestrator_settings.py": "Orchestrator settings page: tabbed sections (ten tabs including Delegation), the active tab is kept across reruns and pinned after a save",
    "AGENT.md": "Key project information for agents (requirements, conventions, constraints); FR18 documents multi-agent orchestration - spawn_agent/invoke_assistant, the run contract, six delegation limits, cascade cancellation, fan-out and the child tool bans; managed doc, replaces SPEC.md",
    "ARCHITECTURE.md": "Architecture description: platform layers and flows, incl. the connectors, the optional URL-fetch capability and the multi-agent orchestration package (run contract, OrchestrationService with the run tree and limits, tool wrappers, fan-out)",
    "README.md": "User-facing documentation: installation, usage, dependencies; the Employees section covers delegation (spawn_agent/invoke_assistant, the Delegation tab limits and cascade cancellation) next to the page-fetch model and the connectors",
})


roles.update({
    "core/version.py": "Single source of truth for the SagaAI platform version (semver __version__ plus a documented release history); 2.10.0 records multi-agent orchestration and 2.11.0 records document text extraction (read_document), explicit delegation contexts and the subagent wording",
    "core/document_text.py": "Text extraction for documents on disk and in dialog attachments: PDF, DOCX, DOC, PPTX, XLSX, XLS, RTF, ODT, HTML and plain text via extract_text(filename, data), plus read_document_path(path, offset, limit, max_chars) with a line window; an unknown format or a missing optional library returns a warning line instead of raising, so the calling tool always answers with text",
    "core/threads_devagent.py": "DevAgent/orchestrator thread persistence (devagent.db) and the thread-search service layer (search_thread_messages, list_threads_filtered, read_thread_window) with access control; thread workspace meta (last folder) and the legacy-meta cleanup migration; read_thread_file extracts attachment text of binary documents through core.document_text",
    "core/orchestration/invoke.py": "invoke_once: exactly one model call for an assistant - no tool loop, no RAG, no file access; service and model resolve from the parent configuration, context_files are rendered as '### File: <name>' blocks and documents (PDF, DOCX, PPTX, XLSX, RTF, ODT, HTML) are read through core.document_text so one file stays one call, while an unreadable file lands in skipped_files",
    "core/orchestration/service.py": "OrchestrationService: run registry and the run tree - ensure_run, run, cancel(cascade=True), subscribe, release_tree; limits and slot semaphores per dialog, child thread derived as {parent}__s{run_id[:8]}; the child task text carries the absolute paths of the parent workspace and the parent dialog files folder",
    "dev_agent/tool_executor.py": "DevAgent tool set; assistant tools, legacy skill aliases, skills-library, document and multimodal tools; read_document reads a document on disk by absolute path with a line window through core.document_text; empty-state workspace guard (workspace_not_selected, neutral cwd for code=)",
    "tests/test_document_text.py": "Unit tests for core.document_text: per-format extraction, line window, warnings for unknown formats and missing libraries",
    "tests/test_read_document.py": "Tests for the read_document tool: path resolution, line window, empty state and error answers",
    "tests/test_orchestrator_delegation_prompt.py": "Tests for the delegation blocks of the system prompt: available agents (self plus allowlist) and available assistants with ids, omitted when the assistant allowlist is empty",
    "tests/scenarios/test_documents_and_delegation_scenario.py": "Scenario tests for documents and delegation: a document goes to an assistant as one invoke_assistant call without run_code, a missing file is skipped, the prompt lists agent and assistant ids, a subagent task carries absolute paths",
    "AGENT.md": "Key project information for agents (requirements, conventions, constraints); FR18 documents multi-agent orchestration - spawn_agent/invoke_assistant, the run contract, six delegation limits, cascade cancellation, fan-out, absolute paths in the child task and the child tool bans; managed doc, replaces SPEC.md",
    "ARCHITECTURE.md": "Architecture description: platform layers and flows, incl. the connectors, the optional URL-fetch capability, document text extraction and the multi-agent orchestration package (run contract, OrchestrationService with the run tree and limits, tool wrappers, fan-out, delegation prompt blocks)",
    "README.md": "User-facing documentation: installation, usage, dependencies; the Employees section covers delegation (spawn_agent/invoke_assistant, the Delegation tab limits and cascade cancellation) and document reading (read_document, read_thread_file) next to the page-fetch model and the connectors",
})


roles.update({
    "core/orchestration/runs.py": "Durable run store for delegated runs: save/load/list_runs with atomically written timestamped records under <install>/.dev_agent/orchestration/runs/<root_run_id>, duplicate sweeping, corrupt files set aside, heal() that turns a dead running claim into failed/stale_run (stale heartbeat, foreign boot_id, missing pid), prune_root/remove_older_than housekeeping and the atomic O_CREAT|O_EXCL claim_notice/notice_claimed pair that lets a notice be published exactly once",
    "core/orchestration/resume.py": "build_resume turns a finished run into a resumable next run: the child journal is read by the absolute journal_path carried by the record (the ordinary reader would resolve the PARENT's journal), step titles are normalized before comparing plan against the [x] progress list, finished steps go into 'Already done, do NOT redo', the rest into 'Still pending', the hint is clipped to MAX_HINT_CHARS=1200, an empty goal with no pending work is refused with resume_unavailable and a chain deeper than MAX_RESUME_CHAIN with resume_chain_limit",
    "tests/test_orchestration_runs.py": "Unit tests for the durable run store: atomic single-file writes, concurrent writers, corrupt records set aside, pruning, healing of a stale/foreign/dead claim, the atomic notice claim across threads, store-root isolation and traversal-safe run ids",
    "tests/test_orchestration_resume.py": "Unit tests for build_resume and the resume path of the service: journal by absolute path, plan/progress title normalization, done and pending lists, hint cap, refusal without material, chain limit, foreign-run confinement and tree/depth inheritance",
    "tests/test_orchestration_background.py": "Unit tests for background children: the launch returns before the child finishes, the durable record is written and healed, notices land in the tree mailbox and are published once, and a cancelled child reports cancelled rather than its own done",
    "tests/test_orchestration_subagent_tools.py": "Unit tests for the observation tools: subagent_status over one run, a whole tree and a bounded wait, read_subagent_state sections and caps, and resume_subagent with and without an explicit request",
    "tests/test_agent_loop_child_mode.py": "Unit tests for subagent autonomy: a child run refuses a call that needs human confirmation with confirmation_required instead of stopping, and the child tool catalog is reduced by child_disabled_tools",
    "tests/test_agent_loop_subagent_notices.py": "Unit tests for notice delivery in the loop: formatting of [SUBAGENT_NOTICE], the hidden system message in the calling_llm phase, destructive draining and the auto-continue wording",
    "tests/test_orchestrator_subagent_ui.py": "UI tests for the background subagent parts of the chat page: the running-runs status line, the _notice badge and the st.fragment refresh",
    "tests/test_system_prompt_subagents.py": "Tests for the subagent sections of the DevAgent system prompt: the launch rules, the background mode, the observation tools, the resume contract and the capped resume chain",
    "tests/scenarios/test_background_delegation_scenario.py": "Scenario tests (given->when->then) for two background children: the dialog keeps working, status answers for one run and for the tree, each child reports exactly once through a hidden system line, Stop wins over the child's own done and a cancelled child stays resumable",
    "tests/scenarios/test_subagent_resume_scenario.py": "Scenario tests (given->when->then) for resuming a child: the first attempt stops half-way, read_subagent_state shows the handoff, resume finishes the remaining file without touching the finished one, inherits the tree and its budget, refuses a foreign run and stops at the resume chain ceiling",
    "AGENT.md": "Key project information for agents (requirements, conventions, constraints); FR18 documents multi-agent orchestration - spawn_agent/invoke_assistant, the run contract, six delegation limits, cascade cancellation, fan-out, absolute paths in the child task, the child tool bans and the background subagents (durable run store, notices, resume, child autonomy); managed doc, replaces SPEC.md",
    "ARCHITECTURE.md": "Architecture description: platform layers and flows, incl. the connectors, the optional URL-fetch capability, document text extraction and the multi-agent orchestration package (run contract, OrchestrationService with the run tree and limits, tool wrappers, fan-out, delegation prompt blocks) plus background subagents - durable run store and heal, the notice mailbox, the resume builder and child autonomy",
    "README.md": "User-facing documentation: installation, usage, dependencies; the Employees section covers delegation (spawn_agent/invoke_assistant, the Delegation tab limits and cascade cancellation), background subagents (status, journal, resume, confirmation-free autonomy) and document reading (read_document, read_thread_file) next to the page-fetch model and the connectors",
})


if __name__ == "__main__":
    main()
