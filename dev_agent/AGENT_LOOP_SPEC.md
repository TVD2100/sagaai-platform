# AGENT_LOOP_SPEC — DevAgent orchestrator + UI page (Phase 1 closing)

> ИСТОРИЧЕСКИЙ ДОКУМЕНТ (Phase 1). Актуальное состояние: движок патчей (`patcher.py`)
> и инструменты `propose_rewrite`/`propose_new_file` удалены. Единственный инструмент
> правки — `propose_file` (полная перезапись файла). См. `dev_agent/README.md`.

Goal: close the missing "model ↔ tools" loop so DevAgent actually does work,
exactly per architecture **§7.2 Рабочий Цикл** (read → plan-in-words → draft →
UI diff → ✅/❌/✏️ → backup+apply+test → report + auto CHANGELOG).

## Scope (build these two files)
1. `dev_agent/agent_loop.py` — provider-independent loop CORE (no Streamlit).
2. `ui/pages/devagent.py` — Streamlit page implementing §7.2 with diff + buttons.
Plus: wire nav entry in `ui/app.py`, add i18n keys, add tests, tweak system prompt.

## HARD CONSTRAINTS
- Do NOT edit PROTECTED files: `universal.py`, `dev_agent/dev_agent.py`,
  `dev_agent/config.py`, `dev_agent/safe_writer.py`,
  `dev_agent/backup_manager.py`, `dev_agent/patcher.py`. (`universal.py` must
  stay 72835 bytes.)
- Code comments in English. ALL UI strings via `t("key", lang=lang)` — no
  hardcoded user-facing text (Russian or English). Add new keys to BOTH
  `langs/ru.json` and `langs/en.json` (keep parity) and to
  `dev_agent/i18n_keys.json`.
- Do NOT install streamlit. Tests must run headless via the existing
  `tests/_st_mock.py` harness.
- Reuse existing machinery — do not reimplement: `DevAgent` (dispatch/
  dispatch_json) from `dev_agent/dev_agent.py`; `send_request(...)` from
  `core/api_layer.py`; skill loaded from DB via `storage.repository`.

## agent_loop.py — the CORE

### Tool-call parsing (robust)
The model emits tool calls as fenced JSON blocks, e.g.:
    ```json
    {"tool": "list_files", "subdir": "."}
    ```
or sometimes multiple blocks, or nested-arg form `{"tool":..., "args":{...}}`.
Write `parse_tool_calls(text) -> list[dict]` that:
- Extracts ALL ```json ... ``` fenced blocks; also tolerate a bare top-level
  JSON object if no fence is present.
- Accepts both `{"tool": X}` and `{"name": X}` for the tool name.
- NORMALIZES flat args into nested form: any top-level keys other than
  tool/name/args/arguments are collected into `args`. (Critical: the model
  produced `{"tool":"list_files","subdir":"."}` and dispatch_json silently
  dropped `subdir` because it only reads `call["args"]`.) Output dicts MUST be
  in `{"tool": name, "args": {...}}` shape so `DevAgent.dispatch_json` works.
- Returns [] if no parseable tool call (that means the model gave a normal
  text/plan reply or a final answer).

### Loop function
`run_agent_loop(task, skill, dispatcher, *, on_event=None, history=None,
                max_steps=12, auto_apply=False, lang=None) -> AgentResult`
where:
- `skill`: dict from repo (has text/service/model/temperature/id).
- `dispatcher`: a `DevAgent` instance (so tests can inject a fake).
- `on_event(event: dict)`: optional callback for UI/console streaming. Emit
  events: {"type":"assistant_text", ...}, {"type":"tool_call", ...},
  {"type":"tool_result", ...}, {"type":"awaiting_approval", "path":..,
  "diff":..}, {"type":"applied", ...}, {"type":"final", "text":..},
  {"type":"error", ...}, {"type":"stopped_max_steps"}.

Loop algorithm (each step):
1. Call `send_request(user_message, skill, history=history, lang=lang)` to get
   the assistant text. (First step user_message = task; later steps = the
   serialized tool results from the previous step.)
2. Append {"role":"assistant","content": text} to history; emit assistant_text.
3. `calls = parse_tool_calls(text)`.
   - If no calls → this is a plan or final answer → return AgentResult(status=
     "final" or "awaiting_plan_ok", text=text). Stop. (Per §7.2 the model must
     describe the plan in words first and the loop pauses for the human.)
   - If a call is `propose_file`: execute it via dispatcher
     (this stages the draft: produces a diff, and in manual mode stops;
     in autonomous mode it auto-applies). Then STOP the loop with
     status="awaiting_approval" (manual) or continue with applied result
     (auto). This is the §7.2 approval gate.
   - If a call is `apply_edit`: only allowed when the human already approved
     (caller sets autoapply or calls a separate `apply_*` helper — see below).
   - Otherwise (read_file/list_files/run_test/...): execute via
     `dispatcher.dispatch_json(normalized_call)`, emit tool_result, append the
     result to history as a user/tool message (role "user", content = a compact
     JSON string of {"tool_result": ...}), and CONTINUE the loop.
4. Guard: if step count reaches max_steps, stop with status="stopped_max_steps".

