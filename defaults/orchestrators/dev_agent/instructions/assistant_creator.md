---
id: assistant_creator
name: Assistant Creator
description: Generates high-quality system prompts for new assistants and guides their creation, editing, and verification. Returns JSON with name, description, prompt, language.
---

# Assistant Creator -- Prompt Generation Rules

When DevAgent needs to create, update, or verify an assistant, follow these rules to produce a high-quality assistant profile. An assistant is a user-facing chat profile with its own system prompt, model, temperature, tools (optional), and settings. It is invoked by the chat logic automatically; DevAgent does NOT call it from inside its own workflow.

---

## Output format

Output ONLY a valid JSON object with exactly these fields:
- **name** -- short, descriptive assistant name in the user's language (the user-defined name slot).
- **name_en** -- optional English translation of the name.
- **name_ru** -- optional Russian translation of the name.
- **name_zh** -- optional Simplified Chinese translation of the name.
- **description** -- one-sentence description of what the assistant does.
- **prompt** -- the complete system prompt for the assistant (Markdown, 300--1200 words).
- **language** -- ISO 639-1 code of the prompt language (e.g. "ru", "en", "zh").

Do NOT wrap in markdown fences. The output MUST be parseable by json.loads().
The platform requests this answer via native JSON Schema (structured output) when the provider supports it; in any case, output ONLY the bare JSON object - never prose, explanations, or markdown wrappers around it.

Example:
{"name": "Проверка орфографии", "name_en": "Spell Check", "name_ru": "Проверка орфографии", "name_zh": "拼写检查", "description": "Проверяет тексты на орфографические и грамматические ошибки.", "prompt": "## Роль\n\nВы - **Проверка орфографии**...", "language": "ru"}

### The four name slots

An assistant carries four name slots: the user-defined `name` and the
`name_en` / `name_ru` / `name_zh` translations. The interface shows the
user-defined name in every language and falls back to the translation of the
interface language (then English) only when the user slot is empty. So:

- a profile the user will own gets a filled `name` and, when the meaning
  survives translation, the three translations next to it;
- a reusable profile meant to be language-neutral may leave `name` empty and
  fill at least one translation;
- never invent a translation that changes the meaning; leave the slot out
  instead of guessing.

---

## Assistant types

1. **Self-contained assistant (default)** -- produces a text answer based solely on the user's input and the model's knowledge, without calling tools. This is the most common type.
2. **Assistant with tools** -- when the task requires up-to-date information (news, current events, fact-checking, latest docs), DevAgent automatically activates the `web_search` tool for the assistant. The prompt must state that the assistant may use web search when needed.
3. **Assistant with attachments** -- when the user provides reference files, the files are attached to the assistant profile as context. The prompt must reference them as contextual data.

---

## Required prompt sections

The created prompt **must** contain the following sections in this exact order:

### Requirements for the prompt sections

1. **## Role** -- a clear statement of who the model should act as.
2. **## Context** (if applicable) -- additional information, background, or input data the model needs to understand the task.
3. **## Task** -- a precise formulation of what the model must do.
4. **## Tone and style** -- description of the desired tone and style.
5. **## Output format** (if applicable) -- exactly how the model's answer should be presented (table, list, essay, code, etc.).
6. **## Constraints** (if applicable) -- length, prohibitions, special conditions, or things the model must NOT do.

Additional sections (e.g. **## Example**, **## Input data**) may be added if helpful.

### Additional quality criteria

1. **Self-sufficient** -- the prompt alone + user message must produce a complete answer.
2. **Clear output format** -- specify exactly what the response should contain (format, tone, length, language).
3. **Edge cases covered** -- handle empty input, invalid input, ambiguous cases.
4. **Examples provided** -- 2--3 concrete examples of user input -> assistant output are strongly recommended.
5. **Language** -- write the prompt in the **same language as the user's request**. If the user writes in Russian, the prompt must be in Russian.

---

## Creation flow (create_assistant_for_task)

When the user explicitly asks to create an assistant, DevAgent follows this flow:

1. Load THIS instruction (Assistant Creator) via `get_instruction('assistant_creator')` or `get_orchestrator_instruction('dev_agent', 'assistant_creator')`.
2. Present the proposed name and description to the user and get explicit confirmation BEFORE calling `create_assistant_for_task`.
3. Call `create_assistant_for_task(task)` with the user's request text.
4. The tool automatically:
   - Classifies the task: complexity tier and `needs_web_search` (`true` / `false`).
   - Resolves the service/model: explicit mention in the request wins; otherwise the main model from DevAgent settings is used; for web_search assistants a YandexAI pro/lite model is chosen and the `web_search` tool is activated.
   - Creates the assistant with its folder under `DATA_DIR/assistants/<slug>/` (manifest.json + prompt.md).
5. Report back: assistant name, slug, service/model, enabled tools, and any warnings (e.g. web_search requested but unsupported by the chosen provider).

---

## Editing flow (update_assistant_by_id)

When the user asks to modify an existing assistant:

1. Load this instruction.
2. Inspect the current state via `get_assistant_by_id(assistant_id)` -- never edit without seeing current fields.
3. Present the planned changes (which fields will change and to what) to the user and get explicit confirmation.
4. Call `update_assistant_by_id` with ONLY the fields being changed; omitted fields keep their current values.
5. Verify the saved profile by reading it back with `get_assistant_by_id` and report the result.

Available editable fields: `name`, `name_en`, `name_ru`, `name_zh`, `description`, `prompt_text`, `service`, `model`, `temperature`, `tools`, `max_tool_calls`, `max_tokens`, `reasoning_effort`.

Name rules when editing:

- pass only the slots you are changing; an omitted slot keeps its stored value;
- an explicitly passed empty string clears that slot;
- the record must keep at least one non-empty name - the tool refuses a call
  that would leave the assistant with no label at all;
- when the user asks for a name in one language only e.g. "добавь китайское
  название", pass just that slot (`name_zh`) and leave the others untouched.
  Do not move the current display name into another slot to "fill" it.

---

## Verification rules

- A prompt must not be empty and must be at least 100 characters.
- Required sections `## Role` and `## Task` must be present.
- The prompt must contain a role statement ("you are").
- For web_search assistants, the prompt must mention that the assistant may use web search for up-to-date information.
- Never create an assistant with an empty or placeholder model; if no model can be resolved, the assistant may be created without a model, but the user must be told to configure one in Settings.

---

## Tone and style

- Write the prompt in the **same language as the user's request**.
- Use **imperative mood** for instructions.
- Use Markdown with `##` section headers.
- Be precise and concise -- target 300--1200 words.
- Avoid fluff, vague advice, or redundant explanation.
- Ensure every section is clearly delineated and the structure is obvious.
