---
id: agents_responses_api
name: Создание агентов через Responses API
description: Инструкция по проектированию агентов через Responses API Yandex AI Studio: авторизация, методы HTTP, параметры запроса, инструменты, управление вызовами, reasoning, формат ответа.
---

# Создание агентов через Responses API

Эта инструкция описывает, как проектировать агентов на базе Responses API Yandex AI Studio. Используйте её вместе со справочником моделей (`yandex_models_reference`) при создании любых агентских решений.

## Эндпоинт и авторизация

Responses API доступен через OpenAI-совместимый базовый URL `https://ai.api.cloud.yandex.net/v1`. Основные маршруты:

- `POST /v1/responses` - создать ответ (генерация, инструменты, структурированный вывод).
- `GET /v1/responses/{response_id}` - получить ответ по ID.
- `DELETE /v1/responses/{response_id}` - удалить ответ.
- `POST /v1/responses/{response_id}/cancel` - отменить обработку.
- `GET /v1/responses/{response_id}/input_items` - список входных элементов.
- `POST /v1/responses/{response_id}/input_tokens` - число входных токенов.
- `POST /v1/responses/{response_id}/compact` - сжать контекст беседы (параметр `previous_response_id`).

### Авторизация

Два способа авторизации:

**1. API-ключ (рекомендуется для серверных решений).** Создаётся в интерфейсе AI Studio (кнопка «Создать API-ключ»). При создании указывается срок действия; вместе с ключом создаётся сервисный аккаунт с минимальными ролями. Значение ключа показывается один раз - сохраните его сразу. Передаётся в заголовке:

```
Authorization: Bearer <значение_API-ключа>
```

**2. IAM-токен.** Токен сервисного аккаунта Yandex Cloud:

```
Authorization: Bearer <IAM-токен>
```

Для большинства методов дополнительно требуется идентификатор каталога (`folder_id`): он входит в URI модели `gpt://<folder_id>/<имя_модели>` и/или передаётся как `project` в OpenAI SDK.

Пример cURL:

```bash
curl --request POST "https://ai.api.cloud.yandex.net/v1/responses" --header "Authorization: Bearer <API_KEY>" --header "Content-Type: application/json" --data '{"model": "gpt://<folder_id>/yandexgpt-5-lite", "input": "Привет!"}'
```

Пример OpenAI SDK (Python):

```python
import openai

client = openai.OpenAI(
    api_key=YANDEX_API_KEY,
    project=YANDEX_FOLDER_ID,
    base_url="https://ai.api.cloud.yandex.net/v1"
)
response = client.responses.create(model="gpt://<folder_id>/yandexgpt-5-lite", input="Привет!")
```

## Основные параметры тела запроса

| Параметр | Тип | Назначение |
| --- | --- | --- |
| `model` | string | URI модели: `gpt://<folder_id>/<имя>`. Передавайте именно URI, а не голое имя. |
| `input` | string или InputItem[] | Ввод модели: строка (эквивалент роли user) или массив входных элементов. Максимальная длина строки: 10 485 760. |
| `instructions` | string или InputItem[] | Системное/разработчика сообщение (string эквивалентен роли `developer`; может быть null или массивом InputItem). При использовании с `previous_response_id` инструкции из предыдущего ответа не переносятся - передавайте их каждый раз. |
| `conversation` | ConversationParam | Беседа, элементы которой добавляются в начало `input_items`. Нельзя использовать вместе с `previous_response_id`. |
| `previous_response_id` | string | ID предыдущего ответа для многоповоротного диалога. Нельзя использовать вместе с `conversation`. |
| `tools` | Tool[] | Инструменты, доступные модели: function, web_search, file_search, image_generation, code_interpreter, mcp. |
| `tool_choice` | ToolChoiceParam | Управление выбором инструмента. Значение `none` отключает инструменты; `auto` - модель решает сама; можно указать конкретный инструмент. |
| `max_tool_calls` | integer | Максимум вызовов инструментов. |
| `parallel_tool_calls` | boolean | Разрешить параллельные вызовы инструментов. |
| `reasoning` | Reasoning | Конфигурация режима рассуждений: `effort` = `none`/`minimal`/`low`/`medium`/`high`/`xhigh` (по умолчанию `medium`). |
| `text` | ResponseTextParam | Конфигурация текстового ответа: обычный текст или структурированный JSON (`text.format`), а также `verbosity` (`low`/`medium`/`high`). |
| `max_output_tokens` | integer | Максимум токенов в ответе. |
| `temperature` | number | Вариативность ответа (обычно 0-1). |
| `stream` | boolean | Потоковая генерация через SSE. |
| `truncation` | string | `auto` (усекать при превышении контекста) или `disabled` (ошибка 400). По умолчанию `disabled`. |

## Входные элементы (input_items)

Элементы ввода формируются как `InputItem` (сам элемент или ссылка `item_reference` по id). Основные типы:

- **InputMessage** - сообщение: `type: "message"`, `role` (`user`/`system`/`developer`), `content` (массив InputContent), `status`.
- **InputTextContent** - текст: `type: "input_text"`, `text`.
- **InputImageContent** - изображение: `type: "input_image"`, `detail` (`low`/`high`/`auto`/`original`), `file_id` или `image_url` (полный URL или base64 data URL, максимум 20 971 520 символов).
- **InputFileContent** - файл: `type: "input_file"`, `detail` (`low`/`high`), `file_data` (base64, максимум 73 400 320), `file_id`, `file_url`, `filename`.