### Approval helpers (called by the UI after the human clicks)
- `approve_and_apply(path, dispatcher, *, note=None) -> dict` → calls
  `dispatcher.dispatch("apply_edit", {"path":path, "note":note})`, which does
  backup → write → changelog. Return the dispatcher result (incl. backup
  version + test outcome if available).
- `discard(path, dispatcher) -> dict` → `dispatcher.dispatch("discard_edit",
  {"path":path})`.

### AgentResult
A small dataclass/dict: status ∈ {"final","awaiting_plan_ok",
"awaiting_approval","stopped_max_steps","error"}, plus text, history,
staged_path, diff, steps.

## ui/pages/devagent.py — the PAGE (§7.2 faithful)
Function `page_devagent()`. Render with `lang = st.session_state.get("ui_lang")`.
Flow & widgets (ALL labels via t(), give every interactive widget a unique
`key=`):
- Title `t("devagent_title", lang=lang)` + short intro `t("devagent_intro",...)`.
- Load DevAgent skill from DB (`repo_get_skill_with_text("devagent")`); if its
  service has no API key configured, show `t("devagent_no_key", ...)` warning
  and a hint to Settings.
- A text_area for the task (`key="devagent_task"`), plus a "Run" button
  (`key="devagent_run"`).
- On run: build a DevAgent dispatcher, call run_agent_loop(...,
  on_event=collect into st.session_state["devagent_log"]). Persist the loop's
  history + last AgentResult in session_state so reruns (button clicks) don't
  lose state.
- Render the event log (assistant text, tool calls, tool results) in readable
  blocks. Tool calls/results in st.expander or code blocks.
- If status == "awaiting_plan_ok": show the plan and a "Продолжить/Continue"
  button (`key="devagent_plan_ok"`) that feeds an approval message back into the
  loop (history continues).
- If status == "awaiting_approval": show the diff (st.code(diff,
  language="diff")) and THREE buttons exactly per §7.2:
  ✅ `t("devagent_apply", ...)` (key="devagent_apply"),
  ❌ `t("devagent_cancel", ...)` (key="devagent_cancel"),
  ✏️ `t("devagent_refine", ...)` (key="devagent_refine").
  - Apply → approve_and_apply(...) → show report `t("devagent_applied", ...,
    version=.., test=..)` (backup version + test result). Per §7.2 the report
    states backup vN + test passed; CHANGELOG entry is automatic (apply_edit
    already writes changelog).
  - Cancel → discard(...) and clear staged state.
  - Refine → reveal a text_input (key="devagent_refine_text") to send refinement
    instructions back into the loop (continue history).
- A "New task" / reset button (key="devagent_reset") clears session log/history.

Keep st.rerun() OUT of any st.form (lesson from earlier UI fix). Use plain
buttons + session flags.

## Wire navigation in ui/app.py (editable)
- Add to NAV list a DevAgent entry, e.g. ("devagent", f"🛠 {t('nav_devagent',
  lang=lang)}"). Add dispatch branch: `elif page == "devagent": page_devagent()`.
- Import `from ui.pages.devagent import page_devagent`.

## System prompt tweak (dev_agent/system_prompts/dev_agent.md, EDITABLE)
Add a short "[ПРОТОКОЛ ВЫЗОВА]" section making the machine contract explicit:
- To call a tool, output exactly ONE fenced ```json block per tool call with
  shape {"tool": "<name>", "args": { ... }} (args nested).
- Emit tool calls one step at a time; after results come back, decide the next.
- When NOT calling a tool, you are either (a) presenting the plan in words for
  approval, or (b) giving the final report. Do not mix prose and a tool call in
  the same message.
Keep the rest of the prompt intact (do not weaken PROTECTED_FILES / approval
rules).

## i18n keys to add (BOTH ru.json + en.json + i18n_keys.json)
nav_devagent, devagent_title, devagent_intro, devagent_no_key,
devagent_task_label, devagent_run, devagent_plan_ok, devagent_apply,
devagent_cancel, devagent_refine, devagent_refine_text, devagent_applied
(placeholders: {version},{test}), devagent_reset, devagent_thinking,
devagent_max_steps, devagent_error (placeholder {error}),
devagent_tool_call, devagent_tool_result, devagent_diff_title.
RU values natural Russian; EN natural English. Keep both files SAME key set.

## Tests (tests/test_phase1_agent_loop.py) — headless, no real API
Use a FakeDispatcher (records dispatch calls, returns canned results) and a
fake model by monkeypatching `agent_loop.send_request` to a scripted sequence.
Cover:
1. parse_tool_calls: fenced single, multiple fences, bare object, nested-args
   form, AND flat-args normalization (subdir ends up under args).
2. Loop runs read tools then stops at `propose_file` with status
   "awaiting_approval" and returns a diff (model script: list_files →
   read_file → propose_file).
3. Loop returns "final" when model emits prose with no tool call.
4. max_steps guard triggers "stopped_max_steps" (model always emits read_file).
5. approve_and_apply calls dispatcher apply_edit; discard calls discard_edit.
6. UI smoke: extend tests/test_app_smoke.py page list with page_devagent (must
   render without duplicate keys / DuplicateWidgetID, and every button has key).

## Done criteria
- `python -m pytest sagaai/tests -q` green (was 160; add new tests).
- No hardcoded Cyrillic/English user strings in ui/pages/devagent.py
  (grep clean except CSS/markup).
- universal.py still 72835 bytes.
- Report back: files created, test count, any deviations.
