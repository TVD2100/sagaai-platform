# DevAgent configuration: project paths, protected files, limits.
#
# This module is intentionally dependency-free (stdlib only) so it can be
# imported by every other DevAgent module without import cycles.
#
# Architecture reference: SagaAI_Architecture_v3-3.md, section 7
# ("DevAgent - Embedded Developer") and section 5.3 (project file layout).
#
# ─────────────────────────────────────────────────────────────────────────────
# UNIVERSAL DEVELOPER MODE
# ─────────────────────────────────────────────────────────────────────────────
# DevAgent can operate on an ARBITRARY target folder, not only on the SagaAI
# install itself. Two roots are distinguished:
#
#   INSTALL_ROOT  - where the dev_agent package physically lives. Fixed. Used to
#                   locate system prompts and to know which files form DevAgent's
#                   own Inviolable Core (so the agent can never corrupt itself
#                   when it happens to be working on the SagaAI folder).
#
#   PROJECT_ROOT  - the TARGET work folder the agent reads/edits. Configurable
#                   via the SAGAAI_DEV_TARGET environment variable. Defaults to
#                   INSTALL_ROOT for full backward compatibility.
#
# All path helpers (to_project_relative / resolve_in_project / is_protected) are
# keyed off PROJECT_ROOT, so simply pointing PROJECT_ROOT at another folder makes
# the whole tool set (read_file, list_files, propose_file, backups, patches)
# operate on that folder - with no changes to the protected core modules.

import os
from pathlib import Path
from typing import Optional

# ─── Install root (fixed) ─────────────────────────────────────────────────────
# DevAgent lives at:  <install_root>/dev_agent/config.py
DEV_AGENT_DIR = Path(__file__).resolve().parent
INSTALL_ROOT = DEV_AGENT_DIR.parent

# ─── Target work root (configurable) ──────────────────────────────────────────
# Environment variable that selects the folder DevAgent should develop in.
TARGET_ENV_VAR = "SAGAAI_DEV_TARGET"


