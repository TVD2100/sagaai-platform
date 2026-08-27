# Архитектурное описание (ARCHITECTURE)

Документ поддерживается DevAgent и редактируется пользователем.

## Обзор

SagaAI построена по модульной архитектуре с чётким разделением на слои:

| Слой | Пакет | Назначение |
|------|-------|------------|
| Точка входа | `app.py` | Streamlit-конфигурация и запуск UI |
| Интерфейс | `ui/` | Страницы приложения + переиспользуемые компоненты |
| Бизнес-логика | `core/` | Работа с API, файлами, помощниками, тредами, оркестраторами, RAG, i18n - без зависимостей от Streamlit |
| Хранилище | `storage/` | SQLAlchemy ORM-модели, репозиторий, движок БД |
| Агент | `dev_agent/` | Цикл DevAgent, безопасный писатель, инструменты, подбор сервисов для помощников, внешняя память задач |
| Встроенное | `defaults/` | Канонические копии: промпты оркестраторов, переводы, сервисы, пресеты, RAG-базы, навыки |
| Тесты | `tests/` | Полный набор модульных и интеграционных тестов |
| Переводы | `langs/` | Рабочие JSON-файлы с переводами интерфейса (исходники - `defaults/langs/`) |

## Компоненты

### 1. Точка входа (`app.py` → `ui/app.py`)
- `app.py` задаёт конфигурацию страницы Streamlit, вызывает `ensure_data_dirs()`
  для создания структуры данных, затем делегирует управление `ui/app.py:main()`.
- `ui/app.py` инициализирует сессионное состояние Streamlit, загружает
  переменные окружения из shell-профилей (`load_env_from_shell_profiles`),
  выполняет начальное заполнение встроенных оркестраторов
  (`default_imports.ensure_all_defaults` / `ensure_builtin_orchestrators`),
  проверяет опциональные зависимости, рендерит боковую панель с навигацией
  (страницы + динамический список оркестраторов) и выбором языка.

### 2. Бизнес-логика (`core/`)
Каждый модуль - самодостаточный, без Streamlit:

| Модуль | Ключевые функции |
|--------|------------------|
| `api_layer` | HTTP-запросы к AI API (Bearer-токен, GigaChat OAuth, Responses API, тест соединения) |
| `api_errors` | Единая иерархия ошибок API и локализованные сообщения |
| `files` | Определение типов файлов, оценка токенов, извлечение контента |
| `fs` | Низкоуровневые операции: чтение/запись JSON и текста, кодировки, `ensure_dir` |
| `i18n` | Обнаружение языковых файлов, загрузка переводов, функция `t()` |
| `paths` | Структура директорий данных, пути к БД (основная и `devagent.db`), путь к RAG-базам |
| `assistants` / `assistant_folders` | CRUD для помощников и их файлов, folder-based хранение, экспорт/импорт |
| `entity_sync` | Синхронизация «папки - источник истины» → БД-кэш |
| `threads` | Управление тредами помощников: создание, чтение, сообщения, удаление |
| `threads_devagent` | Управление тредами оркестраторов (отдельная БД `devagent.db`) |
| `services` | Обнаружение доступных AI-сервисов из `services/` (фолбэк на `defaults/services/`); RAG-модели |
| `config` | Чтение и запись конфигурации (SQLite KV); DevAgent-настройки проксируются через оркестраторы |
| `env_loader` | Загрузка переменных окружения из shell-профилей |
| `render` | Рендеринг сообщений: Markdown → HTML, кнопка копирования |
| `bootstrap` | Первичная инициализация: встроенный оркестратор `dev_agent`, инструкции, устаревшие миграции |
| `instructions` | CRUD для внутренних инструкций (Assistant Creator, Employee Creator) |
| `orchestrators` | **Ядро оркестраторов**: CRUD, `build_assistant_dicts()`, `get_web_search_config()`, `get_economy_config()`, `export_orchestrator()` / `import_orchestrator()` (формат `sagaai_orchestrator/v1`), `ensure_builtin_orchestrators()` |
| `orchestrator_folders` | Папки оркестраторов: `orchestrator.json`, функции, инструкции (front-matter), экспорт/импорт папок |
| `defaults` / `default_imports` | Чтение `defaults/` и импорт встроенных сущностей «из коробки» (оркестраторы, инструкции, навыки, RAG-базы) |
| `prompt_guard` | Защита от prompt-injection: data-fences и санитизация |
| `dangerous` | Анализ опасного кода для `run_code`/`run_test` |
| `crypto` | Шифрование секретов (Fernet), внешний ключ шифрования |
| `auth` | Опциональная парольная аутентификация |
| `recent_workspaces` | Список недавних рабочих папок |
| `skills_library` | Стандартизированная библиотека навыков: реестр, импорт ZIP/GitHub/папки; модель владения developer/adapted; set_skill_adapted; фильтр неадаптированных навыков |
| `contracts` | Типизированные контракты словарей (AssistantDict, OrchestratorConfig, RAG-контракты и пр.) |
| `tools_utils` | Список определений инструментов для страниц помощников |
| `prompt_improver` | LLM-улучшение промптов помощников на слабой модели DevAgent |
| `rag` / `rag_chunker` / `rag_embeddings` / `rag_index` / `rag_indexer` / `rag_search` | RAG-подсистема: CRUD баз знаний, чанкинг, Yandex Embeddings, локальный векторный индекс, индексация, семантический поиск |

