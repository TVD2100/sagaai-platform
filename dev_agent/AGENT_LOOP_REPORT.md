# AGENT_LOOP_REPORT — Phase 1 closing (DevAgent orchestrator + UI page)

> ИСТОРИЧЕСКИЙ ДОКУМЕНТ (Phase 1). Актуальное состояние: движок патчей (`patcher.py`)
> и инструменты `propose_rewrite`/`propose_new_file` удалены. Единственный инструмент
> правки — `propose_file` (полная перезапись файла). См. `dev_agent/README.md`.

## Files Created

| File | Description |
|------|-------------|
| `dev_agent/agent_loop.py` | Provider-independent loop core: `parse_tool_calls`, `run_agent_loop`, `approve_and_apply`, `discard`, `AgentResult`. No Streamlit imports. |
| `ui/pages/devagent.py` | Streamlit page `page_devagent()` implementing §7.2 flow: task input → run → event log → plan-ok → diff + ✅/❌/✏️ buttons → applied report. All strings via `t()`. |
| `tests/test_phase1_agent_loop.py` | 20 headless tests with `FakeDispatcher` + `monkeypatch`; covers all spec-required scenarios. |

## Files Edited

| File | Change |
|------|--------|
| `ui/app.py` | Added `from ui.pages.devagent import page_devagent`; added `("devagent", ...)` to NAV list; added `elif page == "devagent": page_devagent()` dispatch branch. |
| `dev_agent/system_prompts/dev_agent.md` | Appended `[ПРОТОКОЛ ВЫЗОВА / TOOL CALL PROTOCOL]` section specifying the exact fenced-JSON call contract, one-call-per-step rule, and prose-only modes. |
| `langs/ru.json` | Added 18 devagent_* keys + `nav_devagent` with natural Russian translations. |
| `langs/en.json` | Added identical key set with natural English translations (parity maintained). |
| `dev_agent/i18n_keys.json` | Added all 19 new keys to the registry. |
| `tests/test_app_smoke.py` | Extended `test_every_page_renders` and `test_no_duplicate_widget_keys` to include `page_devagent`. |

## PROTECTED Files — Not Touched

`universal.py`, `dev_agent/dev_agent.py`, `dev_agent/config.py`,
`dev_agent/safe_writer.py`, `dev_agent/backup_manager.py`, `dev_agent/patcher.py`.

## Test Results

```
180 passed in 2.37s
```

- Baseline (before this work): **160 passed**
- New tests added: **20** (all in `tests/test_phase1_agent_loop.py`)
- Smoke tests extended: `test_every_page_renders` +1 page, `test_no_duplicate_widget_keys` +1 render pass
- Final count: **180 passed, 0 failed**

## Verification Checklist

| Check | Result |
|-------|--------|
| `python -m pytest sagaai/tests -q` | ✅ 180 passed |
| `python -m py_compile` for all new/edited files | ✅ All OK |
| `grep -nP "[А-Яа-яЁё]" sagaai/ui/pages/devagent.py` | ✅ Clean (no Cyrillic user strings) |
| `wc -c sagaai/universal.py` | ✅ 72835 bytes (unchanged) |
| Every `t()` key in `devagent.py` in both `ru.json` and `en.json` | ✅ All 18 keys present in both |

## Test Coverage (test_phase1_agent_loop.py)

1. **parse_tool_calls** — fenced single, fenced multiple, bare object, `name` key,
   flat-arg normalization (subdir ends up under args), nested args no leakage,
   pure prose returns [], invalid JSON returns [], object without tool key returns [].
2. **Loop stops at propose_edit** with `awaiting_approval`; returns path + diff;
   model script: list_files → read_file → propose_edit.
3. **Loop returns `final`** when model emits prose after step > 1.
4. **Loop returns `awaiting_plan_ok`** when model emits prose on step 1.
5. **max_steps guard** — loop stops with `stopped_max_steps` after N iterations.
6. **approve_and_apply** — calls `dispatcher.dispatch("apply_edit", ...)` with correct args.
7. **discard** — calls `dispatcher.dispatch("discard_edit", ...)`.
8. **propose_new_file** also triggers approval gate.
9. **auto_apply=True** does not pause at propose_edit.
10. **Event emission** — assistant_text, tool_call, tool_result, final, stopped_max_steps.
11. **History growth** — assistant + tool-result roles appended correctly each step.

## Deviations from Spec

None. All requirements from AGENT_LOOP_SPEC.md were implemented as specified.
