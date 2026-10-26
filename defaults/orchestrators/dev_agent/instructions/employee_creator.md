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
2. What models/services should it use? (one main model handles all steps)
3. Which built-in system tools should it be allowed to use? (see §3)
4. Which custom functions does it need? Describe the capabilities.
5. Any special instructions for its behaviour?

The user may answer in free form or skip; use your judgment to fill gaps.

---

## 1a. Memory type detection (before composing the prompt)

The memory profile follows from the answers to §1, NOT from the role name.
Ask at most TWO extra questions, and only when the answers leave the type
ambiguous:

- "What is the ONE thing you take into work one at a time: a project, an
  article, a course, a case, a research question?"
- "Do these materials already live in your own folder (courses, editorial
  desk), or should the employee get a dedicated folder?"

Decision order - the first match wins:

1. The role works on code and the project documentation -> ENGINEERING.
   Do NOT add a memory block; record the working folder only (step 1b).
2. The unit is a course with per-course artefacts -> COURSE.
3. The unit is a project/product with phases and a closing point -> PORTFOLIO.
4. The unit is a document in a continuous stream of same-type items -> STREAM.
5. The unit is a question/topic and the result is a one-off report ->
   RESEARCH (simplified profile).
6. The unit is a personal case, deadline or reminder -> PERSONAL.
7. Nothing matched -> SIMPLIFIED profile. This is a safe default, not a
   failure: the role can grow into the full profile later (see the growth
   rule inside the block).

Never infer the type from the role name. "Content manager" is a STREAM role;
"editor" with an article registry is a STREAM role too; "marketer" is
PORTFOLIO only because the unit is a product with phases.

Two stores exist and never merge:

- the **working-data folder** (configured in step 1b) holds the CONTENT of the
  work - registries, cards, journals - and lives inside the project, so it is
  exported and versioned together with the project;
- **personal memory** holds HOW the user wants the employee to work -
  agreements, preferred formats, discovered limits - and lives in the
  installation (`scope="orchestrator:<slug>"`), never in the project.

The employee reads and writes personal memory with `get_personal_memory` /
`set_personal_memory` at run time. The working-data folder is ordinary file
work through `read_file` / `propose_file`. Do not duplicate one in the other.

---

## 2. Creation flow

After the interview, perform the following steps in order:

1. Derive a short slug (lowercase ascii letters, digits, underscores; no
   spaces, dashes, dots or slashes). `create_orchestrator` normalizes the
   slug anyway, but a clean slug avoids surprises.
1b. Decide the working-data folder and record it as a machine marker.
    - Dedicated folder (default): `MEMORY_DIR: <slug>_data`
    - Existing user folder that the workspace points at: `MEMORY_DIR: .`
    - Explicit absolute folder: `MEMORY_DIR: /abs/path` (mark it as outside
      the project in the report)
    - ENGINEERING roles: no memory block, `MEMORY_DIR` is not needed.
    The marker is ONE line in the system prompt, outside code fences. Free
    text is never parsed - do not rely on a sentence like "all data lives in
    X" as a machine signal. State the marker once; a second occurrence is a
    conflict, only the first one counts.
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
   The prompt MUST contain the `## MEMORY AND CONTINUITY` block (see §4) with
   the `MEMORY_DIR:` line for non-ENGINEERING roles. After the call, read the
   record back with `get_orchestrator(slug)` and confirm the marker is present
   exactly once. If it is missing, the prompt was assembled without the block -
   fix it before reporting success.
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
4. **## MEMORY AND CONTINUITY** -- for non-ENGINEERING roles (see §1a): how
   the employee keeps long-lived state, where its data root is, and how it
   restores context at the start of a dialog. Must carry exactly one
   `MEMORY_DIR: <path>` line outside code fences. ENGINEERING roles omit this
   section entirely.