### 3. Компоненты UI (`ui/components/`)

| Компонент | Назначение |
|-----------|------------|
| `workspace_picker` | Универсальный селектор рабочей папки + загрузчик файлов. |

### 3a. Страницы UI (`ui/pages/`)

| Страница | Назначение |
|----------|------------|
| `welcome` | Главная страница с руководствами |
| `assistants` | Управление помощниками (профили, файлы, инструменты, улучшение промптов) |
| `chat` | Чат с помощниками |
| `history` | Единая история диалогов (помощники + оркестраторы) |
| `orchestrator` | Универсальная страница оркестратора (Чат / История / Навыки; настройки - на отдельной странице) |
| `orchestrator_settings` | Отдельная страница настроек оркестратора |
| `orchestrators` | Управление списком оркестраторов (сотрудников) |
| `settings` | Настройки API-ключей и переменных окружения |
| `skills_library` | Библиотека навыков (установка ZIP/GitHub/папки) |
| `storage` | Управление RAG-базами знаний (создание, файлы, индексация, тестовый поиск) |

### 4. Хранилище (`storage/`)
- `models.py` - SQLAlchemy ORM-модели: `Assistant`, `Thread`, `Message`,
  `ConfigKV`, `Instruction`, **`Orchestrator`**, `OrchestratorInstruction`.
- `repository.py` - слой доступа к данным (CRUD с префиксом `repo_`),
  включая `repo_list_orchestrators`, `repo_get_orchestrator_with_text`,
  `repo_create_orchestrator`, `repo_update_orchestrator`,
  `repo_delete_orchestrator`, плюс legacy-алиасы `repo_*_skill`/
  `repo_*_employee`.
- `repository_devagent.py` - CRUD для тредов оркестраторов (отдельная БД).
- `db.py` - фабрики движков SQLite (основная БД `sagaai.db` + БД
  оркестраторов `devagent.db`), авто-миграции схем.

### 5. DevAgent (`dev_agent/`)
- `agent_loop.py` - парсинг вызовов инструментов, цикл с dual-model routing,
  эконом-режим, approval-гейты (план, применение, подтверждение опасных
  операций).
- `assistant_detector.py` - `detect_and_select_assistant()` **всегда
  возвращает пустой результат** (assistant detection отключён).
- `assistant_model_resolver.py` - автоматический подбор сервиса/модели для
  помощников: классификация (strong/weak, web_search), выбор из настроек
  оркестратора или YandexAI.
- `task_state.py` - внешняя память задач: per-thread журнал
  `TASK_STATE__<thread_id>.md` (архитектура, план, прогресс, handoff,
  история завершённых задач), рендер/парсинг, бэкап перед записью.
- `tool_executor.py` - диспетчер инструментов DevAgent: `propose_file`,
  `apply_patch`, `verify_file`, `run_code`/`run_test`, инструменты
  помощников/оркестраторов/навыков, `web_search()`, RAG-инструменты
  (`list_rag_bases`/`rag_search`), history-инструменты.
- `safe_writer.py` - безопасная запись файлов (проверка защищённых файлов,
  staging, бэкап, верификация).
- `backup_manager.py` - управление версиями файлов на основе SHA-256.
- `workspace_tools.py` - инструменты для внешних проектов: set_workspace,
  build_project_map, снапшоты, ведение документации.
- `universal_agent.py` - `load_system_prompt()` (единый файл
  `dev_agent/system_prompt.md`), `build_assistant_dict_from_config()`,
  `UniversalDevAgent` (core + workspace + orchestrator tools).
- `system_prompt.md` - канонический системный промпт DevAgent (v3.6):
  docs-first workflow (перед началом работы читать `PROJECT_MAP.md` И
  `SPEC.md`), обязательная секция «Documentation» в финальном отчёте.
