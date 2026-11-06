"""Catalog and argument specs of the workspace tools."""
from __future__ import annotations

from typing import Dict, List

WORKSPACE_TOOL_CATALOG: List[Dict[str, str]] = [
    {"name": "set_workspace", "desc": "Select the target work folder. Args: path."},
    {"name": "set_target_file", "desc": "Activate single-file mode. Workspace = parent dir, all operations scoped to this file. Args: file_path."},
    {"name": "current_workspace", "desc": "Return the active workspace root, single-file mode flag, and target_file if any. No args. CALL IT FIRST, before any file or folder work (read_file, list_files, search_in_files, scan_folder, build_project_map, read_doc, propose_file, apply_patch, write_*). If the reply carries workspace_selected: false, this dialog has no folder yet: file tools are blocked with workspace_not_selected - do NOT retry them. Then FOLLOW THE WORKSPACE-SELECTION LOGIC OF YOUR SYSTEM PROMPT FIRST: it defines how this employee picks a folder (project rules, its own data folder, a new-project flow). Only when the prompt gives no rule or the choice stays unclear: pick the folder the user's request clearly points to when it names a project, otherwise call list_recent_workspaces() and offer the result to the user as a numbered menu, then call set_workspace(path). Continue the file work only after the folder is set."},
    {"name": "current_install", "desc": "Return the SagaAI install root (where the dev_agent package lives), its apps/ folder path, and whether the workspace is the install. Use for platform-level paths, e.g. creating a new project under <install>/apps/<name>. No args."},
    {"name": "scan_folder", "desc": "Inspect the workspace (files, languages, docs present). In single-file mode returns only the target file. No args."},
    {"name": "search_in_files", "desc": "Search text files in the workspace for a string or regex. Args: query, [files], [path], [subdir], [extensions], [regex], [case_sensitive], [max_results], [context_before], [context_after]. files is a list of relative file paths to scan (only those files, extension filtering ignored). path narrows the scan to one file or acts as the base directory."},
    {"name": "assess_workspace", "desc": "Classify workspace: empty | software_without_docs | software_with_docs | single_file. No args."},
    {"name": "build_project_map", "desc": "Build the deterministic structural map (files, symbols, deps). In single-file mode maps only the target file. No args."},
    {"name": "write_project_map", "desc": "Write PROJECT_MAP.md. Args: responsibilities (map path->one-line role)."},
    {"name": "write_doc", "desc": "Create/overwrite a project markdown doc. Args: doc ('agent'|'architecture'|'readme'), content. 'spec' is a legacy alias of 'agent'."},
    {"name": "read_doc", "desc": "Read a managed doc. Args: doc ('map'|'agent'|'architecture'|'changelog'|'readme'); 'spec' is a legacy alias of 'agent'."},
    {"name": "snapshot_all", "desc": "Full-system backup of every project file. In single-file mode backs up only the target file. Args: [note]."},
    {"name": "list_snapshots", "desc": "List full-system snapshots. No args."},
    {"name": "restore_all", "desc": "Restore the whole system from a snapshot. Args: snapshot_id (str)."},
    {"name": "list_thread_files", "desc": "List dialog uploads of this thread (history/<tid>/files). No args; uses the active thread. Reports names, absolute paths, sizes, text/binary kind and extractable (whether read_thread_file can return text)."},
    {"name": "read_thread_file", "desc": "Read a dialog upload as TEXT with an optional line window. Args: file_name, [offset], [limit]. Plain text and documents (DOCX, PDF, PPTX, XLSX, RTF, ODT, HTML) are returned as text; an image or archive is reported as is_text=false with a hint."},
    {"name": "read_document", "desc": "Extract text from a document file on disk by ABSOLUTE path and return a line window. Args: path, [offset], [limit], [max_chars]. Supports PDF, DOCX, DOC, PPTX, XLSX, XLS, RTF, ODT, HTML and plain text. Use it instead of run_code for documents; use read_file for source code and other project text files."},
    {"name": "extract_document_images", "desc": "Extract the pictures embedded in a PDF/DOCX/PPTX/XLSX document and save them as image files, so they can be read with analyze_image. Args: [path] or [file_name], [output_dir], [max_images], [min_px], [ocr], [pages], [dpi]. path is a document in the workspace or an absolute path; file_name is a dialog upload. ocr='off' (default) extracts stored pictures only; 'auto' also renders the pages of a PDF that has no text layer (a scan), 'force' renders the pages of any PDF - each rendered page costs one vision call. Returns paths plus ready-made batches of at most 5 images (the analyze_image limit) and merges them with a prompt tuned for transcription. Identification needs a vision model: without one the files are still saved and recognition_available is false."},
    # ── Orchestrator management tools ──────────────────────────────────────
    {"name": "list_orchestrators", "desc": "List all orchestrators (slug, name, description). No args."},
    {"name": "get_orchestrator", "desc": "Return a single orchestrator including prompt_text. Args: slug."},
    {"name": "create_orchestrator", "desc": "Create a new orchestrator. Args: slug, name, [description], [prompt_text], [config], [tools], [max_steps], [auto_apply], [memory_dir] (working-data folder, stored in config)."},
    {"name": "update_orchestrator", "desc": "Update an existing orchestrator. Args: slug, [name], [description], [prompt_text], [config], [tools], [max_steps], [auto_apply], [memory_dir] (working-data folder, stored in config)."},
    {"name": "delete_orchestrator", "desc": "Delete a custom orchestrator and its folder. Args: slug."},
    {"name": "reload_orchestrator", "desc": "Reload an orchestrator from its personal folder into the DB (applies hand-made edits to orchestrator.json / system_prompt.md). Args: slug."},
    {"name": "list_orchestrator_functions", "desc": "List custom Python functions of an orchestrator. Args: slug."},
    {"name": "get_orchestrator_function", "desc": "Return the source code of a custom function. Args: slug, name."},
    {"name": "save_orchestrator_function", "desc": "Create or overwrite a custom Python function for an orchestrator. The code must define invoke(**kwargs) -> dict. Args: slug, name, code."},
    {"name": "delete_orchestrator_function", "desc": "Delete a custom function by name. Args: slug, name."},
    {"name": "list_orchestrator_instructions", "desc": "List orchestrator-specific instructions. Args: slug."},
    {"name": "get_orchestrator_instruction", "desc": "Return a full orchestrator instruction including prompt_text. Args: slug, instruction_id."},
    {"name": "save_orchestrator_instruction", "desc": "Create or update an orchestrator-specific instruction. Args: slug, [instruction_id], name, [description], [prompt_text]."},
    {"name": "delete_orchestrator_instruction", "desc": "Delete an orchestrator-specific instruction. Args: slug, instruction_id."},
    {"name": "search_in_threads", "desc": "Search the message history of dialog threads. Args: query, [thread_id], [scope], [orchestrator], [date_from], [date_to], [date_field], [role], [regex], [include_tool_results], [max_results]. Targets: an explicit thread_id; by default the CURRENT dialog thread (no listing needed); scope='orchestrator' searches the given orchestrator slug(s) or the active orchestrator, scope='all' - every accessible dialog. date_from/date_to (YYYY-MM-DD or ISO) filter by dialog activity date (date_field='updated'|'created'). Returns hits with thread_id, title, orchestrator, message_index, role, ts and a snippet around the match."},
    {"name": "list_threads", "desc": "List dialog threads (newest first by default) with message counts. Args: [orchestrator], [date_from], [date_to], [date_field], [limit], [offset], [order]. Use it to discover dialog ids, then read_thread to view one. Non-DevAgent orchestrators see only their own dialogs."},
    {"name": "read_thread", "desc": "Read a window of one dialog's messages. Args: [thread_id], [offset], [limit], [include_tool_results]. Without thread_id reads the CURRENT dialog; each message carries its absolute index (pair it with search_in_threads hits to view around a match). Returns total/remaining/has_more for pagination."},
    # ── Connector management tools ────────────────────────────────────────
    {"name": "list_connections", "desc": "List all external service connections (public view, no secrets). No args."},
    {"name": "get_connection", "desc": "Return one connection's public manifest (no secrets). Args: conn_id."},
    {"name": "list_connector_services", "desc": "List known connector services (id, name, config/secret fields). No args."},
    {"name": "list_connection_presets", "desc": "List bundled connection presets (bundled MCP servers and other templates) that can be instantiated as drafts. Args: [service]."},
    {"name": "create_connection", "desc": "Create an external service connection. Args: service, name, [token], [account], [config], [secrets], [pending], [preset]. preset creates a draft connection from a bundled preset (no credentials); with pending=true a draft is saved without credentials (the user fills them on the Connectors page)."},
    {"name": "update_connection", "desc": "Update a connection (name/account/config/token/secrets); filling credentials activates a pending draft. Args: conn_id, [name], [account], [token], [config], [secrets]."},
    {"name": "delete_connection", "desc": "Delete a connection. Args: conn_id."},
    {"name": "test_connection", "desc": "Validate a connection against its service and refresh account info. Args: conn_id."},
    {"name": "get_enabled_connections", "desc": "List connection ids enabled for an orchestrator (default: the active one). Args: [orchestrator]."},
    {"name": "set_enabled_connections", "desc": "Replace the enabled connection ids of an orchestrator (default: the active one). Args: connection_ids, [orchestrator]."},
    {"name": "get_connection_memory", "desc": "Read the agent-maintained memory notes of a connection ('' when absent). Args: conn_id."},
    {"name": "set_connection_memory", "desc": "Replace the memory notes of a connection (64 KB cap; never store secrets). Args: conn_id, content."},
    {"name": "get_personal_memory", "desc": "Read the personal memory of one scope - notes about the owner, an employee, a working folder or a connection. Returns content, bytes, updated_at. Args: [scope], [max_chars]. scope: 'current' or omitted (this employee's own notes, default), 'user' (the owner profile shared by every employee), 'orchestrator:<slug>', 'project:<path>', 'connector:<id>'."},
    {"name": "set_personal_memory", "desc": "Write the personal memory of one scope (64 KB cap; credentials are refused). Args: content, [scope], [mode], [note], [overwrite_owner_profile]. mode='replace' (default) or 'append'. scope: 'current' or omitted (own notes, default), 'user' (owner profile), 'project', 'orchestrator:<slug>'. Extend the owner profile with mode='append'; a full rewrite of scope='user' is refused unless overwrite_owner_profile=true and the owner asked for it. Report every write in one line."},
    {"name": "list_personal_memory", "desc": "List the personal memory scopes that hold notes, with size and update time. No args."},
]

