# Карта проекта (PROJECT_MAP)

Автоматически поддерживается DevAgent. Структура - детерминированная, описания назначения файлов - генерируются моделью. Вы можете править этот файл вручную; при следующей доработке DevAgent учтёт ваши правки.

- Обновлено: `2026-08-27T15:09:44+00:00`
- Файлов: **232**
- Языки: Config: 1, JSON: 20, Markdown: 40, PEM certificate: 1, Python: 174, Text: 1

## Файлы и назначение

| Файл | Язык | Назначение | Зависит от |
| --- | --- | --- | --- |
| `COMPARISON.md` | Markdown | _(описание не задано)_ | - |
| `__init__.py` | Python | Package marker | - |
| `app.py` | Python | Entry point for the platform | - |
| `pytest.ini` | Config | Pytest configuration | - |
| `requirements.txt` | Text | Python dependencies | - |
| `ui/__init__.py` | Python | Package marker | - |
| `ui/app.py` | Python | Main Streamlit app: sidebar navigation and page dispatch; assistants use assistant terminology | - |
| `ui/components/__init__.py` | Python | Package marker | - |
| `ui/components/workspace_picker.py` | Python | Workspace picker component | - |
| `ui/pages/__init__.py` | Python | Package marker | - |
| `ui/pages/assistants.py` | Python | Assistants management page (create/edit/delete assistant profiles, files, tools) | - |
| `ui/pages/chat.py` | Python | Chat page for AI assistants: selector, history, send form | - |
| `ui/pages/connectors.py` | Python | _(описание не задано)_ | - |
| `ui/pages/history.py` | Python | Unified dialogue history page (assistants + employees) | - |
| `ui/pages/orchestrator.py` | Python | Reusable orchestrator page (chat/history/settings incl. skills tab; no employee export/import UI) | - |
| `ui/pages/orchestrator_settings.py` | Python | Orchestrator settings entry page | - |
| `ui/pages/orchestrators.py` | Python | Employees (orchestrators) management page (create/open/settings/delete; export/import deferred) | - |
| `ui/pages/settings.py` | Python | LLM provider settings page | - |
| `ui/pages/skills.py` | Python | DEPRECATED shim -> ui/pages/assistants.py (page_assistants) | - |
| `ui/pages/skills_library.py` | Python | Skills library page (install ZIP/GitHub/folder, edit metadata, delete) | - |
| `ui/pages/stats.py` | Python | _(описание не задано)_ | - |
| `ui/pages/storage.py` | Python | _(описание не задано)_ | - |
| `ui/pages/welcome.py` | Python | Welcome / about page | - |
| `core/__init__.py` | Python | Package marker | - |
| `core/api_errors.py` | Python | API error hierarchy and user messages | - |
| `core/api_layer.py` | Python | HTTP requests to AI providers; send_request(assistant=...) with legacy skill= alias | - |
| `core/assistant_creator.py` | Python | Validation and linting helpers for assistant prompts | - |
| `core/assistant_folders.py` | Python | _(описание не задано)_ | - |
| `core/assistant_nav.py` | Python | _(описание не задано)_ | - |
| `core/assistant_tools.py` | Python | _(описание не задано)_ | - |
| `core/assistants.py` | Python | CRUD for AI assistant profiles and their attachment files | storage |
| `core/auth.py` | Python | Optional password authentication gate | - |
| `core/bootstrap.py` | Python | First-run provisioning: Assistant/Employee Creator instructions, DevAgent settings, legacy skill_creator migration | - |
| `core/config.py` | Python | Configuration load/save with secret encryption and env overlay | storage |
| `core/connectors.py` | Python | _(описание не задано)_ | - |
| `core/crypto.py` | Python | Encryption key handling and Fernet helpers | - |
| `core/dangerous.py` | Python | Dangerous-code assessment for run_code/run_test | - |
| `core/default_imports.py` | Python | _(описание не задано)_ | storage |
| `core/defaults.py` | Python | _(описание не задано)_ | - |
| `core/entity_sync.py` | Python | _(описание не задано)_ | storage |
| `core/env_loader.py` | Python | Loads API keys from shell profiles | - |
| `core/files.py` | Python | File upload helpers, token estimation, context checks | - |
| `core/fs.py` | Python | Filesystem helpers (json/text read/write, ensure_dir, combine_nonempty) | - |
| `core/github_connector.py` | Python | _(описание не задано)_ | - |
| `core/github_tools.py` | Python | _(описание не задано)_ | - |
| `core/i18n.py` | Python | Language discovery and translation helper t() | - |
| `core/instructions.py` | Python | CRUD for internal instructions (Assistant Creator, Employee Creator) | - |
| `core/orchestrator_folders.py` | Python | Per-orchestrator folders: bundles, functions, instructions | storage |
| `core/orchestrators.py` | Python | Orchestrator API; build_assistant_dicts (legacy alias build_skill_dicts); enabled_skills for orchestrator skills | storage |
| `core/paths.py` | Python | Base directories and thread paths | - |
| `core/prompt_guard.py` | Python | Prompt-injection protection and sanitization | - |
| `core/prompt_improver.py` | Python | _(описание не задано)_ | - |
| `core/rag.py` | Python | _(описание не задано)_ | - |
| `core/rag_chunker.py` | Python | _(описание не задано)_ | - |
| `core/rag_embeddings.py` | Python | _(описание не задано)_ | - |
| `core/rag_index.py` | Python | _(описание не задано)_ | - |
| `core/rag_indexer.py` | Python | _(описание не задано)_ | - |
| `core/rag_search.py` | Python | _(описание не задано)_ | - |
| `core/recent_assistants.py` | Python | Tracks recently used assistant IDs in session_state | - |
| `core/recent_workspaces.py` | Python | Recent workspaces tracking | storage |
| `core/render.py` | Python | Markdown rendering / clipboard helpers | - |
| `core/services.py` | Python | Service definitions discovery (services/*.json) | - |
| `core/skills.py` | Python | DEPRECATED shim -> core/assistants.py (legacy aliases) | - |
| `core/skills_library.py` | Python | Standardized skills library: registry skills.json, ZIP/GitHub/folder imports, metadata for orchestrator system prompts | - |
| `core/statistics.py` | Python | _(описание не задано)_ | - |
| `core/threads.py` | Python | Chat thread persistence for assistants | storage |
| `core/threads_devagent.py` | Python | DevAgent/orchestrator thread persistence (devagent.db) | storage |
| `core/tools_utils.py` | Python | Tool definitions list for the Skills/Assistants pages | - |
| `core/version.py` | Python | _(описание не задано)_ | - |
| `tests/__init__.py` | Python | Package marker | - |
| `tests/_st_mock.py` | Python | Streamlit mock for tests | - |
| `tests/_test_isolation.py` | Python | _(описание не задано)_ | - |
| `tests/conftest.py` | Python | Pytest fixture bootstrap | - |
| `tests/test_agent_loop_json_repair.py` | Python | _(описание не задано)_ | - |
| `tests/test_app_imports.py` | Python | Importability tests | storage |
| `tests/test_apply_patch.py` | Python | _(описание не задано)_ | - |
| `tests/test_assistant_folders.py` | Python | _(описание не задано)_ | storage |
| `tests/test_assistant_function_tools.py` | Python | _(описание не задано)_ | - |
| `tests/test_assistant_sidebar_sort.py` | Python | _(описание не задано)_ | - |
| `tests/test_assistant_temperature.py` | Python | _(описание не задано)_ | - |
| `tests/test_assistant_tools.py` | Python | _(описание не задано)_ | - |
| `tests/test_backup_and_safewriter.py` | Python | Backup/safe-writer tests | - |
| `tests/test_connectors.py` | Python | _(описание не задано)_ | - |
| `tests/test_core_api_json_schema.py` | Python | _(описание не задано)_ | - |
| `tests/test_core_api_layer.py` | Python | Pure api_layer unit tests | - |
| `tests/test_core_api_send.py` | Python | send_request integration tests (mocked HTTP) | - |
| `tests/test_core_files.py` | Python | File helpers tests | - |
| `tests/test_crypto.py` | Python | Crypto tests | - |
| `tests/test_deepseek_responses.py` | Python | DeepSeek Responses API tests | - |
| `tests/test_default_imports.py` | Python | _(описание не задано)_ | storage |
| `tests/test_default_rag_bases.py` | Python | _(описание не задано)_ | storage |
| `tests/test_devagent_thread_workspace.py` | Python | DevAgent thread workspace persistence tests | storage |
| `tests/test_dispatcher_connections.py` | Python | _(описание не задано)_ | storage |
| `tests/test_employee_management_ui.py` | Python | UI regression tests: employee management pages render and expose no export/import employee UI | - |
| `tests/test_github_connector.py` | Python | _(описание не задано)_ | - |
| `tests/test_github_tools.py` | Python | _(описание не задано)_ | - |
| `tests/test_i18n_serialization.py` | Python | _(описание не задано)_ | - |
| `tests/test_i18n_sync.py` | Python | _(описание не задано)_ | - |
| `tests/test_list_files.py` | Python | _(описание не задано)_ | - |
| `tests/test_llm_utils_json_schema.py` | Python | _(описание не задано)_ | - |
| `tests/test_numeric_arg_coercion.py` | Python | _(описание не задано)_ | - |
| `tests/test_orchestrator_chat_prefs.py` | Python | _(описание не задано)_ | - |
| `tests/test_orchestrator_connections.py` | Python | _(описание не задано)_ | storage |
| `tests/test_orchestrator_economy_cache.py` | Python | _(описание не задано)_ | - |
| `tests/test_orchestrator_folders.py` | Python | Orchestrator folder tests | storage |
| `tests/test_phase1_agent_loop.py` | Python | Agent loop tests | - |
| `tests/test_phase1_core_pure.py` | Python | Pure core tests | - |
| `tests/test_phase1_storage.py` | Python | Storage layer tests (assistants table + legacy aliases) | storage |
| `tests/test_platform_bootstrap.py` | Python | Bootstrap tests | storage |
| `tests/test_platform_scenarios.py` | Python | _(описание не задано)_ | storage |
| `tests/test_preset_orchestrators.py` | Python | _(описание не задано)_ | storage |
| `tests/test_prompt_guard_strict.py` | Python | Prompt guard strict-mode tests | - |
| `tests/test_prompt_improver.py` | Python | _(описание не задано)_ | - |
| `tests/test_propose_file.py` | Python | propose_file tool tests | - |
| `tests/test_propose_file_scenarios.py` | Python | propose_file edge-case tests | - |
| `tests/test_protect_history.py` | Python | History protection tests | - |
| `tests/test_rag_chunks_and_preset_skills.py` | Python | _(описание не задано)_ | storage |
| `tests/test_rag_tools_robustness.py` | Python | _(описание не задано)_ | - |
| `tests/test_recent_workspaces.py` | Python | Recent workspaces tests | storage |
| `tests/test_render_token_line.py` | Python | Token line renderer tests | - |
| `tests/test_safety_mode.py` | Python | Safety-mode gate tests | - |
| `tests/test_sanitized_approval_flow.py` | Python | Sanitized-content approval flow tests | - |
| `tests/test_search_in_files.py` | Python | _(описание не задано)_ | - |
| `tests/test_skills_adaptation.py` | Python | _(описание не задано)_ | storage |
| `tests/test_skills_library.py` | Python | Skills library tests | storage |
| `tests/test_st_mock.py` | Python | _(описание не задано)_ | _st_mock |
| `tests/test_statistics.py` | Python | _(описание не задано)_ | - |
| `tests/test_stats_page_ui.py` | Python | _(описание не задано)_ | - |
| `tests/test_storage_page_ui.py` | Python | _(описание не задано)_ | storage |
| `tests/test_structured_output_consumers.py` | Python | _(описание не задано)_ | - |
| `tests/test_task_state.py` | Python | _(описание не задано)_ | - |
| `tests/test_theme_restore.py` | Python | _(описание не задано)_ | - |
| `tests/test_thread_deeplink.py` | Python | _(описание не задано)_ | - |
| `tests/test_thread_file_save.py` | Python | _(описание не задано)_ | storage |
| `tests/test_tools_utils.py` | Python | _(описание не задано)_ | - |
| `tests/test_ui_pages.py` | Python | UI page tests | - |
| `tests/test_ui_tooltips.py` | Python | _(описание не задано)_ | - |
| `tests/test_ui_tooltips_orchestrator.py` | Python | _(описание не задано)_ | - |
| `tests/test_universal_developer.py` | Python | UniversalDevAgent tests | storage |
| `tests/test_usability_fixes.py` | Python | _(описание не задано)_ | - |
| `tests/test_web_search_prompt.py` | Python | _(описание не задано)_ | - |
| `tests/test_yandex_responses.py` | Python | Yandex Responses API tests | - |
| `tests/smoke/test_app_smoke.py` | Python | App smoke tests | - |
| `tests/scenarios/test_assistant_sidebar_scenarios.py` | Python | _(описание не задано)_ | storage |
| `tests/scenarios/test_connectors_scenarios.py` | Python | _(описание не задано)_ | storage |
| `tests/scenarios/test_first_run_flow.py` | Python | _(описание не задано)_ | - |
| `tests/scenarios/test_json_repair_scenarios.py` | Python | _(описание не задано)_ | - |
| `tests/scenarios/test_orchestrator_chat_prefs_scenario.py` | Python | _(описание не задано)_ | - |
| `tests/scenarios/test_orchestrator_devagent_scenarios.py` | Python | _(описание не задано)_ | storage |
| `tests/scenarios/test_rag_assistant_dialog.py` | Python | _(описание не задано)_ | - |
| `tests/scenarios/test_search_in_files_scenarios.py` | Python | _(описание не задано)_ | - |
| `tests/scenarios/test_skills_adaptation_scenario.py` | Python | _(описание не задано)_ | storage |
| `tests/scenarios/test_stats_scenario.py` | Python | _(описание не задано)_ | - |
| `tests/scenarios/test_structured_output_scenarios.py` | Python | _(описание не задано)_ | - |
| `tests/scenarios/test_theme_switch_scenario.py` | Python | _(описание не задано)_ | - |
| `tests/scenarios/test_welcome_page_scenarios.py` | Python | _(описание не задано)_ | - |
| `storage/__init__.py` | Python | Package marker | - |
| `storage/db.py` | Python | SQLAlchemy engines; auto-migration skills->assistants, skill_*->assistant_* columns | storage |
| `storage/models.py` | Python | ORM models: Assistant (assistants), Thread (assistant_id/assistant_name), Message, ConfigKV, Instruction, Orchestrator | - |
| `storage/repository.py` | Python | High-level CRUD for assistants/threads/config/orchestrators + legacy repo_*_skill wrappers | storage |
| `storage/repository_devagent.py` | Python | DevAgent thread CRUD (devagent.db) | storage |
| `orchestrators/global_instructions/github_connector.md` | Markdown | _(описание не задано)_ | - |
| `orchestrators/dev_agent/orchestrator.json` | JSON | _(описание не задано)_ | - |
| `orchestrators/dev_agent/instructions/assistant_creator.md` | Markdown | _(описание не задано)_ | - |
| `defaults/README.md` | Markdown | _(описание не задано)_ | - |
| `defaults/instructions/github_connector.md` | Markdown | _(описание не задано)_ | - |
| `defaults/settings/global.json` | JSON | _(описание не задано)_ | - |
| `defaults/rag_bases/yaagentai_2020/manifest.json` | JSON | _(описание не задано)_ | - |
| `defaults/assistants/README.md` | Markdown | _(описание не задано)_ | - |
| `defaults/assistants/universalnyy_tyutor/manifest.json` | JSON | _(описание не задано)_ | - |
| `defaults/assistants/universalnyy_tyutor/prompt.md` | Markdown | _(описание не задано)_ | - |
| `defaults/assistants/korrektor/manifest.json` | JSON | _(описание не задано)_ | - |
| `defaults/assistants/korrektor/prompt.md` | Markdown | _(описание не задано)_ | - |
| `defaults/orchestrators/ya_agent/orchestrator.json` | JSON | _(описание не задано)_ | - |
| `defaults/orchestrators/ya_agent/system_prompt.md` | Markdown | _(описание не задано)_ | - |
| `defaults/orchestrators/ya_agent/instructions/agent_atelier_agents.md` | Markdown | _(описание не задано)_ | - |
| `defaults/orchestrators/ya_agent/instructions/agent_security_guardrails.md` | Markdown | _(описание не задано)_ | - |
| `defaults/orchestrators/ya_agent/instructions/agent_tools_mcp.md` | Markdown | _(описание не задано)_ | - |
| `defaults/orchestrators/ya_agent/instructions/agents_responses_api.md` | Markdown | _(описание не задано)_ | - |
| `defaults/orchestrators/ya_agent/instructions/fine_tuning_classifiers.md` | Markdown | _(описание не задано)_ | - |
| `defaults/orchestrators/ya_agent/instructions/multimodal_yandex_services.md` | Markdown | _(описание не задано)_ | - |
| `defaults/orchestrators/ya_agent/instructions/rag_search_embeddings.md` | Markdown | _(описание не задано)_ | - |
| `defaults/orchestrators/ya_agent/instructions/yandex_models_reference.md` | Markdown | _(описание не задано)_ | - |
| `defaults/orchestrators/dev_agent/instructions/assistant_creator.md` | Markdown | _(описание не задано)_ | - |
| `defaults/orchestrators/dev_agent/instructions/employee_creator.md` | Markdown | _(описание не задано)_ | - |
| `defaults/orchestrators/dev_agent/instructions/github_connector.md` | Markdown | _(описание не задано)_ | - |
| `defaults/orchestrators/dev_agent/instructions/prompt_improver.md` | Markdown | _(описание не задано)_ | - |
| `defaults/orchestrators/dev_agent/instructions/self_reflection.md` | Markdown | _(описание не задано)_ | - |
| `defaults/orchestrators/dev_agent/instructions/skill_developer.md` | Markdown | _(описание не задано)_ | - |
| `defaults/langs/en.json` | JSON | _(описание не задано)_ | - |
| `defaults/langs/en_guide.md` | Markdown | _(описание не задано)_ | - |
| `defaults/langs/ru.json` | JSON | _(описание не задано)_ | - |
| `defaults/langs/ru_guide.md` | Markdown | _(описание не задано)_ | - |
| `defaults/langs/zh-CN.json` | JSON | _(описание не задано)_ | - |
| `defaults/langs/zh_CN_guide.md` | Markdown | _(описание не задано)_ | - |
| `defaults/skills/README.md` | Markdown | _(описание не задано)_ | - |
| `defaults/skills/rag_base_creator/SKILL.md` | Markdown | _(описание не задано)_ | - |
| `defaults/skills/rag_base_creator/scripts/build_base.py` | Python | _(описание не задано)_ | - |
| `defaults/services/deepseek.json` | JSON | _(описание не задано)_ | - |
| `defaults/services/gigachat.json` | JSON | _(описание не задано)_ | - |
| `defaults/services/yandex.json` | JSON | _(описание не задано)_ | - |
| `langs/en.json` | JSON | English UI strings | - |
| `langs/en_guide.md` | Markdown | English user guide | - |
| `langs/ru.json` | JSON | Russian UI strings | - |
| `langs/ru_guide.md` | Markdown | Russian user guide | - |
| `langs/zh-CN.json` | JSON | Simplified Chinese UI strings | - |
| `langs/zh_CN_guide.md` | Markdown | Chinese user guide | - |
| `certs/russian_trusted_root_ca.pem` | PEM certificate | Russian Trusted Root CA certificate for GigaChat TLS | - |
| `scripts/regenerate_project_map.py` | Python | Regenerates PROJECT_MAP.md with assistant terminology | - |
| `skills/skills.json` | JSON | _(описание не задано)_ | - |
| `skills/Github_Clean_Copy/SKILL.md` | Markdown | _(описание не задано)_ | - |
| `skills/Github_Clean_Copy/tests/test_clean_copy_skill.py` | Python | _(описание не задано)_ | - |
| `skills/Github_Clean_Copy/scripts/make_clean_copy.py` | Python | _(описание не задано)_ | - |
| `dev_agent/AGENT_LOOP_REPORT.md` | Markdown | Agent loop implementation report | - |
| `dev_agent/AGENT_LOOP_SPEC.md` | Markdown | Agent loop specification | - |
| `dev_agent/__init__.py` | Python | Package marker | agent_loop, backup_manager, safe_writer, tool_executor, universal_agent, workspace_tools |
| `dev_agent/agent_loop.py` | Python | Provider-independent agent loop (strong/weak assistant routing, economy mode, skills-library tools classified as weak) | - |
| `dev_agent/assistant_detector.py` | Python | Assistant detection/creation helpers (renamed from skill_detector) | storage |
| `dev_agent/assistant_model_resolver.py` | Python | Auto model resolution for assistant creation | llm_utils |
| `dev_agent/backup_manager.py` | Python | Per-file backup/restore manager | - |
| `dev_agent/config.py` | Python | DevAgent runtime config and protected path policy | - |
| `dev_agent/llm_utils.py` | Python | Unified LLM-call helper (assistant dict contract, legacy skill alias) | - |
| `dev_agent/safe_writer.py` | Python | Safe full-file rewrite with diff/verification | backup_manager |
| `dev_agent/system_prompt.md` | Markdown | DevAgent system prompt (assistant tool names, skills vs assistants section, skills-invocation tools) | - |
| `dev_agent/task_state.py` | Python | _(описание не задано)_ | backup_manager |
| `dev_agent/tool_executor.py` | Python | DevAgent tool set; assistant tools + legacy skill tool aliases + skills-library tools | assistant_detector, assistant_model_resolver, backup_manager, llm_utils, safe_writer |
| `dev_agent/universal_agent.py` | Python | Universal dispatcher (core + workspace tools + orchestrator tools) | tool_executor |
| `dev_agent/workspace_tools.py` | Python | Workspace layer: folders, project map, docs, snapshots | backup_manager |
| `services/deepseek.json` | JSON | DeepSeek service definition | - |
| `services/gigachat.json` | JSON | GigaChat service definition | - |
| `services/yandex.json` | JSON | YandexAI service definition | - |

## Структура Python-модулей

### `ui/app.py`
- `_build_orch_nav` (func, строка 58)
- `_build_assistants_nav` (func, строка 84)
- `_apply_theme` (func, строка 114)
- `_build_ui_restore_payload` (func, строка 150)
- `_restore_ui_reload_state` (func, строка 185)
- `_handle_thread_deeplink` (func, строка 234)
- `main` (func, строка 273)

### `ui/components/workspace_picker.py`
- `_picker_state_keys` (func, строка 26)
- `_init_picker_state` (func, строка 36)
- `_resolve_path` (func, строка 50)
- `render_workspace_picker` (func, строка 66)

### `ui/pages/assistants.py`
- `_get_show_form` (func, строка 36)
- `_set_show_form` (func, строка 43)
- `_get_edit_id` (func, строка 48)
- `_set_edit_id` (func, строка 52)
- `_format_tools_badge` (func, строка 57)
- `_get_model_max_tokens_limit` (func, строка 76)
- `_temperature_bounds` (func, строка 96)
- `_clamp_temperature` (func, строка 110)
- `page_assistants` (func, строка 116)

### `ui/pages/chat.py`
- `_get_preselected` (func, строка 38)
- `_uploader_counter` (func, строка 44)
- `_uploader_key` (func, строка 49)
- `_detach_file` (func, строка 60)
- `page_run_query` (func, строка 67)

### `ui/pages/connectors.py`
- `_service_options` (func, строка 32)
- `_test_connection` (func, строка 42)
- `_render_create_form` (func, строка 58)
- `_render_edit_form` (func, строка 102)
- `_render_connection_card` (func, строка 134)
- `page_connectors` (func, строка 202)

### `ui/pages/history.py`
- `_active_orch_thread_id` (func, строка 29)
- `_clear_all_orch_markers` (func, строка 47)
- `_clear_active_orch_state` (func, строка 60)
- `_delete_all_chat_threads` (func, строка 73)
- `_delete_all_orch_threads` (func, строка 82)
- `page_history` (func, строка 91)
- `_last_reply` (func, строка 362)

### `ui/pages/orchestrator.py`
- `_chat_pref_config_keys` (func, строка 65)
- `_chat_prefs` (func, строка 72)
- `_save_chat_pref` (func, строка 80)
- `_make_state_keys` (func, строка 96)
- `_init_orch_state` (func, строка 124)
- `_sk` (func, строка 131)
- `_ss` (func, строка 136)
- `_set_ss` (func, строка 141)
- `_pop_ss` (func, строка 145)
- `_save_economy_cache` (func, строка 149)
- `_load_economy_cache` (func, строка 168)
- `_attachments_manifest_path` (func, строка 175)
- `_load_attachments_manifest` (func, строка 179)
- `_append_attachment_manifest` (func, строка 189)
- `_save_attachment_to_workspace` (func, строка 200)
- `_scroll_page` (func, строка 226)
- `_make_send_adapter` (func, строка 280)
- `_make_dispatcher` (func, строка 312)
- `_assistant_has_api_key` (func, строка 331)
- `_strip_html_details_tags` (func, строка 346)
- `_strip_empty_fenced_blocks` (func, строка 362)
- `_strip_tool_calls` (func, строка 413)
- `_first_two_lines` (func, строка 475)
- `_format_call_args_preview` (func, строка 488)
- `_extract_result_body` (func, строка 506)
- `_render_tool_result` (func, строка 520)
- `_render_events` (func, строка 595)
- `_render_event` (func, строка 615)
- `_do_step` (func, строка 667)
- `_reset_dialog` (func, строка 818)
- `_load_thread` (func, строка 833)
- `_chat_toolbar_widget_key` (func, строка 859)
- `_sync_chat_pref_checkbox` (func, строка 869)
- `_chat_toolbar_pref_changed` (func, строка 887)
- `_render_chat_toolbar` (func, строка 897)
- `_render_chat_tab` (func, строка 955)
- `_services_with_web_search` (func, строка 1402)
- `_temp_slider` (func, строка 1414)
- `_get_max_tokens_limit` (func, строка 1423)
- `_render_models_settings` (func, строка 1443)

### `ui/pages/orchestrator_settings.py`
- `page_orchestrator_settings` (func, строка 27)

### `ui/pages/orchestrators.py`
- `_go_to_page` (func, строка 23)
- `page_orchestrators` (func, строка 28)

### `ui/pages/settings.py`
- `_resolve_label` (func, строка 32)
- `_render_extra_fields` (func, строка 41)
- `_render_env_variables_section` (func, строка 90)
- `_render_models_table` (func, строка 140)
- `_render_api_keys` (func, строка 176)
- `_render_folder_sync` (func, строка 288)
- `page_settings` (func, строка 318)

### `ui/pages/skills_library.py`
- `page_skills_library` (func, строка 28)
- `_render_import_section` (func, строка 48)
- `_request_skill_adaptation` (func, строка 172)
- `_render_skills_list` (func, строка 192)
- `_render_edit_form` (func, строка 231)

### `ui/pages/stats.py`
- `page_stats` (func, строка 25)

### `ui/pages/storage.py`
- `_status_label` (func, строка 36)
- `_render_test_search` (func, строка 41)
- `_render_chunk_editor` (func, строка 67)
- `_render_chunks_section` (func, строка 107)
- `_render_base_card` (func, строка 216)
- `page_storage` (func, строка 294)

### `ui/pages/welcome.py`
- `_guide_filename` (func, строка 13)
- `_goto` (func, строка 23)
- `page_welcome` (func, строка 29)

### `core/api_errors.py`
- `APIError` (class, строка 27)
- `ServiceNotFoundError` (class, строка 54)
- `ApiKeyMissingError` (class, строка 67)
- `AuthTypeUnknownError` (class, строка 81)
- `ProviderHTTPError` (class, строка 95)
- `RequestTimeoutError` (class, строка 116)
- `NetworkError` (class, строка 125)
- `api_error_message` (func, строка 137)

### `core/api_layer.py`
- `_gigachat_verify` (func, строка 66)
- `_parse_sanitized_info` (func, строка 93)
- `_get_model_max_tokens` (func, строка 119)
- `_prepare_response_content` (func, строка 140)
- `_format_function_call_item` (func, строка 175)
- `_normalise_json_schema` (func, строка 203)
- `_responses_json_format` (func, строка 222)
- `_openai_response_format` (func, строка 235)
- `_gigachat_response_format` (func, строка 249)
- `_unwrap_json_text` (func, строка 266)
- `_is_schema_rejection` (func, строка 294)
- `_extract_responses_text` (func, строка 316)
- `_extract_deepseek_responses_text` (func, строка 374)
- `_normalise_tools` (func, строка 386)
- `_has_native_function_tools` (func, строка 404)
- `_protect_history` (func, строка 419)
- `_estimate_tokens_in` (func, строка 492)
- `_bearer_request` (func, строка 503)
- `_deepseek_reasoning_effort` (func, строка 578)
- `_deepseek_responses_request` (func, строка 594)
- `_yandex_reasoning_effort` (func, строка 696)
- `_yandex_web_search_config` (func, строка 723)
- `_assistant_web_search_config` (func, строка 753)
- `_yandex_responses_request` (func, строка 782)
- `_gigachat_token` (func, строка 899)
- `_assistant_rag_context` (func, строка 918)
- `send_request` (func, строка 969)
- `_do_request` (func, строка 1137)
- `_extract_error_body` (func, строка 1297)
- `_extract_gigachat_error` (func, строка 1318)
- `test_connection` (func, строка 1333)

### `core/assistant_creator.py`
- `_section_headers` (func, строка 23)
- `validate_prompt` (func, строка 33)
- `lint_prompt` (func, строка 63)

### `core/assistant_folders.py`
- `normalize_slug` (func, строка 48)
- `get_assistants_root` (func, строка 65)
- `get_assistant_dir` (func, строка 70)
- `ensure_assistant_dir` (func, строка 76)
- `remove_assistant_dir` (func, строка 84)
- `assistant_folder_exists` (func, строка 95)
- `list_assistant_folder_names` (func, строка 100)
- `save_assistant_bundle` (func, строка 113)
- `load_assistant_bundle` (func, строка 135)
- `get_assistant_prompt_path` (func, строка 153)
- `save_assistant_prompt` (func, строка 158)
- `load_assistant_prompt` (func, строка 164)
- `list_assistant_files` (func, строка 171)
- `save_assistant_file` (func, строка 179)
- `delete_assistant_file` (func, строка 190)
- `load_assistant_file_content` (func, строка 201)
- `load_all_assistant_files` (func, строка 207)
- `export_assistant_folder` (func, строка 219)
- `import_assistant_folder` (func, строка 235)
- `build_manifest_from_assistant` (func, строка 266)
- `sync_assistant_to_folder` (func, строка 283)
- `set_assistant_rag_bases` (func, строка 320)
- `_normalize_domain_list` (func, строка 340)
- `set_assistant_web_search_settings` (func, строка 358)
- `get_assistant_web_search_settings` (func, строка 384)
- `_copy_legacy_files_if_needed` (func, строка 403)

### `core/assistant_nav.py`
- `_parse_ts` (func, строка 27)
- `_num` (func, строка 49)
- `last_dialogue_at` (func, строка 54)
- `sort_assistants` (func, строка 75)
- `split_nav_lists` (func, строка 98)

### `core/assistant_tools.py`
- `_yandex_reasoning_effort` (func, строка 32)
- `_normalise_tools` (func, строка 39)
- `_yandex_web_search_config` (func, строка 45)
- `_build_responses_input_items` (func, строка 58)
- `_build_yandex_tool_payload` (func, строка 85)
- `_post_yandex_responses` (func, строка 140)
- `_extract_function_calls` (func, строка 160)
- `_item_text` (func, строка 188)
- `_assistant_allowed_rag_bases` (func, строка 194)
- `execute_assistant_rag_search` (func, строка 219)
- `_report_usage` (func, строка 270)
- `run_yandex_responses_tool_loop` (func, строка 291)

### `core/assistants.py`
- `_get_user_data_dir` (func, строка 54)
- `ensure_dir` (func, строка 59)
- `_unique_slug` (func, строка 77)
- `load_assistants_index` (func, строка 96)
- `load_assistant_prompt_text` (func, строка 101)
- `save_assistant_prompt_text` (func, строка 107)
- `get_assistant_by_id` (func, строка 123)
- `get_assistant_by_slug` (func, строка 131)
- `create_assistant` (func, строка 138)
- `update_assistant` (func, строка 167)
- `delete_assistant` (func, строка 191)
- `get_legacy_assistant_files_dir` (func, строка 207)
- `get_assistant_files_dir` (func, строка 212)
- `list_assistant_files` (func, строка 225)
- `save_assistant_file` (func, строка 236)
- `delete_assistant_file` (func, строка 254)
- `load_assistant_files_context` (func, строка 268)
- `export_assistant` (func, строка 294)
- `import_assistant` (func, строка 319)
- `reload_assistant_from_folder` (func, строка 393)

### `core/auth.py`
- `_get_configured_password_hash` (func, строка 29)
- `is_auth_enabled` (func, строка 38)
- `is_authenticated` (func, строка 43)
- `_resolve_auth_lang` (func, строка 53)
- `_render_login_form` (func, строка 74)
- `require_auth` (func, строка 126)

### `core/bootstrap.py`
- `ensure_default_skills` (func, строка 59)
- `ensure_instructions` (func, строка 64)
- `ensure_devagent_settings` (func, строка 162)

### `core/config.py`
- `_secret_keys` (func, строка 34)
- `load_config` (func, строка 49)
- `save_config` (func, строка 75)
- `has_key` (func, строка 87)
- `_env_key_for_service` (func, строка 101)
- `is_env_key_set_for_service` (func, строка 110)
- `list_env_keys` (func, строка 118)
- `_merge_env_keys` (func, строка 165)
- `_merge_defaults_overrides` (func, строка 218)
- `_load_devagent_defaults_from_json` (func, строка 230)
- `_get_devagent_defaults` (func, строка 257)
- `reload_devagent_defaults` (func, строка 265)
- `get_default_economy_tail_messages` (func, строка 271)
- `get_default_economy_cache_enabled` (func, строка 284)
- `get_default_economy_cache_multiplier` (func, строка 293)
- `get_default_strong_max_tokens` (func, строка 302)
- `get_default_weak_max_tokens` (func, строка 311)
- `_get_global_defaults` (func, строка 325)
- `reload_global_defaults` (func, строка 337)
- `get_default_ui_lang` (func, строка 343)
- `get_default_providers_preset` (func, строка 349)
- `load_devagent_config` (func, строка 355)
- `save_devagent_config` (func, строка 378)

### `core/connectors.py`
- `_now` (func, строка 55)
- `_connectors_root` (func, строка 59)
- `_manifest_path` (func, строка 66)
- `_manifest_read` (func, строка 72)
- `_manifest_write` (func, строка 87)
- `_validate_service` (func, строка 95)
- `_unique_conn_id` (func, строка 102)
- `public_manifest` (func, строка 110)
- `list_connections` (func, строка 124)
- `get_connection` (func, строка 148)
- `get_connection_full` (func, строка 157)
- `create_connection` (func, строка 165)
- `update_connection` (func, строка 200)
- `set_connection_token` (func, строка 224)
- `delete_connection` (func, строка 235)
- `decrypt_token` (func, строка 246)
- `list_services` (func, строка 262)
- `get_service` (func, строка 272)

### `core/crypto.py`
- `_legacy_key_file_path` (func, строка 29)
- `get_key_file_path` (func, строка 38)
- `_default_key_file` (func, строка 43)
- `_write_key_file` (func, строка 49)
- `_migrate_legacy_key` (func, строка 61)
- `get_encryption_key` (func, строка 83)
- `encrypt` (func, строка 115)
- `decrypt` (func, строка 124)
- `is_secret_key` (func, строка 137)

### `core/dangerous.py`
- `DangerRule` (class, строка 36)
- `DangerAssessment` (class, строка 231)
- `_is_subprocess_command_safe` (func, строка 247)
- `_check_python_rules` (func, строка 273)
- `_check_shell_rules` (func, строка 293)
- `assess_code` (func, строка 302)
- `tool_needs_confirmation` (func, строка 326)
- `format_reasons_for_ui` (func, строка 341)

### `core/default_imports.py`
- `_full_devagent_toolset` (func, строка 43)
- `ensure_default_orchestrators` (func, строка 53)
- `ensure_default_rag_bases` (func, строка 129)
- `_stamp_manifest_source` (func, строка 179)
- `_merge_preset_rag_bases` (func, строка 207)
- `ensure_default_assistants` (func, строка 218)
- `ensure_default_instructions` (func, строка 296)
- `ensure_default_skills` (func, строка 360)
- `ensure_all_defaults` (func, строка 454)

### `core/defaults.py`
- `defaults_root` (func, строка 47)
- `settings_dir` (func, строка 52)
- `orchestrators_dir` (func, строка 57)
- `assistants_dir` (func, строка 62)
- `services_dir` (func, строка 67)
- `langs_dir` (func, строка 72)
- `skills_dir` (func, строка 77)
- `rag_bases_dir` (func, строка 82)
- `list_default_rag_base_slugs` (func, строка 87)
- `exists` (func, строка 107)
- `read_json` (func, строка 114)
- `parse_front_matter` (func, строка 128)
- `list_default_orchestrator_slugs` (func, строка 177)
- `_load_orchestrator_new_format` (func, строка 200)
- `_load_orchestrator_old_format` (func, строка 294)
- `load_default_orchestrator` (func, строка 372)
- `list_default_assistant_folders` (func, строка 393)
- `load_default_rag_base` (func, строка 413)
- `load_default_assistant` (func, строка 424)
- `load_global_settings` (func, строка 492)

### `core/entity_sync.py`
- `ensure_entity_folders_sync` (func, строка 39)
- `sync_assistants` (func, строка 52)
- `sync_orchestrators` (func, строка 92)
- `_backfill_slug` (func, строка 104)

### `core/env_loader.py`
- `_parse_shell_exports` (func, строка 12)
- `_profile_candidates` (func, строка 30)
- `load_env_from_shell_profiles` (func, строка 43)

### `core/files.py`
- `get_file_uploader_types` (func, строка 24)
- `estimate_tokens` (func, строка 29)
- `check_upload_tokens` (func, строка 55)
- `should_store_uploaded_file` (func, строка 75)
- `build_attachment_metadata` (func, строка 80)
- `build_attachments_context` (func, строка 101)
- `build_saved_files_registry` (func, строка 127)
- `get_model_context_window` (func, строка 149)
- `check_context` (func, строка 160)
- `ensure_optional_dependencies` (func, строка 183)
- `extract_file_content` (func, строка 203)

### `core/fs.py`
- `ensure_dir` (func, строка 10)
- `read_json_file` (func, строка 16)
- `write_json_file` (func, строка 27)
- `read_text_file` (func, строка 38)
- `write_text_file` (func, строка 49)
- `decode_bytes` (func, строка 60)
- `combine_nonempty` (func, строка 72)

### `core/github_connector.py`
- `GithubConnectorError` (class, строка 23)
- `_ensure_github` (func, строка 27)
- `_client` (func, строка 38)
- `_repo_spec` (func, строка 45)
- `_describe_gh_error` (func, строка 75)
- `test_connection` (func, строка 96)
- `get_user_info` (func, строка 125)
- `list_repos` (func, строка 143)
- `get_repo_info` (func, строка 169)
- `create_repo` (func, строка 189)
- `read_file` (func, строка 220)
- `upload_file` (func, строка 253)
- `update_file` (func, строка 290)
- `delete_file` (func, строка 324)
- `list_files` (func, строка 350)
- `_content_sha` (func, строка 381)
- `_is_conflict` (func, строка 395)

### `core/github_tools.py`
- `_get_connector_id` (func, строка 34)
- `_wrap` (func, строка 42)
- `github_list_repos` (func, строка 53)
- `github_create_repo` (func, строка 72)
- `github_upload_file` (func, строка 97)
- `github_update_file` (func, строка 133)
- `github_read_file` (func, строка 171)
- `get_tools` (func, строка 241)

### `core/i18n.py`
- `_lang_dirs` (func, строка 28)
- `discover_langs` (func, строка 42)
- `get_langs` (func, строка 63)
- `invalidate_langs_cache` (func, строка 68)
- `_cached_langs` (func, строка 78)
- `load_lang_data` (func, строка 83)
- `t` (func, строка 92)
- `dumps_lang` (func, строка 121)
- `dump_lang_file` (func, строка 131)

### `core/instructions.py`
- `_root` (func, строка 29)
- `_safe_filename` (func, строка 35)
- `_instruction_path` (func, строка 41)
- `_read_instruction_file` (func, строка 45)
- `_write_instruction_file` (func, строка 68)
- `list_instructions` (func, строка 91)
- `get_instruction` (func, строка 111)
- `get_instruction_prompt` (func, строка 124)
- `_connector_service_for_instruction` (func, строка 129)
- `list_instructions_for` (func, строка 148)
- `get_instruction_for` (func, строка 181)
- `ensure_global_instructions` (func, строка 195)
- `create_instruction` (func, строка 231)
- `update_instruction` (func, строка 241)
- `delete_instruction` (func, строка 250)

### `core/orchestrator_folders.py`
- `get_orchestrators_root` (func, строка 47)
- `safe_orchestrator_slug` (func, строка 52)
- `get_orchestrator_dir` (func, строка 67)
- `ensure_orchestrator_dir` (func, строка 81)
- `remove_orchestrator_dir` (func, строка 90)
- `orchestrator_folder_exists` (func, строка 102)
- `list_orchestrator_folder_slugs` (func, строка 107)
- `save_orchestrator_bundle` (func, строка 120)
- `load_orchestrator_bundle` (func, строка 147)
- `load_orchestrator_prompt_file` (func, строка 196)
- `list_orchestrator_functions` (func, строка 203)
- `get_orchestrator_function` (func, строка 231)
- `save_orchestrator_function` (func, строка 254)
- `delete_orchestrator_function` (func, строка 276)
- `load_orchestrator_function_module` (func, строка 290)
- `load_all_orchestrator_functions` (func, строка 318)
- `_instructions_dir` (func, строка 330)
- `_safe_filename` (func, строка 334)
- `_md_path` (func, строка 340)
- `_legacy_instructions_json_path` (func, строка 344)
- `_migrate_instructions_json_to_md` (func, строка 348)
- `_write_instruction_md` (func, строка 380)
- `_read_instructions_from_folder` (func, строка 401)
- `sync_orchestrator_instructions` (func, строка 430)
- `list_orchestrator_instructions` (func, строка 450)
- `get_orchestrator_instruction` (func, строка 463)
- `save_orchestrator_instruction` (func, строка 476)
- `delete_orchestrator_instruction` (func, строка 517)
- `export_orchestrator_folder` (func, строка 538)
- `import_orchestrator_folder` (func, строка 561)

### `core/orchestrators.py`
- `_ensure_default_orchestrators` (func, строка 91)
- `_default_economy_tail_messages` (func, строка 123)
- `_default_economy_cache_enabled` (func, строка 130)
- `_default_economy_cache_multiplier` (func, строка 135)
- `_get_known_tool_names` (func, строка 149)
- `_invalidate_known_tool_names` (func, строка 188)
- `list_orchestrators` (func, строка 202)
- `get_orchestrator` (func, строка 207)
- `get_orchestrator_by_slug` (func, строка 212)
- `create_orchestrator` (func, строка 220)
- `save_orchestrator` (func, строка 258)
- `delete_orchestrator` (func, строка 275)
- `_sync_orchestrator_folder` (func, строка 295)
- `reload_orchestrator_from_folder` (func, строка 326)
- `sync_all_orchestrator_folders` (func, строка 390)
- `get_enabled_skills` (func, строка 408)
- `set_enabled_skills` (func, строка 422)
- `get_enabled_connections` (func, строка 448)
- `set_enabled_connections` (func, строка 462)
- `_extend_prompt_with_connections` (func, строка 485)
- `get_orchestrator_rag_bases` (func, строка 550)
- `set_orchestrator_rag_bases` (func, строка 567)
- `_extend_prompt_with_rag_bases` (func, строка 593)
- `_extend_prompt_with_skills` (func, строка 638)
- `_extend_prompt_with_instructions` (func, строка 660)
- `build_assistant_dicts` (func, строка 727)
- `get_web_search_prompt` (func, строка 803)
- `get_web_search_config` (func, строка 818)
- `get_economy_tail_messages` (func, строка 846)
- `get_economy_cache_enabled` (func, строка 864)
- `get_economy_cache_multiplier` (func, строка 881)
- `get_economy_config` (func, строка 899)
- `export_orchestrator` (func, строка 913)
- `_validate_imported_tools` (func, строка 959)
- `import_orchestrator` (func, строка 985)
- `_import_instructions` (func, строка 1104)
- `_import_functions` (func, строка 1131)
- `orch_list_instructions` (func, строка 1149)
- `orch_get_instruction` (func, строка 1154)
- `orch_save_instruction` (func, строка 1159)

### `core/paths.py`
- `ensure_data_dirs` (func, строка 35)
- `get_thread_dir` (func, строка 41)
- `get_thread_file_path` (func, строка 46)

### `core/prompt_guard.py`
- `wrap_data` (func, строка 34)
- `is_wrapped_data` (func, строка 48)
- `sanitize_text` (func, строка 71)
- `detect_injection_signatures` (func, строка 107)
- `is_tool_result_text` (func, строка 130)
- `sanitize_tool_result_content` (func, строка 135)
- `sanitize_search_result` (func, строка 167)

### `core/prompt_improver.py`
- `get_improver_instruction` (func, строка 30)
- `improve_prompt_with_weak_model` (func, строка 49)

### `core/rag.py`
- `_now` (func, строка 34)
- `_slugify` (func, строка 38)
- `_manifest_path` (func, строка 45)
- `base_dir` (func, строка 51)
- `files_dir` (func, строка 57)
- `index_db_path` (func, строка 62)
- `_ensure_files_dir` (func, строка 67)
- `list_bases` (func, строка 73)
- `get_base` (func, строка 93)
- `_with_index_stats` (func, строка 105)
- `_validate_create` (func, строка 116)
- `create_base` (func, строка 132)
- `update_base` (func, строка 179)
- `set_status` (func, строка 210)
- `_load_removed_defaults` (func, строка 227)
- `_save_removed_defaults` (func, строка 237)
- `_load_manifest_raw` (func, строка 244)
- `delete_base` (func, строка 252)
- `add_file` (func, строка 273)
- `remove_file` (func, строка 283)
- `list_files` (func, строка 293)
- `read_file_contents` (func, строка 306)
- `allowed_for_slot` (func, строка 322)
- `base_has_credentials` (func, строка 331)
- `list_chunks` (func, строка 370)
- `get_chunk` (func, строка 386)
- `update_chunk` (func, строка 392)
- `delete_chunk` (func, строка 433)
- `list_bases_with_activity` (func, строка 439)
- `_save_manifest` (func, строка 449)
- `json_load` (func, строка 456)

### `core/rag_chunker.py`
- `_list_index` (func, строка 23)
- `_split_units` (func, строка 28)
- `chunk_text` (func, строка 72)

### `core/rag_embeddings.py`
- `get_yandex_embedding_credentials` (func, строка 45)
- `embed_text` (func, строка 66)
- `embed_query` (func, строка 119)
- `embed_many` (func, строка 132)

### `core/rag_index.py`
- `_now` (func, строка 28)
- `_connect` (func, строка 33)
- `create_index_db` (func, строка 46)
- `pack_vector` (func, строка 96)
- `unpack_vector` (func, строка 101)
- `reset_index` (func, строка 106)
- `read_meta` (func, строка 128)
- `add_chunk` (func, строка 143)
- `add_embedding` (func, строка 172)
- `count_chunks` (func, строка 190)
- `get_chunk` (func, строка 205)
- `list_chunks` (func, строка 239)
- `search_chunks_text` (func, строка 280)
- `update_chunk_text` (func, строка 334)
- `delete_chunk` (func, строка 362)
- `delete_embedding` (func, строка 376)
- `_cosine` (func, строка 392)
- `search_similar` (func, строка 406)
- `index_stats` (func, строка 451)
- `dump_chunks` (func, строка 484)

### `core/rag_indexer.py`
- `IndexingError` (class, строка 34)
- `extract_text` (func, строка 38)
- `_manifest_int` (func, строка 52)
- `index_base` (func, строка 59)

### `core/rag_search.py`
- `RagSearchError` (class, строка 26)
- `search_base` (func, строка 30)
- `build_search_context` (func, строка 67)
- `chat_context` (func, строка 95)

### `core/recent_assistants.py`
- `record_assistant_use` (func, строка 9)

### `core/recent_workspaces.py`
- `_normalise_path` (func, строка 32)
- `get_recent_workspaces` (func, строка 44)
- `add_recent_workspace` (func, строка 80)
- `clear_recent_workspaces` (func, строка 105)

### `core/render.py`
- `clipboard_button` (func, строка 13)
- `_md_to_html` (func, строка 122)
- `_md_to_txt` (func, строка 154)
- `format_token_line` (func, строка 177)
- `_iter_md_blocks` (func, строка 218)
- `format_ts_label` (func, строка 298)

### `core/services.py`
- `_scan_dir` (func, строка 15)
- `discover_services` (func, строка 32)
- `get_services` (func, строка 48)
- `_cached_services` (func, строка 54)
- `get_reasoning_effort_options` (func, строка 60)
- `_model_entry` (func, строка 78)
- `get_model_reasoning_effort_options` (func, строка 94)
- `service_supports_reasoning_effort` (func, строка 115)
- `default_reasoning_effort` (func, строка 120)
- `get_embedding_models` (func, строка 154)
- `get_rag_models` (func, строка 167)
- `service_supports_embeddings` (func, строка 180)

### `core/skills_library.py`
- `SkillsLibraryError` (class, строка 52)
- `_data_dir` (func, строка 66)
- `get_skills_root` (func, строка 81)
- `ensure_skills_root` (func, строка 86)
- `_registry_path` (func, строка 93)
- `_load_registry` (func, строка 97)
- `_save_registry` (func, строка 113)
- `_removed_defaults_path` (func, строка 125)
- `_load_removed_defaults` (func, строка 130)
- `_save_removed_defaults` (func, строка 147)
- `_new_skill_id` (func, строка 160)
- `_safe_folder_name` (func, строка 170)
- `_unique_folder` (func, строка 182)
- `_skill_record` (func, строка 196)
- `list_skills` (func, строка 222)
- `get_skill` (func, строка 240)
- `skill_exists` (func, строка 250)
- `_skill_dir` (func, строка 255)
- `register_skill` (func, строка 261)
- `update_skill` (func, строка 286)
- `set_skill_adapted` (func, строка 322)
- `delete_skill` (func, строка 344)
- `get_skill_folder` (func, строка 373)
- `list_skill_files` (func, строка 382)
- `_target_path_within` (func, строка 396)
- `_extract_zip_to_folder` (func, строка 407)
- `_find_skill_root_in_dir` (func, строка 437)
- `_copy_tree` (func, строка 452)
- `import_skill_from_zip` (func, строка 477)
- `_install_from_folder` (func, строка 509)
- `import_skill_from_folder` (func, строка 548)
- `parse_github_url` (func, строка 570)
- `import_skill_from_github` (func, строка 592)
- `get_enabled_skills_metadata` (func, строка 642)
- `build_skills_metadata_text` (func, строка 660)

### `core/statistics.py`
- `_parse_ts` (func, строка 30)
- `_bucket_key` (func, строка 47)
- `_int` (func, строка 65)
- `cache_pct` (func, строка 73)
- `_message_tokens` (func, строка 86)
- `_chat_provider_map` (func, строка 94)
- `_orchestrator_provider_map` (func, строка 107)
- `collect_chat_records` (func, строка 131)
- `collect_orchestrator_records` (func, строка 163)
- `collect_usage` (func, строка 191)
- `available_bounds` (func, строка 203)
- `_bucketize` (func, строка 218)
- `_bucket_rows` (func, строка 231)
- `build_summary` (func, строка 251)

### `core/threads.py`
- `get_thread_messages` (func, строка 34)
- `_sanitize_title` (func, строка 39)
- `create_devagent_thread` (func, строка 56)
- `create_thread` (func, строка 70)
- `load_thread_messages` (func, строка 89)
- `_restore_events` (func, строка 93)
- `save_thread_messages` (func, строка 126)
- `append_thread_message` (func, строка 157)
- `sum_thread_tokens` (func, строка 172)
- `save_thread_file` (func, строка 190)
- `load_thread_file` (func, строка 200)
- `load_thread_meta` (func, строка 209)
- `delete_thread` (func, строка 213)
- `list_devagent_threads` (func, строка 221)
- `list_chat_threads` (func, строка 226)
- `list_all_threads` (func, строка 231)
- `messages_to_api_history` (func, строка 236)

### `core/threads_devagent.py`
- `_sanitize_title` (func, строка 43)
- `create_devagent_thread` (func, строка 57)
- `save_thread_workspace` (func, строка 88)
- `load_thread_messages` (func, строка 109)
- `_restore_events` (func, строка 115)
- `save_thread_messages` (func, строка 148)
- `append_thread_message` (func, строка 179)
- `sum_thread_tokens` (func, строка 202)
- `load_thread_meta` (func, строка 220)
- `delete_thread` (func, строка 224)
- `list_devagent_threads` (func, строка 232)
- `list_orchestrator_threads` (func, строка 241)
- `delete_all_devagent_threads` (func, строка 246)

### `core/tools_utils.py`
- `list_tool_definitions` (func, строка 12)
- `build_rag_search_tool` (func, строка 26)
- `service_supported_tools` (func, строка 73)

### `tests/_st_mock.py`
- `StopRerun` (class, строка 15)
- `_SessionState` (class, строка 19)
- `StreamlitMock` (class, строка 31)
- `_NullCtx` (class, строка 193)
- `_make_components_stub` (func, строка 233)
- `install_streamlit_mock` (func, строка 243)

### `tests/_test_isolation.py`
- `is_app_module` (func, строка 18)
- `snapshot_app_modules` (func, строка 23)
- `drop_app_modules` (func, строка 32)
- `names_created_since` (func, строка 39)
- `restore_app_modules` (func, строка 47)
- `isolated_app_modules` (func, строка 56)

### `tests/conftest.py`
- `isolated_app_modules` (func, строка 8)

### `tests/test_agent_loop_json_repair.py`
- `TestRepairUnclosedBracesUnit` (class, строка 26)
- `TestParseToolCallsRepair` (class, строка 48)
- `TestSystemPromptDocumentsJsonSelfCheck` (class, строка 117)

### `tests/test_app_imports.py`
- `test_all_core_packages_importable` (func, строка 17)
- `test_no_st_get_option_with_two_args` (func, строка 62)

### `tests/test_apply_patch.py`
- `sandbox` (func, строка 18)
- `test_apply_patch_single_replacement` (func, строка 40)
- `test_apply_patch_missing_anchor_leaves_file_untouched` (func, строка 53)
- `test_apply_patch_ambiguous_anchor_rejected` (func, строка 62)
- `test_apply_patch_occurrence_argument` (func, строка 71)
- `test_apply_patch_multi_edit_atomic_on_error` (func, строка 84)
- `test_apply_patch_reports_applied_true_on_success` (func, строка 100)
- `test_apply_patch_reports_applied_false_when_only_staged` (func, строка 113)
- `test_apply_patch_append_mode` (func, строка 135)
- `test_apply_patch_append_mode_no_trailing_newline` (func, строка 150)
- `test_apply_patch_append_empty_file` (func, строка 163)
- `test_apply_patch_append_plus_replace_in_one_batch` (func, строка 176)
- `test_apply_patch_missing_anchor_error_has_edit_index_and_snippet` (func, строка 193)
- `test_apply_patch_non_dict_error_has_edit_index` (func, строка 209)
- `test_apply_patch_ambiguous_error_has_edit_index` (func, строка 223)
- `test_apply_patch_via_dispatch` (func, строка 238)
- `test_apply_patch_via_dispatch_json` (func, строка 247)
- `test_dispatch_rejects_unknown_args_structured` (func, строка 257)
- `test_dispatch_known_args_still_work` (func, строка 270)
- `test_apply_patch_unicode_and_quotes` (func, строка 277)
- `test_apply_patch_occurrence_out_of_range` (func, строка 289)
- `test_apply_patch_append_unicode` (func, строка 296)
- `test_apply_patch_multi_edit_with_occurrence` (func, строка 308)
- `test_apply_patch_large_file_roundtrip` (func, строка 323)
- `test_apply_patch_empty_old_rejected` (func, строка 335)
- `test_apply_patch_non_string_new_rejected` (func, строка 342)
- `test_apply_patch_edits_as_json_string` (func, строка 349)
- `test_apply_patch_edits_invalid_type` (func, строка 362)
- `test_apply_patch_reports_new_text_and_diff_when_staged` (func, строка 369)
- `test_ap_syntax_error_keeps_file_untouched_and_flags_propagated` (func, строка 389)
- `test_ap_syntax_error_rejects_whole_batch_atomically` (func, строка 408)
- `test_ap_refuses_to_wipe_file` (func, строка 426)
- `test_ap_success_reports_verified_true` (func, строка 441)
- `test_ap_fuzzy_indent` (func, строка 453)
- `test_ap_fuzzy_spaces` (func, строка 461)
- `test_ap_crlf` (func, строка 469)
- `test_ap_suggestions` (func, строка 479)
- `test_ap_fuzzy_disabled` (func, строка 489)
- `test_ap_fuzzy_ambiguous` (func, строка 495)
- `test_ap_utf8_bom_py_file_is_accepted` (func, строка 503)

### `tests/test_assistant_folders.py`
- `isolated_data_dir` (func, строка 22)
- `TestAssistantFolders` (class, строка 62)
- `TestAssistantCRUDWithFolders` (class, строка 215)
- `TestEntitySync` (class, строка 317)

### `tests/test_assistant_function_tools.py`
- `test_normalise_tools_converts_strings_and_passes_dicts` (func, строка 45)
- `test_has_native_function_tools` (func, строка 53)
- `test_extract_function_calls` (func, строка 61)
- `test_execute_rag_search_missing_args` (func, строка 79)
- `test_execute_rag_search_access_denied` (func, строка 86)
- `test_execute_rag_search_denied_when_no_bases_assigned` (func, строка 98)
- `test_execute_rag_search_ok` (func, строка 110)
- `_resp` (func, строка 129)
- `test_execute_rag_search_search_base_error` (func, строка 137)
- `test_loop_single_rag_call_happy_path` (func, строка 151)
- `test_loop_no_tool_call_single_request` (func, строка 196)
- `test_loop_iteration_limit` (func, строка 216)
- `test_loop_400_fallback_textual` (func, строка 243)
- `test_send_request_routes_to_tool_loop_when_native_function_tools` (func, строка 290)
- `test_send_request_preserves_legacy_yandex_path` (func, строка 339)
- `test_yandex_web_search_config_parses_provider_values` (func, строка 379)
- `test_yandex_web_search_config_list_values_and_defaults` (func, строка 397)
- `test_assistant_web_search_config_prefers_manifest_overrides` (func, строка 416)
- `test_assistant_web_search_config_falls_back_to_provider` (func, строка 439)
- `test_loop_assistant_no_web_search_tool_has_no_web_tool` (func, строка 458)
- `test_loop_unknown_function_tool_returns_error` (func, строка 496)
- `test_loop_payload_uses_assistant_web_search_overrides` (func, строка 528)

### `tests/test_assistant_sidebar_sort.py`
- `_a` (func, строка 22)
- `_t` (func, строка 26)
- `test_parse_ts_accepts_iso_numeric_datetime_and_junk` (func, строка 39)
- `test_last_dialogue_at_picks_newest_thread` (func, строка 57)
- `test_last_dialogue_at_filters_by_assistant_id` (func, строка 67)
- `test_last_dialogue_at_falls_back_to_created_at` (func, строка 78)
- `test_last_dialogue_at_empty_inputs` (func, строка 83)
- `test_new_assistant_lands_at_very_top` (func, строка 91)
- `test_all_assistants_without_dialogues_sorted_by_creation_desc` (func, строка 105)
- `test_assistants_with_dialogues_sorted_by_dialogue_desc` (func, строка 115)
- `test_dialogue_older_than_creation_still_sorts_first_for_that_assistant` (func, строка 130)
- `test_tie_without_dates_falls_back_to_name_and_input_is_not_mutated` (func, строка 143)
- `test_created_at_fallbacks_to_updated_at` (func, строка 154)
- `test_split_nav_lists_defaults_to_five` (func, строка 165)
- `test_split_nav_lists_respects_custom_count_and_empty_list` (func, строка 174)

### `tests/test_assistant_temperature.py`
- `test_temperature_bounds_from_service` (func, строка 16)
- `test_temperature_bounds_fallback` (func, строка 20)
- `test_temperature_bounds_bad_range` (func, строка 24)
- `test_clamp_temperature_within_range` (func, строка 28)
- `test_clamp_temperature_low` (func, строка 32)
- `test_clamp_temperature_high` (func, строка 36)
- `test_clamp_temperature_fallback` (func, строка 39)

### `tests/test_assistant_tools.py`
- `env` (func, строка 18)
- `_invoke` (func, строка 30)
- `_tools_multiselect_options` (func, строка 41)
- `_caption_texts` (func, строка 50)
- `test_assistant_form_shows_only_provider_tools` (func, строка 59)
- `test_assistant_form_hides_tools_unsupported_provider` (func, строка 84)

### `tests/test_backup_and_safewriter.py`
- `sandbox` (func, строка 12)
- `test_backup_creates_versions` (func, строка 32)
- `test_restore_backup` (func, строка 43)
- `test_protected_file_blocks_write` (func, строка 52)
- `test_protected_check_raises` (func, строка 60)
- `test_stage_and_apply_full_rewrite` (func, строка 66)
- `test_apply_without_draft_fails` (func, строка 85)
- `test_path_traversal_blocked` (func, строка 91)

### `tests/test_connectors.py`
- `isolated_data_dir` (func, строка 17)
- `_load_raw` (func, строка 23)
- `test_create_connection_roundtrip` (func, строка 29)
- `test_manifest_on_disk_has_no_plaintext_token` (func, строка 42)
- `test_list_and_get` (func, строка 54)
- `test_update_connection` (func, строка 66)
- `test_set_connection_token` (func, строка 79)
- `test_delete_connection` (func, строка 86)
- `test_validation` (func, строка 94)
- `test_services_registry` (func, строка 104)
- `test_public_manifest_never_leaks_token` (func, строка 113)

### `tests/test_core_api_json_schema.py`
- `_svc` (func, строка 23)
- `_cfg` (func, строка 34)
- `_skill` (func, строка 44)
- `test_bearer_request_sends_openai_response_format` (func, строка 57)
- `test_responses_transports_render_text_format` (func, строка 83)
- `test_send_request_yandex_payload_contains_text_format` (func, строка 94)
- `test_send_request_deepseek_payload_contains_text_format` (func, строка 120)
- `test_send_request_gigachat_payload_contains_response_format` (func, строка 148)
- `test_unwrap_json_text_strips_unk_fence_and_envelope` (func, строка 181)
- `test_unwrap_json_text_strips_unk_token` (func, строка 188)
- `test_bearer_unwraps_gigachat_style_envelope` (func, строка 194)
- `test_send_request_retries_without_schema_on_rejection` (func, строка 214)
- `test_send_request_does_not_retry_on_other_errors` (func, строка 243)
- `test_normalise_json_schema_envelopes` (func, строка 259)

### `tests/test_core_api_layer.py`
- `test_normalise_tools_strings` (func, строка 18)
- `test_normalise_tools_dicts` (func, строка 28)
- `test_normalise_tools_mixed` (func, строка 37)
- `test_normalise_tools_empty` (func, строка 52)
- `test_extract_responses_text_from_output_text` (func, строка 60)
- `test_extract_responses_text_from_output_blocks` (func, строка 66)
- `test_extract_responses_text_empty` (func, строка 83)
- `test_prepare_response_content_text_only` (func, строка 93)
- `test_prepare_response_content_ignores_reasoning_when_content_empty` (func, строка 99)
- `test_prepare_response_content_ignores_reasoning_when_content_present` (func, строка 111)
- `test_prepare_response_content_tool_calls_only` (func, строка 120)
- `test_prepare_response_content_both` (func, строка 138)
- `test_prepare_response_content_empty` (func, строка 156)
- `test_prepare_response_content_broken_tool_args` (func, строка 163)
- `test_gigachat_verify_honours_global_disable` (func, строка 182)
- `test_gigachat_verify_returns_default_bundle_when_present` (func, строка 188)
- `test_gigachat_verify_honours_env_bundle` (func, строка 198)
- `test_gigachat_verify_falls_back_when_bundle_missing` (func, строка 207)

### `tests/test_core_api_send.py`
- `_make_svc` (func, строка 26)
- `_make_cfg` (func, строка 39)
- `_make_skill` (func, строка 50)
- `test_bearer_request_success` (func, строка 69)
- `test_bearer_request_http_error` (func, строка 95)
- `test_bearer_request_with_tools` (func, строка 115)
- `test_gigachat_token_success` (func, строка 139)
- `test_gigachat_token_http_error` (func, строка 151)
- `test_send_request_bearer_success` (func, строка 165)
- `test_send_request_bearer_missing_key` (func, строка 182)
- `test_send_request_unknown_service` (func, строка 195)
- `test_send_request_yandex_iam_with_tools` (func, строка 207)
- `test_send_request_yandex_iam_without_tools` (func, строка 240)
- `test_send_request_yandex_iam_missing_key` (func, строка 271)
- `test_send_request_yandex_iam_missing_folder_id` (func, строка 289)
- `test_send_request_gigachat_success` (func, строка 307)
- `test_send_request_gigachat_http_error` (func, строка 337)
- `test_send_request_unknown_auth_type` (func, строка 370)
- `test_send_request_timeout` (func, строка 384)
- `test_send_request_network_error` (func, строка 398)
- `test_send_request_with_file_context` (func, строка 412)
- `test_test_connection_bearer_success` (func, строка 436)
- `test_test_connection_bearer_missing_key` (func, строка 453)
- `test_test_connection_unknown_service` (func, строка 465)
- `test_test_connection_yandex_iam_success` (func, строка 474)
- `test_test_connection_yandex_iam_missing_key` (func, строка 501)
- `test_test_connection_yandex_iam_missing_folder_id` (func, строка 517)
- `test_test_connection_gigachat_success` (func, строка 533)
- `test_test_connection_gigachat_models_failure` (func, строка 560)
- `test_test_connection_missing_service` (func, строка 587)
- `test_test_connection_exception` (func, строка 596)
- `test_test_connection_truly_unknown_auth` (func, строка 609)

### `tests/test_core_files.py`
- `test_max_upload_tokens_constant` (func, строка 12)
- `test_check_upload_tokens_within_limit` (func, строка 16)
- `test_check_upload_tokens_over_limit` (func, строка 22)
- `test_check_upload_tokens_at_limit` (func, строка 29)
- `test_check_upload_tokens_custom_limit` (func, строка 36)
- `test_estimate_tokens_returns_positive_int` (func, строка 43)
- `test_should_store_uploaded_file_threshold` (func, строка 48)
- `test_build_attachment_metadata_small_file` (func, строка 54)
- `test_build_attachment_metadata_large_file` (func, строка 65)
- `test_build_attachments_context_mixed` (func, строка 73)
- `test_build_attachments_context_empty` (func, строка 91)
- `test_build_saved_files_registry_empty` (func, строка 97)
- `test_build_saved_files_registry_entries` (func, строка 102)

### `tests/test_crypto.py`
- `isolated_crypto` (func, строка 24)
- `test_key_is_created_in_external_file_not_next_to_db` (func, строка 48)
- `test_key_file_permissions_are_0600` (func, строка 65)
- `test_encrypt_decrypt_round_trip` (func, строка 75)
- `test_encrypt_empty_returns_empty` (func, строка 84)
- `test_decrypt_invalid_token_raises` (func, строка 91)
- `test_decrypt_token_from_foreign_key_raises` (func, строка 98)
- `test_legacy_key_is_migrated_and_removed` (func, строка 112)
- `test_no_legacy_file_does_not_fail` (func, строка 135)
- `test_env_key_takes_precedence` (func, строка 144)
- `test_env_key_file_override` (func, строка 160)

### `tests/test_deepseek_responses.py`
- `_responses_body` (func, строка 26)
- `test_extract_deepseek_responses_text_ignores_reasoning` (func, строка 42)
- `test_extract_deepseek_responses_text_output_text_shortcut` (func, строка 48)
- `test_extract_deepseek_responses_text_function_call` (func, строка 53)
- `test_extract_deepseek_responses_text_empty` (func, строка 65)
- `test_reasoning_effort_none_disables_thinking` (func, строка 73)
- `test_reasoning_effort_uses_configured_value` (func, строка 79)
- `test_reasoning_effort_defaults_to_max` (func, строка 85)
- `test_deepseek_responses_request_payload_and_parse` (func, строка 95)
- `test_deepseek_responses_request_none_sets_none` (func, строка 133)
- `test_deepseek_reports_cached_tokens` (func, строка 154)
- `test_deepseek_cached_tokens_zero_when_absent` (func, строка 178)
- `test_deepseek_tool_choice_in_payload` (func, строка 204)
- `test_deepseek_tool_choice_omitted_by_default` (func, строка 228)
- `test_send_request_deepseek_responses_routes` (func, строка 253)
- `test_send_request_deepseek_forwards_reasoning_effort` (func, строка 282)
- `test_send_request_deepseek_forwards_tool_choice` (func, строка 316)
- `test_send_request_deepseek_web_search_not_auto_forced` (func, строка 348)
- `test_parse_tool_calls_accepts_xml_wrapped_json` (func, строка 387)
- `test_parse_tool_calls_ignores_question_wrapper` (func, строка 393)
- `test_strip_tool_calls_removes_xml_wrappers` (func, строка 400)
- `test_strip_tool_calls_removes_empty_fences` (func, строка 415)
- `test_strip_tool_calls_removes_dsml_blocks` (func, строка 424)
- `test_protect_history_still_honors_flags` (func, строка 452)
- `test_settings_page_hides_reasoning_effort_field` (func, строка 469)

### `tests/test_default_imports.py`
- `isolated_data_dir` (func, строка 30)
- `TestDefaultsLoaders` (class, строка 82)
- `TestBootstrapDefaults` (class, строка 133)
- `TestLegacyFallbacks` (class, строка 256)

### `tests/test_default_rag_bases.py`
- `isolated_data_dir` (func, строка 31)
- `TestDefaultsRagHelpers` (class, строка 73)
- `TestDefaultRagImport` (class, строка 86)
- `TestPresetRagAssignment` (class, строка 142)

### `tests/test_devagent_thread_workspace.py`
- `isolated_data` (func, строка 23)
- `_create_temp_workspaces` (func, строка 41)
- `test_create_devagent_thread_saves_workspace` (func, строка 52)
- `test_save_thread_workspace_updates_last_workspace` (func, строка 81)
- `test_repo_devagent_create_thread_persists_columns` (func, строка 109)
- `test_thread_columns_are_migrated` (func, строка 126)
- `test_to_dict_contains_new_columns` (func, строка 169)

### `tests/test_dispatcher_connections.py`
- `isolated_data_dir` (func, строка 16)
- `orch_slug` (func, строка 51)
- `TestDispatcherConnectionTools` (class, строка 58)

### `tests/test_employee_management_ui.py`
- `isolated_data` (func, строка 25)
- `_fresh_ui` (func, строка 46)
- `test_orchestrators_management_page_renders` (func, строка 54)
- `test_orchestrator_settings_page_has_no_export_import_tab` (func, строка 74)
- `test_no_export_import_employee_ui_in_code` (func, строка 106)

### `tests/test_github_connector.py`
- `isolated_connector` (func, строка 15)
- `test_test_connection_sets_account` (func, строка 23)
- `test_test_connection_auth_error` (func, строка 51)
- `test_get_user_info` (func, строка 69)
- `test_list_repos` (func, строка 94)
- `test_get_repo_info_owner_repo` (func, строка 126)
- `test_get_repo_info_bare_name_uses_login` (func, строка 154)
- `test_create_repo` (func, строка 177)
- `test_read_file_decodes_base64` (func, строка 208)
- `test_upload_file_calls_create_file` (func, строка 244)
- `test_upload_file_conflict` (func, строка 276)
- `test_update_file_uses_passed_sha` (func, строка 306)
- `test_delete_file_uses_passed_sha` (func, строка 334)
- `test_list_files` (func, строка 362)
- `test_repo_name_validation` (func, строка 394)

### `tests/test_github_tools.py`
- `test_github_list_repos_ok` (func, строка 17)
- `test_github_list_repos_missing_connector_id` (func, строка 25)
- `test_github_list_repos_default_sort` (func, строка 32)
- `test_github_create_repo_ok` (func, строка 39)
- `test_github_create_repo_missing_name` (func, строка 50)
- `test_github_upload_file_ok` (func, строка 57)
- `test_github_upload_file_missing_repo_or_path` (func, строка 73)
- `test_github_update_file_ok` (func, строка 79)
- `test_github_read_file_ok` (func, строка 91)
- `test_github_tool_wraps_connector_error` (func, строка 102)
- `test_get_tools_metadata` (func, строка 110)

### `tests/test_i18n_serialization.py`
- `test_dumps_lang_preserves_insertion_order` (func, строка 20)
- `test_dumps_lang_sort_keys_is_alphabetical` (func, строка 32)
- `test_dumps_lang_keeps_unicode_chars` (func, строка 39)
- `test_dump_lang_file_writes_and_roundtrips` (func, строка 46)
- `test_dump_lang_file_creates_parent_dirs` (func, строка 57)
- `test_dump_lang_file_returns_false_on_write_error` (func, строка 64)

### `tests/test_i18n_sync.py`
- `_repo_root` (func, строка 26)
- `_collect_keys` (func, строка 31)
- `_load_lang` (func, строка 54)
- `test_t_keys_exist_in_every_lang_file` (func, строка 59)
- `test_lang_files_are_valid_json` (func, строка 72)

### `tests/test_list_files.py`
- `ws` (func, строка 26)
- `_paths` (func, строка 39)
- `test_default_depth_one_level_no_recursion` (func, строка 43)
- `test_max_depth_two_levels_flat_files` (func, строка 57)
- `test_max_depth_three_levels_full_tree` (func, строка 69)
- `test_invalid_max_depth_coerces_to_one` (func, строка 78)
- `test_subdir_scoping_with_max_depth` (func, строка 87)

### `tests/test_llm_utils_json_schema.py`
- `_send_request_stub` (func, строка 14)
- `test_direct_send_request_receives_json_schema_in_assistant` (func, строка 20)
- `test_direct_send_request_passes_bare_schema_without_name` (func, строка 32)
- `test_no_json_schema_field_when_not_requested` (func, строка 43)
- `test_adapter_form_call_unchanged` (func, строка 51)

### `tests/test_numeric_arg_coercion.py`
- `sandbox` (func, строка 18)
- `test_read_file_coerces_string_offset_and_limit` (func, строка 34)
- `test_list_files_coerces_string_max_depth` (func, строка 45)
- `test_apply_patch_coerces_string_occurrence` (func, строка 51)
- `test_search_in_files_coerces_bools_and_ints` (func, строка 64)
- `test_coerce_numeric_args_rejects_garbage_strings` (func, строка 74)
- `test_coerce_numeric_args_keeps_unknown_params` (func, строка 81)

### `tests/test_orchestrator_chat_prefs.py`
- `ui_env` (func, строка 23)
- `_rerender` (func, строка 35)
- `_markdown_calls` (func, строка 42)
- `_checkbox_by_key` (func, строка 46)
- `_orch_dict` (func, строка 53)
- `_render_chat` (func, строка 64)
- `test_devagent_page_uses_devagent_welcome` (func, строка 80)
- `test_custom_orchestrator_keeps_generic_welcome` (func, строка 95)
- `test_checkbox_values_come_from_saved_config` (func, строка 111)
- `test_checkbox_defaults_without_saved_config` (func, строка 141)
- `test_toolbar_renders_once_without_bottom_duplicates` (func, строка 154)
- `test_save_chat_pref_merges_config` (func, строка 168)
- `test_save_chat_pref_rejects_unknown_key_and_missing_orch` (func, строка 195)
- `test_chat_prefs_fallback_on_non_dict_config` (func, строка 209)

### `tests/test_orchestrator_connections.py`
- `isolated_data_dir` (func, строка 17)
- `orch_slug` (func, строка 52)
- `test_default_enabled_connections_empty` (func, строка 63)
- `test_set_get_enabled_connections` (func, строка 68)
- `test_set_enabled_connections_missing_orchestrator` (func, строка 74)
- `test_prompt_extended_with_connections` (func, строка 79)
- `test_prompt_unchanged_when_no_connections` (func, строка 95)
- `test_build_assistant_dicts_includes_connections_block` (func, строка 100)
- `test_devagent_default_config_has_key` (func, строка 110)

### `tests/test_orchestrator_economy_cache.py`
- `_FakeCore` (class, строка 25)
- `_FakeDispatcher` (class, строка 39)
- `ui_env` (func, строка 54)
- `_mk` (func, строка 83)
- `_sent_len` (func, строка 91)
- `_drive` (func, строка 96)
- `_setup_page` (func, строка 116)
- `test_do_step_window_grows_again_after_terminal_status` (func, строка 150)
- `test_do_step_full_cycle_resets_then_grows` (func, строка 197)

### `tests/test_orchestrator_folders.py`
- `isolated_data_dir` (func, строка 21)
- `TestOrchestratorFolders` (class, строка 61)
- `TestBootstrapInstructions` (class, строка 199)
- `TestUniversalAgentOrchestratorTools` (class, строка 221)
- `TestOrchestratorCRUDWithFolders` (class, строка 268)
- `TestSlugSafety` (class, строка 340)
- `TestOrchestratorLifecycleGuards` (class, строка 363)

### `tests/test_phase1_agent_loop.py`
- `_make_skill` (func, строка 32)
- `_scripted_send` (func, строка 41)
- `FakeDispatcher` (class, строка 57)
- `TestProseQuestion` (class, строка 77)
- `TestParse` (class, строка 109)
- `TestLoop` (class, строка 171)
- `TestDSML` (class, строка 456)
- `TestManualMode` (class, строка 520)
- `TestLoopStatus` (class, строка 641)
- `TestEconomyCacheMode` (class, строка 708)
- `TestPlanConfirmationStops` (class, строка 1015)
- `TestUnparsedDiagnostics` (class, строка 1119)
- `test_live_loop_history_entries_get_ts` (func, строка 1256)
- `TestDsmlValidation` (class, строка 1276)
- `TestDsmlStepLoop` (class, строка 1307)

### `tests/test_phase1_core_pure.py`
- `test_py_compile` (func, строка 51)
- `test_core_imports_without_streamlit` (func, строка 74)
- `StreamlitBlocker` (class, строка 86)
- `test_fs_json_roundtrip` (func, строка 110)
- `test_fs_json_missing_returns_default` (func, строка 120)
- `test_fs_text_roundtrip` (func, строка 127)
- `test_fs_text_missing_returns_default` (func, строка 137)
- `test_fs_ensure_dir` (func, строка 143)
- `test_decode_bytes_utf8` (func, строка 154)
- `test_decode_bytes_cp1251` (func, строка 159)
- `test_decode_bytes_fallback` (func, строка 166)
- `test_combine_nonempty_basic` (func, строка 177)
- `test_combine_nonempty_skips_empty` (func, строка 182)
- `test_combine_nonempty_custom_sep` (func, строка 187)
- `test_combine_nonempty_all_empty` (func, строка 192)
- `test_md_to_txt_basic` (func, строка 199)
- `test_md_to_txt_removes_links` (func, строка 213)
- `test_md_to_txt_removes_code_blocks` (func, строка 221)
- `test_md_to_html_nonempty` (func, строка 230)
- `test_md_to_html_contains_body` (func, строка 239)
- `test_i18n_t_returns_key_when_no_langs` (func, строка 247)
- `test_i18n_t_returns_key_when_translation_missing` (func, строка 255)
- `test_i18n_t_returns_translation` (func, строка 269)
- `test_i18n_t_no_lang_param_uses_first` (func, строка 283)
- `test_ui_syntax` (func, строка 310)

### `tests/test_phase1_storage.py`
- `isolated_db` (func, строка 16)
- `test_db_creates_tables` (func, строка 35)
- `test_orm_models_match_actual_schema` (func, строка 48)
- `test_orm_to_dict_keys_match_real_columns` (func, строка 73)
- `test_repo_functions_use_only_existing_columns` (func, строка 121)
- `test_skill_create_and_load` (func, строка 159)
- `test_skill_update` (func, строка 182)
- `test_skill_delete` (func, строка 200)
- `test_skill_round_trip` (func, строка 209)
- `test_skill_reasoning_effort_round_trip` (func, строка 230)
- `test_thread_create_and_load` (func, строка 251)
- `test_thread_messages_round_trip` (func, строка 262)
- `test_thread_append_message` (func, строка 282)
- `test_thread_delete` (func, строка 298)
- `test_list_all_threads_sorted` (func, строка 311)
- `test_delete_all_threads` (func, строка 329)
- `test_config_save_and_load` (func, строка 343)
- `test_config_overwrite` (func, строка 355)
- `test_config_empty` (func, строка 365)
- `test_core_config_has_key` (func, строка 374)
- `test_core_config_has_key_missing` (func, строка 382)

### `tests/test_platform_bootstrap.py`
- `isolated_data_dir` (func, строка 37)
- `_load_default_instruction` (func, строка 85)
- `test_assistant_creator_prompt_is_long_enough` (func, строка 97)
- `test_employee_creator_prompt_is_long_enough` (func, строка 105)
- `test_prompt_improver_instruction_is_long_enough` (func, строка 112)
- `test_ensure_instructions_returns_expected_keys` (func, строка 120)
- `test_ensure_instructions_is_idempotent` (func, строка 133)
- `test_ensure_devagent_settings_seeds_builtin_orchestrator` (func, строка 148)
- `test_ensure_devagent_settings_sets_prompt_from_system_prompt_md` (func, строка 161)
- `test_ensure_devagent_settings_seeds_config_and_tools` (func, строка 172)
- `test_ensure_devagent_settings_seeds_economy_defaults` (func, строка 189)
- `test_ensure_devagent_settings_seeds_max_tokens_in_bundle` (func, строка 212)
- `test_ensure_devagent_settings_is_idempotent` (func, строка 225)
- `test_ensure_devagent_settings_preserves_user_config` (func, строка 234)
- `test_ensure_devagent_settings_backfills_missing_config_fields` (func, строка 271)
- `test_ensure_devagent_settings_backfills_zero_max_tokens` (func, строка 318)
- `test_ensure_devagent_settings_keeps_user_economy_tail` (func, строка 341)

### `tests/test_platform_scenarios.py`
- `isolated_data` (func, строка 43)
- `devagent_sandbox` (func, строка 79)
- `test_assistant_full_lifecycle_scenario` (func, строка 102)
- `test_chat_with_model_and_files_scenario` (func, строка 178)
- `test_employee_orchestrator_scenario` (func, строка 238)
- `test_devagent_edit_cycle_scenario` (func, строка 353)
- `test_universal_developer_external_project_scenario` (func, строка 424)
- `test_rag_base_full_scenario` (func, строка 497)
- `test_i18n_scenario` (func, строка 585)
- `test_config_secrets_and_connection_scenario` (func, строка 620)
- `test_prompt_improvement_scenario` (func, строка 677)
- `test_api_roundtrip_scenarios` (func, строка 710)

### `tests/test_preset_orchestrators.py`
- `isolated_data_dir` (func, строка 31)
- `_run_bootstrap` (func, строка 69)
- `_get_preset` (func, строка 74)
- `test_preset_created_on_first_boot` (func, строка 79)
- `test_preset_prompt_loaded_from_md` (func, строка 107)
- `test_preset_grants_full_toolset` (func, строка 118)
- `test_preset_is_idempotent` (func, строка 126)
- `test_preset_preserves_user_changes` (func, строка 133)

### `tests/test_prompt_guard_strict.py`
- `TestSanitizeToolResultStrict` (class, строка 6)

### `tests/test_prompt_improver.py`
- `_load_instruction_body` (func, строка 22)
- `test_instruction_file_is_long_enough` (func, строка 36)
- `test_get_improver_instruction_returns_text` (func, строка 44)
- `test_get_improver_instruction_missing_returns_empty` (func, строка 64)
- `test_improve_uses_weak_model_and_returns_text` (func, строка 73)
- `test_improve_empty_prompt_raises` (func, строка 114)
- `test_improve_missing_instruction_raises` (func, строка 122)
- `test_improve_no_weak_model_raises` (func, строка 129)
- `test_improve_empty_model_output_raises` (func, строка 146)

### `tests/test_propose_file.py`
- `sandbox` (func, строка 22)
- `test_propose_file_auto_applies_full_content` (func, строка 40)
- `test_propose_file_creates_new_file_on_disk` (func, строка 69)
- `test_apply_edit_after_manual_staging` (func, строка 82)
- `test_verify_file_finds_expected_and_unexpected` (func, строка 101)
- `test_verify_file_unknown_path` (func, строка 129)
- `test_propose_file_in_tool_catalog` (func, строка 136)
- `test_dispatch_routes_propose_file` (func, строка 144)

### `tests/test_propose_file_scenarios.py`
- `sandbox` (func, строка 26)
- `_read` (func, строка 40)
- `test_basic_create_and_readback` (func, строка 46)
- `test_basic_overwrite_preserves_exact_content` (func, строка 56)
- `test_rewrite_twice` (func, строка 65)
- `test_empty_file` (func, строка 75)
- `test_single_line_no_trailing_newline` (func, строка 83)
- `test_create_file_in_nested_directories` (func, строка 93)
- `test_unicode_and_emoji_preserved` (func, строка 104)
- `test_escape_sequences_preserved_literally` (func, строка 117)
- `test_quotes_and_all_sorts_of_symbols_preserved` (func, строка 129)
- `test_json_content_preserved` (func, строка 150)
- `test_markdown_content_preserved` (func, строка 165)
- `test_python_file_valid_syntax_preserved` (func, строка 181)
- `test_python_multiline_string_with_pipe_numbers` (func, строка 196)
- `test_lines_looking_like_line_numbers_are_preserved` (func, строка 213)
- `test_numbered_list_with_pipes_in_markdown` (func, строка 239)
- `test_pipe_number_lines_inside_json_string` (func, строка 253)
- `test_large_file_roundtrip` (func, строка 264)
- `test_protected_file_rejected` (func, строка 277)
- `test_path_traversal_rejected` (func, строка 287)
- `test_directory_path_rejected` (func, строка 293)
- `test_verified_text_matches_disk` (func, строка 301)
- `test_changelog_created` (func, строка 309)
- `test_draft_cleared_after_apply` (func, строка 317)
- `test_propose_file_non_utf8_returns_clean_error` (func, строка 323)

### `tests/test_protect_history.py`
- `TestProtectHistory` (class, строка 9)
- `TestParseSanitizedInfo` (class, строка 160)

### `tests/test_rag_chunks_and_preset_skills.py`
- `isolated_data_dir` (func, строка 25)
- `_make_index_db` (func, строка 57)
- `TestRagIndexChunkOps` (class, строка 72)
- `TestRagChunkWrappers` (class, строка 133)
- `TestPresetSkillAutoRegistration` (class, строка 192)

### `tests/test_rag_tools_robustness.py`
- `sandbox` (func, строка 16)
- `test_rag_search_rejects_legacy_arg_names_with_suggestion` (func, строка 29)
- `test_rag_search_missing_slug_has_suggestion` (func, строка 41)
- `test_rag_search_missing_query_has_suggestion` (func, строка 51)
- `test_rag_search_valid_call_reaches_backend` (func, строка 61)

### `tests/test_recent_workspaces.py`
- `isolated_db` (func, строка 16)
- `two_dirs` (func, строка 32)
- `test_add_and_get` (func, строка 43)
- `test_get_empty_returns_list` (func, строка 57)
- `test_duplicates_are_deduplicated` (func, строка 63)
- `test_max_limit_is_five` (func, строка 77)
- `test_nonexistent_folders_are_filtered` (func, строка 97)
- `test_clear_removes_all` (func, строка 112)
- `test_add_idempotent_on_failure` (func, строка 124)
- `test_list_recent_workspaces_tool` (func, строка 137)

### `tests/test_render_token_line.py`
- `test_format_token_line_basic` (func, строка 10)
- `test_format_token_line_with_economy_meta` (func, строка 17)
- `test_format_token_line_zeros` (func, строка 24)
- `test_format_token_line_custom_color` (func, строка 30)
- `test_format_token_line_with_cache` (func, строка 35)
- `test_format_token_line_cache_zero_hidden` (func, строка 40)
- `test_format_token_line_cache_clamped_at_100` (func, строка 46)
- `test_format_token_line_cache_ignored_when_in_zero` (func, строка 51)
- `_html_calls` (func, строка 56)
- `_attr_value` (func, строка 61)
- `_call_payload` (func, строка 67)
- `test_clipboard_button_with_html_and_newlines` (func, строка 74)
- `test_clipboard_button_with_quotes_and_html_label` (func, строка 91)
- `test_clipboard_button_uses_theme_css_variables` (func, строка 107)
- `test_clipboard_button_copy_url_params_mode` (func, строка 122)

### `tests/test_safety_mode.py`
- `test_safety_enabled_is_true_by_default` (func, строка 25)
- `test_run_code_skips_confirmation_when_safety_disabled` (func, строка 30)
- `test_run_code_requires_confirmation_when_safety_enabled` (func, строка 41)
- `test_run_test_skips_confirmation_when_safety_disabled` (func, строка 52)
- `test_run_test_requires_confirmation_when_safety_enabled` (func, строка 63)

### `tests/test_sanitized_approval_flow.py`
- `_read_result` (func, строка 44)
- `ReadDispatcher` (class, строка 54)
- `_run_until_terminal` (func, строка 66)
- `test_approve_sanitized_lets_loop_continue` (func, строка 75)
- `test_protect_history_honors_approved_paths` (func, строка 138)

### `tests/test_search_in_files.py`
- `ws` (func, строка 29)
- `test_literal_search` (func, строка 50)
- `test_regex_search` (func, строка 60)
- `test_case_sensitive` (func, строка 67)
- `test_subdir_scoping` (func, строка 73)
- `test_path_file_targets_single_file` (func, строка 84)
- `test_path_directory_acts_like_subdir` (func, строка 93)
- `test_path_takes_precedence_over_subdir` (func, строка 103)
- `test_path_missing_rejected` (func, строка 112)
- `test_path_escape_rejected` (func, строка 118)
- `test_extensions_as_string` (func, строка 124)
- `test_extensions_as_list` (func, строка 131)
- `test_csv_file_searchable_with_extensions` (func, строка 139)
- `test_legacy_encoding_file_searchable` (func, строка 147)
- `test_empty_query_rejected` (func, строка 154)
- `test_invalid_regex_rejected` (func, строка 159)
- `test_missing_subdir_rejected` (func, строка 164)
- `test_no_matches_returns_ok` (func, строка 169)
- `test_max_results_truncates` (func, строка 175)
- `test_large_file_skipped_with_counter` (func, строка 184)
- `test_context_windows_present` (func, строка 195)
- `test_context_default_absent` (func, строка 207)
- `test_context_invalid_values_coerced_to_zero` (func, строка 215)
- `test_context_lines_trimmed` (func, строка 223)
- `test_context_multiline_window` (func, строка 235)
- `test_files_scans_only_listed_files` (func, строка 249)
- `test_files_ignores_extension_filter` (func, строка 259)
- `test_files_missing_file_rejected` (func, строка 267)
- `test_files_directory_rejected` (func, строка 273)
- `test_files_escape_rejected` (func, строка 280)
- `test_files_takes_precedence_over_path_and_subdir` (func, строка 286)
- `test_files_string_coerced_to_list` (func, строка 293)
- `test_files_empty_list_rejected` (func, строка 299)
- `test_files_non_string_entry_rejected` (func, строка 305)
- `test_files_duplicates_do_not_crash` (func, строка 311)

### `tests/test_skills_adaptation.py`
- `isolated_data_dir` (func, строка 39)
- `TestAdaptationRegistry` (class, строка 65)
- `TestDevAgentMarkSkillAdapted` (class, строка 162)
- `ui_env` (func, строка 197)
- `_rerender` (func, строка 208)
- `_button_keys` (func, строка 215)
- `test_adapt_button_requests_adaptation` (func, строка 219)

### `tests/test_skills_library.py`
- `isolated_data_dir` (func, строка 30)
- `make_zip` (func, строка 62)
- `TestRegistry` (class, строка 71)
- `TestZipImport` (class, строка 142)
- `TestHelpers` (class, строка 206)
- `TestOrchestratorIntegration` (class, строка 256)

### `tests/test_st_mock.py`
- `test_sidebar_children_are_logged` (func, строка 10)
- `test_sidebar_with_context_logs_widgets_as_top_level` (func, строка 18)
- `test_columns_children_are_logged_with_index_path` (func, строка 27)
- `test_tabs_children_are_logged_with_index_path` (func, строка 38)
- `test_deep_sidebar_columns_chain_is_logged` (func, строка 47)
- `test_empty_container_logs_children` (func, строка 56)
- `test_context_manager_widgets_still_work` (func, строка 65)

### `tests/test_statistics.py`
- `_rec` (func, строка 13)
- `test_parse_ts_accepts_stored_iso_format` (func, строка 26)
- `test_parse_ts_rejects_garbage_and_empty` (func, строка 31)
- `test_int_coercion` (func, строка 37)
- `test_cache_pct_normal` (func, строка 47)
- `test_cache_pct_zero_and_clamping` (func, строка 51)
- `test_bucket_keys` (func, строка 59)
- `test_week_bucket_uses_monday` (func, строка 66)
- `_sorted_keys` (func, строка 77)
- `test_bucketize_groups_and_sorts` (func, строка 81)
- `test_bucket_rows_sums_tokens` (func, строка 93)
- `test_build_summary_totals_and_sorting` (func, строка 109)
- `test_build_summary_period_has_no_buckets` (func, строка 137)
- `test_build_summary_invalid_granularity_falls_back_to_day` (func, строка 144)
- `test_build_summary_empty_records` (func, строка 150)
- `test_build_summary_unknown_provider_grouping` (func, строка 158)
- `test_collect_usage_filters_period` (func, строка 171)
- `test_available_bounds` (func, строка 182)
- `test_available_bounds_empty` (func, строка 191)

### `tests/test_stats_page_ui.py`
- `_make_records` (func, строка 22)
- `_inject_records` (func, строка 33)
- `_fresh_page_stats` (func, строка 45)
- `test_stats_page_renders_empty_state` (func, строка 51)
- `test_stats_page_renders_data` (func, строка 61)
- `test_stats_page_selectbox_defaults_to_day` (func, строка 75)
- `test_stats_page_key_uniqueness` (func, строка 86)
- `test_stats_period_granularity_hides_buckets` (func, строка 99)

### `tests/test_storage_page_ui.py`
- `isolated_data_dir` (func, строка 25)
- `_make_base_with_chunks` (func, строка 57)
- `_fresh_storage_page` (func, строка 77)
- `_render` (func, строка 83)
- `_call_names` (func, строка 91)
- `_button_keys` (func, строка 95)
- `test_page_has_no_create_or_indexing_ui` (func, строка 99)
- `test_page_without_bases_shows_empty_hint` (func, строка 115)
- `test_base_card_actions_and_chunks_section` (func, строка 123)

### `tests/test_structured_output_consumers.py`
- `test_classify_passes_json_schema_and_name` (func, строка 38)
- `test_classify_fenced_json_still_parsed` (func, строка 58)
- `test_classify_exception_falls_back_to_defaults` (func, строка 69)
- `test_classify_keyword_hints_when_json_invalid` (func, строка 80)
- `sandbox` (func, строка 98)
- `test_assistant_creator_passes_json_schema_and_creates` (func, строка 110)
- `test_assistant_creator_parse_failure_returns_error` (func, строка 170)

### `tests/test_task_state.py`
- `sandbox` (func, строка 20)
- `test_journal_file_name_embeds_thread_id` (func, строка 43)
- `test_build_contains_all_sections` (func, строка 50)
- `test_split_active_sections_roundtrip` (func, строка 63)
- `test_extract_step_ids` (func, строка 75)
- `test_parse_step_handles_variants` (func, строка 80)
- `test_ensure_and_read_roundtrip` (func, строка 89)
- `test_ensure_existing_not_overwritten` (func, строка 101)
- `test_update_section_preserves_others` (func, строка 113)
- `test_update_section_unknown_rejected` (func, строка 125)
- `test_mark_step_updates_status_and_progress` (func, строка 132)
- `test_mark_step_records_context_for_next_step` (func, строка 148)
- `test_mark_step_replaces_existing_meta` (func, строка 159)
- `test_mark_step_unknown_fails` (func, строка 172)
- `test_mark_step_invalid_status_fails` (func, строка 180)
- `test_clear_archives_active_task_and_keeps_file` (func, строка 190)
- `test_archive_and_start_task_appends_to_same_journal` (func, строка 209)
- `test_history_survives_multiple_tasks` (func, строка 225)
- `test_legacy_root_file_migrated_once` (func, строка 236)
- `test_context_helper_missing_returns_none` (func, строка 257)
- `test_context_helper_returns_block_with_meta` (func, строка 261)
- `test_context_includes_recent_history` (func, строка 271)
- `test_context_helper_truncates` (func, строка 285)
- `test_tool_methods_roundtrip` (func, строка 296)
- `test_tool_init_archives_previous_task` (func, строка 318)
- `test_tool_catalog_lists_task_state_tools` (func, строка 331)
- `test_agent_loop_helpers_present` (func, строка 341)

### `tests/test_theme_restore.py`
- `_drop_ui_modules` (func, строка 21)
- `app_under_mock` (func, строка 28)
- `test_payload_for_assistant_page` (func, строка 43)
- `test_payload_for_orchestrator_page` (func, строка 64)
- `test_payload_skips_settings_pages` (func, строка 76)
- `test_apply_theme_uses_replace_with_restore_marker` (func, строка 86)
- `test_restore_reapplies_assistant_snapshot_once` (func, строка 103)
- `test_restore_without_marker_leaves_state_untouched` (func, строка 125)
- `test_restore_orchestrator_reloads_thread` (func, строка 133)

### `tests/test_thread_deeplink.py`
- `_drop_ui_modules` (func, строка 28)
- `deeplink_context` (func, строка 35)
- `test_deeplink_navigates_and_loads_thread` (func, строка 50)
- `test_deeplink_without_thread_starts_fresh_dialog` (func, строка 72)
- `test_deeplink_unknown_thread_falls_back_to_fresh_dialog` (func, строка 85)
- `test_deeplink_without_params_is_a_noop` (func, строка 103)
- `test_deeplink_runs_only_once_per_session` (func, строка 111)

### `tests/test_thread_file_save.py`
- `isolated_data` (func, строка 18)
- `test_create_thread_creates_files_dir` (func, строка 36)
- `test_save_thread_file_creates_files_dir` (func, строка 46)

### `tests/test_tools_utils.py`
- `test_no_service_def_returns_empty` (func, строка 13)
- `test_missing_tools_options_returns_empty` (func, строка 18)
- `test_tools_options_dict_keys` (func, строка 23)
- `test_tools_options_strings` (func, строка 28)
- `test_tools_options_unknown_keys` (func, строка 33)
- `test_filters_by_catalog` (func, строка 38)
- `test_empty_tools_options_returns_empty` (func, строка 44)
- `test_malformed_entries_skipped` (func, строка 48)

### `tests/test_ui_pages.py`
- `_apply_all` (func, строка 79)
- `invoke_page` (func, строка 92)
- `_call_names` (func, строка 103)
- `mock_env` (func, строка 111)
- `test_main_base_renders` (func, строка 135)
- `test_theme_selector_rendered_in_sidebar` (func, строка 141)
- `_render_theme_select` (func, строка 160)
- `test_theme_dark_select_writes_native_streamlit_theme_key` (func, строка 168)
- `test_theme_system_and_light_select_emit_correct_mode` (func, строка 187)
- `test_skills_page_when_empty` (func, строка 213)
- `test_skills_page_with_skills_list` (func, строка 218)
- `test_settings_api_keys_tab_renders` (func, строка 233)
- `test_devagent_settings_tab_renders` (func, строка 241)
- `test_instructions_tab_renders` (func, строка 256)
- `test_settings_provider_save_shows_success` (func, строка 262)
- `test_settings_global_save_button_absent` (func, строка 283)
- `test_workspace_picker_initial_state` (func, строка 306)
- `test_orchestrator_favicon_is_static_only` (func, строка 312)
- `test_orchestrator_models_settings_renders_search_prompt_area` (func, строка 332)
- `test_orchestrator_models_settings_saves_reasoning_effort` (func, строка 388)
- `test_assistants_improve_prompt_does_not_mutate_existing_widget` (func, строка 433)
- `_open_assistant_create_form` (func, строка 491)
- `test_assistant_form_field_order` (func, строка 500)
- `test_assistant_form_max_tool_calls_default_three` (func, строка 535)
- `test_assistant_form_renders_reasoning_effort_select` (func, строка 555)
- `test_assistant_create_saves_reasoning_effort` (func, строка 582)
- `test_assistants_page_back_to_chat_returns_to_chat` (func, строка 612)
- `test_strip_html_details_tags_removes_wrappers` (func, строка 634)
- `test_strip_tool_calls_removes_details_tags` (func, строка 652)
- `test_render_tool_result_shows_call_and_first_two_lines` (func, строка 669)
- `test_render_tool_result_short_result_no_nested_expander` (func, строка 709)
- `test_render_events_pairs_tool_call_with_tool_result` (func, строка 731)
- `test_render_events_standalone_tool_call_falls_back` (func, строка 759)
- `test_chat_page_has_settings_and_new_dialog_buttons` (func, строка 775)
- `test_chat_page_keeps_selected_assistant_across_reruns` (func, строка 820)

### `tests/test_ui_tooltips.py`
- `_lang_files` (func, строка 49)
- `_service_files` (func, строка 59)
- `_ui_help_keys` (func, строка 69)
- `test_ui_tooltip_keys_exist_and_are_non_empty_in_all_langs` (func, строка 91)
- `test_no_anthropics_skills_example_link` (func, строка 108)
- `test_devagent_search_model_help_does_not_advertise_provider` (func, строка 115)
- `mock_env` (func, строка 128)
- `_invoke` (func, строка 138)
- `_help_by_key` (func, строка 145)
- `_assert_real_help` (func, строка 153)
- `test_settings_api_key_widgets_use_service_key_help` (func, строка 192)
- `test_assistant_form_widgets_have_tooltips` (func, строка 221)
- `test_skills_library_widgets_have_tooltips` (func, строка 275)
- `test_storage_widgets_have_tooltips` (func, строка 301)

### `tests/test_ui_tooltips_orchestrator.py`
- `test_orchestrator_settings_widgets_have_tooltips` (func, строка 49)

### `tests/test_universal_developer.py`
- `isolated_db` (func, строка 27)
- `empty_ws` (func, строка 45)
- `code_ws` (func, строка 57)
- `test_set_workspace_creates_and_repoints` (func, строка 75)
- `test_set_workspace_empty_path_rejected` (func, строка 87)
- `test_external_workspace_has_no_protected_files` (func, строка 92)
- `test_set_target_file_activates_single_file_mode` (func, строка 100)
- `test_set_target_file_narrows_scan_to_one_file` (func, строка 111)
- `test_set_target_file_assess_returns_single_file_state` (func, строка 122)
- `test_set_target_file_current_workspace_reports_mode` (func, строка 132)
- `test_set_target_file_rejects_directory` (func, строка 142)
- `test_set_target_file_rejects_missing_file` (func, строка 148)
- `test_set_workspace_clears_single_file_mode` (func, строка 154)
- `test_single_file_project_map_narrows` (func, строка 165)
- `test_dispatch_set_target_file` (func, строка 175)
- `test_assess_empty` (func, строка 186)
- `test_assess_software_without_docs` (func, строка 193)
- `test_assess_software_with_docs` (func, строка 201)
- `test_build_project_map_detects_symbols_and_deps` (func, строка 212)
- `test_render_project_map_markdown_uses_responsibilities` (func, строка 225)
- `test_write_and_read_docs` (func, строка 237)
- `test_write_doc_unknown_kind_errors` (func, строка 247)
- `test_write_project_map_tool_writes_file` (func, строка 253)
- `test_snapshot_and_restore_all` (func, строка 264)
- `test_restore_all_missing_snapshot` (func, строка 283)
- `test_dispatch_routes_core_tools` (func, строка 291)
- `test_dispatch_routes_workspace_tools` (func, строка 299)
- `test_dispatch_unknown_tool_errors` (func, строка 306)
- `test_dispatch_workspace_tool_rejects_unknown_args` (func, строка 312)
- `test_dispatch_workspace_tool_known_args_still_ok` (func, строка 325)
- `test_dispatch_json_compatible` (func, строка 332)
- `test_set_workspace_via_dispatch_recreates_core` (func, строка 339)
- `test_set_workspace_preserves_history_cache` (func, строка 351)
- `test_system_prompt_combines_core_and_universal` (func, строка 380)
- `test_system_prompt_documents_list_files_max_depth` (func, строка 391)
- `test_system_prompt_documents_listing_scenarios` (func, строка 399)
- `test_catalog_docs_list_files_max_depth` (func, строка 409)
- `test_run_code_missing_path_returns_structured_error` (func, строка 418)
- `test_run_test_missing_path_returns_structured_error` (func, строка 426)
- `test_read_file_window_reports_remaining_and_hint` (func, строка 434)

### `tests/test_usability_fixes.py`
- `ui_env` (func, строка 20)
- `_rerender` (func, строка 32)
- `_button_keys` (func, строка 39)
- `_captions` (func, строка 43)
- `test_assistant_delete_requires_confirmation` (func, строка 49)
- `test_skills_library_delete_requires_confirmation` (func, строка 91)
- `test_orchestrator_delete_requires_confirmation` (func, строка 126)
- `_render_orch_chat_with_employee` (func, строка 162)
- `test_orchestrator_caption_uses_employee_description` (func, строка 176)
- `test_orchestrator_caption_falls_back_to_name` (func, строка 190)
- `_models_settings_warn_env` (func, строка 206)
- `_svc` (func, строка 221)
- `test_orch_models_settings_warns_unavailable_service` (func, строка 232)
- `test_orch_models_settings_warns_unavailable_model` (func, строка 249)
- `test_orch_models_settings_no_warning_when_all_available` (func, строка 265)
- `test_assistant_chat_warns_unavailable_saved_provider` (func, строка 281)
- `test_assistant_chat_warns_unavailable_saved_model` (func, строка 324)
- `_sidebar_app` (func, строка 370)
- `test_sidebar_assistant_click_resets_search` (func, строка 398)
- `test_sidebar_settings_providers_label_updated` (func, строка 422)
- `test_assistant_form_switch_clears_stale_prompt_keys` (func, строка 449)
- `_orch_chat_env` (func, строка 521)
- `test_orch_chat_renders_datetime_captions` (func, строка 565)
- `test_orch_chat_omits_caption_without_ts` (func, строка 580)
- `test_orchestrator_strips_empty_json_fences` (func, строка 591)
- `_assistant_chat_env` (func, строка 617)
- `test_assistant_chat_renders_datetime_captions` (func, строка 660)
- `test_assistant_chat_omits_caption_without_ts` (func, строка 676)
- `_FakeUpload` (class, строка 691)
- `_orch_upload_env` (func, строка 698)
- `test_orch_upload_attaches_file_and_bumps_uploader_key` (func, строка 722)
- `test_orch_upload_second_render_settles_without_refire` (func, строка 737)
- `test_orch_upload_too_large_shows_error_without_rerun_loop` (func, строка 761)
- `test_orch_upload_duplicate_is_ignored_without_rerun_loop` (func, строка 783)

### `tests/test_web_search_prompt.py`
- `test_default_prompt_is_stable` (func, строка 39)
- `test_get_web_search_prompt_returns_default_when_orchestrator_missing` (func, строка 44)
- `test_get_web_search_prompt_returns_default_when_key_missing` (func, строка 49)
- `test_get_web_search_prompt_returns_default_when_key_empty` (func, строка 55)
- `test_get_web_search_prompt_uses_custom_value` (func, строка 61)
- `test_get_web_search_config_includes_prompt` (func, строка 69)
- `test_get_web_search_config_uses_default_prompt_when_missing` (func, строка 88)
- `_make_executor` (func, строка 98)
- `_patch_services` (func, строка 107)
- `test_web_search_sends_orchestrator_prompt_with_instructions` (func, строка 115)
- `test_web_search_falls_back_to_global_config` (func, строка 159)
- `test_web_search_without_instructions_uses_base_prompt_only` (func, строка 184)
- `test_web_search_blocked_when_disabled` (func, строка 204)
- `test_web_search_returns_error_when_not_configured` (func, строка 212)
- `test_web_search_yandex_forces_tool_choice` (func, строка 229)
- `test_web_search_deepseek_not_forced_and_gets_one_search_rule` (func, строка 250)
- `test_web_search_yandex_retries_once_without_tool_choice_on_empty` (func, строка 272)
- `test_web_search_both_empty_returns_explicit_error` (func, строка 296)
- `test_catalog_web_search_mentions_instructions` (func, строка 319)

### `tests/test_yandex_responses.py`
- `_yandex_cfg` (func, строка 30)
- `_responses_payload` (func, строка 36)
- `test_yandex_responses_request_payload` (func, строка 53)
- `test_yandex_responses_request_without_tools_and_system` (func, строка 98)
- `test_yandex_tool_choice_in_payload` (func, строка 127)
- `test_yandex_tool_choice_omitted_by_default` (func, строка 145)
- `test_yandex_uses_responses_usage_fields` (func, строка 164)
- `test_yandex_reports_cached_tokens` (func, строка 189)
- `test_yandex_cached_tokens_zero_when_absent` (func, строка 209)
- `test_yandex_reasoning_effort_in_payload` (func, строка 229)
- `test_yandex_reasoning_effort_invalid_omitted` (func, строка 246)
- `test_yandex_web_search_filters_and_context_size` (func, строка 265)
- `test_yandex_web_search_defaults_when_unset` (func, строка 288)
- `test_send_request_yandex_always_uses_responses` (func, строка 308)
- `test_send_request_yandex_forward_tool_choice` (func, строка 333)
- `test_send_request_yandex_auto_forces_web_search_tool_choice` (func, строка 358)
- `test_send_request_yandex_auto_tool_choice_with_dict_tool` (func, строка 389)
- `test_send_request_yandex_explicit_tool_choice_untouched` (func, строка 413)
- `test_send_request_yandex_no_web_search_no_forced_tool_choice` (func, строка 438)
- `test_test_connection_yandex_uses_responses` (func, строка 463)
- `test_test_connection_yandex_missing_credentials` (func, строка 485)
- `test_test_connection_yandex_missing_folder` (func, строка 497)
- `test_extract_responses_text_prefers_output_text` (func, строка 512)
- `test_extract_responses_text_falls_back_to_output` (func, строка 524)
- `test_extract_responses_text_content_as_string` (func, строка 536)
- `test_extract_responses_text_ignores_reasoning_and_web_search_call` (func, строка 543)
- `test_extract_responses_text_skips_blank_blocks` (func, строка 558)
- `test_extract_responses_text_function_call_fenced` (func, строка 573)
- `test_extract_responses_text_incomplete_status_still_returns_text` (func, строка 586)
- `test_extract_responses_text_empty_output` (func, строка 597)
- `test_extract_deepseek_wraps_unified_extractor` (func, строка 604)

### `tests/smoke/test_app_smoke.py`
- `isolated_data` (func, строка 24)
- `_fresh_ui` (func, строка 33)
- `test_main_renders_without_error` (func, строка 41)
- `test_every_page_renders` (func, строка 55)
- `test_create_skill_button_fires` (func, строка 89)
- `test_new_query_button_resets_thread` (func, строка 112)
- `test_nav_button_changes_page` (func, строка 133)
- `test_no_duplicate_widget_keys` (func, строка 151)
- `test_nav_order_stats_before_about_and_lang_theme_after` (func, строка 300)

### `tests/scenarios/test_assistant_sidebar_scenarios.py`
- `isolated_data` (func, строка 28)
- `_set_column` (func, строка 36)
- `_make_assistant` (func, строка 46)
- `_make_thread` (func, строка 60)
- `_nav` (func, строка 69)
- `_ids` (func, строка 73)
- `_fresh_app_mod` (func, строка 77)
- `test_scenario_restart_keeps_five_most_active` (func, строка 90)
- `test_scenario_fresh_assistant_appears_first` (func, строка 132)
- `test_scenario_more_than_five_assistants` (func, строка 158)
- `test_scenario_order_without_dialogues` (func, строка 186)

### `tests/scenarios/test_connectors_scenarios.py`
- `isolated_data_dir` (func, строка 29)
- `_github_connection` (func, строка 61)
- `test_scenario_connection_lifecycle` (func, строка 67)
- `test_scenario_token_never_leaks_to_public_views` (func, строка 117)
- `test_scenario_validation_rejects_bad_input` (func, строка 136)
- `test_scenario_orchestrator_binding` (func, строка 148)
- `test_scenario_github_tools_return_clean_dicts` (func, строка 180)
- `test_scenario_github_tool_available_through_dispatcher` (func, строка 233)

### `tests/scenarios/test_first_run_flow.py`
- `isolated_data` (func, строка 64)
- `_render` (func, строка 72)
- `_help_by_key` (func, строка 80)
- `_all_strings` (func, строка 89)
- `test_first_run_welcome_walkthrough` (func, строка 104)
- `test_settings_explain_keys_and_save_them` (func, строка 140)
- `test_create_first_assistant_with_real_storage` (func, строка 203)
- `test_skills_library_install_forms_and_neutral_placeholder` (func, строка 272)

### `tests/scenarios/test_json_repair_scenarios.py`
- `_make_skill` (func, строка 24)
- `_scripted_send` (func, строка 33)
- `RecordingDispatcher` (class, строка 45)
- `test_scenario_happy_path_truncated_call_is_repaired_and_executed` (func, строка 59)
- `test_scenario_edge_case_truncation_inside_closed_fence` (func, строка 81)
- `test_scenario_error_state_unrepairable_truncation_stops_loop` (func, строка 103)

### `tests/scenarios/test_orchestrator_chat_prefs_scenario.py`
- `page_env` (func, строка 31)
- `_render` (func, строка 43)
- `_markdown_calls` (func, строка 77)
- `_checkbox` (func, строка 81)
- `test_scenario_devagent_welcome_and_defaults` (func, строка 88)
- `test_scenario_saved_prefs_restored_in_new_dialog` (func, строка 117)
- `test_scenario_toggling_checkbox_persists` (func, строка 147)
- `test_scenario_single_toolbar_after_toggle` (func, строка 196)
- `test_scenario_safety_mode_off_persists` (func, строка 231)

### `tests/scenarios/test_orchestrator_devagent_scenarios.py`
- `isolated_data_dir` (func, строка 40)
- `test_scenario_full_employee_lifecycle_via_devagent_dispatch` (func, строка 77)
- `test_scenario_boundary_failures_are_rejected_cleanly` (func, строка 149)
- `test_scenario_import_validation_gate` (func, строка 182)
- `test_scenario_folder_is_source_of_truth` (func, строка 209)

### `tests/scenarios/test_rag_assistant_dialog.py`
- `_function_call` (func, строка 22)
- `_final_message` (func, строка 31)
- `_mock_responses` (func, строка 39)
- `isolated_data` (func, строка 60)
- `_load_default_assistant` (func, строка 68)
- `_send_request` (func, строка 97)
- `test_rag_assistant_dialog_happy_path` (func, строка 118)
- `test_rag_assistant_without_bases_has_no_function_tool` (func, строка 162)
- `test_rag_assistant_manifest_web_search_overrides_in_payload` (func, строка 188)
- `test_rag_assistant_rejects_unassigned_base` (func, строка 241)

### `tests/scenarios/test_search_in_files_scenarios.py`
- `project` (func, строка 23)
- `test_scenario_happy_path_search_across_project` (func, строка 45)
- `test_scenario_edge_case_explicit_extensions_find_csv` (func, строка 58)
- `test_scenario_error_state_missing_subdir` (func, строка 70)

### `tests/scenarios/test_skills_adaptation_scenario.py`
- `isolated_data_dir` (func, строка 29)
- `_import_external_skill` (func, строка 55)
- `_make_devagent_dispatcher` (func, строка 67)
- `test_external_skill_hidden_from_prompt_until_adapted` (func, строка 84)
- `test_adapt_button_hands_off_to_devagent` (func, строка 115)
- `test_mark_adapted_reveals_skill_in_prompt` (func, строка 170)

### `tests/scenarios/test_stats_scenario.py`
- `stats_data` (func, строка 34)
- `test_stats_page_aggregates_real_seeded_usage` (func, строка 122)
- `test_stats_page_empty_database_renders_info_without_metrics` (func, строка 199)

### `tests/scenarios/test_structured_output_scenarios.py`
- `sandbox` (func, строка 44)
- `_wire_flow` (func, строка 56)
- `test_structured_output_full_creation_flow` (func, строка 96)
- `test_fenced_json_fallback_still_creates_assistant` (func, строка 137)
- `test_prose_creator_response_reports_parse_error` (func, строка 168)

### `tests/scenarios/test_theme_switch_scenario.py`
- `_drop_ui_modules` (func, строка 24)
- `_fresh_app` (func, строка 30)
- `test_theme_switch_returns_to_assistant_chat` (func, строка 44)
- `test_theme_switch_returns_to_orchestrator_dialog` (func, строка 81)
- `test_stale_restore_marker_does_not_resurrect_dialog` (func, строка 112)

### `tests/scenarios/test_welcome_page_scenarios.py`
- `isolated_data` (func, строка 35)
- `_render` (func, строка 43)
- `test_welcome_step_button_navigates` (func, строка 62)
- `test_devagent_settings_full_render_without_api_key_warning` (func, строка 90)

### `storage/db.py`
- `_db_url` (func, строка 50)
- `_ensure_thread_columns` (func, строка 55)
- `_ensure_assistant_columns` (func, строка 76)
- `_migrate_thread_skill_columns` (func, строка 97)
- `_migrate_assistant_table` (func, строка 121)
- `get_engine` (func, строка 137)
- `get_devagent_engine` (func, строка 164)
- `_migrate_threads_table_if_needed` (func, строка 186)
- `get_session` (func, строка 202)
- `get_devagent_session` (func, строка 210)
- `reset_engine` (func, строка 218)
- `reset_devagent_engine` (func, строка 228)

### `storage/models.py`
- `Assistant` (class, строка 16)
- `Thread` (class, строка 63)
- `Message` (class, строка 98)
- `ConfigKV` (class, строка 123)
- `Instruction` (class, строка 138)
- `Orchestrator` (class, строка 164)
- `OrchestratorInstruction` (class, строка 222)

### `storage/repository.py`
- `repo_load_assistants` (func, строка 13)
- `repo_get_assistant` (func, строка 19)
- `repo_get_assistant_by_slug` (func, строка 26)
- `repo_get_assistant_with_text` (func, строка 35)
- `repo_create_assistant` (func, строка 46)
- `repo_update_assistant` (func, строка 73)
- `repo_set_assistant_slug` (func, строка 106)
- `repo_delete_assistant` (func, строка 121)
- `repo_load_assistant_prompt_text` (func, строка 134)
- `repo_save_assistant_prompt_text` (func, строка 141)
- `repo_list_instructions` (func, строка 158)
- `repo_get_instruction` (func, строка 164)
- `repo_get_instruction_with_text` (func, строка 171)
- `repo_create_instruction` (func, строка 182)
- `repo_update_instruction` (func, строка 199)
- `repo_delete_instruction` (func, строка 217)
- `repo_get_instruction_prompt_text` (func, строка 230)
- `repo_save_instruction_prompt_text` (func, строка 237)
- `repo_list_orchestrator_instructions` (func, строка 254)
- `repo_get_orchestrator_instruction` (func, строка 265)
- `repo_save_orchestrator_instruction` (func, строка 279)
- `repo_delete_orchestrator_instruction` (func, строка 312)
- `repo_delete_all_orchestrator_instructions` (func, строка 332)
- `repo_create_thread` (func, строка 347)
- `repo_load_thread_meta` (func, строка 366)
- `repo_save_thread_meta` (func, строка 373)
- `repo_load_thread_messages` (func, строка 391)
- `repo_save_thread_messages` (func, строка 403)
- `repo_append_message` (func, строка 424)
- `repo_list_all_threads` (func, строка 447)
- `repo_list_threads_by_type` (func, строка 454)
- `repo_list_chat_threads` (func, строка 466)
- `repo_delete_thread` (func, строка 475)
- `repo_delete_all_threads` (func, строка 488)
- `repo_load_config` (func, строка 502)
- `repo_save_config` (func, строка 515)
- `repo_list_orchestrators` (func, строка 531)
- `repo_get_orchestrator_by_slug` (func, строка 541)
- `repo_get_orchestrator_by_id` (func, строка 548)
- `repo_get_orchestrator_with_text` (func, строка 555)

### `storage/repository_devagent.py`
- `repo_devagent_create_thread` (func, строка 23)
- `repo_devagent_load_thread_meta` (func, строка 56)
- `repo_devagent_save_thread_meta` (func, строка 63)
- `repo_devagent_load_thread_messages` (func, строка 85)
- `repo_devagent_save_thread_messages` (func, строка 97)
- `repo_devagent_append_message` (func, строка 118)
- `repo_devagent_list_threads` (func, строка 142)
- `repo_devagent_delete_thread` (func, строка 159)
- `repo_devagent_delete_all_threads` (func, строка 172)

### `defaults/skills/rag_base_creator/scripts/build_base.py`
- `_fallback_vector` (func, строка 91)
- `_check_deps` (func, строка 99)
- `_collect_files` (func, строка 114)
- `_add_file` (func, строка 147)
- `_read_text` (func, строка 162)
- `_product_of` (func, строка 174)
- `_headings` (func, строка 183)
- `_trail_at` (func, строка 191)
- `_split_units` (func, строка 210)
- `_group_units` (func, строка 226)
- `build_chunks` (func, строка 261)
- `_embed_chunk` (func, строка 301)
- `run` (func, строка 309)
- `main` (func, строка 405)

### `skills/Github_Clean_Copy/tests/test_clean_copy_skill.py`
- `_run_script` (func, строка 12)
- `runtime_source` (func, строка 21)
- `test_derive_clean_copy_dest_uses_version_and_strips_number` (func, строка 60)
- `test_e2e_copy_is_full_clean_and_gitignored` (func, строка 69)
- `test_audit_does_not_scan_runtime_dirs` (func, строка 109)

### `skills/Github_Clean_Copy/scripts/make_clean_copy.py`
- `_load_module_strings` (func, строка 118)
- `_read_version` (func, строка 133)
- `_safe_component` (func, строка 142)
- `_base_name` (func, строка 147)
- `derive_clean_copy_dest` (func, строка 163)
- `looks_placeholder` (func, строка 220)
- `classify_hit` (func, строка 225)
- `scan_text` (func, строка 259)
- `scan_sqlite_file` (func, строка 280)
- `build_plan` (func, строка 315)
- `do_copy` (func, строка 388)
- `verify_copy` (func, строка 407)
- `_sha256` (func, строка 506)
- `cleanup_copy` (func, строка 516)
- `parse_args` (func, строка 554)
- `main` (func, строка 614)

### `dev_agent/agent_loop.py`
- `_now_ts` (func, строка 152)
- `_parse_loop_status` (func, строка 183)
- `_parse_requires_user_response` (func, строка 192)
- `_prose_contains_progress` (func, строка 210)
- `_prose_looks_like_question` (func, строка 218)
- `_looks_like_confirmation_request` (func, строка 237)
- `_prose_looks_weak` (func, строка 245)
- `_looks_like_plan` (func, строка 264)
- `normalize_hyphens` (func, строка 302)
- `classify_step_strength` (func, строка 306)
- `_summarise_result` (func, строка 326)
- `_extract_balanced_json_objects` (func, строка 361)
- `_unbalanced_json_details` (func, строка 391)
- `_unclosed_summary` (func, строка 432)
- `_repair_unclosed_braces` (func, строка 447)
- `_escape_raw_newlines_in_strings` (func, строка 471)
- `_json_loads_lenient` (func, строка 506)
- `_truncated_tool_json_segments` (func, строка 520)
- `_unparsed_tool_json_blocks` (func, строка 545)
- `_json_parse_cause` (func, строка 568)
- `_unparsed_tool_json_diagnostics` (func, строка 591)
- `_unparsed_block_signature` (func, строка 641)
- `_normalize_call` (func, строка 658)
- `_call_signature` (func, строка 700)
- `_coerce_dsml_param` (func, строка 709)
- `_extract_dsml_calls` (func, строка 751)
- `_dsml_required_args` (func, строка 794)
- `_dsml_json_hint` (func, строка 837)
- `_dsml_validation_error` (func, строка 847)
- `_fallback_parse_propose_file` (func, строка 871)
- `_repair_unclosed_tool_json` (func, строка 905)
- `parse_tool_calls` (func, строка 943)
- `AgentResult` (class, строка 970)
- `_maybe_task_state_context` (func, строка 989)
- `_with_task_state` (func, строка 998)
- `_maybe_thread_context` (func, строка 1013)
- `_with_thread_context` (func, строка 1037)
- `_make_short_summary` (func, строка 1059)
- `_classify_message` (func, строка 1087)
- `_index_message` (func, строка 1112)

### `dev_agent/assistant_detector.py`
- `list_all_assistants_for_detection` (func, строка 49)
- `detect_and_select_assistant` (func, строка 60)

### `dev_agent/assistant_model_resolver.py`
- `classify_assistant_requirements` (func, строка 59)
- `_validate_classification` (func, строка 116)
- `_get_available_services_with_keys` (func, строка 129)
- `_get_first_model` (func, строка 140)
- `_find_model_in_service` (func, строка 148)
- `_parse_explicit_service_model` (func, строка 157)
- `_service_supports_web_search` (func, строка 179)
- `_pick_web_search_service` (func, строка 189)
- `resolve_service_model_for_assistant` (func, строка 233)
- `_resolve_reasoning_effort` (func, строка 381)

### `dev_agent/backup_manager.py`
- `_safe_relpath_key` (func, строка 22)
- `_sha256` (func, строка 31)
- `BackupEntry` (class, строка 36)
- `BackupManager` (class, строка 45)

### `dev_agent/config.py`
- `_resolve_project_root` (func, строка 43)
- `set_target_root` (func, строка 133)
- `ensure_runtime_dirs` (func, строка 174)
- `to_project_relative` (func, строка 180)
- `is_protected` (func, строка 199)
- `resolve_in_project` (func, строка 209)

### `dev_agent/llm_utils.py`
- `_prefers_assistant_dict` (func, строка 32)
- `_use_system_keyword` (func, строка 52)
- `call_llm_with_system` (func, строка 61)

### `dev_agent/safe_writer.py`
- `ProtectedFileError` (class, строка 30)
- `DraftResult` (class, строка 35)
- `ApplyResult` (class, строка 45)
- `SafeWriter` (class, строка 54)
- `_safe_rel` (func, строка 237)
- `render_diff` (func, строка 245)

### `dev_agent/task_state.py`
- `TaskStateError` (class, строка 86)
- `current_thread_id` (func, строка 92)
- `task_state_path` (func, строка 105)
- `_now_iso` (func, строка 110)
- `_backup_if_exists` (func, строка 115)
- `_split_top_sections` (func, строка 131)
- `_split_active_sections` (func, строка 153)
- `_parse_legacy` (func, строка 180)
- `_read_active_meta` (func, строка 202)
- `_parse_history_entries` (func, строка 214)
- `_parse_step` (func, строка 245)
- `extract_step_ids` (func, строка 262)
- `render_active_task` (func, строка 275)
- `render_task_history` (func, строка 297)
- `build_task_state` (func, строка 321)
- `_has_active_content` (func, строка 354)
- `_summarize_active` (func, строка 362)
- `_archive_active_task` (func, строка 380)
- `_write_raw` (func, строка 400)
- `_write_journal` (func, строка 409)
- `_migrate_legacy_file` (func, строка 428)
- `ensure_task_state_file` (func, строка 461)
- `read_task_state` (func, строка 498)
- `archive_and_start_task` (func, строка 533)
- `update_task_state_section` (func, строка 578)
- `_set_meta_line` (func, строка 616)
- `update_plan_step_status` (func, строка 632)
- `clear_task_state` (func, строка 747)
- `task_state_for_context` (func, строка 778)

### `dev_agent/tool_executor.py`
- `_strip_line_numbers` (func, строка 79)
- `_normalize_line_endings` (func, строка 99)
- `_ws_tolerant_pattern` (func, строка 104)
- `_norm_to_orig_map` (func, строка 130)
- `_fuzzy_find_matches` (func, строка 144)
- `_exact_spans` (func, строка 160)
- `_line_of` (func, строка 173)
- `_suggest_anchor_lines` (func, строка 178)
- `_unknown_args` (func, строка 242)
- `_usage_string` (func, строка 267)
- `_coerce_numeric_args` (func, строка 292)
- `_validate_python_syntax` (func, строка 346)
- `ToolExecutor` (class, строка 369)

### `dev_agent/universal_agent.py`
- `load_system_prompt` (func, строка 29)
- `_workspace_usage` (func, строка 135)
- `build_assistant_dict_from_config` (func, строка 147)
- `UniversalDevAgent` (class, строка 194)

### `dev_agent/workspace_tools.py`
- `detect_language` (func, строка 66)
- `set_workspace` (func, строка 72)
- `set_target_file` (func, строка 108)
- `current_workspace` (func, строка 139)
- `current_install` (func, строка 154)
- `list_recent_workspaces` (func, строка 169)
- `_iter_project_files` (func, строка 199)
- `_coerce_nonneg_int` (func, строка 247)
- `search_in_files` (func, строка 255)
- `scan_folder` (func, строка 449)
- `assess_workspace` (func, строка 491)
- `_python_symbols` (func, строка 526)
- `_python_imports` (func, строка 539)
- `build_project_map` (func, строка 553)
- `render_project_map_markdown` (func, строка 598)
- `default_spec_markdown` (func, строка 646)
- `default_architecture_markdown` (func, строка 659)
- `default_readme_markdown` (func, строка 671)
- `_backup_before_overwrite` (func, строка 689)
- `write_project_map` (func, строка 703)
- `write_doc` (func, строка 718)
- `read_doc` (func, строка 739)
- `_snapshot_dir` (func, строка 761)
- `SnapshotInfo` (class, строка 766)
- `snapshot_all` (func, строка 773)
- `list_snapshots` (func, строка 806)
- `restore_all` (func, строка 825)
