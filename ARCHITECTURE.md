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
- После диспетчеризации страницы `main()` вызывает `render_flash()`
  (`ui/components/flash.py`): очередь уведомлений рисуется ОДИН раз за проход и
  именно в конце, поэтому подтверждение сохранения появляется под содержимым
  страницы, а не над её заголовком. Очередь живёт в `session_state` и переживает
  `st.rerun()`, который страница вызывает сразу после сохранения.

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
| `threads_devagent` | Управление тредами оркестраторов (отдельная БД `devagent.db`); поиск по сообщениям (`search_thread_messages`), список диалогов (`list_threads_filtered`) и окно чтения (`read_thread_window`) с контролем доступа (`allowed_slugs`); мета рабочей папки треда и одноразовая миграция легаси-меты; `read_thread_file` извлекает текст документов вложений через `core.document_text` |
| `assistant_nav` / `orchestrator_nav` | Сортировка и разбиение списков помощников/сотрудников для сайдбара (5 видимых + «Все (N)»; сортировка сотрудников по последнему диалогу) |
| `services` | Обнаружение доступных AI-сервисов из `services/` (фолбэк на `defaults/services/`); RAG-модели; каталоги vision/image-моделей (`get_vision_models` / `get_image_models`) |
| `config` | Чтение и запись конфигурации (SQLite KV); DevAgent-настройки проксируются через оркестраторы |
| `env_loader` | Загрузка переменных окружения из shell-профилей |
| `render` | Рендеринг сообщений: Markdown → HTML, кнопка копирования |
| `bootstrap` | Первичная инициализация: встроенный оркестратор `dev_agent`, инструкции, устаревшие миграции |
| `instructions` | CRUD для внутренних инструкций (Assistant Creator, Employee Creator) |
| `orchestrators` | **Ядро оркестраторов**: CRUD, `build_assistant_dicts()`, `get_web_search_config()`, `get_economy_config()`, `export_orchestrator()` / `import_orchestrator()` (формат `sagaai_orchestrator/v1`), `ensure_builtin_orchestrators()`; права делегирования - `get_allowed_agents` / `set_allowed_agents`, `get_allowed_assistants` / `set_allowed_assistants` (список помощников сверяется с живыми карточками) и `get_delegation_limits` / `set_delegation_limits` (шесть лимитов с клампом на записи) |
| `document_text` | Извлечение текста из документов на диске и во вложениях: PDF, DOCX, DOC, PPTX, XLSX, XLS, RTF, ODT, HTML и plain text через `extract_text(filename, data)`, плюс `read_document_path(path, offset, limit, max_chars)` - чтение файла с окном строк. Неизвестный формат или отсутствующая библиотека возвращаются предупреждением, а не исключением, поэтому вызывающий инструмент всегда отвечает текстом |
| `orchestrator_tools` | Каталог инструментов оркестраторов: core + workspace + подключения + кастомные функции; англоязычный блок `## Available tools` для системного промпта (без отключённых инструментов); `disabled_tools` blacklist, разрешение legacy-алиасов, `list_system_tools()` для UI |
| `orchestration/*` | Мультиагентная оркестрация запусков: контракты конвейера (`contracts` - `AgentBrief`, `SpawnRequest`, `InvokeRequest`, `AgentRunResult`, `CancelReport`, `Artifact`, сериализация в dict), одноразовый вызов помощника без цикла инструментов (`invoke` - `invoke_once`), реестр запусков с деревом, пулом, лимитами, таймаутами и каскадной отменой (`service` - `OrchestrationService`), обёртки инструментов `spawn_agent` / `invoke_assistant` с правами доступа (`tools`) |
| `orchestrator_folders` | Папки оркестраторов: `orchestrator.json`, функции, инструкции (front-matter), экспорт/импорт папок |
| `defaults` / `default_imports` | Чтение `defaults/` и импорт встроенных сущностей «из коробки» (оркестраторы, инструкции, навыки, RAG-базы) |
| `prompt_guard` | Защита от prompt-injection: data-fences и санитизация |
| `dangerous` | Анализ опасного кода для `run_code`/`run_test` |
| `crypto` | Шифрование секретов (Fernet), внешний ключ шифрования |
| `auth` | Опциональная парольная аутентификация |
| `recent_workspaces` | Недавние рабочие папки в скоупе оркестратора: dict `{slug: [...]}` с сидом из собственных тредов (до 10 позиций, опциональный лимит) и фильтром платформенных путей (легаси-плоский список читается как `dev_agent`) |
| `folder_dialog` | Нативный выбор папки на компьютере (Streamlit не имеет своего): tkinter в дочернем процессе с откатом на `osascript` в macOS; вердикт через временный UTF-8 JSON, таймаут, деградация без GUI |
| `feed_autoscroll` | Клиентская авто-прокрутка ленты чата: слежение за выводом агента, пауза при ручной прокрутке вверх (sessionStorage), возобновление у низа; отступ снизу не задаёт (Streamlit 1.66 сам резервирует место под липкой панелью) |
| `dependency_check` | Проверка зависимостей на холодном старте: реестр пакетов с минимальными версиями (синхронизирован с `requirements.txt`), чтение установленных версий через `importlib.metadata`; отчёт `missing`/`outdated` для предупреждения пользователю |
| `skills_library` | Стандартизированная библиотека навыков: реестр, импорт ZIP/GitHub/папки; модель владения developer/adapted; set_skill_adapted; фильтр неадаптированных навыков |
| `contracts` | Типизированные контракты словарей (AssistantDict, OrchestratorConfig, RAG-контракты и пр.) |
| `tools_utils` | Список определений инструментов для страниц помощников |
| `prompt_improver` | LLM-улучшение промптов помощников на основной модели DevAgent |
| `rag` / `rag_bm25` / `rag_chunker` / `rag_embeddings` / `rag_index` / `rag_indexer` / `rag_search` | RAG-подсистема: CRUD баз знаний, чанкинг, собственная лексическая BM25-реализация, Yandex Embeddings, локальный индекс, индексация, поиск по режиму базы (bm25/vector/hybrid) |
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
  пропускается по явному выбору пользователя. Цикл несёт идентичность запуска
  (`run_id`/`parent_run_id`/`depth` в `state`) и умеет fan-out в фазе
  `executing`: независимые вызовы `spawn_agent` проверяются на права и
  аргументы ДО захвата пула, затем выполняются параллельно, а их события
  уходят в ленту с `run_id` ребёнка.
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
  Идентификатор треда передаётся явно (`current_thread_id(thread_id)`,
  `ensure_task_state_file`/`archive_and_start_task`), а `config.ACTIVE_THREAD_ID`
  остаётся фолбэком; контекст-менеджер `thread_scope` пинит тред в текущем
  потоке Python, чтобы параллельные дочерние запуски не перезаписывали журнал
  друг друга.
