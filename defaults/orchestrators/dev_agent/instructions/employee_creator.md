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
  work - registries, cards, journals. By default it is CENTRAL
  (`DATA_DIR/agents_data/<slug>/`): it belongs to the installation, so the
  same role has the same memory in every project and keeps it when the working
  folder changes. A role whose materials must travel WITH a project says so
  explicitly;
- **personal memory** holds HOW the user wants the employee to work -
  agreements, preferred formats, discovered limits - and lives in the
  installation (`scope="orchestrator:<slug>"`), never in the project.

The employee reads and writes personal memory with `get_personal_memory` /
`set_personal_memory` at run time. The working-data folder is read and written
through the memory tools, which enforce the declared budgets:

- `memory_status` - what the levels hold and how large they are, without
  reading their bodies;
- `memory_read(level)` - the head of an `always` level within its `read_lines`
  budget, or one `on_demand` file in full. A level whose path carries `<key>`
  is a family of per-unit files: `memory_read` refuses it, and the concrete
  file is read with `read_file`. A `search` level is refused too - it is
  answered only by `memory_search`;
- `memory_append(level, row)` - prepend a record, validated against the
  declared columns;
- `memory_search(level, query)` - query a `search` level with a few lines of
  context around each hit;
- `memory_rotate(level)` - move the oldest rows into the archive.

A whole-file read or rewrite of a level skips the budgets, so the level's file
is not edited as ordinary text: the tools are the surface, and the file itself
is never the address. Do not duplicate personal memory and the working-data
folder in one another.

---

## 2. Creation flow

After the interview, perform the following steps in order:

1. Derive a short slug (lowercase ascii letters, digits, underscores; no
   spaces, dashes, dots or slashes). `create_orchestrator` normalizes the
   slug anyway, but a clean slug avoids surprises.
1b. Decide the working-data folder. The DEFAULT needs no marker at all: the
    platform resolves it to `DATA_DIR/agents_data/<slug>/` and announces the
    folder itself (see §4), so the prompt carries no installation path.
    Add a machine marker only for a NON-default choice:
    - existing user folder that the project points at: `MEMORY_DIR: .`
    - folder that must travel with the project: `MEMORY_DIR: workspace:<sub>`
    - shared folder inside the installation: `MEMORY_DIR: central:<sub>`
    - explicit absolute folder: `MEMORY_DIR: /abs/path` (report it as outside
      both roots)
    - ENGINEERING roles: no memory block at all, no marker.
    The marker is ONE line in the system prompt, outside code fences. Free
    text is never parsed - do not rely on a sentence like "all data lives in
    X" as a machine signal. State the marker once; a second occurrence is a
    conflict, only the first one counts.
1c. Declare the memory levels in `config.memory_profile` (all roles except
    ENGINEERING). The declaration answers the one question the platform cannot
    guess: WHEN each file is read. Pass it through `create_orchestrator` /
    `update_orchestrator` as part of `config`, preserving the other keys:

```json
{"levels": [
  {"id": "registry", "kind": "always", "path": "registry.md",
   "columns": ["date", "unit", "status"], "read_lines": 40,
   "describe": "one row per unit; rows are never deleted"},
  {"id": "card", "kind": "on_demand", "path": "items/<key>/card.md",
   "describe": "passport and current state of one unit"},
  {"id": "journal", "kind": "search", "path": "journal.md",
   "columns": ["date", "decision", "reason"], "max_rows": 2000,
   "archive": "archive",
   "describe": "chronology of role-level decisions"}
]}
```

    - `kind` sets the read policy: `always` is read at the start of a task
      within `read_lines` (max 200); `on_demand` is loaded whole only when the
      task touches the unit it describes; `search` is never read whole - it is
      queried with `memory_search` and answers with a few surrounding lines.
    - `path` is relative to the employee folder and may carry a `<key>`
      placeholder for a family of per-unit files. An absolute path, `..` and an
      empty path are rejected: the folder comes from the platform, never from
      a declaration.
    - `columns` (max 16) is the record schema. `memory_append` refuses a row
      that does not match it, so the declared columns are what keeps a
      registry readable by `memory_read` instead of a full rewrite.
    - `max_rows` + `archive` enable `memory_rotate`: the oldest rows move into
      the archive folder, so the read cost of a long-lived journal stays flat.
    - A role with nothing durable to keep declares no levels and gets no
      block at all.

    The schema lives in `core/memory_profile.py` and a declaration is validated
    on read, so a malformed level is reported instead of being ignored.

2. Call `create_orchestrator(slug, name, description, prompt_text, config, tools, max_steps, auto_apply, name_en, name_ru, name_zh)`.
   The response contains `slug` - the ACTUAL (normalized) slug. Always use
   that value for all later calls; never reuse your original string.

   Names: `name` is the user-defined slot and `name_en`/`name_ru`/`name_zh`
   are its translations. At least one of the four must be non-empty - the tool
   refuses a call that would leave the employee with no label at all. A
   personal employee gets a filled `name` in the user's language; a reusable
   role meant to be language-neutral may leave `name` empty and fill the
   translations instead. Never invent a translation that changes the meaning:
   leave the slot out rather than guessing.
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
   `max_steps`, `auto_apply`, `sort_order` and the name slots `name`,
   `name_en`, `name_ru`, `name_zh` - only the passed fields change. A name
   slot passed as `None` or omitted keeps its stored value; an explicitly
   passed empty string clears that translation. A request like "add a Chinese
   name" touches only `name_zh` - never copy the current display name into
   another slot to "fill" it.
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
  task_state_clear (keep the whole set together or none: a role that can open
  a journal but not close it leaves the file open forever)
