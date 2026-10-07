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
| Агент | `dev_agent/` | Цикл DevAgent, безопасный писатель, инструменты, подбор сервисов для помощников, внешняя память задач, изоляция рабочей папки по диалогам (пустое состояние «папка не выбрана», guard файловых инструментов) |
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
| `api_layer` | HTTP-запросы к AI API (Bearer-токен, GigaChat OAuth, Responses API, тест соединения); нормализация GigaChat-payload (в единственное ведущее system-сообщение фолдятся только ведущие system-блоки; mid-history system-блоки остаются на месте с ролью `user`) и кламп max_tokens; разбор кэш-бакетов usage на всех чат-транспортах (bearer/DeepSeek - `prompt_tokens_details.cached_tokens` с фолбэком `prompt_cache_hit_tokens`; GigaChat - `precached_prompt_tokens`); транспорты изображений `send_vision_request` (chat completions для bearer/yandex_iam; выделенный DeepSeek chat/completions для `deepseek_responses` с `detail="original"`) и `send_image_generation_request` (синхронный OpenAI-совместимый Images API YandexAI); детекция `failed`-статуса Responses API (`ProviderResponseError`) и фильтрация пустых input-элементов |
| `api_errors` | Единая иерархия ошибок API и локализованные сообщения |
| `files` | Определение типов файлов, оценка токенов, извлечение контента |
| `fs` | Низкоуровневые операции: чтение/запись JSON и текста, кодировки, `ensure_dir` |
| `i18n` | Обнаружение языковых файлов, загрузка переводов, функция `t()` |
| `paths` | Структура директорий данных, пути к БД (основная и `devagent.db`), путь к RAG-базам |
| `assistants` / `assistant_folders` | CRUD для помощников и их файлов, folder-based хранение, экспорт/импорт |
| `entity_sync` | Синхронизация «папки - источник истины» → БД-кэш |
| `threads` | Управление тредами помощников: создание, чтение, сообщения, удаление |
| `threads_devagent` | Управление тредами оркестраторов (отдельная БД `devagent.db`); поиск по сообщениям (`search_thread_messages`), список диалогов (`list_threads_filtered`) и окно чтения (`read_thread_window`) с контролем доступа (`allowed_slugs`); мета рабочей папки треда и одноразовая миграция легаси-меты |
| `assistant_nav` / `orchestrator_nav` | Сортировка и разбиение списков помощников/сотрудников для сайдбара (5 видимых + «Все (N)»; сортировка сотрудников по последнему диалогу) |
| `services` | Обнаружение доступных AI-сервисов из `services/` (фолбэк на `defaults/services/`); RAG-модели; каталоги vision/image-моделей (`get_vision_models` / `get_image_models`) |
| `config` | Чтение и запись конфигурации (SQLite KV); DevAgent-настройки проксируются через оркестраторы |
| `env_loader` | Загрузка переменных окружения из shell-профилей |
| `render` | Рендеринг сообщений: Markdown → HTML, кнопка копирования |
| `bootstrap` | Первичная инициализация: встроенный оркестратор `dev_agent`, инструкции, устаревшие миграции |
| `instructions` | CRUD для внутренних инструкций (Assistant Creator, Employee Creator) |
| `orchestrators` | **Ядро оркестраторов**: CRUD, `build_assistant_dicts()`, `get_web_search_config()`, `get_economy_config()`, `export_orchestrator()` / `import_orchestrator()` (формат `sagaai_orchestrator/v1`), `ensure_builtin_orchestrators()` |
| `orchestrator_tools` | Каталог инструментов оркестраторов: core + workspace + подключения + кастомные функции; англоязычный блок `## Available tools` для системного промпта (без отключённых инструментов); `disabled_tools` blacklist, разрешение legacy-алиасов, `list_system_tools()` для UI |
| `orchestrator_folders` | Папки оркестраторов: `orchestrator.json`, функции, инструкции (front-matter), экспорт/импорт папок |
| `defaults` / `default_imports` | Чтение `defaults/` и импорт встроенных сущностей «из коробки» (оркестраторы, инструкции, навыки, RAG-базы) |
| `prompt_guard` | Защита от prompt-injection: data-fences и санитизация |
| `dangerous` | Анализ опасного кода для `run_code`/`run_test` |
| `crypto` | Шифрование секретов (Fernet), внешний ключ шифрования |
| `auth` | Опциональная парольная аутентификация |
| `recent_workspaces` | Недавние рабочие папки в скоупе оркестратора: dict `{slug: [...]}` с сидом из собственных тредов и фильтром платформенных путей (легаси-плоский список читается как `dev_agent`) |
| `skills_library` | Стандартизированная библиотека навыков: реестр, импорт ZIP/GitHub/папки; модель владения developer/adapted; set_skill_adapted; фильтр неадаптированных навыков |
| `contracts` | Типизированные контракты словарей (AssistantDict, OrchestratorConfig, RAG-контракты и пр.) |
| `tools_utils` | Список определений инструментов для страниц помощников |
| `prompt_improver` | LLM-улучшение промптов помощников на основной модели DevAgent |
| `rag` / `rag_chunker` / `rag_embeddings` / `rag_index` / `rag_indexer` / `rag_search` | RAG-подсистема: CRUD баз знаний, чанкинг, Yandex Embeddings, локальный векторный индекс, индексация, семантический поиск |
| `updater` | Конвейер обновлений: fetch_manifest, check_updates, stage_updates, apply_updates, rollback_updates; CLI check/stage/apply/rollback (raw-канал без токена) |
| `updater_apply` | Чистый stdlib cold-start апплаер: атомарная запись, бэкапы, откат; хранилище `.dev_agent/updates/` (pending/, state.json, health.json) |

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
| `updates` | Страница обновлений платформы: проверка, выборочная установка (selectable-файлы, пакеты), применение и откат |

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
- `agent_loop.py` - парсинг вызовов инструментов, цикл с единой основной
  моделью, эконом-режим, approval-гейты (план, применение, подтверждение опасных
  операций); в режиме pre-approved autonomous mode фаза утверждения плана
  пропускается по явному выбору пользователя.