- `tool_executor.py` - диспетчер инструментов DevAgent: `propose_file`,
  `apply_patch`, `verify_file`, `run_code`/`run_test`, `read_document`
  (текст документа по пути с окном строк, через `core.document_text`),
  инструменты помощников/оркестраторов/навыков, `web_search()`, RAG-инструменты
  (`list_rag_bases`/`rag_search`), мультимодальные инструменты
  (`analyze_image`/`generate_image`), history-инструменты. Описание `run_code`
  помечено как средство последней очереди: для документов, исходников, текста,
  изображений и вложений диалога есть специализированные инструменты.
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
  делегирована `core.threads_devagent`. Дочерний запуск получает
  собственное связывание через `bind_child_thread` (рабочая папка родителя,
  свой производный тред `{parent}__s{run_id[:8]}`, без `target_file`) и
  освобождает его `unbind_thread` по завершении.
- `universal_agent.py` - `load_system_prompt()` (единый файл
  `dev_agent/system_prompt.md`), `build_assistant_dict_from_config()`,
  `UniversalDevAgent` (core + workspace + orchestrator tools).
- `system_prompt.md` - канонический системный промпт DevAgent (v3.20):
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
  `manifest.json` (поле `search_mode`), файлы документов, локальный
  SQLite-индекс `index.db`.
- **Режимы поиска** (`search_mode` в манифесте; отсутствие поля = `bm25`,
  миграция на чтении): `bm25` - собственная лексическая реализация
  (`core/rag_bm25`: Unicode-токенизация ru/en, k1=1.5/b=0.75, без
  API-ключей); `vector` - косинусное сходство по эмбеддингам;
  `hybrid` - Reciprocal Rank Fusion (k=60) обоих ранжирований с
  деградацией до `bm25` при недоступности эмбеддингов. Хиты несут `mode`.
