# Changelog

Все существенные изменения проекта документируются в этом файле.
Формат основан на [Keep a Changelog](https://keepachangelog.com/ru/1.0.0/),
версионирование - SemVer (совместимо с версиями файлов в `file_versions.json`).

## [Unreleased]

### Добавлено

- **Основной GitHub-коннектор по прямому REST API** (`github_rest`):
  - `core/github_connector_rest.py` - прямые запросы к REST API v3 через
    `requests` (без PyGithub): repo CRUD, чтение/запись файлов, метаданные,
    refs/коммиты/деревья через Git Data API;
  - оптимизированная пакетная публикация: все blobs заранее, затем ОДНО
    дерево, ОДИН коммит и ОДНО обновление ref для всей пачки
    (`batch_commit`; ветка создаётся для пустого репозитория) и `batch_upsert`
    с пропуском неизменённых файлов по blob-SHA;
  - инструменты для оркестраторов (`core/github_tools_rest.py`, префикс
    `ghr_*`): `ghr_list_repos`, `ghr_create_repo`, `ghr_read_file`,
    `ghr_upload_file`, `ghr_update_file`, `ghr_delete_file`, `ghr_list_files`,
    `ghr_batch_commit`, `ghr_batch_upsert`;
  - связывание инструментов по сервисам включённых соединений
    (`_extend_prompt_with_connections`, диспетчер, UI), инструкция
    `github_connector` расширена, i18n-ключ `connectors_service_github_rest`;
  - тесты: юнит (`tests/test_github_connector_rest.py`,
    `tests/test_github_tools_rest.py`) и сценарные на FakeGitHub
    (`tests/scenarios/test_github_rest_scenario.py`);
- Старый PyGithub-коннектор (`github`) сохранён как резервный.
- **Страница «Доступы» - доступ по паролю:**
  - `ui/pages/access.py` - настройки доступа: переключатель «Доступ по
    паролю» (по умолчанию выключен), поля пароля и подтверждения,
    интервал повторного запроса (по умолчанию 12 часов);
  - `core/auth.py` - настройки доступа в ConfigKV (пароль шифруется
    Fernet), приоритет пароля формы над `SAGAAI_AUTH_PASSWORD`,
    повторный вход по истечении интервала и пропуск запроса при
    работающем оркестраторе;
  - пункт меню «🔐 Доступы» перед «Обновления» (`ui/app.py`), dispatch и
    smoke-проверка порядка навигации;
  - локализации ru/en/zh-CN, 17 юнит-тестов (`tests/test_auth_access.py`) и
    5 сценарных тестов (`tests/scenarios/test_access_scenarios.py`).

### Исправлено

- **Пустой репозиторий и Git Data API:** реальный прогон на живом GitHub
  показал, что в полностью пустом репозитории API отвечает
  409 «Git Repository is empty» и на чтение ref, и на создание blobs (до
  первого коммита). Обработано в `_resolve_target_ref` и `_create_blob`;
  при batch в репозитории без единого коммита выводится понятное сообщение
  (создать репозиторий с `auto_init=True` или выложить первый файл через
  `upload_file`); добавлено 3 юнит-теста.
- **Проверка на живом GitHub:** приватный репозиторий
  `sagaai-connector-test` - 246 файлов совпали с локальным git-индексом по
  blob-SHA (0 missing / 0 mismatch / 0 extra), 3 коммита (seed + batch
  244 текстовых файлов одним коммитом + бинарный `index.db`
  base64-blob'ом).

## 0.1.0-preview.2 - 2026-09-07

### Добавлено

- **Конвейер обновлений через GitHub:**
  - манифест версий файлов `file_versions.json` (units - атомарные пакеты,
    selectable - выборочные файлы; версии semver + sha256 + релизные заметки);
  - инструмент сопровождения манифеста `scripts/verify_manifest.py`
    (`--init` / `--add` / `--fix-hashes` / `--strict` / `--json`);
  - апдейтер `core/updater.py` (CLI `check` / `stage` / `apply` / `rollback`,
    raw-канал без токена) и атомарный cold-start апплаер
    `core/updater_apply.py` (чистый stdlib: бэкапы
    `.dev_agent/updates/backup/<run_id>/`, проверка sha256, `state.json` /
    `health.json`, откат);
  - хук применения обновлений при холодном старте в `app.py` (до импорта
    Streamlit) с маркером работающего процесса;
  - страница «Обновления» (`ui/pages/updates.py`): проверка обновлений,
    выборочная установка selectable-файлов и пакетов, применённые обновления
    и откат; локализации en/ru/zh-CN;

### Исправлено

- **Применение отложенных обновлений:**
  - детектор запущенного приложения (`core/updater.py`): остановленные
    (Ctrl+Z, state `T`) и зомби-процессы (state `Z`), а также
    переиспользованный pid больше не считаются работающим приложением;
    при недоступности `ps` - консервативный фолбэк через `os.kill`;
  - при отказе применения из-за работающего приложения пишется
    `.dev_agent/updates/health.json` и вызывается переданный логгер;
  - страница «Обновления» (`ui/pages/updates.py`): кнопки Apply/Rollback
    всегда активны; при работающем приложении требуется чекбокс
    подтверждения, обновление применяется с `force=True`, показывается
    подсказка о необходимости перезапуска;
  - результат cold-start применения логируется в stderr в `app.py`;
  - добавлен сценарный тест `tests/scenarios/test_updater_runtime_flow.py`
    (force-apply/force-rollback из живого UI, отказ и повторное применение
    после «перезапуска»).
- [2026-09-09T06:49:22] CHANGELOG.md (backup v9)
- [2026-09-09T06:49:36] README.md (backup v17)
- [2026-09-09T06:49:56] README.md (backup v18)
- [2026-09-09T06:50:32] SPEC.md (backup v5)
- [2026-09-09T06:51:00] SPEC.md (backup v6)
- [2026-09-09T06:51:06] SPEC.md (backup v7)
- [2026-09-09T06:59:16] file_versions.json (backup v20)
- [2026-09-09T06:59:23] file_versions.json (backup v21)
- [2026-09-09T06:59:30] file_versions.json (backup v22)
