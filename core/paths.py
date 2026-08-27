"""
core.paths - base directories for SagaAI.
Reads SAGAAI_DATA_DIR from the environment; falls back to the package directory.
No streamlit imports.
"""
import os
from pathlib import Path

# Allow overriding via environment variable for tests or deployments
_env_data = os.environ.get("SAGAAI_DATA_DIR", "")

# BASE_DIR: directory where the sagaai package lives
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# DATA_DIR: where runtime data (DB, prompts, history) is stored
DATA_DIR: str = _env_data if _env_data else BASE_DIR

# DEFAULTS_DIR: bundled default data (orchestrators, assistants, instructions,
# services, langs, settings, skills). Deleting a file/folder here excludes the
# corresponding entity from the default import; adding one includes it.
DEFAULTS_DIR: str = os.path.join(BASE_DIR, "defaults")

SYSTEM_PROMPTS_DIR: str = os.path.join(DATA_DIR, "system_prompts")
LANGS_DIR: str          = os.path.join(BASE_DIR, "langs")
SERVICES_DIR: str       = os.path.join(BASE_DIR, "services")
HISTORY_DIR: str        = os.path.join(DATA_DIR, "history")
DB_PATH: str            = os.path.join(DATA_DIR, "sagaai.db")
DEVAGENT_DB_PATH: str   = os.path.join(DATA_DIR, "devagent.db")
RAG_BASES_DIR: str      = os.path.join(DATA_DIR, "rag_bases")

TEXT_FILE_EXTENSIONS = (".txt", ".md", ".py", ".csv")
SUPPORTED_UPLOAD_TYPES = ["txt", "md", "py", "csv", "pdf", "docx", "pptx", "xlsx"]


def ensure_data_dirs() -> None:
    """Create all required runtime directories if they do not exist."""
    for d in (SYSTEM_PROMPTS_DIR, LANGS_DIR, SERVICES_DIR, HISTORY_DIR, RAG_BASES_DIR):
        Path(d).mkdir(parents=True, exist_ok=True)


def get_thread_dir(tid: str) -> str:
    """Return the directory path for a thread by its ID."""
    return os.path.join(HISTORY_DIR, tid)


def get_thread_file_path(tid: str, file_name: str) -> str:
    """Return the path to a file stored in a thread's files directory."""
    fname_lower = file_name.lower()
    if not (fname_lower.endswith(".txt") or fname_lower.endswith(".md")):
        file_name = f"{file_name}.txt"
    return os.path.join(get_thread_dir(tid), "files", file_name)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