- **Анализ текста в лексическом поиске** (схема `bm25_version = 3`,
  полезависимый BM25F): кириллица стеммится консервативным суффиксным
  стеммером (своя реализация, без внешних зависимостей; латиница не
  трогается), служебные слова отбрасываются (русский/английский
  стоп-список). Тело чанка и его самоописывающая шапка (`Документ`/
  `Продукт`/`Заголовок`/`Раздел`) индексируются как ДВА отдельных поля
  со своими df и средней длиной: повтор шапки в каждом чанке обрушивает
  IDF только внутри поля шапки, а IDF доменного термина тела
  сохраняется; при этом имя продукта или раздела, встречающееся лишь в
  шапке, остаётся находимым (раньше такой чанк был недостижим).
  Служебный `Фрагмент` (маркер N/M) в индекс не входит. Многословные
  запросы получают координационный множитель (совпадение всех
  содержательных терминов ранжируется выше частичного). Индексы старой
  схемы (v1/v2, а также таблицы без поля `field`) пересобираются лениво
  один раз.
- **Активность базы** (`active`) учитывает режим: `bm25` и `hybrid`
  остаются активными и без ключей провайдера (`hybrid` работает по своей
  лексической половине), ключи требуются только режиму `vector`.
- **Индексация**: `rag_chunker` (чанкинг) → `rag_indexer` пишет чанки в
  `rag_index` всегда и поддерживает BM25-структуры (`core/rag_bm25`;
  ленивая первая сборка для баз старых версий, далее инкрементальные
  хуки add/update/delete); в режимах `vector`/`hybrid` дополнительно
  получает эмбеддинги Yandex Embeddings API (BYOK).
- **Поиск**: `rag_search` диспетчеризует по режиму базы и собирает
  контекст для LLM; заголовки контекста несут `chunk_id` и 0-based
  позицию найденного чанка.
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
   `retrying_llm`. Если тело HTTP 200 помечено `status == "incomplete"`
   (усечение по лимиту токенов) и видимого текста нет, запрос повторяется
   РОВНО ОДИН раз с `reasoning_effort='none'`, в ленту идёт событие
   `incomplete_retry`.
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
Поставляемые дефолты DevAgent (канонический бандл
`orchestrators/dev_agent/orchestrator.json` → фолбэк
`_DEVAGENT_FALLBACK_DEFAULTS` в `core/config.py`, дублируется в
`_devagent_default_config()` и первом запуске `ensure_builtin_orchestrators()`):
основная модель DeepSeek/`deepseek-flash` с `strong_reasoning_effort`
`xhigh` и лимитом вывода 384 000 токенов, распознавание изображений
DeepSeek/`deepseek-flash`, генерация изображений не назначена
(`image_service`/`image_model` пусты), поисковая модель
DeepSeek/`deepseek-flash` с `search_temperature` 0.4,
`search_max_tool_calls` 3 и `search_reasoning_effort` `high`. Бэкфилл на
старте заполняет только поля без значения, поэтому уже выбранные
пользователем модели и настройки экономии не перезаписываются.
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
- Системный промпт DevAgent v3.20 дополняет механизм правилами: закрытый шаг
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

Отдельный необязательный `incomplete_callback` (по образцу
`usage_callback`/`sanitized_callback`) сообщает вызывающему о теле HTTP 200
со статусом `incomplete` (`_responses_incomplete_details`, reason обычно
`max_output_tokens`), которое иначе выглядело бы как обычный пустой ответ.
`dev_agent/agent_loop.py` подписывается на него и при отсутствии видимого
текста делает один восстановительный повтор с пониженным `reasoning_effort`.

### Web-search модель (search_service/model)
Отдельная пара service/model для задач с веб-поиском, хранится в
конфигурации оркестратора; используется `tool_executor.web_search()`.
У DeepSeek встроенный инструмент `web_search` в Responses API молча
игнорируется, поэтому веб-поиск для DeepSeek выполняется отдельным
запросом к Anthropic-совместимому endpoint (`web_search_base_url`,
`https://api.deepseek.com/anthropic/v1/messages`) с моделью
`deepseek-flash`; ветка выбирается в `tool_executor.web_search()` по
`auth_type == "deepseek_responses"`.

### Fetch-модель (fetch_service/fetch_model) и url_fetch
Необязательная пара service/model для загрузки веб-страниц, хранится в
конфигурации оркестратора. Поддержку объявляет сам профиль сервиса:
каталог `fetch_models` плюс endpoint `fetch_base_url`
(`https://api.deepseek.com/v1/tools/fetch` в `defaults/services/deepseek.json`).
Транспорт - `core/url_fetch.py::fetch_page_content()`: POST `{"url": ...}`
с Bearer-ключом сервиса, ответ `{"title", "content"}` (Markdown), ошибки -
через контракт `APIError` (`ApiKeyMissingError`, `ProviderHTTPError`,
`RequestTimeoutError`, `NetworkError`, `ProviderResponseError`), проверка TLS
по общей политике платформы.