def _resolve_project_root() -> Path:
    """Resolve the active target work folder.

    Reads SAGAAI_DEV_TARGET at import time. If unset or empty, falls back to the
    install root (legacy behavior: DevAgent develops SagaAI itself).
    """
    raw = os.environ.get(TARGET_ENV_VAR, "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return INSTALL_ROOT


PROJECT_ROOT = _resolve_project_root()

# True when DevAgent is operating on its own SagaAI install (the legacy case).
# Only in this case do the DevAgent-core files need self-protection.
WORKING_ON_INSTALL = PROJECT_ROOT.resolve() == INSTALL_ROOT.resolve()

# ─── Runtime directories ──────────────────────────────────────────────────────
# When developing the SagaAI install itself (legacy), runtime data stays inside
# the dev_agent package (so existing tests and tooling keep their paths). When
# developing an EXTERNAL target, runtime data lives inside that target under a
# hidden ".dev_agent" folder, so each project owns its own backups/drafts and
# "restore from backup" travels with the project.
if WORKING_ON_INSTALL:
    _RUNTIME_DIR = DEV_AGENT_DIR
else:
    _RUNTIME_DIR = PROJECT_ROOT / ".dev_agent"

# Versioned backups created before any write (see backup_manager.py).
BACKUPS_DIR = _RUNTIME_DIR / "backups"
# Draft area: proposed edits are written here first, never directly to source.
WORKSPACE_DIR = _RUNTIME_DIR / "workspace"
# DevAgent system prompt - single file bundled with the install, never in target.
SYSTEM_PROMPT_FILE = DEV_AGENT_DIR / "system_prompt.md"
CHANGELOG_FILE = PROJECT_ROOT / "CHANGELOG.md"

# ─── Project documentation files (Universal Developer) ─────────────────────────
# Markdown documents the agent maintains inside the TARGET folder so that users
# can read and hand-edit them. See workspace_tools.py for generation logic.
PROJECT_MAP_FILE = PROJECT_ROOT / "PROJECT_MAP.md"        # file map + responsibilities
SPEC_FILE = PROJECT_ROOT / "SPEC.md"                       # requirements specification
ARCHITECTURE_FILE = PROJECT_ROOT / "ARCHITECTURE.md"       # architecture description
README_FILE = PROJECT_ROOT / "README.md"                    # user-facing documentation
# Names (basename only) of the docs DevAgent manages, for scan/skip logic.
PROJECT_DOC_NAMES = ("PROJECT_MAP.md", "SPEC.md", "ARCHITECTURE.md", "CHANGELOG.md", "README.md")

# ─── Inviolable Core ("Неприкосновенное Ядро") ─────────────────────────────────
# DevAgent physically cannot modify these files. Enforced on two levels:
#   1. DevAgent system prompt rule (PROTECTED_FILES).
#   2. A hard check inside safe_writer.py before any write.
#
# If DevAgent breaks any OTHER file, it can restore it from a backup.
# If DevAgent breaks itself, it can be restored manually from source control.
#
# These paths are meaningful ONLY when DevAgent works on its own install
# (WORKING_ON_INSTALL). When developing an external target, none of these files
# exist inside that target, so the tuple is empty and only path-traversal
# protection applies.
_CORE_PROTECTED_FILES = (
    "dev_agent/agent_loop.py",        # core agent loop
    "dev_agent/config.py",            # this configuration
    "dev_agent/safe_writer.py",       # the guard itself
    "dev_agent/backup_manager.py",    # the recovery mechanism
)
PROTECTED_FILES = _CORE_PROTECTED_FILES if WORKING_ON_INSTALL else ()

# ─── Limits & safety ───────────────────────────────────────────────────────────
MAX_BACKUPS_PER_FILE = 50          # rotate oldest backups beyond this count
MAX_TEST_TIMEOUT_SEC = 60          # run_test child-process timeout
MAX_RUN_CODE_TIMEOUT_SEC = 180     # run_code child-process timeout (universal escape hatch)
MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024  # refuse to read/write files larger than this
DEFAULT_ENCODING = "utf-8"

# ─── Single-file mode ─────────────────────────────────────────────────────────
# Central storage so workspace_tools, universal_agent and callers all see the
# same value.  None means full-workspace mode.
TARGET_FILE: Optional[str] = None

# ─── Task-state memory (TASK_STATE.md per dialog thread) ─────────────────────
# TASK_STATE files live in a hidden .dev_agent/task_states folder INSIDE the
# active project - one journal file per dialog thread, named
# TASK_STATE__<thread_id>.md (see task_state.py). ACTIVE_THREAD_ID is set by
# the agent loop at the start of every step so the task-state layer always
# knows which thread's journal it should read/write.
TASK_STATES_DIR = _RUNTIME_DIR / "task_states"
ACTIVE_THREAD_ID: str = ""


# ─── Runtime reconfiguration ──────────────────────────────────────────────────
def set_target_root(path) -> Path:
    """Repoint DevAgent at a new target work folder at runtime.

    Updates the module-level PROJECT_ROOT and every derived path/flag in place,
    so already-imported core modules (which read config.* lazily inside their
    methods) pick up the new root on their next call. Returns the new root.

    Passing None or an empty value resets to the install root (legacy mode).
    """
    global PROJECT_ROOT, WORKING_ON_INSTALL, _RUNTIME_DIR
    global BACKUPS_DIR, WORKSPACE_DIR, CHANGELOG_FILE, TASK_STATES_DIR
    global PROJECT_MAP_FILE, SPEC_FILE, ARCHITECTURE_FILE, README_FILE
    global PROJECT_DOC_NAMES, PROTECTED_FILES
    global TARGET_FILE

    if path is None or str(path).strip() == "":
        os.environ.pop(TARGET_ENV_VAR, None)
        new_root = INSTALL_ROOT
    else:
        new_root = Path(path).expanduser().resolve()
        os.environ[TARGET_ENV_VAR] = str(new_root)

    PROJECT_ROOT = new_root
    WORKING_ON_INSTALL = PROJECT_ROOT.resolve() == INSTALL_ROOT.resolve()
    _RUNTIME_DIR = DEV_AGENT_DIR if WORKING_ON_INSTALL else (PROJECT_ROOT / ".dev_agent")
    BACKUPS_DIR = _RUNTIME_DIR / "backups"
    WORKSPACE_DIR = _RUNTIME_DIR / "workspace"
    TASK_STATES_DIR = _RUNTIME_DIR / "task_states"
    CHANGELOG_FILE = PROJECT_ROOT / "CHANGELOG.md"
    PROJECT_MAP_FILE = PROJECT_ROOT / "PROJECT_MAP.md"
    SPEC_FILE = PROJECT_ROOT / "SPEC.md"
    ARCHITECTURE_FILE = PROJECT_ROOT / "ARCHITECTURE.md"
    README_FILE = PROJECT_ROOT / "README.md"
    PROJECT_DOC_NAMES = ("PROJECT_MAP.md", "SPEC.md", "ARCHITECTURE.md", "CHANGELOG.md", "README.md")
    PROTECTED_FILES = _CORE_PROTECTED_FILES if WORKING_ON_INSTALL else ()
    TARGET_FILE = None   # any workspace switch clears single-file mode

    ensure_runtime_dirs()
    return PROJECT_ROOT


def ensure_runtime_dirs() -> None:
    """Create DevAgent runtime directories if missing. Safe to call repeatedly."""
    for d in (BACKUPS_DIR, WORKSPACE_DIR, TASK_STATES_DIR):
        d.mkdir(parents=True, exist_ok=True)


def to_project_relative(path) -> str:
    """Return a normalized, POSIX-style path relative to PROJECT_ROOT.

    Raises ValueError if the resolved path escapes PROJECT_ROOT (path traversal
    protection). Accepts absolute or relative inputs.
    """
    p = Path(path)
    if not p.is_absolute():
        p = (PROJECT_ROOT / p)
    resolved = p.resolve()
    try:
        rel = resolved.relative_to(PROJECT_ROOT.resolve())
    except ValueError as exc:
        raise ValueError(
            f"Path escapes project root: {path!r} (resolved {resolved})"
        ) from exc
    return rel.as_posix()


def is_protected(path) -> bool:
    """True if the given path targets one of the inviolable-core files."""
    try:
        rel = to_project_relative(path)
    except ValueError:
        # A traversal attempt is treated as protected (deny by default).
        return True
    return rel in PROTECTED_FILES


def resolve_in_project(path) -> Path:
    """Resolve a path inside the project and verify it stays within PROJECT_ROOT.

    Returns an absolute Path. Raises ValueError on traversal.
    """
    rel = to_project_relative(path)  # validates containment
    return (PROJECT_ROOT / rel).resolve()
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
