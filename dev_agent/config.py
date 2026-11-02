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
from typing import Any, Dict, Optional

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

# ─── Empty workspace state ("no folder selected") ─────────────────────────────
# A NEW dialog starts with NO workspace. Instead of inheriting whatever folder
# another dialog happens to use (the historical leak), a bound thread without
# a chosen folder runs in this NEUTRAL root: file tools are blocked by the
# dispatch guard (see tool_executor) and WORKSPACE_SELECTED is False, so the
# UI/agent can tell the user to pick a folder first.
NEUTRAL_ROOT = (INSTALL_ROOT / ".dev_agent" / "neutral").resolve()

# True when a concrete target folder was explicitly selected. False only in
# the neutral empty state. Legacy callers (tests, scripts) select a root via
# set_target_root/apply_paths and stay unaffected.
WORKSPACE_SELECTED: bool = PROJECT_ROOT.resolve() != NEUTRAL_ROOT


def ensure_neutral_root() -> Path:
    """Create and return the neutral (empty-state) root folder."""
    NEUTRAL_ROOT.mkdir(parents=True, exist_ok=True)
    return NEUTRAL_ROOT

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
AGENT_FILE = PROJECT_ROOT / "AGENT.md"                    # key project information for agents
ARCHITECTURE_FILE = PROJECT_ROOT / "ARCHITECTURE.md"       # architecture description
README_FILE = PROJECT_ROOT / "README.md"                    # user-facing documentation
# Names (basename only) of the docs DevAgent manages, for scan/skip logic.
# SPEC.md is the legacy name of AGENT.md, still recognized during the transition.
PROJECT_DOC_NAMES = ("PROJECT_MAP.md", "AGENT.md", "ARCHITECTURE.md", "CHANGELOG.md", "README.md", "SPEC.md")

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
MAX_TEST_TIMEOUT_SEC = 180         # run_test child-process timeout
MAX_RUN_CODE_TIMEOUT_SEC = 300     # run_code child-process timeout (universal escape hatch)
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
    if path is None or str(path).strip() == "":
        # No os.environ mutation: the process environment is shared by all
        # dialogs and leaked the last-switched workspace into subprocesses.
        new_root = INSTALL_ROOT
    else:
        new_root = Path(path).expanduser().resolve()

    apply_paths(new_root, target_file=None, thread_id=None, create_dirs=True)
    return PROJECT_ROOT


def apply_paths(root, *, target_file: Optional[str] = None,
                thread_id: Optional[str] = None,
                create_dirs: bool = False,
                selected: Optional[bool] = None) -> None:
    """Repoint every derived path/flag at a (possibly new) target root.

    ``selected=False`` switches to the NEUTRAL empty state ("no folder
    selected"): PROJECT_ROOT becomes the neutral root and file tools are
    blocked by the dispatch guard. When ``selected`` is None the flag is
    derived from the root itself (a real root counts as selected).

    The single writer for all mutable workspace state. Lighter than
    set_target_root: does not create runtime dirs unless asked and does not
    clear the target file unless one is explicitly provided. The workspace-
    binding layer uses it to swap state for the duration of one tool call.
    """
    global PROJECT_ROOT, WORKING_ON_INSTALL, _RUNTIME_DIR
    global BACKUPS_DIR, WORKSPACE_DIR, CHANGELOG_FILE, TASK_STATES_DIR
    global PROJECT_MAP_FILE, AGENT_FILE, ARCHITECTURE_FILE, README_FILE
    global PROJECT_DOC_NAMES, PROTECTED_FILES, TARGET_FILE, ACTIVE_THREAD_ID
    global WORKSPACE_SELECTED

    if selected is False:
        new_root = NEUTRAL_ROOT
        WORKSPACE_SELECTED = False
    else:
        new_root = Path(root).expanduser().resolve()
        WORKSPACE_SELECTED = (True if selected is True
                              else new_root != NEUTRAL_ROOT)
    PROJECT_ROOT = new_root
    WORKING_ON_INSTALL = new_root == INSTALL_ROOT.resolve()
    _RUNTIME_DIR = DEV_AGENT_DIR if WORKING_ON_INSTALL else (new_root / ".dev_agent")
    BACKUPS_DIR = _RUNTIME_DIR / "backups"
    WORKSPACE_DIR = _RUNTIME_DIR / "workspace"
    TASK_STATES_DIR = _RUNTIME_DIR / "task_states"
    CHANGELOG_FILE = new_root / "CHANGELOG.md"
    PROJECT_MAP_FILE = new_root / "PROJECT_MAP.md"
    AGENT_FILE = new_root / "AGENT.md"
    ARCHITECTURE_FILE = new_root / "ARCHITECTURE.md"
    README_FILE = new_root / "README.md"
    # SPEC.md is the legacy name of AGENT.md, still recognized during the transition.
    PROJECT_DOC_NAMES = ("PROJECT_MAP.md", "AGENT.md", "ARCHITECTURE.md", "CHANGELOG.md", "README.md", "SPEC.md")
    PROTECTED_FILES = _CORE_PROTECTED_FILES if WORKING_ON_INSTALL else ()
    if target_file is not None:
        TARGET_FILE = target_file or None
    if thread_id is not None:
        ACTIVE_THREAD_ID = thread_id

    if create_dirs:
        ensure_runtime_dirs()