Второй, независимый источник возможности - флаг `fetch_selenium`
(чек-бокс «Использовать Selenium»): страница открывается в headless-браузере,
собирается её видимый текст, а Markdown формирует основная модель
оркестратора (`llm_service` / `llm_model` из `get_fetch_config()`).
Selenium - опциональная зависимость: при её отсутствии ветка возвращает
структурированную ошибку `SeleniumUnavailableError` с подсказкой об установке.
На вкладке «Модели» чек-бокс рисуется первым: пока он включён, селекторы
пары `fetch_service` / `fetch_model` скрыты (подпись
`orch_fetch_selenium_main_model` объясняет, что Markdown формирует основная
модель), а сохранённая пара не сбрасывается и возвращается на место после
снятия флажка.

Инструмент называется `url_fetch` (`dev_agent/tool_executor.py`), прежнее имя
`page_fetch` сохранено как legacy-алиас (с ним же резолвятся `disabled_tools`).
Ветки выбора: адрес с PDF-расширением или `application/pdf` в ответе -
скачивание с лимитом размера и извлечение текста через `pypdf`/`pdfplumber`
с разбивкой `### Page N`; иначе при включённом Selenium - рендер браузером;
иначе - Search/Fetch API провайдера. Прямые загрузки проходят SSRF-гард
`validate_fetch_url()`: схема http/https, длина до 8192 символов, без
логина и пароля, локальные и приватные адреса запрещены (loopback, частные
диапазоны, link-local, служебные суффиксы `.local`/`.internal`/`.lan`) - без
DNS-резолвинга, поэтому проверка детерминирована и работает офлайн.

Каталог `core/orchestrator_tools.py` помечает инструмент как capability-gated
(`_CONDITIONAL_TOOLS`) и включает его в `build_tool_catalog()`,
`list_system_tools()` и блок `## Available tools`, когда
`core.orchestrators.get_fetch_config()` вернул `configured=True` - то есть
назначена модель либо включён Selenium. Снятие обоих источников (или
исчезновение модели из каталога провайдера) убирает инструмент из промпта и
из диспетчера; UI-секция «Модель для загрузки страниц» на вкладке «Модели»
предлагает только сервисы с `fetch_models` (при включённом Selenium секция
заменена подписью об основной модели - см. выше).

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

### Выбор папки в чате ("Выбор папки")
Под полем ввода чата - статичный селектор `ui/pages/orchestrator.py`:
«Авто», до 10 недавних папок оркестратора и пункт нативного выбора папки
(`core/folder_dialog.py`; без GUI пункт скрыт, остаётся ручной ввод пути).
Смена папки в селекторе не трогает рабочую папку сразу: путь применяется
автоматически при отправке сообщения (до вызова модели) - `set_workspace`
вызывает UI-слой, а не агент, - после чего селектор возвращается в «Авто», а
активная папка видна под полем. Уже действующая папка повторно не
применяется; отображаемый путь берётся из привязки треда
(`workspace_binding`), поэтому временное переключение папки агентом не
конфликтует с выбором в интерфейсе.

### Файлы во вводе чата
Загрузчик вложений встроен в само поле ввода: `ui/pages/orchestrator.py`
вызывает `st.chat_input(accept_file="multiple")` - Streamlit показывает
иконку-скрепку с всплывающей подсказкой, а результат приходит объектом с
полями `.text` и `.files`. Отдельный блок `st.file_uploader` и подпись
«Прикрепить файлы любого формата…» удалены, как и пред-отправочные чипы
вложений. Хелпер `_split_chat_prompt` нормализует ответ виджета в пару
`(text, files)`, поэтому отправка срабатывает при непустом тексте ИЛИ
непустом списке файлов. Байты файлов сохраняются в `history/<tid>/files`
без разбора (лимит `MAX_THREAD_FILE_BYTES` - 100 МиБ на файл), а если текста
нет, задачей становится i18n-строка `orch_attach_only_message` с именами
вложений.

Чтение вложений штатное, без обхода через `run_code`: `read_thread_file`
извлекает текст документов через `core.document_text` (PDF, DOCX, PPTX,
XLSX, XLS, RTF, ODT, HTML), а `read_document` делает то же самое для любого
файла на диске по абсолютному пути с окном строк. `run_code` помечен в
каталоге как средство последней очереди - текстовые и документные форматы
читаются специализированными инструментами.