- `config.py` - разрешение путей, защищённые файлы, runtime-директории.
- `llm_utils.py` - унифицированный вызов LLM (контракт assistant-словаря).

### 6. Помощники и инструкции
- **Assistant Creator** - внутренняя инструкция DevAgent-оркестратора
  (id: `assistant_creator`, файл `defaults/orchestrators/dev_agent/instructions/assistant_creator.md`),
  не является пользовательским помощником. Содержит правила генерации
  системных промптов для новых помощников.
- **Prompt Improver** - внутренняя инструкция (id: `prompt_improver`),
  правила улучшения существующих промптов.
- **Автосоздание помощников отключено** - помощники создаются только по
  явному запросу пользователя.

### 7. RAG-подсистема (`core/rag*.py`, `ui/pages/storage.py`)
- **База знаний** - папка `DATA_DIR/rag_bases/<slug>/`:
  `manifest.json`, файлы документов, локальный SQLite-индекс `index.db`.
- **Индексация**: `rag_chunker` (чанкинг) → `rag_embeddings` (Yandex
  Embeddings API, BYOK) → `rag_indexer` (запись в `rag_index`).
- **Поиск**: `rag_search` - косинусное сходство по локальному индексу
  и сборка контекста для LLM.
- **Инструменты DevAgent**: `list_rag_bases()`, `rag_search()`; метаданные
  баз попадают в системный промпт оркестратора (`Available RAG knowledge
  bases`).
- **Навык Rag Base Creator** (`defaults/skills/rag_base_creator/`) описывает
  процедуру создания и индексации баз.

### 8. Библиотека навыков и адаптация (`core/skills_library.py`, `ui/pages/skills_library.py`)
- **Навык** - папка `DATA_DIR/skills/<folder>/` + запись в реестре
  `skills/skills.json` с полями `developer` (правообладатель) и `adapted`
  (статус адаптации под SagaAI).
- **Импорт**: платформенные навыки (из `defaults/skills/`) получают
  `developer=SagaAI`, `adapted=True`; сторонние - `developer=unknown`,
  `adapted=False`.
- **Фильтрация промптов**: `get_enabled_skills_metadata()` возвращает только
  адаптированные навыки; блок `Available skills` в системном промпте
  оркестратора строится из них. Инструмент `list_skills_library` видит все
  навыки.
- **Поток адаптации**: кнопка «Адаптировать» на странице библиотеки
  передаёт задачу в DevAgent (Skill Developer) → DevAgent выполняет
  адаптацию и вызывает инструмент `mark_skill_adapted(skill_id)` →
  `set_skill_adapted()` обновляет реестр, и навык появляется в промпте.

## Поток данных

### Чат с LLM
Пользователь → `ui/pages/chat.py` → `core/api_layer` → LLM API → ответ и
сохранение треда через `storage/repository`.

### Цикл оркестратора (DevAgent)
1. Пользователь ставит задачу на странице оркестратора.
2. `ui/pages/orchestrator.py` создаёт `AgentLoopState`, заполняет
   `strong_assistant`/`weak_assistant` через
   `core/orchestrators.build_assistant_dicts(slug)`.
3. `agent_loop.py` переходит в фазу `calling_llm` и отправляет запрос
   сильной/слабой моделью.
4. LLM → `parse_tool_calls` → `tool_executor` → результаты → повтор до
   терминального статуса (`loop_status` или approval-гейт).

### RAG-запрос
Пользователь (или агент) → `ui/pages/storage.py` / `rag_search()` →
`core/rag_search` → локальный индекс `index.db` → релевантные чанки →
контекст для LLM.

### Навигация оркестраторов
`ui/app.py` → `core/orchestrators.list_orchestrators()` → строит меню:
кастомные оркестраторы (сортировка `sort_order`) и DevAgent. Каждый
оркестратор → `ui/pages/orchestrator.page_orchestrator(slug)`.

### Импорт встроенных сущностей
`defaults/` - канонический источник для дефолтных оркестраторов
(`defaults/orchestrators/*/`, например YaAgent - orchestrator.json +
system_prompt.md), переводов, сервисов, помощников, навыков и RAG-баз
(`defaults/rag_bases/yaagentai_2020/`). Исключение -
системный промпт встроенного DevAgent: он хранится в одном месте -
`dev_agent/system_prompt.md` (рядом с кодом агента), а в
`defaults/orchestrators/dev_agent/` лежат только его инструкции.
Рантайм-папки оркестраторов (DATA_DIR/orchestrators/) создаются при первом
запуске из defaults/ и в репозитории не хранятся.

## Решения и принципы