def snapshot_state() -> Dict[str, Any]:
    """Capture the full mutable workspace state for later restore_state()."""
    return {
        "PROJECT_ROOT": PROJECT_ROOT,
        "WORKING_ON_INSTALL": WORKING_ON_INSTALL,
        "BACKUPS_DIR": BACKUPS_DIR,
        "WORKSPACE_DIR": WORKSPACE_DIR,
        "TASK_STATES_DIR": TASK_STATES_DIR,
        "CHANGELOG_FILE": CHANGELOG_FILE,
        "PROJECT_MAP_FILE": PROJECT_MAP_FILE,
        "AGENT_FILE": AGENT_FILE,
        "ARCHITECTURE_FILE": ARCHITECTURE_FILE,
        "README_FILE": README_FILE,
        "PROJECT_DOC_NAMES": PROJECT_DOC_NAMES,
        "PROTECTED_FILES": PROTECTED_FILES,
        "TARGET_FILE": TARGET_FILE,
        "ACTIVE_THREAD_ID": ACTIVE_THREAD_ID,
        "WORKSPACE_SELECTED": WORKSPACE_SELECTED,
        "MEMORY_ROOT": MEMORY_ROOT,
    }


def restore_state(state: Dict[str, Any]) -> None:
    """Restore every mutable global captured by snapshot_state()."""
    global PROJECT_ROOT, WORKING_ON_INSTALL
    global BACKUPS_DIR, WORKSPACE_DIR, TASK_STATES_DIR
    global CHANGELOG_FILE, PROJECT_MAP_FILE, AGENT_FILE
    global ARCHITECTURE_FILE, README_FILE, PROJECT_DOC_NAMES
    global PROTECTED_FILES, TARGET_FILE, ACTIVE_THREAD_ID
    global WORKSPACE_SELECTED

    PROJECT_ROOT = state["PROJECT_ROOT"]
    WORKING_ON_INSTALL = state["WORKING_ON_INSTALL"]
    BACKUPS_DIR = state["BACKUPS_DIR"]
    WORKSPACE_DIR = state["WORKSPACE_DIR"]
    TASK_STATES_DIR = state["TASK_STATES_DIR"]
    CHANGELOG_FILE = state["CHANGELOG_FILE"]
    PROJECT_MAP_FILE = state["PROJECT_MAP_FILE"]
    AGENT_FILE = state["AGENT_FILE"]
    ARCHITECTURE_FILE = state["ARCHITECTURE_FILE"]
    README_FILE = state["README_FILE"]
    PROJECT_DOC_NAMES = state["PROJECT_DOC_NAMES"]
    PROTECTED_FILES = state["PROTECTED_FILES"]
    TARGET_FILE = state["TARGET_FILE"]
    ACTIVE_THREAD_ID = state["ACTIVE_THREAD_ID"]
    WORKSPACE_SELECTED = state.get("WORKSPACE_SELECTED", True)
    # The working-memory root travels with the thread state: two dialogs of two
    # different employees must never resolve ``memory:`` to each other's folder.
    global MEMORY_ROOT
    MEMORY_ROOT = state.get("MEMORY_ROOT")


def ensure_runtime_dirs() -> None:
    """Create DevAgent runtime directories if missing. Safe to call repeatedly."""
    for d in (BACKUPS_DIR, WORKSPACE_DIR, TASK_STATES_DIR):
        d.mkdir(parents=True, exist_ok=True)


# ─── Working memory root (the agent's own data folder) ───────────────────────
# The working-data folder of an employee (registries, cards, journals) lives in
# the INSTALLATION - DATA_DIR/agents_data/<slug> by default, or wherever the
# employee's MEMORY_DIR marker points. It is a second allowed root next to the
# project: file tools accept paths inside it, addressed either by the
# ``memory:`` prefix (``memory:registry.md``) or by an absolute path.
MEMORY_PREFIX: str = "memory:"