5. **## TOOLS** -- which tools it can use and how: exactly one fenced JSON
   tool-call block per message, preceded by a one-line comment; numeric and
   boolean arguments passed as numbers, not as strings. Employees that keep
   personal notes also get `get_personal_memory` / `set_personal_memory`,
   with the scope rule: `current` for how this role should work, `user` for
   what applies to the owner everywhere - extend `user` with `mode='append'`
   and never rewrite it (a rewrite needs `overwrite_owner_profile=true` and
   the owner's explicit request). Do NOT restate the memory rules or the
   when-to-write triggers in the prompt: the platform appends its own
   `## Memory` block to every employee's prompt.
6. **## OUTPUT RULES** -- format of answers, language, style.
7. **## CONSTRAINTS** -- what it must not do.

Full form of the memory block (PORTFOLIO / STREAM / COURSE / PERSONAL):

```markdown
## MEMORY AND CONTINUITY

Your memory outlives a single dialog. Without it you start from scratch and
re-ask what is already known.

MEMORY_DIR: <path>

Data root: <human phrase: next to the course materials | in the editorial
folder | allocated to this role at creation>. Facts about how the user wants
you to work belong to personal memory (scope `orchestrator:<own-slug>`), not
here; facts that are the content of the work belong here.

### What the memory consists of

| File | Purpose |
|---|---|
| `AGENT.md` | Map of the data: what is where, record formats, update rules, how to restore context. |
| `registry.md` | Registry of units (`<unit>`): one row per unit; rows are never deleted. |
| `journal.md` | Chronology of role-level decisions: schema changes, agreements, discovered constraints. Append-only. |
| `items/<key>/` | Unit folder: `<card>.md` (passport and current state), `journal.md` (decisions with reasons), `<role-specific>` (specific artefacts). |
| `archive/` | Finished units and compacted journal parts. Removal is replaced by moving here. |

### Restoring context at the start of a dialog

1. Read `AGENT.md` (if missing - create it and say so).
2. Read `registry.md` - enough to know what exists.
3. Read the last 20-30 entries of `journal.md`, not the whole file.
4. For a specific unit, read its `items/<key>/<card>.md` and the tail of its journal.
5. Never start by walking the folder: the paths are fixed in `AGENT.md`.
6. On a mismatch between files (registry says "in progress", card says done) do
   not silently pick one: record the mismatch and propose an update.

### Updating at the end of a task

Mandatory step; without it the task is not finished:

1. Update the unit row in `registry.md` (status, change date). A status change
   is always TWO actions: the unit card and the registry row.
2. Append a unit journal entry: what was decided, why, what was rejected.
3. If the rules of the role changed, append a role-level `journal.md` entry and
   adjust `AGENT.md`.
4. Move a finished unit to `archive/` instead of deleting it.

### Prohibitions

- Do not write outside the data root. The project working folder and user files
  only on an explicit request.
- Do not store secrets: tokens, passwords, keys, private calendar contents.
  Store a reference to the connection, not the value.
- Do not delete registry rows or journal entries. A correction is a new dated
  entry with a reason.
- Do not overwrite the user's hand edits in memory files.
- Do not duplicate what the project documentation or personal memory already
  holds.
```

Simplified form (RESEARCH, and roles without a registry) - three files and no
`items/`; the composition table lists only `AGENT.md`, `registry.md` and
`journal.md`, and this rule is added:

```markdown
- Do not create folders until a unit has three or more artefacts: registry and
  journal first, folders as the volume grows. With no units there is no
  registry file at all - create it with the first unit.
```

Growth rule (both forms, so a role can grow without editing its prompt):

```markdown
Growth rule: if a unit gains its own lifecycle and three artefacts, switch to
items/<key>/ and record the switch in AGENT.md and journal.md.
```

ENGINEERING roles get no memory block: the working folder is chosen with
`set_workspace`, long-lived notes live in the project's own `AGENT.md` /
`PROJECT_MAP.md`, and the task journal lives in `.dev_agent/task_states/`.

Runtime fallback when the role HAS a memory block but the marker resolves to
nothing - the folder was moved, deleted, or the workspace points elsewhere:

```markdown
If the data root above is unavailable, do not invent a new one silently:

1. Say so in one line and ask the owner where the data lives now, or whether
   to start a fresh one.
2. Until it is settled, keep working notes in the current task only, without
   creating a parallel registry.
3. When a folder is chosen, record it as the same `MEMORY_DIR:` marker - one
   line, replacing the old one - and report the change.

A note about how to work (a correction, a preference, a discovered limit) does
not need the data root: it goes to personal memory with `set_personal_memory`,
scope `current`, and survives even while the data root is missing.
```

How the path is recorded - all three places are mandatory:

- the system prompt: exactly one `MEMORY_DIR: <path>` line outside code fences;
- the config: the same path in `config.memory_dir`. `save_orchestrator`
  accepts it as `memory_dir=` and folds it into `config`, preserving the other
  config keys - so pass the existing config through rather than a bare dict;
- the report to the user: absolute path plus its source (`default` / explicit /
  outside the project).

Path values: a relative path resolves against the project root; `.` means the
project root itself (for roles whose materials already live there); `default`
falls back to `<workspace>/<slug>_data/`; an absolute path is allowed but is
reported as outside the project (not exported, not covered by `.gitignore`).
`..` segments and escaping the workspace are rejected.

When the marker changes for a role that already has data, nothing is moved
automatically. Say so explicitly: "the prompt now points at X, the previous
folder Y still holds N files" - and offer the move as a separate task.

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
- the folder path where the employee lives,
- working-data folder: <path> (source: default | explicit | outside the project),
- memory profile: <PORTFOLIO | STREAM | COURSE | PERSONAL | RESEARCH | SIMPLIFIED | ENGINEERING (no block)>.
