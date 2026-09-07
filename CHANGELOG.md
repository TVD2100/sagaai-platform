# Changelog

Все существенные изменения проекта документируются в этом файле.
Формат основан на [Keep a Changelog](https://keepachangelog.com/ru/1.0.0/),
версионирование - SemVer (совместимо с версиями файлов в `file_versions.json`).

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