- `assistant_detector.py` - `detect_and_select_assistant()` **всегда
  возвращает пустой результат** (assistant detection отключён).
- `assistant_model_resolver.py` - автоматический подбор сервиса/модели для
  помощников: классификация (сложность, web_search); без веб-поиска -
  основная модель из настроек оркестратора, с веб-поиском - YandexAI
  (pro/lite по сложности).
- `task_state.py` - внешняя память задач: per-thread журнал
  `TASK_STATE__<thread_id>.md` (архитектура, план, прогресс, handoff,
  история завершённых задач), рендер/парсинг, бэкап перед записью. При
  пустом состоянии «папка не выбрана» журнал живёт в папке диалога
  (`history/<tid>/task_states/`). Компактный дайджест
  (`read_task_state(compact=True)`) и бюджеты секций ограничивают
  инъекцию `CURRENT TASK STATE`; полное чтение - по требованию.
- `tool_executor.py` - диспетчер инструментов DevAgent: `propose_file`,
  `apply_patch`, `verify_file`, `run_code`/`run_test`, инструменты
  помощников/оркестраторов/навыков, `web_search()`, RAG-инструменты
  (`list_rag_bases`/`rag_search`), мультимодальные инструменты
  (`analyze_image`/`generate_image`), history-инструменты.
- `safe_writer.py` - безопасная запись файлов (проверка защищённых файлов,
  staging, бэкап, верификация).
- `backup_manager.py` - управление версиями файлов на основе SHA-256.
- `workspace_tools.py` - инструменты для внешних проектов: set_workspace,
  build_project_map (AST-разбор символов с regex-фолбэком, лимит 200
  символов на файл, фильтр состава `include_paths`/`scope_note`,
  sha256-отпечаток содержимого в шапке, extra-секция для файлов вне
  текстового скана), снапшоты, ведение документации.
- `workspace_binding.py` - привязка рабочей папки к потоку диалога:
  per-thread реестр состояний под RLock, контекст-менеджер
  `thread_context` (снапшот конфига -> применение состояния потока ->
  восстановление), `ensure_thread_active`; персистентность привязок
  делегирована `core.threads_devagent`.