#: Absolute path of the active employee's working-data folder, or None.
MEMORY_ROOT: Optional[Path] = None


def set_memory_root(slug: str = "", workspace: str = "") -> Optional[Path]:
    """Point MEMORY_ROOT at the resolved working-data folder of *slug*.

    Resolution follows the platform rules (prompt marker, config value, central
    default) through ``core.orchestrator_folders.resolve_data_dir``, so the
    folder the tools reach is exactly the folder the prompt announces. A slug
    whose folder cannot be resolved disables the second root instead of
    guessing. Returns the new root (or None).
    """
    global MEMORY_ROOT
    safe = (slug or "").strip()
    if not safe:
        MEMORY_ROOT = None
        return None
    try:
        from core.orchestrator_folders import resolve_data_dir
        resolved = resolve_data_dir(safe, workspace or "")
        path = str(resolved.get("path") or "")
    except Exception:
        path = ""
    if not path:
        MEMORY_ROOT = None
        return None
    try:
        MEMORY_ROOT = Path(path).expanduser().resolve()
    except (OSError, RuntimeError):
        MEMORY_ROOT = None
    return MEMORY_ROOT


def memory_root() -> Optional[Path]:
    """Return the active working-memory root, or None when unavailable."""
    return MEMORY_ROOT


def is_memory_path(path) -> bool:
    """True when *path* addresses the working-memory root (prefix or absolute)."""
    raw = str(path or "")
    if raw.lower().startswith(MEMORY_PREFIX):
        return True
    if MEMORY_ROOT is None or not raw:
        return False
    try:
        p = Path(raw).expanduser()
    except (OSError, RuntimeError):
        return False
    if not p.is_absolute():
        return False
    try:
        p.resolve().relative_to(MEMORY_ROOT)
        return True
    except (ValueError, OSError, RuntimeError):
        return False


def to_project_relative(path) -> str:
    """Return a normalized, POSIX-style path relative to its allowed root.

    A path inside PROJECT_ROOT becomes project-relative; a path inside the
    working-memory root becomes ``memory:<relative>`` so the model can address
    it again with the same string. Raises ValueError when the resolved path
    escapes BOTH roots (path traversal protection). Accepts absolute or
    relative inputs.
    """
    raw = str(path or "")
    if raw.lower().startswith(MEMORY_PREFIX):
        if MEMORY_ROOT is None:
            raise ValueError(
                f"Working memory is not available for this employee: {path!r}"
            )
        return MEMORY_PREFIX + _memory_relative(raw[len(MEMORY_PREFIX):])
    p = Path(raw)
    if not p.is_absolute():
        p = (PROJECT_ROOT / p)
    resolved = p.resolve()
    try:
        rel = resolved.relative_to(PROJECT_ROOT.resolve())
        return rel.as_posix()
    except ValueError:
        pass
    if MEMORY_ROOT is not None:
        try:
            rel = resolved.relative_to(MEMORY_ROOT)
            return MEMORY_PREFIX + rel.as_posix()
        except ValueError:
            pass
    raise ValueError(
        f"Path escapes project root: {path!r} (resolved {resolved})"
    )


def _memory_relative(sub: str) -> str:
    """Validate a ``memory:`` sub-path and return it in POSIX form."""
    if MEMORY_ROOT is None:
        raise ValueError("Working memory is not available for this employee")
    candidate = (MEMORY_ROOT / sub.strip().lstrip("/")).resolve()
    try:
        rel = candidate.relative_to(MEMORY_ROOT)
    except ValueError as exc:
        raise ValueError(
            f"Path escapes the working-memory folder: {sub!r}"
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
    """Resolve a path inside one of the allowed roots.

    Two roots are accepted: the active project (PROJECT_ROOT) and the
    employee's working-data folder (MEMORY_ROOT), the latter addressed by the
    ``memory:`` prefix or by an absolute path inside it. Returns an absolute
    Path. Raises ValueError on traversal.
    """
    rel = to_project_relative(path)  # validates containment
    if rel.startswith(MEMORY_PREFIX):
        assert MEMORY_ROOT is not None  # to_project_relative already checked
        return (MEMORY_ROOT / rel[len(MEMORY_PREFIX):]).resolve()
    return (PROJECT_ROOT / rel).resolve()
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