## Инструменты (tools)

### Функция (FunctionTool)

| Поле | Тип | Назначение |
| --- | --- | --- |
| `type` | string | Всегда `function`. |
| `name` | string | Имя функции. |
| `description` | string | Описание для модели (на основе него модель решает, вызывать ли функцию). |
| `parameters` | object | JSON Schema параметров функции. |
| `strict` | boolean | Строгая валидация параметров. По умолчанию `true`. |
| `defer_loading` | boolean | Функция загружается отложенно через tool search. |

### Веб-поиск (WebSearchTool)

| Поле | Тип | Назначение |
| --- | --- | --- |
| `type` | string | `web_search` (по умолчанию) или `web_search_2025_08_26`. |
| `filters.allowed_domains` | string[] | Ограничение поиска доменами (поддомены учитываются). |
| `search_context_size` | string | `low`, `medium` (по умолчанию) или `high`. |
| `user_location` | object | Приблизительное местоположение: `country` (ISO-2), `region`, `city`, `timezone` (IANA). |

### Поиск по файлам (FileSearchTool)

| Поле | Тип | Назначение |
| --- | --- | --- |
| `type` | string | Всегда `file_search`. |
| `vector_store_ids` | string[] | ID векторных хранилищ для поиска. |
| `filters` | object | ComparisonFilter (`eq`, `ne`, `gt`, `gte`, `lt`, `lte`, `in`, `nin`, по умолчанию `eq`) или CompoundFilter (`and`/`or`). |
| `max_num_results` | integer | Максимум результатов (1-50). |
| `ranking_options.ranker` | string | `auto` или `default-2024-11-15`. |
| `ranking_options.score_threshold` | number | Порог релевантности (0-1). |
| `ranking_options.hybrid_search` | object | Веса гибридного поиска: `embedding_weight` и `text_weight`. |

### MCP-инструменты

Для MCP-серверов через OpenAI-совместимый API каждый инструмент в массиве `tools` содержит:
- `server_label` - метка сервера;
- `server_url` - URL внешнего MCP-сервера;
- `type` - всегда `mcp`;
- `metadata.description` - описание инструмента для модели.

## Типы вызовов инструментов (что возвращает модель)

- **FileSearchToolCall** - `type: "file_search_call"`, `queries`, `status` (in_progress/searching/complete/incomplete/failed), `results` (file_id, filename, text, score 0-1).
- **WebSearchToolCall** - `type: "web_search_call"`, `status`, `action` (search/open_page/find_in_page).
- **FunctionToolCall** - `type: "function_call"`, `name`, `arguments` (JSON-строка), `status`.
- **ImageGenToolCall** - `type: "image_generation_call"`, `result` (изображение в base64).
- **CodeInterpreterToolCall** - `type: "code_interpreter_call"`, `code`, `outputs` (logs / image).
- **MCPToolCall** - `type: "mcp_call"`, `server_label`, `name`, `arguments`, `output`, `status`; возможны `mcp_list_tools`, `mcp_approval_request`, `mcp_approval_response`.

## Формат ответа (text.format)

- **Text** - обычный текст: `type: "text"` (по умолчанию).
- **JSON Schema** - строгий структурированный JSON: `type: "json_schema"`, `name` (обязателен), `schema` (JSON Schema), `description`, `strict` (по умолчанию false).
- **JSON Object** - свободный JSON: `type: "json_object"`. Дополнительно укажите требование JSON словами в промпте.
- `verbosity` - ограничение многословности: `low`/`medium`/`high`.

## Рекомендации по проектированию агента

1. **Системный промпт** - передавайте в `instructions`: роль агента, границы ответственности, правила использования инструментов.
2. **Инструменты по необходимости** - не подключайте лишние инструменты: каждый расширяет поверхность атаки и увеличивает стоимость.
3. **Описания функций** - давайте точные описания с примерами аргументов: это критически влияет на качество вызовов.
4. **JSON-ответы** - для строгого JSON используйте `text.format` с `json_schema` (properties/required/type object); для свободного JSON - `json_object` плюс явное требование в промпте.
5. **Многоповоротность** - для диалога передавайте `previous_response_id` (или `conversation`), а не повторяйте всю историю вручную. При передаче `instructions` в каждом ходе учитывайте, что предыдущие инструкции не переносятся.
6. **Рассуждения** - для сложных задач включайте `reasoning.effort`: medium/high. Для простых - low ради скорости.
7. **Лимиты** - синхронные генерации: 10 одновременных; таймаут синхронного запроса: 20 минут. Фоновый режим (без ожидания) доступен через Responses API для долгих операций.

## Ключевые поля ответа

- `response.status` - `completed`, `in_progress`, `incomplete`, `failed` и др.
- `response.incomplete_details.reason` - причина неполного ответа (например, `content_filter` при срабатывании модерации).
- `response.output` - массив выходных элементов (сообщения, вызовы инструментов).
- `response.usage` - потреблённые токены (`input_tokens`, `output_tokens`, `input_tokens_details.cached_tokens`).
- При срабатывании модерации: `status: incomplete`, `incomplete_details.reason: content_filter`, в `output` - сообщение пользователю. Запрос с модерационным срабатыванием нельзя использовать в `previous_response_id`.