### Авто-прокрутка ленты чата
Лента истории растёт под липкой нижней панелью (блок «Выбор папки» + поле
ввода + тулбар, `st.bottom`). Пока агент работает, `ui/pages/orchestrator.py`
эмитит в `_render_chat_tab` клиентский скрипт `core.feed_autoscroll`,
который держит ленту у последнего сообщения: JS находит скролл-контейнер,
единожды вешает слушатели `scroll`/`wheel`/`touchmove` на родительское окно
и хранит флаг паузы в `sessionStorage` (переживает пересоздание iframe
компонента на каждом rerun). Ручная прокрутка вверх (> порога
`AUTOSCROLL_THRESHOLD_PX`) ставит слежение на паузу, возврат к низу -
возобновляет. При отправке сообщения флаг `scroll_bottom` форсирует один
прокрут вниз независимо от паузы. Отступ снизу модуль не задаёт: Streamlit
1.66 сам резервирует место под липкой нижней панелью, а ручной
`padding-bottom` давал двойной отступ и большую пустоту в ленте.

### RAG: локальные базы знаний
Индексация локальная (SQLite: собственный BM25-индекс + опциональные
векторы), эмбеддинги - удалённо через Yandex Embeddings API (BYOK, режимы
vector/hybrid). Базы хранятся в `DATA_DIR/rag_bases/`.

### Коннекторы
- **`core/connectors.py`** - CRUD подключений: папка
  `DATA_DIR/connectors/<id>/manifest.json`; токен шифруется через
  `core.crypto.encrypt` и не попадает в публичные представления.
- **`core/github_connector_rest.py`** - единственный прямой REST-коннектор
  (requests, REST API v3, `X-GitHub-Api-Version: 2022-11-28`, Bearer).
  Реализует repo CRUD, чтение/запись файлов и метаданных, плюс Git Data API:
  `get_ref`, `get_commit`, `get_tree`, `create_ref` (произвольный ref -
  ветка или тег) и `create_tag` (лёгкий или аннотированный через
  `POST /git/tags`), а также оптимизированная пакетная публикация
  `batch_commit` (blobs -> одно дерево с чанкингом по 9000 записей -> один
  коммит -> PATCH ref; ветка создаётся через POST /git/refs для пустого
  репозитория) и `batch_upsert` (дифф по blob-SHA с пропуском неизменённых).
  Ошибки - `GithubRestError` (наследник ValueError) со статусом; токен
  расшифровывается внутри модуля и не попадает в результаты/ошибки.
- **`core/github_tools_rest.py`** - инструменты `ghr_*` (18 штук:
  `ghr_list_repos`, `ghr_create_repo`, `ghr_read_file`, `ghr_upload_file`,
  `ghr_update_file`, `ghr_delete_file`, `ghr_list_files`, `ghr_batch_commit`,
  `ghr_batch_commit_paths`, `ghr_batch_upsert`, `ghr_test_connection`,
  `ghr_get_repo_info`, `ghr_read_file_meta`, `ghr_get_ref`, `ghr_get_commit`,
  `ghr_get_tree`, `ghr_create_ref`, `ghr_create_tag`)
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
- **Черновики подключений (draft/pending):** `create_connection(...,
  pending=True)` сохраняет конфиг без секретов и ставит
  `credentials_status=pending`; заполнение секрета (токен/пароль/ключ)
  через `update_connection` / `set_connection_token` /
  `set_connection_secret` переводит соединение в `active`. Промпт-блок
  `_extend_prompt_with_connections` помечает pending-соединения и добавляет
  quick note о файле памяти.
- **Файл памяти подключения:** `DATA_DIR/connectors/<id>/memory.md`
  (лимит 64 КиБ), API `get_connection_memory` / `set_connection_memory`.
- **Инструменты коннекторов в диспетчере:** 11 общих инструментов
  (`list_connections`, `get_connection`, `list_connector_services`,
  `create_connection`, `update_connection`, `delete_connection`,
  `test_connection`, `get_enabled_connections`, `set_enabled_connections`,
  `get_connection_memory`, `set_connection_memory`) в
  `dev_agent/universal_agent.py`; инструменты сервисов (`ghr_*`, `ssh_*`) -
  только для включённых соединений.
- **Инструкция `connector_creator`** (встроенная DevAgent) - полный флоу
  создания подключения; `ssh_connector` / `github_connector` дополнены
  правилами первого входа на неизвестный сервер и файлом памяти.