# Keyword-argument allow-lists for the catalog tools above. Used by
# UniversalDevAgent.dispatch to reject unknown argument names with a
# structured error BEFORE the call, so the LLM gets ``unknown_args`` +
# ``suggestion`` instead of "anything goes" **kwargs swallowing.
WORKSPACE_TOOL_ARGS: Dict[str, Dict[str, set]] = {
    "set_workspace": {"required": {"path"}, "optional": set()},
    "set_target_file": {"required": {"file_path"}, "optional": set()},
    "current_workspace": {"required": set(), "optional": set()},
    "current_install": {"required": set(), "optional": set()},
    "scan_folder": {"required": set(), "optional": set()},
    "search_in_files": {
        "required": {"query"},
        "optional": {"files", "path", "subdir", "extensions", "regex",
                     "case_sensitive", "max_results", "context_before", "context_after"},
    },
    "assess_workspace": {"required": set(), "optional": set()},
    "build_project_map": {"required": set(), "optional": set()},
    "write_project_map": {"required": {"responsibilities"}, "optional": set()},
    "write_doc": {"required": {"doc"}, "optional": {"content"}},
    "read_doc": {"required": {"doc"}, "optional": set()},
    "snapshot_all": {"required": set(), "optional": {"note"}},
    "list_snapshots": {"required": set(), "optional": set()},
    "restore_all": {"required": {"snapshot_id"}, "optional": set()},
    "list_thread_files": {"required": set(), "optional": set()},
    "read_thread_file": {"required": {"file_name"}, "optional": {"offset", "limit"}},
    "read_document": {"required": {"path"}, "optional": {"offset", "limit", "max_chars"}},
    "extract_document_images": {
        "required": set(),
        "optional": {"path", "file_name", "output_dir", "max_images",
                     "min_px", "ocr", "pages", "dpi"},
    },
    "search_in_threads": {
        "required": {"query"},
        "optional": {"thread_id", "scope", "orchestrator", "date_from",
                     "date_to", "date_field", "role", "regex",
                     "include_tool_results", "max_results"},
    },
    "list_threads": {
        "required": set(),
        "optional": {"orchestrator", "date_from", "date_to", "date_field",
                     "limit", "offset", "order"},
    },
    "read_thread": {
        "required": set(),
        "optional": {"thread_id", "offset", "limit", "include_tool_results"},
    },
    "list_orchestrators": {"required": set(), "optional": set()},
    "get_orchestrator": {"required": {"slug"}, "optional": set()},
    "create_orchestrator": {
        "required": {"slug", "name"},
        "optional": {"description", "prompt_text", "config", "tools",
                     "max_steps", "auto_apply", "memory_dir"},
    },
    "update_orchestrator": {
        "required": {"slug"},
        "optional": {"name", "description", "prompt_text", "config", "tools",
                     "max_steps", "auto_apply", "sort_order", "memory_dir"},
    },
    "delete_orchestrator": {"required": {"slug"}, "optional": set()},
    "reload_orchestrator": {"required": {"slug"}, "optional": set()},
    "list_orchestrator_functions": {"required": {"slug"}, "optional": set()},
    "get_orchestrator_function": {"required": {"slug", "name"}, "optional": set()},
    "save_orchestrator_function": {"required": {"slug", "name", "code"}, "optional": set()},
    "delete_orchestrator_function": {"required": {"slug", "name"}, "optional": set()},
    "list_orchestrator_instructions": {"required": {"slug"}, "optional": set()},
    "get_orchestrator_instruction": {"required": {"slug", "instruction_id"}, "optional": set()},
    "save_orchestrator_instruction": {
        "required": {"slug", "name"},
        "optional": {"instruction_id", "description", "prompt_text"},
    },
    "delete_orchestrator_instruction": {"required": {"slug", "instruction_id"}, "optional": set()},
    "list_connections": {"required": set(), "optional": set()},
    "get_connection": {"required": {"conn_id"}, "optional": set()},
    "list_connector_services": {"required": set(), "optional": set()},
    "list_connection_presets": {"required": set(), "optional": {"service"}},
    "create_connection": {
        "required": {"service", "name"},
        "optional": {"token", "account", "config", "secrets", "pending", "preset"},
    },
    "update_connection": {
        "required": {"conn_id"},
        "optional": {"name", "account", "token", "config", "secrets"},
    },
    "delete_connection": {"required": {"conn_id"}, "optional": set()},
    "test_connection": {"required": {"conn_id"}, "optional": set()},
    "get_enabled_connections": {"required": set(), "optional": {"orchestrator"}},
    "set_enabled_connections": {"required": {"connection_ids"}, "optional": {"orchestrator"}},
    "get_connection_memory": {"required": {"conn_id"}, "optional": set()},
    "set_connection_memory": {"required": {"conn_id", "content"}, "optional": set()},
    "get_personal_memory": {"required": set(), "optional": {"scope", "max_chars"}},
    "set_personal_memory": {"required": {"content"},
                           "optional": {"scope", "mode", "note",
                                        "overwrite_owner_profile"}},
    "list_personal_memory": {"required": set(), "optional": set()},
    # ── Delegation tools (executed by the agent loop, not by dispatch) ─────
    # Without these specs the argument check would silently pass for the two
    # new tools: the dictionary is the only source of truth for accepted
    # keyword names.
    "spawn_agent": {
        "required": {"agent_slug", "request"},
        "optional": {"inline_orchestrator", "background", "resume_run_id"},
    },
    "subagent_status": {
        "required": set(),
        "optional": {"run_id", "root_run_id", "wait_seconds"},
    },
    "subagent_result": {
        "required": {"run_id"},
        "optional": {"wait_seconds"},
    },
    "read_subagent_state": {
        "required": {"run_id"},
        "optional": {"section"},
    },
    "resume_subagent": {
        "required": {"run_id"},
        "optional": {"request", "background"},
    },
    "sleep": {
        "required": {"seconds"},
        "optional": {"until"},
    },
    "cancel_subagent": {
        "required": {"run_id"},
        "optional": {"cascade", "reason"},
    },
    "read_subagent_thread": {
        "required": {"run_id"},
        # mode/index/before/after drive the recovery reads: 'tail' answers
        # "where did it stop", 'around' pairs with a search hit, 'meta'
        # reports cost without content.
        "optional": {"offset", "limit", "include_tool_results", "mode",
                     "index", "before", "after"},
    },
    "verify_result": {
        "required": {"run_id"},
        "optional": {"criteria"},
    },
    "invoke_assistant": {
        "required": set(),
        "optional": {"assistant_id", "user", "context_files", "system",
                     "model", "max_tokens"},
    },
    # ── Agent dialog (a child asks, its parent answers) ────────────────────
    "ask_parent": {
        "required": {"question"},
        "optional": {"options", "done", "blocked"},
    },
    "answer_child": {
        "required": {"run_id", "answer"},
        "optional": set(),
    },
}

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