- `universal_agent.py` - `load_system_prompt()` (единый файл
  `dev_agent/system_prompt.md`), `build_assistant_dict_from_config()`,
  `UniversalDevAgent` (core + workspace + orchestrator tools).
- `system_prompt.md` - канонический системный промпт DevAgent (v3.19):
  docs-first workflow (перед началом работы читать `AGENT.md`, если он есть,
  затем `PROJECT_MAP.md`), обязательная секция «Documentation» в финальном отчёте,
  режим pre-approved autonomous mode (при подробном ТЗ агент явно
  спрашивает «автономно или с согласованием плана»; при выборе «без
  согласования» план составляется, но фаза утверждения пропускается).
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
  и сборка контекста для LLM; заголовки контекста несут `chunk_id` и
  0-based позицию найденного чанка.
- **Добор контекста**: `rag_get_chunks` - выборка конкретных чанков без
  эмбеддингов (по `chunk_ids` или `source` + `chunk_indices`) поверх
  `core.rag_index.fetch_chunks`; доступен и DevAgent, и function-циклу
  ассистентов.
- **Инструменты DevAgent**: `list_rag_bases()`, `rag_search()`,
  `rag_get_chunks()`; метаданные баз попадают в системный промпт
  оркестратора (`Available RAG knowledge bases`).
- **Статистика без полных сканов**: счётчики чанков/эмбеддингов кэшируются
  в meta-таблице `index.db` и обновляются инкрементально при каждой записи;
  API `list_bases` / `get_base_slug` / `list_bases_with_activity` принимают
  `with_stats` - горячие пути рендера и API запрашивают базы без статистики,
  полный расчёт выполняется только при явном запросе одной базы или на
  странице «Хранилище».
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
   `strong_assistant` (единая модель; `weak_assistant` - совместимый
   алиас-копия) через `core/orchestrators.build_assistant_dicts(slug)`.
3. `agent_loop.py` сохраняет сообщение пользователя в историю, переходит
   в фазу `calling_llm` и отправляет запрос основной моделью; при
   обрыве связи (`RequestTimeoutError` / `NetworkError`) запрос повторяется
   прозрачно (`retry_call` в `core/api_layer.py`), в ленту чата идёт событие
   `retrying_llm`.
4. Перед шагом LLM `refresh_context_snapshots` добавляет в историю
   изменившиеся снапшоты task state / thread context скрытыми
   system-сообщениями (append-only: проводная последовательность
   запроса N+1 - строгое расширение запроса N); если последняя копия
   снапшота вытеснена из окна, в хвост добавляется одна свежая копия.
5. LLM → `parse_tool_calls` → `tool_executor` → результаты → повтор до
   терминального статуса (`loop_status` или approval-гейт).

### RAG-запрос
Пользователь (или агент) → `ui/pages/storage.py` / `rag_search()` →
`core/rag_search` → локальный индекс `index.db` → релевантные чанки →
контекст для LLM.

### Навигация оркестраторов
`ui/app.py` → `core/orchestrators.list_orchestrators()` + треды
`core/threads_devagent.list_devagent_threads()` → `core/orchestrator_nav.py`
строит меню: первые 5 сотрудников (сортировка по времени последнего
диалога, иначе по времени создания - новый сотрудник вверху), остальные -
в свёрнутом блоке «Все (N)»; поле поиска появляется только при количестве
сотрудников > 5. Каждый оркестратор →
`ui/pages/orchestrator.page_orchestrator(slug)`. Секция «Помощники»
работает по тем же правилам (`core/assistant_nav.py`).

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
- Правки промпта встроенного оркестратора (DevAgent) переживают перезапуск:
  при редактировании промпта через UI `save_orchestrator()` ставит в config
  маркер `prompt_user_edited`, и `ensure_builtin_orchestrators()` на старте не
  перезаписывает `prompt_text` из `dev_agent/system_prompt.md`, пока маркер
  установлен. Сброс маркера и возврат к поставляемому промпту -
  `reset_builtin_prompt(slug)` (кнопка сброса на вкладке «Промпт»). Прочие
  разделы настроек (модели, эконом-режим, функции, max_steps, навыки,
  подключения) старт не затрагивает: они бэкфиллятся только при отсутствии
  значения.