- **`core/mcp_client.py`** - собственный клиент JSON-RPC 2.0 для двух
  транспортов (только stdlib, без официального SDK). `McpClient`
  (stdio): `subprocess.Popen` без shell, построчный UTF-8 JSON;
  handshake `initialize` -> `notifications/initialized`; `tools/list`
  с кэшем на время жизни процесса; `tools/call` принимает только
  текстовые `content` (нетекстовые результаты отклоняются); таймауты
  30 с / 60 с с завершением процесса и блокировкой коннектора до
  пересоздания; реестр процессов на коннектор (старый процесс
  убивается при пересоздании); `stderr` в лог. `McpHttpClient` (http,
  urllib): POST-запросы, ответ - обычный JSON или SSE-поток,
  `Mcp-Session-Id` воспроизводится в следующих запросах; заголовок
  авторизации собирается из шаблона `auth_header` с плейсхолдерами
  `{ИМЯ_ПЕРЕМЕННОЙ}`, подставляемыми из расшифрованных секретов на
  каждый запрос (пустые секреты заголовок не порождают); обновление
  секретов сбрасывает кэш клиента. Трансляция `inputSchema` (JSON
  Schema 2020-12) в параметры инструментов платформы - общая.
- **`core/mcp_tools.py`** - инструменты `mcp_list_tools` /
  `mcp_call_tool` и динамические `mcp__<коннектор>__<инструмент>`
  (обратный реестр, кэш discovery 60 с); `env_json` расшифровывается
  только для процесса сервера; недоверенные данные сервера санируются.
- **`core/connectors.py`** - сервис `mcp` в `CONNECTOR_SERVICES`
  (`config_fields`: `transport` (stdio/http), `command` с белым списком
  команд, `args`, `url` с проверкой схемы, `auth_header`, `preset`;
  `secret_fields`: `env_json` - одна строка JSON, шифруется; приём
  `env_pairs` с слиянием в зашифрованный `env_json`).
- **Пресеты (`core/connector_presets.py`)** - шаблоны без секретов из
  `defaults/connectors/mcp/*.json` (видимый каталог: `vkusvill` - http;
  `github_mcp` скрыт флагом `hidden`):
  `list_presets` (скрытые пресеты не отдаёт без `include_hidden`),
  `resolve_preset` (находит пресет по id, включая скрытые) и
  `instantiate_preset` -
  черновик `pending` без секретов с посевом файла памяти
  (`memory_seed`). У пресета обязательна иконка `icon` (эмодзи для
  плитки вкладки MCP), поддерживаются необязательные `sort_order` и
  `hidden` (boolean: убирает плитку из каталога, но сохраняет резолв по
  id; некорректное значение трактуется как `false`).
- **Каталог и диспетчеризация:** ветка `mcp` в `_connection_catalog`
  (`core/orchestrator_tools.py`); статические и динамические
  MCP-инструменты регистрируются в диспетчере
  (`dev_agent/universal_agent.py`, включая `list_connection_presets` и
  параметр `preset` у `create_connection`) только для включённых
  соединений; `disabled_tools` и dangerous-гейты действуют; ошибка
  старта не роняет каталог (инструменты не попадают +
  предупреждение).
- **UI**: `ui/pages/connectors.py` - страница по модели
  `список ↔ форма` (как страница помощников): режим страницы лежит в
  `session_state` (`connectors_view` = `list` | `create` | `edit`), и
  форма ЗАМЕНЯЕТ список на той же странице, а не открывается в
  экспандере или на другой вкладке.
  Список - три вкладки («General services», «MCP servers»,
  «Connections»). `st.tabs` создаётся stateful: `on_change="rerun"`
  делает вкладки виджетом с состоянием, `key` даёт стабильную
  идентичность, а `default=<метка>` переключает вкладку по запросу.
  Без `on_change="rerun"` (значение по умолчанию) виджет состояния не
  хранит вообще, поэтому любой rerun открывал первую вкладку, и
  переключение из кода было невозможно; хранимое значение сбрасывается
  ДО создания виджета - единственный момент, когда Streamlit разрешает
  менять состояние виджета.
  «General services»: плитки сервисов с иконкой и описанием
  (github_rest, ssh; `mcp` исключён) - клик только открывает форму
  создания, в БД ничего не пишется. «MCP servers»: плитки пресетов
  рядами по три (иконка + имя + описание) и плитка «+ Свой
  MCP-сервер»; клик открывает ту же форму с конфигурацией пресета и
  полями `secret_env_fields` (либо сырыми полями stdio
  command/args/env_json или http url/auth_header/env_json).
  «Connections»: карточки ВСЕХ сохранённых подключений с иконкой
  сервиса, индикатором учётных данных, предупреждением о pending,
  тестом (запуск сервера/опрос HTTP-эндпоинта и каталог
  инструментов), редактированием и удалением.
