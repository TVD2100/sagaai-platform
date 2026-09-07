---
id: local_repo_guide
name: Local Repo Guide
description: Guide for working with the local copy of the sagaai-platform repository: where the system prompts and related files live, how to edit them, the file_versions.json manifest workflow, and to offer a GitHub update after finishing work. Load at the start of any task touching this repository copy.
---

# Локальная копия репозитория sagaai-platform - руководство

Эта рабочая папка - локальная копия репозитория **sagaai-platform**
(GitHub: `TVD2100/sagaai-platform`, ветка `main`, публичный (MIT)). Корень рабочей
папки совпадает с корнем репозитория: относительные пути файлов в workspace
и в GitHub **одинаковые**.

---

## Блок 1. Где и как править системные промпты и связанные файлы

### 1.1 Карта файлов

**Системные промпты оркестраторов:**

- `dev_agent/system_prompt.md` - системный промпт DevAgent (этот агент).
  Версия - в первой строке файла: `# DevAgent - System Prompt (vX.Y)`.
  Это каноничная копия промпта в репозитории (в `defaults/orchestrators/`
  системного промпта DevAgent НЕТ - только его инструкции).
- `defaults/orchestrators/ya_agent/system_prompt.md` - системный промпт
  YaAgent. Версия - в первой строке: `# YaAgent - Системный промпт (vX.Y)`
  (сейчас v2.5).
- `defaults/orchestrators/ya_agent/orchestrator.json` - конфигурация YaAgent
  (модели, температура, инструменты).

**Инструкции оркестраторов (формат: front-matter `id`/`name`/`description` + тело):**

- `defaults/orchestrators/dev_agent/instructions/*.md` - встроенные
  инструкции DevAgent: `assistant_creator`, `employee_creator`,
  `github_connector`, `prompt_improver`, `self_reflection`, `skill_developer`
  и `local_repo_guide` (эта инструкция).
- `defaults/orchestrators/ya_agent/instructions/*.md` - инструкции YaAgent
  (`yandex_models_reference`, `agent_security_guardrails`, `agent_tools_mcp`,
  `agents_responses_api`, `fine_tuning_classifiers`, `multimodal_yandex_services`,
  `rag_search_embeddings`, `agent_atelier_agents`).
- `defaults/instructions/*.md` - глобальные инструкции (сейчас
  `github_connector`).

**Рантайм-хранилище (НЕ каноничная копия):**

- Глобальные инструкции: `<DATA_DIR>/orchestrators/global_instructions/<id>.md`.
- Инструкции оркестратора: `<DATA_DIR>/orchestrators/<slug>/instructions/<id>.md`.
- Рантайм-файлы импортируются из `defaults/...` при bootstrap идемпотентно:
  существующие рантайм-файлы НЕ перезаписываются. Поэтому долговечная
  правка = правим каноничный файл в `defaults/...` + регистрируем/обновляем
  рантайм-копию явно (см. 1.3).

**Прочие дефолты:**

- `defaults/services/*.json` - профили сервисов моделей (deepseek, gigachat,
  yandex); рядом `services/*.json` - рантайм-копии.
- `defaults/settings/global.json` - глобальные настройки установки.
- `langs/*.json` и `defaults/langs/*.json` - локализации (ru, en, zh-CN) +
  `*_guide.md` в тех же папках.
- `defaults/assistants/`, `defaults/skills/`, `defaults/rag_bases/` - пресеты
  ассистентов, навыков и RAG-баз.

**Код платформы и тесты:**

- `core/` - бизнес-логика (bootstrap, defaults, orchestrators, instructions,
  skills_library, rag, github_tools, updater и т.д.); `dev_agent/` - пакет
  агента (agent_loop, tool_executor, task_state, workspace_tools);
  `storage/`, `ui/`, `app.py`.
- `tests/` - регрессионный набор (включая `tests/scenarios/`, `tests/smoke/`).

**Манифест версий и конвейер обновлений:**

- `file_versions.json` (корень репо) - манифест версий всех поставляемых
  файлов: units (пакет `core` = исполняемый код) и selectable (промпты,
  инструкции, пресеты, локализации, документация - по одному файлу); semver
  + sha256. Обновляется СТРОГО через `python3 scripts/verify_manifest.py`
  (`--add <путь>` для новых файлов, `--fix-hashes` после правок).
- `scripts/verify_manifest.py` - валидатор/сопроводитель манифеста.
- `core/updater.py` + `core/updater_apply.py` - конвейер обновлений
  (check/stage/apply/rollback, холодный старт); `ui/pages/updates.py` -
  страница «Обновления»; хук применения - в `app.py`.
- `.dev_agent/updates/` - рантайм-хранилище обновлений локальной установки
  (pending/, state.json, health.json, backup/). НЕ коммитится и НЕ
  публикуется.

