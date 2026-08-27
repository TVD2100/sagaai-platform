# 📖 SagaAI User Guide

## 🌟 Overview

SagaAI is a universal AI platform powered by large language models, where you can create your own AI assistants and digital employees.

🔹 **New dialog** - chat with your created AI assistants, attach files, search the web.
🔹 **Assistants** - create reusable AI experts with unique prompts and files.
🔹 **Employees** - orchestrator-based AI agents with flexible settings for complex workflows.
🔹 **DevAgent** - universal AI developer for reading, editing and creating code with auto-backups.
🔹 **History** - all conversations are saved, searchable and recoverable.
🔹 **Multilingual** - Russian, English, Chinese and more interface languages.

---

## 🚀 How to start

Below is the correct setup order for full SagaAI functionality.

### 1. ⚙️ Configure LLM

> **Page:** Settings → Providers

First, connect AI services - without them the platform cannot respond.

- Add API keys for required services (OpenAI-compatible, YandexAI, GigaChat, etc.).
- Test the connection with the «🔍 Test connection» button.
- Choose a default model.

💡 **Tip:** API keys can also be set via environment variables `SAGAAI_<SERVICE>_KEY` - then they are not stored in the database.

### 2. 🔧 Configure DevAgent

> **Page:** Sidebar → DevAgent → the ⚙️ «Settings» button in the chat panel. From the welcome page, step 2 («⚙️ Configure DevAgent») opens the settings directly.

DevAgent is your AI programmer. Configure it:

- **Strong model** - for code writing, planning and complex tasks.
- **Weak model** - for quick operations (file reading, backups).
- **Web-search model** - so DevAgent can search the internet.
- **Economy mode** - reduces token consumption on long sessions.

💡 **Tip:** choose powerful models for the strong model, cheaper/faster ones for the weak model.

### 3. 🧩 Create assistants

> **Page:** Settings → Assistants

Assistants are AI experts with unique system prompts.

- Go to Settings → Assistants and click «+ Create assistant».
- Set a name, system prompt and choose a model.
- Optionally attach context files (.txt, .pdf, .md, etc.).
- Select the ready assistant in the sidebar to start chatting immediately.

💡 **Examples:** «Translator», «Proofreader», «Tutor» - the possibilities are limited only by your prompt.

### 4. 👥 Create employees

> **Page:** Settings → Employees

Employees are advanced orchestrator-based AI agents with custom settings, instructions and Python functions. They can execute complex work scenarios according to your scripts.

- Click «🛠 Create employee via DevAgent» - DevAgent will run a questionnaire and set everything up.
- Or open DevAgent and simply ask: «Create an employee for code testing».
- Employees can have their own Python functions, specific instructions and model.

💡 **Examples:** «Data analyst», «SEO optimizer», «Project manager».

---

## 📋 Key features

| Feature | Description |
|---|---|
| 🧩 **Assistants** | Reusable AI experts with prompts and files |
| 👥 **Employees** | Orchestrator-based AI agents with functions and skills |
| 🔧 **DevAgent** | Agent for editing any code, auto-backups, testing |
| 🌐 **Web search** | AI searches the internet when needed |
| 🔢 **Multi-model** | Strong + weak + search model for different tasks |
| 💡 **Economy mode** | Compact context for token savings |
| 📜 **History** | All conversations are saved, searchable and recoverable |
| 🌍 **Languages** | Interface in Russian, English, Chinese and more |

---

## 🛡️ Safety and data protection

- DevAgent **automatically creates a backup** before every file change.
- You can restore individual files or the entire project from system snapshots.
- Optional authentication lets you protect access to the platform with a password.
- API keys saved to the database are stored in protected form; the recovery key is kept outside the data folder.
- API keys can be provided via environment variables - in that case they are not stored in the database.
- DevAgent guards itself against prompt-injection: file contents, tool results and web-search output are marked as data and cleaned from control characters.

---

## 🔢 Interface languages

Select a language in the sidebar (🌐 Language). Supported:
- 🇷🇺 Русский
- 🇬🇧 English
- 🇨🇳 简体中文

The welcome page and all interface elements adapt to the chosen language.