### Единая модель и модели изображений
Агент использует единую основную модель на всех шагах; служебная
классификация `classify_step_strength()` сохраняется для журналирования и
модель не переключает. Дополнительно в конфигурации оркестратора могут быть
назначены `vision_service`/`vision_model` (распознавание изображений) и
`image_service`/`image_model` (генерация изображений); пустые значения -
«не назначена». Legacy-ключи `weak_*` читаются как алиасы основной модели.
Назначенные модели используются системными инструментами `analyze_image`
(файлы проекта и вложения диалога; JPEG/PNG/WebP, до 5 изображений и
10 МБ каждое; каталоги `vision_models`: YandexAI - `qwen3.6-35b-a3b` и `deepseek-v4.1-flash`, DeepSeek - `deepseek-flash` через выделенный `vision_base_url` с `detail="original"`) и `generate_image` (синхронный OpenAI-совместимый Images API
YandexAI, единственная модель `aliceai-image-art-3.0`; результат сохраняется
файлом - в workspace или в папку файлов диалога, base64 не возвращается -
и показывается в чате с кнопкой скачивания) через `core/multimodal.py`
и транспорты `core/api_layer.py`.
Правила применения, сценарии «модель не назначена» / «модель не умеет» и
согласие на установку зависимостей - в глобальной инструкции
`multimodal_mode` (`defaults/instructions/multimodal_mode.md`).

### Защиты цикла агента от зацикливания
- `AgentLoopState.tool_fail_counts` - счётчик подряд идущих отказов по каждому
  инструменту (без учёта аргументов). После `_MAX_CONSECUTIVE_ERRORS` (3)
  неудачных вызовов одной функции ошибка дополняется подсказкой перейти по
  fallback-цепочке (`apply_patch` → `propose_file` → `run_code`), а успешный
  вызов сбрасывает счётчик инструмента.
- Детекторы `_identical_block_count` / `_dominant_duplicate_call` определяют
  флуд одинаковых вызовов в одном ответе LLM; такой поток схлопывается в одну
  компактную ошибку `identical_blocks`/`identical_calls` без диспатча.
- **Батч-протокол вызовов.** `parse_tool_calls()` разбирает КАЖДЫЙ
  фенс-блок сообщения по отдельности; независимые вызовы (read-only и
  прочие, см. условия в промпте) исполняются по порядку, а результаты
  склеиваются в одно user-сообщение. Если часть блоков не распарсилась,
  `AgentLoopState.partial_parse_warning` (собирается в parsing-фазе,
  дописывается в executing-фазе один раз) возвращает рядом с результатами
  предупреждение `partial_batch` с диагностикой по каждому сломанному
  блоку (`_unparsed_tool_json_diagnostics`: truncation, несбалансированные
  скобки/строки, невалидный JSON). Диагностика срабатывает только когда
  есть исполненные вызовы и ни один из них не был JSON-отремонтирован,
  чтобы не дублировать авто-ремонт.
- Системный промпт DevAgent v3.19 дополняет механизм правилами: закрытый шаг
  не пересказывается, не более 3 попыток на функцию, прозовый
  `loop_status: continue` продолжает цикл, независимые вызовы можно
  батчить, ветка принятия готового плана пользователя дословно;
  восстановление состояния - по компактному дайджесту
  (`task_state_read(compact=True)`), дисциплина микровызовов - окна
  `read_file(offset/limit)` и `PRAGMA table_info` перед прямым SQL.