### 1.2 Правила правки системных промптов

1. Перед правкой всегда читать файл целиком (`read_file`); сохранять
   несвязанный текст без переформатирования.
2. Инструменты: точечные правки в большом файле - `apply_patch` (≤2 правок
   за вызов); полная перезапись / новый файл - `propose_file`. После каждой
   записи - `verify_file`.
3. **Всегда поднимать версию в заголовке**: `# DevAgent - System Prompt
   (v3.7)` → `(v3.8)`; у YaAgent - `# YaAgent - Системный промпт (v2.4)` →
   `(v2.5)`.
4. При синхронизации промптов DevAgent ↔ YaAgent сохранять специфику
   YaAgent: роль специалиста Yandex AI Studio, инструкция
   `yandex_models_reference`, правила RAG §1d, приоритет локального
   `docs/YaAgentAI`, пункт §15 (управление ассистентами/оркестраторами -
   прерогатива DevAgent). Не переносить в YaAgent разделы Assistant /
   Orchestrator management, которых нет в его наборе инструментов.
5. UI-строки - через i18n (`langs/`); код-комментарии - по-английски.
6. После правки промпта - обязательные проверки:
   - инлайн-тест инвариантов: заголовок версии и присутствие ключевых
     блоков в изменённом файле (`run_test(code=...)`);
   - регрессия: `tests/test_preset_orchestrators.py`,
     `tests/test_platform_bootstrap.py`, `tests/test_universal_developer.py`,
     `tests/test_agent_loop_json_repair.py`.

### 1.3 Правка инструкций и дефолтов

- Инструкции DevAgent: правим каноничный файл
  `defaults/orchestrators/dev_agent/instructions/<id>.md`, затем регистрируем
  в рантайме: `save_orchestrator_instruction('dev_agent', '<id>', name=...,
  description=..., prompt_text=<тело без front-matter>)`.
- Глобальные инструкции: правим `defaults/instructions/<id>.md`; рантайм
  обновляем через механизм в `core/instructions.py` (импорт идемпотентен -
  при необходимости сначала удалить/перезаписать рантайм-файл).
- Локализации: после правки `langs/ru.json` прогнать
  `tests/test_i18n_sync.py`, `tests/test_i18n_serialization.py`.
- Сервисы/настройки: после правки дефолтов прогнать
  `tests/test_default_imports.py`, `tests/test_platform_bootstrap.py`.

---

## Блок 2. После завершения работ - предлагать обновление GitHub

1. **Автопуша НЕТ.** После ЛЮБОЙ задачи, менявшей файлы этой локальной
   копии, в финальном отчёте перечислить изменённые файлы (для каждого -
   суть изменения и версия в манифесте «было → стало») и спросить
   пользователя, публиковать ли их на GitHub. Пушить только после явного
   согласия пользователя.
2. **Манифест обязателен.** Любая правка файла этой копии = bump его версии
   в `file_versions.json`, затем `python3 scripts/verify_manifest.py
   --fix-hashes` и контрольный прогон (без ошибок). Новый файл: локальный
   `git add + commit`, затем `python3 scripts/verify_manifest.py --add <путь>`.
   Перед публикацией - полная регрессия (`python3 -m pytest`) и
   `python3 scripts/verify_manifest.py`; ошибки верификатора = блокер.
3. **Процедура публикации:**
   - загрузить инструкцию `github_connector`;
   - изменённые файлы по одному: `github_read_file` (актуальный `sha`) →
     `github_update_file` с sha (или `github_upload_file` для новых файлов);
   - **`file_versions.json` - ПОСЛЕДНИМ** (маркер завершённой публикации;
     при релизе `CHANGELOG.md` публикуется до манифеста);
   - верификация: повторный `github_read_file(file_versions.json)` - сверка с
     локальным манифестом; по выложенным файлам - повторное чтение ключевых
     строк;
   - после успешной публикации - локальный `git add + commit`, чтобы история
     копии отражала состояние GitHub.
4. **Пути идентичны** workspace-корню, например: `dev_agent/system_prompt.md`,
   `defaults/orchestrators/ya_agent/system_prompt.md`,
   `defaults/orchestrators/dev_agent/instructions/local_repo_guide.md`.
5. После push сообщить результат: путь файла, commit SHA (если есть),
   статус верификации.

---

## Как загружать эту инструкцию

`get_orchestrator_instruction('dev_agent', 'local_repo_guide')` - в начале
любой задачи, затрагивающей системные промпты, инструкции, дефолты или
требующей обновления GitHub. Для задач с правками файлов этой копии
придерживайся правил Блока 1 и Блока 2 (манифест, тесты, порядок
публикации).