- **Создание и редактирование в UI:** форма `create` собирает имя,
  аккаунт и поля сервиса/пресета и вызывает `create_connection` (или
  `instantiate_preset(..., pending=False)`) ТОЛЬКО по кнопке «Создать»,
  передавая введённые секреты сразу - подключение создаётся активным,
  без промежуточного черновика. Форма `edit` вызывает
  `update_connection` по «Сохранить». «Отмена» и «← Назад»
  возвращают к списку, не записывая ничего; после сохранения список
  открывается на вкладке «Connections» через `default`. Ошибка
  сохранения оставляет форму открытой с введёнными значениями.
  Черновики `pending`, созданные DevAgent из чата
  (`instantiate_preset` по умолчанию `pending=True`), заполняются
  через ту же форму кнопкой «Заполнить учётные данные».
- **Ошибки MCP HTTP:** код 401/403 дополняется подсказкой о
  credentials - для эндпоинта GitHub Copilot классические PAT
  `ghp_*` не поддерживаются, нужен fine-grained `github_pat_*`; тело
  ответа (первые 300 символов) включается в сообщение, секреты не
  раскрываются.
- **Инструкция `mcp_connector`** (встроенная DevAgent) - флоу работы с
  MCP-подключениями: пресеты, черновики и заполнение учётных данных
  пользователем, http-транспорт, правила безопасности.

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
Каноничные промпты (`dev_agent/system_prompt.md` v3.20, YaAgent v2.11) не
дублируют этот справочник: их раздел справочника ссылается на
автоматический блок, оставляя в промпте только собственные правила
использования инструментов (форматы вызова, fallback-цепочки).
Чёрный список `config['disabled_tools']` исключает инструменты из каталога,
а диспетчер (`dev_agent/universal_agent.py`) блокирует их вызов ошибкой
`disabled: true` (гейт до выполнения; legacy-алиасы в обе стороны). Вкладка
Функции показывает системные функции с чекбоксами и кнопкой Сохранить.

### Мультиагентная оркестрация (`core/orchestration/`)
Запуск агента из агента описан единым контрактом, отделённым от UI и от
цикла оркестратора. Пакет `core/orchestration/` держит четыре слоя:

- **`contracts.py`** - типы конвейера: `AgentBrief` (цель, ограничения,
  входные артефакты `Dict[str, str]`, критерии готовности), `SpawnRequest`
  (`agent_slug`, `brief`, `parent_run_id`, `depth`), `InvokeRequest`
  (`system`, `user`, `context_files`, `parent_run_id`), `Artifact`,
  `AgentRunResult` (`run_id`, `status`, `stop_reason`, `summary`,
  `artifacts_out`, `steps_used`, `error_code`) и `CancelReport`. Все
  структуры сериализуются в dict: результат инструмента уходит в ленту как
  данные, а не как объект.
- **`invoke.py`** - `invoke_once()`: один вызов модели без цикла
  инструментов, без RAG и без доступа к файлам. Контекст передаётся
  вызывающим явно (`context_files` -> блоки `### File: <name>`); документ
  (PDF, DOCX, PPTX, XLSX, RTF, ODT, HTML) читается через
  `core.document_text`, поэтому один файл - это один вызов, без дробления
  текста на части. Нечитаемый файл попадает в `skipped_files`, а не
  срывает вызов. Сервис и модель резолвятся по конфигурации родителя.
- **`service.py`** - `OrchestrationService`: реестр запусков и их дерево.
  `ensure_run()` регистрирует корневой запуск диалога, `run()` запускает
  ребёнка, `cancel(cascade=True)` отменяет поддерево, `subscribe()` отдаёт
  события в ленту, `release_tree()` освобождает записи и семафоры после
  завершения диалога. Идентификаторы: `run_id` - уникальный, тред ребёнка -
  производный `{parent}__s{run_id[:8]}` (свой журнал, рабочая папка
  родителя).
- **`tools.py`** - обёртки инструментов `spawn_agent` и `invoke_assistant`
  с правами доступа: `agent_not_allowed`, `unknown_agent`,
  `assistant_not_allowed`, `unknown_assistant`, `invalid_args`,
  `not_implemented`. Права проверяются до захвата ресурсов, поэтому
  отклонённый вызов не занимает слот пула.