- memory_status, memory_read, memory_append, memory_search, memory_rotate
  (the declared long-term memory levels; see §1a)
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

**Do not describe the task journal or the memory tools in the prompt.** The
platform appends both blocks itself: `## Task journal (task_state)` states when
a journal is worth creating and is added to every employee that has the
journal tools, and `## Memory profile` is rendered from the declared levels.
A hand-written copy in the role prompt duplicates the rule and drifts from the
platform one - the prompt only states what the employee does, not how the loop
or the memory mechanism works.

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
4. **## MEMORY AND CONTINUITY** -- for non-ENGINEERING roles (see §1a): what
   the memory consists of and which unit a record belongs to. The read order,
   the budgets and the append rule are NOT written here: they follow from the
   declared levels (step 1c) and are rendered by the platform into the
   `## Memory profile` block. It carries a `MEMORY_DIR:` line ONLY for a
   non-default folder (step 1b). ENGINEERING roles omit this section entirely.

   Do NOT write the data folder's path, nor explain the `memory:` prefix, in
   the prompt: the platform appends its own `## Working memory` block with the
   resolved path, the way to address it, the folder state and any legacy-folder
   warning. A path copied into the role prompt goes stale the moment the folder
   moves, and two sources of truth for one path is how an employee starts
   writing to a folder nobody reads.
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

<!-- only for a NON-default folder (step 1b): MEMORY_DIR: <path> -->

Data root: announced by the platform (see the working-memory block of this
prompt). Facts about how the user wants you to work belong to personal memory
(scope `orchestrator:<own-slug>`), not here; facts that are the content of the
work belong here.

### What the memory consists of

| File | Purpose | Level |
|---|---|---|
| `AGENT.md` | Map of the data: what is where, record formats, update rules. | usually `always` |
| `registry.md` | Registry of units (`<unit>`): one row per unit; rows are never deleted. | `always` |
| `journal.md` | Chronology of role-level decisions: schema changes, agreements, discovered constraints. Append-only. | `search` |
| `items/<key>/` | Unit folder: `<card>.md` (passport and current state), `journal.md` (decisions with reasons), `<role-specific>` (specific artefacts). | `on_demand` |
| `archive/` | Finished units and compacted journal parts. Removal is replaced by moving here. | the `archive` of a level |

The level column is what the declaration in step 1c must reproduce: a file with
the `always` policy is read at the start of every task, so it stays small; a
`search` journal is never read whole, so it can grow for years.

### What the role adds to the mechanism

The read policy, the budgets and the record schemas are declared in
`config.memory_profile` (step 1c) and the platform renders them into the
`## Memory profile` block. The prompt does NOT repeat them: a hand-written copy
drifts from the declaration the moment a level is added or a budget changes.
What the prompt states is the role-specific part:

1. Which unit a record belongs to and what its lifecycle looks like.
2. That a status change is TWO records - the unit card and the registry row -
   because a row alone loses the reasoning behind the status.
3. That a finished unit moves to `archive/` and nothing is deleted.
4. On a mismatch between records (registry says "in progress", card says done)
   do not silently pick one: record the mismatch and propose an update.

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

How the folder is recorded:

- a NON-default choice gets exactly one `MEMORY_DIR: <path>` line in the system
  prompt outside code fences, and the same value in `config.memory_dir`.
  `save_orchestrator` accepts it as `memory_dir=` and folds it into `config`,
  preserving the other config keys - so pass the existing config through
  rather than a bare dict;
- the default needs neither: it is derived from the slug, and adding a
  `<slug>_data` row would pin the role to a project-relative location again;
- the report to the user: absolute path plus its source (`default` / `prompt` /
  `config`) and scope (`central` / `workspace` / `external`).

Path values: `workspace:<sub>` resolves against the project root; `central:<sub>`
resolves against the installation-wide root `DATA_DIR/agents_data/`; `.` means
the project root itself (for roles whose materials already live there);
`default` falls back to the central `DATA_DIR/agents_data/<slug>/`; an absolute
path is allowed but is reported as outside both roots. `..` segments and
escaping the chosen root are rejected.

When the marker changes for a role that already has data, nothing is moved
automatically. Say so explicitly: "the prompt now points at X, the previous
folder Y still holds N files" - and offer the move as a separate task. The
platform reports such folders in the working-memory block (`previous layout`)
and in the resolver result (`conflicts`), so read that report instead of
walking the disk.

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
- employee name and slug (the four name slots: user-defined plus en/ru/zh
  translations, and which of them are filled),
- which system tools are enabled,
- which custom functions were created,
- which instructions were added,
- the folder path where the employee lives,
- working-data folder: <path> (scope: central | workspace | external; source:
  default | prompt | config),
- what the platform announces by itself (path, `memory:` prefix) - never
  repeated in the role prompt,
- memory profile: <PORTFOLIO | STREAM | COURSE | PERSONAL | RESEARCH | SIMPLIFIED | ENGINEERING (no block)>,
- declared levels: one line per level - id, kind, path, read budget - so the
  owner can see what the platform will render.