### Прозрачные ретраи при обрывах связи
LLM-запросы обёрнуты в `retry_call` (`core/api_layer.py`): повторяются
только `RequestTimeoutError` и `NetworkError`, пауза между попытками -
`SAGAAI_NETWORK_RETRY_DELAY` (по умолчанию 30 с), максимум
`SAGAAI_NETWORK_RETRY_ATTEMPTS` попыток подряд (по умолчанию 3); последнее
исключение помечается атрибутом `attempts`. Колбэк `retry_callback`
протягивается из `dev_agent/agent_loop.py` через `core/api_layer._do_request`
и генерирует событие `retrying_llm`, которое рендерит
`ui/pages/orchestrator.py` (i18n-ключ `orch_retry_llm`). POST-запросы Yandex
Responses (`core/assistant_tools.py`) обёрнуты тем же механизмом. Сообщение
пользователя персистится в историю ДО вызова модели - при исчерпании всех
попыток диалог не теряется, а продолжается со следующего сообщения.

### Web-search модель (search_service/model)
Отдельная пара service/model для задач с веб-поиском, хранится в
конфигурации оркестратора; используется `tool_executor.web_search()`.
У DeepSeek встроенный инструмент `web_search` в Responses API молча
игнорируется, поэтому веб-поиск для DeepSeek выполняется отдельным
запросом к Anthropic-совместимому endpoint (`web_search_base_url`,
`https://api.deepseek.com/anthropic/v1/messages`) с моделью
`deepseek-flash`; ветка выбирается в `tool_executor.web_search()` по
`auth_type == "deepseek_responses"`.

### Assistant Creator
Внутренняя инструкция с id `assistant_creator`. Содержит правила генерации
промптов. DevAgent читает её через `get_instruction("assistant_creator")`
при создании помощника.

### Автоматический подбор сервиса/модели для помощников (assistant_model_resolver)
1. Классификация задачи (сложность - только для выбора pro/lite при
   веб-поиске; web_search).
2. Явное указание в запросе.
3. Настройки оркестратора (основная модель).
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

### Изоляция рабочей папки и пустое состояние
Активная папка привязана к потоку диалога: реестр состояний -
`dev_agent/workspace_binding.py` (RLock, `thread_context`, `ensure_thread_active`),
персистентность - мета треда `workspace`/`target_file` в `devagent.db`
(`core/threads_devagent.py`, `storage/repository_devagent.py`). Новый или
сброшенный диалог стартует в пустом состоянии «папка не выбрана»:
`config.NEUTRAL_ROOT` + `WORKSPACE_SELECTED=False`; файловые инструменты
отвечают структурной ошибкой `workspace_not_selected` (guard в
`dev_agent/tool_executor.py` и `dev_agent/universal_agent.py`), а
`run_code`/`run_test` с `code=` выполняются с нейтральным cwd. Диалог без
папки или с исчезнувшей папкой открывается пустым; одноразовая миграция
очищает легаси-мету тредов с платформенными корнями. Недавние папки
(`core/recent_workspaces.py`) скоупятся по оркестратору; журналы задач и
вложения диалога при пустой папке живут в `history/<tid>/`.

### RAG: локальные базы знаний
Индексация локальная (SQLite + косинусное сходство), эмбеддинги - удалённо
через Yandex Embeddings API (BYOK). Базы хранятся в `DATA_DIR/rag_bases/`.

### Коннекторы
- **`core/connectors.py`** - CRUD подключений: папка
  `DATA_DIR/connectors/<id>/manifest.json`; токен шифруется через
  `core.crypto.encrypt` и не попадает в публичные представления.
- **`core/github_connector_rest.py`** - единственный прямой REST-коннектор
  (requests, REST API v3, `X-GitHub-Api-Version: 2022-11-28`, Bearer).
  Реализует repo CRUD, чтение/запись файлов и метаданных, плюс Git Data API:
  `get_ref`, `get_commit`, `get_tree`, а также оптимизированная пакетная публикация
  `batch_commit` (blobs -> одно дерево с чанкингом по 9000 записей -> один
  коммит -> PATCH ref; ветка создаётся через POST /git/refs для пустого
  репозитория) и `batch_upsert` (дифф по blob-SHA с пропуском неизменённых).
  Ошибки - `GithubRestError` (наследник ValueError) со статусом; токен
  расшифровывается внутри модуля и не попадает в результаты/ошибки.
