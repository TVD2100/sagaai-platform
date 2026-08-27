---
id: employee_creator
name: Employee Creator
description: Conducts a short questionnaire and creates a new Employee with its system prompt, tools, instructions and custom functions.
---

# Employee Creator -- Employee Generation Rules

When DevAgent needs to create a new Employee, follow these rules.
An Employee is a self-contained autonomous agent (like DevAgent itself):
it has its own system prompt, model configuration, tool set, economy settings,
instructions, and optionally custom Python functions.

---

## 1. Purpose interview (short questionnaire)

Before creating the employee, ask the user a SHORT series of clarifying
questions (in the user's language). Do not ask more than 3-5 questions.
Suggested questions (adapt to context):

1. What is the employee's main purpose / role? (e.g. "code reviewer",
   "SQL analyst", "content editor")
2. What models/services should it use? (strong model for main reasoning,
   weak model for cheap steps)
3. Which built-in system tools should it be allowed to use? (see §3)
4. Which custom functions does it need? Describe the capabilities.
5. Any special instructions for its behaviour?

The user may answer in free form or skip; use your judgment to fill gaps.

---

## 2. Creation flow

After the interview, perform the following steps in order:

1. Derive a short slug (lowercase ascii letters, digits, underscores; no
   spaces, dashes, dots or slashes). `create_orchestrator` normalizes the
   slug anyway, but a clean slug avoids surprises.
2. Call `create_orchestrator(slug, name, description, prompt_text, config, tools, max_steps, auto_apply)`.
   The response contains `slug` - the ACTUAL (normalized) slug. Always use
   that value for all later calls; never reuse your original string.
3. If custom Python functions are needed, create them under the employee
   folder via `save_orchestrator_function(slug, name, code)`. Each function
   file must define a callable named `invoke(**kwargs) -> dict`.
4. If employee-specific instructions are needed, add them via
   `save_orchestrator_instruction(slug, instruction_id, name, description, prompt_text)`.
   The response returns the EFFECTIVE `instruction_id` (the one you passed,
   or an auto-generated 8-char hex id when you passed ''). Remember that id
   when you later need `get_orchestrator_instruction` or
   `delete_orchestrator_instruction`.
5. Set the full system prompt via `update_orchestrator(slug, prompt_text=...)`
   after creation (the prompt written by `create_orchestrator` may be
   incomplete). `update_orchestrator` also accepts `tools`, `config`,
   `max_steps`, `auto_apply`, `sort_order` - only the passed fields change.
6. Hand-written edits to the employee's folder (orchestrator.json /
   system_prompt.md / instructions) become visible to the running employee
   only after `reload_orchestrator(slug)`; calling it is the supported way
   to apply manual folder edits. Do not edit `dev_agent/system_prompt.md`
   expecting it to affect a running employee.
7. Confirm to the user what was created, including the folder location under
   DATA_DIR/orchestrators/<slug>/.

---

## 3. System tools available to employees

Employees can use the same system tools as DevAgent when those tools are
listed in the employee's `tools` list. Common useful tools:

- read_file, list_files, propose_file, verify_file, apply_patch
- run_test, run_code, search_in_files
- create_backup, restore_backup, show_history
- list_assistants, get_assistant_by_id, create_assistant_for_task,
  update_assistant_by_id
- list_skills_library, get_skill_folder, get_skill_prompt, get_skill_file
- list_instructions, get_instruction
- set_workspace, set_target_file, current_workspace, current_install
- scan_folder, assess_workspace, build_project_map, write_project_map
- write_doc, read_doc
- web_search
- snapshot_all, list_snapshots, restore_all
- get_history_index, get_history_messages
- list_recent_workspaces
- task_state_init, task_state_read, task_state_update, task_state_mark_step,
  task_state_clear
- list_rag_bases, rag_search
- list_orchestrators, get_orchestrator, create_orchestrator,
  update_orchestrator, delete_orchestrator, reload_orchestrator
- list_orchestrator_functions, get_orchestrator_function,
  save_orchestrator_function, delete_orchestrator_function
- list_orchestrator_instructions, get_orchestrator_instruction,
  save_orchestrator_instruction, delete_orchestrator_instruction

Select only the tools that match the employee's purpose. For example,
a content editor may only need read_file/propose_file, while a full developer
employee gets the whole DevAgent set.

---

## 4. System prompt for the employee

Write the employee's system prompt as a Markdown document with sections:

1. **## ROLE** -- who the employee is and what it does.
2. **## TASK** -- the main responsibilities.
3. **## OPERATING LOOP / BEHAVIOUR** -- how it should work step by step.
   Include the loop-control contract: the employee must end EVERY response
   with a fenced JSON block containing loop_status, set to continue or
   awaiting_user. awaiting_user is mandatory after presenting a plan or a
   question and after the final report; continue is allowed only in the
   middle of an already approved plan.
4. **## TOOLS** -- which tools it can use and how: exactly one fenced JSON
   tool-call block per message, preceded by a one-line comment; numeric and
   boolean arguments passed as numbers, not as strings.
5. **## OUTPUT RULES** -- format of answers, language, style.
6. **## CONSTRAINTS** -- what it must not do.

Use the **same language as the user's request** for the generated prompt.
The prompt should be precise and complete enough that the employee can
operate autonomously.

---

## 5. Custom functions

When the employee needs capabilities beyond the built-in system tools,
create custom Python functions. Each function is a separate .py file in the
employee's `functions/` folder and MUST define:

```python
def invoke(**kwargs) -> dict:
    # kwargs contains the arguments the LLM passed in the tool call
    # Return a JSON-serializable dict: {"ok": bool, ...}
```

Name each function with a short, descriptive, valid Python identifier
(e.g. `calculate_metrics`, `fetch_orders`).

---

## 6. Output / confirmation to the user

After creation, report:
- employee name and slug,
- which system tools are enabled,
- which custom functions were created,
- which instructions were added,
- the folder path where the employee lives.