**Лимиты делегирования** хранятся в конфигурации оркестратора и читаются
через `get_delegation_limits(slug)` (шесть ключей): `max_depth` - глубина
дерева от диалога (0 - делегирование запрещено, по умолчанию 1),
`max_active_subagents` - одновременные дети и одноразовые вызовы (семафор
дерева), `subagent_max_steps` - шагов на ребёнка, `total_subagent_runs` -
суммарное число запусков в дереве, `subagent_timeout_seconds` - предел
одного запуска, `subagent_budget_tokens` - потолок токенов ребёнка
(0 - без потолка). Сеттер кламп-ит значения к допустимому диапазону:
нулевой пул защёлкнул бы первый же запуск.

**Права.** `spawn_agent` доступен всегда (делегирование самому себе
структурно), `invoke_assistant` появляется в каталоге только при непустом
`allowed_assistants`; `allowed_agents` перечисляет сотрудников, которым
разрешён запуск. Само-слаг в списке не хранится, а список помощников
сверяется с живыми карточками при записи, поэтому удалённый помощник не
остаётся в правах. Каталог инструментов и диспетчер читают один и тот же
источник, так что модель не получает инструмент, который будет отклонён.

**Fan-out.** В фазе `executing` цикл группирует независимые вызовы
`spawn_agent`: сначала проверяются права и аргументы всего набора, затем
захватываются слоты пула и дети выполняются параллельно; события
помечаются `run_id` ребёнка, и лента группирует их по запускам. Дочерний
запуск получает собственное связывание рабочей папки и собственный журнал
задач, а по завершении освобождает и то, и другое.

**Задание субагенту.** `_build_child_task_text()` собирает текст запуска из
цели, брифа и входных артефактов и добавляет блок «Контекст запуска» с
АБСОЛЮТНЫМИ путями: рабочая папка родителя и папка файлов диалога родителя
(`history/<thread_id>/files`). Субагент стартует с чистым контекстом, его
собственный диалог не содержит вложений родителя, поэтому относительный путь
или имя файла без каталога для него - тупик; описание `spawn_agent` требует
абсолютные пути прямо в контракте.

**Блок делегирования в промпте.** `_extend_prompt_with_delegation()` в
`core/orchestrators.py` добавляет в системный промпт два раздела:
`## Available agents (spawn_agent)` - сам сотрудник (самоделегирование
разрешено всегда) плюс `allowed_agents` с именами, и
`## Available assistants (invoke_assistant)` - `allowed_assistants` с id и
именами карточек и напоминанием передавать документ файлом в `context_files`.
Раздел помощников появляется только при непустом списке, поэтому промпт не
обещает вызов, который среда отклонит.

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
- **M3/B4 - единый бюджет истории (порог + гистерезис).**
  `build_economy_context` (`dev_agent/agent_loop.py`) +
  `_enforce_history_token_budget` берут бюджет из общей формулы
  `core.context_guard.history_cap` = `0.75 × (окно - лимит вывода) -
  промпт` (`input_budget`); вычитается `_ECONOMY_GUARD_MARGIN` (2048) под
  пользовательское сообщение. Под бюджетом список не меняется; при
  превышении ведущие сообщения режутся за один проход, причём в
  кэш-режиме фронт сбрасывается до `tail` сообщений - тот же сброс, что
  даёт переполнение окна `tail × multiplier` (единый триггер резета, а
  не отдельный глубокий срез); если и `tail` не укладывается, срез
  продолжается до нижней ватерлинии `0.75×бюджета`. Срез сворачивается в
  `economy_anchor` (фронт окна стабилен). Служебная head-мета -
  всегда. Одна формула для экономики и guard'а означает, что guard никогда
  не сдвигает фронт экономического payload'а.
- **M4/M5 - pre-flight guard в api_layer.** `core/context_guard.py`
  (apply_context_guard, `input_ceiling`/`input_budget`/`history_cap`) +
  `ContextWindowError` в `core/api_errors.py`; вызов из `send_request` до
  отправки. Резерв вывода - `min(max_out, max(4096, 0.25×окна))`;
  служебный head-блок эконом-меты не срезается; мягкий трим на 0.75
  окна, жёсткий предел 0.9 окна (выше общего бюджета, чтобы экономический
  payload проходил без среза), понятная ошибка без нового диалога;
  публичный `history_cap(window, max_tokens, prompt_tokens)` отдаёт
  эффективный предел истории.

### Экспорт/импорт оркестраторов (core API)
Формат `sagaai_orchestrator/v1` (JSON). Slug-конфликты разрешаются
генерацией нового (`slug_2`, ...). Инструкции импортируются без
перезаписи существующих. UI-интерфейс для экспорта/импорта намеренно
отложен.