- **`core/github_tools_rest.py`** - инструменты `ghr_*` (15 штук:
  `ghr_list_repos`, `ghr_create_repo`, `ghr_read_file`, `ghr_upload_file`,
  `ghr_update_file`, `ghr_delete_file`, `ghr_list_files`, `ghr_batch_commit`,
  `ghr_batch_upsert`, `ghr_test_connection`, `ghr_get_repo_info`,
  `ghr_read_file_meta`, `ghr_get_ref`, `ghr_get_commit`, `ghr_get_tree`)
  и каталог `get_tools()`; `files` для batch принимается списком dict'ов
  или JSON-строкой.
- **`core/ssh_connector.py`** - SSH/SFTP-коннектор (paramiko + SFTP, ленивый
  импорт): `test_connection`, `exec_command` (таймаут 60 с, максимум 300 с;
  обрезка stdout/stderr до 100 КиБ), `list_dir`, `read_file` (UTF-8, до
  256 КиБ), `write_file` (до 1 МиБ, `create_dirs`), `upload_file`
  (потоковая загрузка локального файла, до 50 МиБ, sha256-верификация).
  Ошибки - `SSHConnectorError` (наследник ValueError) с понятными
  сообщениями;
  секреты расшифровываются внутри модуля и не попадают в ошибки.
- **`core/ssh_tools.py`** - инструменты `ssh_*` для оркестраторов (5 штук:
  `ssh_exec`, `ssh_list_dir`, `ssh_read_file`, `ssh_test_connection`,
  `ssh_write_file`) и каталог `get_tools()`; результат - `{'ok': True,
  'result': ...}` или `{'ok': False, 'error': ...}`.
- **Привязка к оркестраторам**: `config['enabled_connections']` в
  `core/orchestrators.py`; `_extend_prompt_with_connections` добавляет блок
  `Available service connections` в системный промпт и каталог инструментов
  по сервисам включённых соединений (`github_rest` ->
  `github_tools_rest.get_tools()`); инструменты регистрируются в диспетчере
  (`dev_agent/universal_agent.py`); инструкция `github_connector` доступна
  для этого сервиса.
- **UI**: `ui/pages/connectors.py` (раздел после «Хранилища») - создание
  соединений GitHub (REST), тест соединения через `github_connector_rest`;
  таб «Подключения» в настройках оркестратора.

### Загрузка ключей из shell-профилей
`env_loader` читает ~/.zshrc и др., не перезаписывает существующие.

### Универсальный разработчик
PROJECT_MAP.md, AGENT.md, ARCHITECTURE.md, CHANGELOG.md, снапшоты. Перед
началом работы над проектом читается `AGENT.md` (ключевая информация для
агентов), затем `PROJECT_MAP.md` (файлы и
ответственность); финальный отчёт завершается
секцией «Documentation» с перечнем документов, требующих обновления.
Карта проекта - генерируемый документ: состав ограничен публикуемыми файлами,
sha256-отпечаток шапки сигнализирует об устаревании, ручная правка запрещена -
обновление только регенерацией.

### Каталог инструментов и gating (`core/orchestrator_tools.py`)
Блок `## Available tools` добавляется к промпту каждого оркестратора
(`_extend_prompt_with_tools` -> `render_available_tools_block`) и перечисляет
доступные инструменты (core, workspace, подключения, кастомные функции).
Каноничные промпты (`dev_agent/system_prompt.md` v3.19, YaAgent v2.11) не
дублируют этот справочник: их раздел справочника ссылается на
автоматический блок, оставляя в промпте только собственные правила
использования инструментов (форматы вызова, fallback-цепочки).
Чёрный список `config['disabled_tools']` исключает инструменты из каталога,
а диспетчер (`dev_agent/universal_agent.py`) блокирует их вызов ошибкой
`disabled: true` (гейт до выполнения; legacy-алиасы в обе стороны). Вкладка
Функции показывает системные функции с чекбоксами и кнопкой Сохранить.

