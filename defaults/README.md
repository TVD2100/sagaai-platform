# Bundled defaults (defaults/)

Эта папка - единственный источник дефолтных данных SagaAI. Она описывает,
какие сущности создаются при первом запуске: оркестраторы, помощники,
инструкции, LLM-провайдеры, языки интерфейса, глобальные настройки и навыки.

Управление дефолтами сводится к управлению файлами:

- Удалить файл/папку - сущность перестаёт импортироваться.
- Добавить файл/папку - сущность появится при следующей инициализации.
- Изменить файл - новые установки получат обновлённые значения
  (существующие пользовательские настройки не перезаписываются).

Старые источники (services/, langs/) читаются как fallback перед
встроенными значениями; каталог presets/ больше не используется. Для
встроенного DevAgent системный промпт берётся из dev_agent/system_prompt.md
(рядом с кодом агента) - отдельная копия в defaults/ не поддерживается.

---

## Структура

Имена файлов в defaults/ совпадают с каноничными именами runtime-хранилища DATA_DIR/ (orchestrator.json, manifest.json): папку из defaults/ можно копировать в runtime как есть (и наоборот).

defaults/
|-- settings/
|   +-- global.json                  # глобальные настройки платформы
|-- orchestrators/
|   |-- dev_agent/                   # инструкции встроенного DevAgent
|   |   +-- instructions/            #   (системный промпт DevAgent - в dev_agent/system_prompt.md)
|   |   |-- assistant_creator.md
|   |   |-- employee_creator.md
|   |   +-- self_reflection.md
|   |-- ya_agent/                    # дефолтный оркестратор YaAgent
|   |   |-- orchestrator.json
|   |   +-- system_prompt.md
|   +-- ...                          # любые другие дефолтные оркестраторы
|-- assistants/                      # дефолтные помощники (по папке на помощника)
|   +-- <assistant_name>/
|       |-- manifest.json
|       |-- prompt.md
|       +-- files/                   # прикреплённые текстовые файлы
|-- services/                        # LLM-провайдеры (JSON-определения)
|   |-- deepseek.json
|   |-- gigachat.json
|   +-- yandex.json
|-- langs/                           # языки интерфейса
|   |-- en.json
|   |-- ru.json
|   +-- ...
+-- skills/                          # стандартизированные навыки
    +-- <skill_folder>/              # папка навыка (SKILL.md)

---

## Форматы настроек

### defaults/settings/global.json

{
  "ui_lang": "Русский",
  "providers_preset": "default"
}

- ui_lang - язык интерфейса по умолчанию (должен совпадать с lang_display_name
  одного из defaults/langs/*.json).
- providers_preset - ключ пресета для страницы настроек провайдеров.

### Оркестратор: defaults/orchestrators/<slug>/

Новый (рекомендуемый) формат:

<slug>/
|-- orchestrator.json   # метаданные + config
|-- system_prompt.md   # промпт оркестратора
|-- instructions/      # md-файлы с front-matter: id, name, description
+-- functions/         # .py-файлы: invoke(**kwargs) -> dict

orchestrator.json:

{
  "name": "YaAgent",
  "description": "Описание...",
  "config": { "strong_service": "YandexAI", "strong_model": "deepseek-v4-flash" },
  "tools": [],
  "max_steps": 100,
  "auto_apply": true,
  "sort_order": 150
}

- Пустой tools: [] означает полный набор инструментов DevAgent.
- Отсутствующий web_search_prompt в config заполняется стандартным.

Каноничное имя - orchestrator.json (совпадает с runtime DATA_DIR/orchestrators/<slug>/).
Для обратной совместимости читается и старое имя settings.json рядом с system_prompt.md, instructions.json, functions/.

### Инструкции: defaults/orchestrators/dev_agent/instructions/*.md

Markdown с front-matter:

---
id: assistant_creator
name: Assistant Creator
description: Generates high-quality system prompts for new assistants.
---

<тело инструкции>

### Помощник: defaults/assistants/<name>/

|-- manifest.json   # name, description, service, model, temperature, tools
|-- prompt.md       # системный промпт
+-- files/          # прикреплённые файлы (сохраняются как вложения)

### Сервисы, языки, навыки

- Сервисы: JSON-файлы; defaults/services/ имеет приоритет над services/.
- Языки: *.json + *_guide.md; defaults/langs/ имеет приоритет над langs/.
- Навыки: подпапки в defaults/skills/. При первом запуске, если библиотека
  навыков пуста, каждая непустая подпапка импортируется как навык.

---

## Как это работает

- core/defaults.py - пути, загрузчики, парсер front-matter.
- core/default_imports.py - импорт сущностей в runtime (БД/папки).
- Точка входа: ensure_all_defaults() вызывается из ui.app.main() при старте.
- Импорт идемпотентен: существующие сущности не перезаписываются.

## Советы

- Папка defaults/orchestrators/dev_agent/ содержит только инструкции;
  системный промпт DevAgent живёт в dev_agent/system_prompt.md (одна копия).
  Удаление папок из defaults/ предотвращает создание сущностей при первой
  установке.
- Удаление из defaults/ предотвращает СОЗДАНИЕ сущности, а не удаляет уже
  созданное в runtime-хранилище (DATA_DIR/orchestrators/, БД).