### Архитектура оркестраторов
- **`core/orchestrators.py`** - ядро: CRUD, assistant-словари,
  экспорт/импорт (core API), bootstrap встроенных оркестраторов.
- **`ui/pages/orchestrator.py`** - универсальная страница,
  параметризуемая slug; отдельные `orchestrators.py`/`orchestrator_settings.py` -
  список и настройки.
- **`storage/models.py:Orchestrator`** - модель с полями `slug`, `prompt_text`,
  `config_json`, `tools`, `max_steps`, `auto_apply`, `is_builtin`, `sort_order`.
- Экспорт/импорт оркестраторов реализован в core API, **UI отложен** -
  кнопки экспорта/импорта в интерфейсе намеренно отсутствуют.
- Старые `load_devagent_config()` / `save_devagent_config()` - прокси на
  оркестратор `dev_agent`.

### Dual-model routing
Агент выбирает модель (strong/weak) на каждом шаге по `classify_step_strength()`.
Модели берутся из конфигурации оркестратора (`config_json`).

### Web-search модель (search_service/model)
Отдельная пара service/model для задач с веб-поиском, хранится в
конфигурации оркестратора; используется `tool_executor.web_search()`.

### Assistant Creator
Внутренняя инструкция с id `assistant_creator`. Содержит правила генерации
промптов. DevAgent читает её через `get_instruction("assistant_creator")`
при создании помощника.

### Автоматический подбор сервиса/модели для помощников (assistant_model_resolver)
1. Классификация задачи (strong/weak, web_search).
2. Явное указание в запросе.
3. Настройки оркестратора (strong/weak).
4. YandexAI (pro/lite по сложности).
5. Fallback - первый доступный сервис.

### Assistant detection (отключён)
`detect_and_select_assistant()` всегда возвращает пустой результат.

### Безопасный писатель
Staging (propose_file) → diff → apply → бэкап; точечные правки - через
`apply_patch` с атомарным откатом при ошибке.

### Внешняя память задач (журнал TASK_STATE__<thread_id>.md)
Для каждой задачи DevAgent ведёт per-thread журнал в скрытой папке проекта
`.dev_agent/task_states/TASK_STATE__<thread_id>.md` (имя содержит ID треда;
ID и путь передаются мета-блоком системного промпта). Файл никогда не
удаляется: по завершении задача архивируется в Task History того же файла,
новая задача в том же треде дописывается туда же. Каждый этап плана может
хранить обобщающий `- context:` (нужен следующему этапу при обрезанной
истории). Журнал автоматически создаётся платформой и инжектится в контекст
перед каждым шагом.

### RAG: локальные базы знаний
Индексация локальная (SQLite + косинусное сходство), эмбеддинги - удалённо
через Yandex Embeddings API (BYOK). Базы хранятся в `DATA_DIR/rag_bases/`.

### Коннекторы
- **`core/connectors.py`** - CRUD подключений: папка
  `DATA_DIR/connectors/<id>/manifest.json`; токен шифруется через
  `core.crypto.encrypt` и не попадает в публичные представления.
- **`core/github_connector.py`** - тонкий адаптер PyGithub (ленивый импорт,
  `GithubConnectorError = ValueError`, `test_connection`, repo/file операции).
- **`core/github_tools.py`** - инструменты `github_list_repos`,
  `github_create_repo`, `github_upload_file`, `github_update_file`,
  `github_read_file` в конвенции `invoke(**kwargs) -> dict`.
- **Привязка к оркестраторам**: `config['enabled_connections']` в
  `core/orchestrators.py`; `_extend_prompt_with_connections` добавляет блок
  `Available service connections` в системный промпт; инструменты
  регистрируются в диспетчере.
- **UI**: `ui/pages/connectors.py` (раздел после «Хранилища»), таб
  «Подключения» в настройках оркестратора.

### Загрузка ключей из shell-профилей
`env_loader` читает ~/.zshrc и др., не перезаписывает существующие.

### Универсальный разработчик
PROJECT_MAP.md, SPEC.md, ARCHITECTURE.md, CHANGELOG.md, снапшоты. Перед
началом работы над проектом читается `PROJECT_MAP.md` (файлы и
ответственность) и `SPEC.md` (требования); финальный отчёт завершается
секцией «Documentation» с перечнем документов, требующих обновления.

### Экспорт/импорт оркестраторов (core API)
Формат `sagaai_orchestrator/v1` (JSON). Slug-конфликты разрешаются
генерацией нового (`slug_2`, ...). Инструкции импортируются без
перезаписи существующих. UI-интерфейс для экспорта/импорта намеренно
отложен.