### Поиск по диалогам (thread-search tools)
Три инструмента оркестраторов открывают историю диалогов: `search_in_threads`
(поиск по сообщениям), `list_threads` (список диалогов с пагинацией и
счётчиками) и `read_thread` (окно сообщений с абсолютными индексами). Без
`thread_id` все три работают с текущим диалогом
(`dev_agent.config.ACTIVE_THREAD_ID`) и не требуют предварительных
листингов; `scope` расширяет охват до одного оркестратора, списка
оркестраторов или всех диалогов. Read-side слой -
`core/threads_devagent.py` (`search_thread_messages`, `list_threads_filtered`,
`read_thread_window`), SQL-выборки - в `storage/repository_devagent.py`
(`repo_devagent_list_threads_filtered`, `repo_devagent_count_messages`,
`repo_devagent_load_threads_messages`, `repo_devagent_load_messages_window`).
Контроль доступа задаёт параметр `allowed_slugs`: DevAgent (`dev_agent`) видит
все диалоги, остальные оркестраторы - только свои; чужой `thread_id`
получает `Access denied`. Скрытые служебные сообщения (`tool_result` /
`AUTO_CONTINUE`) исключаются из поиска и чтения по умолчанию.

### Защита от переполнения контекста LLM (M1-M5)
Пятиуровневая защита от HTTP 400 «context length exceeded» (инцидент
1 053 249 > 1 048 576 токенов из-за tool_result на 1,65 млн символов):

- **M1 - политика размера результатов инструментов (спилл до отправки).**
  `dev_agent/tool_executor.py` и `dev_agent/universal_agent.py` пропускают
  результат через `_apply_tool_result_cap`: документ больше
  `_TOOL_RESULT_INLINE_LIMIT` (35 000 символов) до первой отправки
  записывается целиком в `<workspace>/.dev_agent/tool_results/` (ротация
  до 100 последних файлов), а модели уходит ограниченное превью
  head+tail со `spill_path` (полный payload восстанавливается через
  `read_file` с offset/limit). Аварийный фолбэк: если запись спилла не
  удалась, результат выше `MAX_TOOL_RESULT_CHARS` (200 000) заменяется
  ошибкой `result_too_large`/`result_size` без payload.
- **M2 - персист скрытых tool_result с сохранением проводной формы
  (resume-идентичность).** `summarize_tool_result_for_storage`
  (`dev_agent/agent_loop.py`) + оба пути записи `core/threads_devagent.py`
  обрабатывают каждое сообщение пакета по отдельности: документы до
  50 000 символов (включая все спилл-превью) сохраняются в БД
  байт-в-байт, так что после reload треда отправляемая форма совпадает с
  записанной; более крупные документы (аварийный путь) сжимаются до
  сводки (status, path, applied, размеры bulk-полей).
- **M3 - бюджет истории эконом-режима (порог + гистерезис).**
  `build_economy_context` (`dev_agent/agent_loop.py`) +
  `_enforce_history_token_budget`: бюджет - минимум из легаси-доли
  (0.8×окна минус лимит вывода) и эффективного предела истории из
  context_guard (`history_cap(...) - 2048`); под бюджетом список не
  меняется, при превышении ведущие сообщения режутся за один проход до
  0.75×бюджета; в кэш-режиме срез сворачивается в `economy_anchor`
  (фронт окна стабилен). Служебная head-мета сохраняется всегда.
- **M4/M5 - pre-flight guard в api_layer.** `core/context_guard.py`
  (apply_context_guard) + `ContextWindowError` в `core/api_errors.py`;
  вызов из `send_request` до отправки. Резерв вывода -
  `min(max_out, max(4096, 0.25×окна))`; служебный head-блок эконом-меты
  не срезается; мягкий трим на 0.5 окна, жёсткий предел 0.8 окна,
  понятная ошибка без нового диалога; публичный `history_cap(window,
  max_tokens, prompt_tokens)` отдаёт эффективный предел истории.

### Экспорт/импорт оркестраторов (core API)
Формат `sagaai_orchestrator/v1` (JSON). Slug-конфликты разрешаются
генерацией нового (`slug_2`, ...). Инструкции импортируются без
перезаписи существующих. UI-интерфейс для экспорта/импорта намеренно
отложен.
