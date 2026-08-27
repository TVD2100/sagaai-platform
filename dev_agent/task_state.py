# -*- coding: utf-8 -*-
"""Task State - per-thread task journal (external memory) for DevAgent.

For EVERY task (big or small) DevAgent maintains one Markdown journal file
for the current dialog thread, stored in the project's hidden runtime dir:

    <project>/.dev_agent/task_states/TASK_STATE__<thread_id>.md

Key behaviours (v2):

* The file name embeds the dialog thread id. The thread id and the file
  path are passed to the model inside the injected context block (meta
  info in the system prompt), so the agent always knows WHERE the journal
  lives (see task_state_for_context()).
* The file is NEVER deleted. When a task completes, its condensed summary
  is archived into the "Task History" section of the SAME file; when a new
  task starts in the SAME thread, it is appended to the SAME journal (one
  journal per thread, many tasks).
* Each plan step supports a ``- context:`` meta line - the condensed state
  the NEXT step needs - so the agent can continue correctly even when a
  large part of its chat history is truncated (economy mode).

Journal layout::

    # Task State Journal
    ## Active Task
    - started: <iso>
    - updated: <iso>
    ### Task
    ### Architecture
    ### Plan
    ### Step 1 - ... (status: done)
    ### Progress
    ### Handoff
    ## Task History
    ### Completed 1 - <title>
    - finished: <iso>
    - completed_steps: 2/3
    - summary: <condensed context>

The Active Task block is injected into the LLM context before every request
by the agent loop (agent_loop._maybe_task_state_context).

A legacy project-root TASK_STATE.md (previous format) is migrated once into
the new journal by _migrate_legacy_file() - nothing is lost, the old file is
kept under a .legacy name.

Only plain stdlib is used. Writes use explicit UTF-8 encoding and are
preceded by a best-effort backup via BackupManager.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config
from .backup_manager import BackupManager

# Journal file naming: TASK_STATE__<thread_id>.md
TASK_STATE_PREFIX = "TASK_STATE__"
TASK_STATE_SUFFIX = ".md"
# Legacy file name (project root) kept for one-time migration.
TASK_STATE_FILENAME = "TASK_STATE.md"

# Hard cap on how many characters are injected into the LLM context.
MAX_STATE_CHARS = 8000

# Active-task section keys (canonical, lowercase keys used by the tools).
_SECTION_KEYS = {"task", "architecture", "plan", "progress", "handoff"}
_SECTION_TITLES = {
    "task": "Task",
    "architecture": "Architecture",
    "plan": "Plan",
    "progress": "Progress",
    "handoff": "Handoff",
}

# Top-level journal sections.
_TOP_TITLES = {"active_task": "Active Task", "task_history": "Task History"}

_NOT_SET_MARKER = "_(not set)_"


class TaskStateError(Exception):
    """Raised when task-state operations receive invalid input."""


# ─── Thread id & paths ─────────────────────────────────────────────────────────

def current_thread_id() -> str:
    """Return the current dialog thread id (from config.ACTIVE_THREAD_ID).

    Falls back to ``nothread`` when no thread is set (single-file mode / no
    dialog attached), so the journal is still written for every task.
    """
    tid = (getattr(config, "ACTIVE_THREAD_ID", "") or "").strip()
    if not tid:
        return "nothread"
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", tid).strip("_")
    return safe or "nothread"


def task_state_path() -> Path:
    """Absolute path of THIS thread's journal file."""
    return config.TASK_STATES_DIR / f"{TASK_STATE_PREFIX}{current_thread_id()}{TASK_STATE_SUFFIX}"


def _now_iso() -> str:
    """Current UTC time as a human-friendly ISO string."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _backup_if_exists(path: Path) -> None:
    """Best-effort backup of *path* before it is overwritten.

    Failures are swallowed so a backup issue never blocks the write.
    """
    if not path.exists():
        return
    try:
        bm = BackupManager()
        bm.create_backup(str(path), note="pre-task-state-overwrite backup")
    except Exception:
        pass


# ─── Parsing helpers ───────────────────────────────────────────────────────────

def _split_top_sections(content: str) -> Dict[str, str]:
    """Split a journal into '## Active Task' / '## Task History' sections."""
    result: Dict[str, str] = {}
    lines = content.split("\n")
    current: Optional[str] = None
    buf: List[str] = []
    for ln in lines:
        m = re.match(r"^##\s+([A-Za-zА-Яа-яЁё_ -]+)\s*$", ln.strip())
        if m:
            if current:
                result[current] = "\n".join(buf).strip()
            title = m.group(1).strip().lower().replace(" ", "_")
            current = title if title in _TOP_TITLES else None
            buf = []
        else:
            if current:
                buf.append(ln)
    if current:
        result[current] = "\n".join(buf).strip()
    return result


def _split_active_sections(active_body: str) -> Dict[str, str]:
    """Split the Active Task body into '### Task/Architecture/Plan/...' blocks.

    Step headings (``### Step <digit> ...``) never match the heading regex
    (digits are not allowed by the character class), so they stay inside the
    Plan block.
    """
    result: Dict[str, str] = {}
    lines = (active_body or "").split("\n")
    current: Optional[str] = None
    buf: List[str] = []
    for ln in lines:
        m = re.match(r"^###\s+([A-Za-zА-Яа-яЁё_ -]+)\s*$", ln.strip())
        if m:
            if current:
                result[current] = "\n".join(buf).strip()
            title = m.group(1).strip().lower().replace(" ", "_")
            current = title if title in _SECTION_KEYS else None
            buf = []
        else:
            if current:
                buf.append(ln)
    if current:
        result[current] = "\n".join(buf).strip()
    return result


def _parse_legacy(content: str) -> Dict[str, str]:
    """Parse the OLD project-root TASK_STATE.md format (## Task / ## Plan ...)."""
    result: Dict[str, str] = {}
    current: Optional[str] = None
    buf: List[str] = []
    for ln in content.split("\n"):
        m = re.match(r"^##\s+([A-Za-zА-Яа-яЁё_ -]+)\s*$", ln.strip())
        if m:
            title = m.group(1).strip().lower().replace(" ", "_")
            if title in _SECTION_KEYS:
                if current:
                    result[current] = "\n".join(buf).strip()
                current = title
                buf = []
                continue
        if current:
            buf.append(ln)
    if current:
        result[current] = "\n".join(buf).strip()
    return result


def _read_active_meta(active_body: str) -> Dict[str, str]:
    """Read meta lines (started/updated/status) at the top of the Active Task."""
    meta: Dict[str, str] = {}
    for ln in (active_body or "").splitlines():
        if ln.strip().startswith("###"):
            break
        m = re.match(r"^- (started|updated|status):\s*(.*)$", ln.strip())
        if m:
            meta[m.group(1)] = m.group(2).strip()
    return meta


def _parse_history_entries(content: str) -> List[Dict[str, str]]:
    """Parse the Task History section into a list of completed-task dicts."""
    if not content:
        return []
    entries: List[Dict[str, str]] = []
    blocks = re.split(r"(?m)^###\s+Completed\s+\d+\s*[-:]+\s*", content)
    for block in blocks[1:]:
        lines = block.split("\n")
        if not lines:
            continue
        entry: Dict[str, str] = {"task": lines[0].strip(), "summary": ""}
        summary_parts: List[str] = []
        in_summary = False
        for ln in lines[1:]:
            s = ln.strip()
            m = re.match(r"^- (started|finished|status|completed_steps|summary):\s*(.*)$", s)
            if m:
                if m.group(1) == "summary":
                    in_summary = True
                    if m.group(2).strip():
                        summary_parts.append(m.group(2).strip())
                else:
                    in_summary = False
                    entry[m.group(1)] = m.group(2).strip()
            elif in_summary and s:
                summary_parts.append(s)
        entry["summary"] = "\n".join(summary_parts).strip()
        entries.append(entry)
    return entries


def _parse_step(line: str) -> Optional[Dict[str, Any]]:
    """Parse a plan step heading into a dict.

    Input: `### Step 3 - Some title` (em-dash, hyphen or colon accepted).
    Output: {"num": 3, "title": "Some title", "id": "step_3"}.
    """
    m = re.match(
        r"^###\s+Step\s+(\d+)\s*(?:\s*[\u2014\u2013\-:]\s*|\s+)(.+?)\s*$",
        line.strip(),
        re.IGNORECASE,
    )
    if not m:
        return None
    num = int(m.group(1))
    return {"num": num, "title": m.group(2).strip(), "id": f"step_{num}"}


def extract_step_ids(content: str) -> List[str]:
    """Return the list of plan step ids present in a plan text."""
    ids: List[str] = []
    for ln in content.split("\n"):
        if ln.strip().startswith("### Step "):
            info = _parse_step(ln)
            if info:
                ids.append(info["id"])
    return ids


# ─── Rendering ─────────────────────────────────────────────────────────────────

def render_active_task(
    task: str = "",
    architecture: str = "",
    plan: str = "",
    progress: str = "",
    handoff: str = "",
    started: str = "",
) -> str:
    """Render the '## Active Task' journal section."""
    parts: List[str] = ["## Active Task", ""]
    parts.append(f"- started: {started or _now_iso()}")
    parts.append(f"- updated: {_now_iso()}")
    parts.append("")
    for key in ("task", "architecture", "plan", "progress", "handoff"):
        value = (locals()[key] or "").strip()
        parts.append(f"### {_SECTION_TITLES[key]}")
        parts.append("")
        parts.append(value if value else _NOT_SET_MARKER)
        parts.append("")
    return "\n".join(parts).rstrip()


def render_task_history(entries: List[Dict[str, str]]) -> str:
    """Render the '## Task History' journal section from completed-task dicts."""
    if not entries:
        return f"## Task History\n\n{_NOT_SET_MARKER}"
    parts: List[str] = ["## Task History", ""]
    for i, entry in enumerate(entries, start=1):
        raw_title = (entry.get("task") or "Completed task").strip()
        title = (raw_title.splitlines() or ["Completed task"])[0][:120]
        parts.append(f"### Completed {i} - {title}")
        for key in ("started", "finished", "status", "completed_steps"):
            value = (entry.get(key) or "").strip()
            if value:
                parts.append(f"- {key}: {value}")
        summary = (entry.get("summary") or "").strip()
        if summary:
            parts.append("")
            summary_lines = summary.splitlines()
            parts.append(f"- summary: {summary_lines[0]}")
            for extra in summary_lines[1:]:
                parts.append("  " + extra)
        parts.append("")
    return "\n".join(parts).rstrip()


def build_task_state(
    task: str = "",
    architecture: str = "",
    plan: str = "",
    progress: str = "",
    handoff: str = "",
    history: Optional[List[Dict[str, str]]] = None,
    started: str = "",
) -> str:
    """Render the complete journal file content.

    Missing active-task sections are scaffolded with ``_(not set)_`` so the
    file always has a stable, parseable structure.
    """
    parts = [
        "# Task State Journal",
        "",
        f"> Thread: {current_thread_id()}",
        "> Журнал задач этого диалога. Ведётся DevAgent автоматически.",
        "> Файл не удаляется: выполненные задачи архивируются в Task History,",
        "> а новые задачи этого же треда дополняют этот же файл.",
        "",
        render_active_task(
            task=task, architecture=architecture, plan=plan,
            progress=progress, handoff=handoff, started=started,
        ),
        "",
        render_task_history(history or []),
        "",
    ]
    return "\n".join(parts).rstrip() + "\n"


def _has_active_content(active: Dict[str, str]) -> bool:
    """True when the Active Task block carries any real content."""
    return any(
        (active.get(k) or "").strip() not in ("", _NOT_SET_MARKER)
        for k in _SECTION_KEYS
    )


def _summarize_active(active: Dict[str, str]) -> str:
    """Condensed context of a task: step contexts/results + handoff facts.

    This is what the next stage (or the next task in the same thread) needs,
    so the agent keeps working correctly even when chat history is truncated.
    """
    parts: List[str] = []
    plan_text = active.get("plan", "") or ""
    for ln in plan_text.splitlines():
        s = ln.strip()
        if s.startswith("- context:") or s.startswith("- result:"):
            parts.append(s)
    handoff = (active.get("handoff") or "").strip()
    if handoff and handoff != _NOT_SET_MARKER:
        parts.append(handoff)
    return "\n".join(parts).strip()


def _archive_active_task(
    active: Dict[str, str],
    meta: Dict[str, str],
    finished: str,
    status: str = "done",
) -> Dict[str, str]:
    """Build a Task History entry from the current Active Task block."""
    plan_text = active.get("plan", "") or ""
    steps = extract_step_ids(plan_text)
    done_count = len(re.findall(r"\(status:\s*done\)", plan_text))
    return {
        "task": (active.get("task") or "").strip() or "Unnamed task",
        "started": (meta.get("started") or "").strip(),
        "finished": finished,
        "status": status,
        "completed_steps": f"{done_count}/{len(steps)}" if steps else "",
        "summary": _summarize_active(active),
    }


def _write_raw(text: str) -> Path:
    """Backup + write *text* to the journal file. Returns the path."""
    config.ensure_runtime_dirs()
    path = task_state_path()
    _backup_if_exists(path)
    path.write_text(text, encoding=config.DEFAULT_ENCODING)
    return path


def _write_journal(
    active: Dict[str, str],
    history: List[Dict[str, str]],
    started: str = "",
) -> str:
    """Render + write the journal; returns the rendered text."""
    text = build_task_state(
        task=active.get("task", ""),
        architecture=active.get("architecture", ""),
        plan=active.get("plan", ""),
        progress=active.get("progress", ""),
        handoff=active.get("handoff", ""),
        history=history,
        started=started,
    )
    _write_raw(text)
    return text


def _migrate_legacy_file() -> bool:
    """One-time migration of a legacy project-root TASK_STATE.md.

    The old file is archived as the first Completed task in the new journal
    and renamed to TASK_STATE.md.legacy (data is never lost). Returns True
    when a migration actually happened.
    """
    legacy = config.PROJECT_ROOT / TASK_STATE_FILENAME
    target = task_state_path()
    if not legacy.exists() or target.exists():
        return False
    try:
        content = legacy.read_text(encoding=config.DEFAULT_ENCODING)
        sections = _parse_legacy(content)
        if not any((sections.get(k) or "").strip() for k in _SECTION_KEYS):
            return False
        entry: Dict[str, str] = {
            "task": (sections.get("task") or "Legacy task").strip()[:200] or "Legacy task",
            "started": _now_iso(),
            "finished": _now_iso(),
            "status": "done",
            "completed_steps": "",
            "summary": _summarize_active(sections),
        }
        _write_raw(build_task_state(history=[entry], started=""))
        legacy.rename(legacy.with_suffix(".md.legacy"))
        return True
    except Exception:
        return False


# ─── File operations (journal API) ─────────────────────────────────────────────

def ensure_task_state_file(force: bool = False) -> Dict[str, Any]:
    """Create a scaffolded journal when missing (or when *force*).

    Never overwrites an existing non-forced journal. Also performs the
    one-time migration of a legacy root TASK_STATE.md. Returns the
    tool-style result dict.
    """
    config.ensure_runtime_dirs()
    migrated = _migrate_legacy_file()
    path = task_state_path()
    if path.exists() and not force:
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        return {
            "ok": True, "path": str(path), "thread_id": current_thread_id(),
            "exists": True, "wrote": False, "migrated_legacy": migrated,
            "size_bytes": size,
        }
    if force:
        _backup_if_exists(path)
    try:
        text = build_task_state(history=[], started=_now_iso())
        path.write_text(text, encoding=config.DEFAULT_ENCODING)
    except OSError as e:
        return {"ok": False, "error": str(e)}
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    return {
        "ok": True, "path": str(path), "thread_id": current_thread_id(),
        "exists": True, "wrote": True, "migrated_legacy": migrated, "size_bytes": size,
    }


def read_task_state() -> Dict[str, Any]:
    """Read and parse this thread's journal.

    Returns {"ok", "path", "exists", "thread_id", "content", "sections"
    (active task task/architecture/plan/progress/handoff), "step_ids",
    "history" (completed tasks), "size_bytes"}. When the file is missing,
    returns exists=False (the feature must never be an error).
    """
    _migrate_legacy_file()
    path = task_state_path()
    if not path.exists():
        return {
            "ok": True, "path": str(path), "thread_id": current_thread_id(),
            "exists": False, "content": "", "sections": {}, "step_ids": [],
            "history": [], "size_bytes": 0,
        }
    try:
        content = path.read_text(encoding=config.DEFAULT_ENCODING)
    except OSError as e:
        return {"ok": False, "error": str(e)}
    top = _split_top_sections(content)
    active = _split_active_sections(top.get("active_task", ""))
    return {
        "ok": True,
        "path": str(path),
        "thread_id": current_thread_id(),
        "exists": True,
        "content": content,
        "sections": active,
        "step_ids": extract_step_ids(active.get("plan", "")),
        "history": _parse_history_entries(top.get("task_history", "")),
        "size_bytes": len(content.encode(config.DEFAULT_ENCODING)),
    }


def archive_and_start_task(task: str, architecture: str = "", plan: str = "") -> Dict[str, Any]:
    """Start a NEW task in this thread's journal.

    If the journal already has an Active Task with real content, it is first
    archived into Task History (requirement: a new task in the same thread
    extends the same file). The journal file itself is never deleted.
    """
    task = (task or "").strip()
    architecture = (architecture or "").strip()
    plan = (plan or "").strip()
    _migrate_legacy_file()
    path = task_state_path()
    archived_previous = False
    history: List[Dict[str, str]] = []
    if path.exists():
        try:
            content = path.read_text(encoding=config.DEFAULT_ENCODING)
            top = _split_top_sections(content)
            active = _split_active_sections(top.get("active_task", ""))
            history = _parse_history_entries(top.get("task_history", ""))
            if _has_active_content(active):
                meta = _read_active_meta(top.get("active_task", ""))
                history.append(_archive_active_task(active, meta, _now_iso()))
                archived_previous = True
        except OSError as e:
            return {"ok": False, "error": str(e)}
    text = build_task_state(
        task=task, architecture=architecture, plan=plan,
        history=history, started=_now_iso(),
    )
    try:
        _write_raw(text)
    except OSError as e:
        return {"ok": False, "error": str(e)}
    return {
        "ok": True,
        "path": str(path),
        "thread_id": current_thread_id(),
        "size_bytes": len(text.encode(config.DEFAULT_ENCODING)),
        "step_ids": extract_step_ids(plan),
        "archived_previous": archived_previous,
        "history_entries": len(history),
    }


def update_task_state_section(section: str, content: str) -> Dict[str, Any]:
    """Update one section of the Active Task, preserving all others.

    Args:
        section: one of task|architecture|plan|progress|handoff.
        content: new section body (without the `### Title` heading).

    Creates a scaffolded journal first if it does not exist.
    """
    key = (section or "").strip().lower()
    if key not in _SECTION_KEYS:
        return {"ok": False, "error": f"Unknown section: {section}. "
                                      f"Use one of: {', '.join(sorted(_SECTION_KEYS))}."}
    _migrate_legacy_file()
    path = task_state_path()
    if not path.exists():
        ensure_res = ensure_task_state_file()
        if not ensure_res.get("ok"):
            return ensure_res
    try:
        current = path.read_text(encoding=config.DEFAULT_ENCODING)
    except OSError as e:
        return {"ok": False, "error": str(e)}
    top = _split_top_sections(current)
    active = _split_active_sections(top.get("active_task", ""))
    history = _parse_history_entries(top.get("task_history", ""))
    meta = _read_active_meta(top.get("active_task", ""))
    active[key] = (content or "").strip()
    try:
        new_text = _write_journal(active, history, started=meta.get("started", ""))
    except OSError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "path": str(path), "section": key,
            "thread_id": current_thread_id(),
            "size_bytes": len(new_text.encode(config.DEFAULT_ENCODING)),
            "step_ids": extract_step_ids(active.get("plan", ""))}


def _set_meta_line(block: List[str], key: str, value: str) -> List[str]:
    """Replace (or append) a `- key: value` meta line inside a step block."""
    prefix = f"- {key}:"
    found = False
    out: List[str] = []
    for ln in block:
        if ln.strip().startswith(prefix):
            out.append(f"- {key}: {value}")
            found = True
        else:
            out.append(ln)
    if not found:
        out.append(f"- {key}: {value}")
    return out


def update_plan_step_status(
    step_id: str,
    status: str = "done",
    verification: Optional[str] = None,
    result: Optional[str] = None,
    context: Optional[str] = None,
) -> Dict[str, Any]:
    """Mark one plan step, update Progress, and record its summary context.

    The step must exist as `### Step N -- title` in the Active Task's Plan.
    Its status is set to one of pending|in_progress|done|blocked. When
    *verification* / *result* / *context* are given, matching meta lines
    inside the step block are replaced (or appended). *context* is the
    condensed state the NEXT step needs (requirement: the agent must be able
    to continue from the journal alone when chat history is truncated).
    """
    status = (status or "done").strip().lower()
    if status not in {"pending", "in_progress", "done", "blocked"}:
        return {"ok": False, "error": f"Invalid status: {status}. "
                                      "Use pending|in_progress|done|blocked."}
    step_id = (step_id or "").strip().lower()
    m = re.match(r"^step_(\d+)$", step_id)
    if not m:
        return {"ok": False, "error": "step_id must look like 'step_1'."}

    _migrate_legacy_file()
    path = task_state_path()
    if not path.exists():
        ensure_res = ensure_task_state_file()
        if not ensure_res.get("ok"):
            return ensure_res
    try:
        current = path.read_text(encoding=config.DEFAULT_ENCODING)
    except OSError as e:
        return {"ok": False, "error": str(e)}
    top = _split_top_sections(current)
    active = _split_active_sections(top.get("active_task", ""))
    history = _parse_history_entries(top.get("task_history", ""))
    meta = _read_active_meta(top.get("active_task", ""))
    plan = active.get("plan", "")
    lines = plan.split("\n")

    # Find the target step block: heading line + following block until the
    # next `### Step` heading.
    target_idx = None
    for i, ln in enumerate(lines):
        if ln.strip().startswith("### Step "):
            info = _parse_step(ln)
            if info and info["id"] == step_id:
                target_idx = i
                break
    if target_idx is None:
        return {"ok": False, "error": f"Step not found in Plan: {step_id}. "
                                      "Use task_state_update(section='plan', ...) first "
                                      "or include the step as '### Step N -- title'."}

    # Replace the status in the heading.
    heading = lines[target_idx]
    heading = re.sub(r"\s*\(status:[^)]*\)", "", heading).rstrip()
    lines[target_idx] = f"{heading} (status: {status})"

    block_end = len(lines)
    for j in range(target_idx + 1, len(lines)):
        if lines[j].strip().startswith("### Step "):
            block_end = j
            break

    block = lines[target_idx + 1:block_end]
    if verification is not None:
        block = _set_meta_line(block, "verification", verification)
    if result is not None:
        block = _set_meta_line(block, "result", result)
    if context is not None:
        block = _set_meta_line(block, "context", context)
    lines[target_idx + 1:block_end] = block
    active["plan"] = "\n".join(lines).strip()

    # Regenerate Progress: mark all done steps with `[x]`.
    progress_lines: List[str] = []
    done_items = 0
    total = 0
    for ln in lines:
        s = ln.strip()
        if not s.startswith("### Step "):
            continue
        total += 1
        info = _parse_step(s)
        if info is None:
            continue
        item_status = status if info["id"] == step_id else None
        cur_status = None
        sm = re.search(r"\(status:\s*([^)]+)\)", s)
        if sm:
            cur_status = sm.group(1).strip()
        if item_status is None:
            item_status = cur_status or "pending"
        mark = "[x]" if item_status == "done" else "[ ]"
        if item_status == "done":
            done_items += 1
        progress_lines.append(f"- {mark} Step {info['num']} - {info['title']}")
    if progress_lines:
        progress_lines.append("")
        progress_lines.append(f"Progress: {done_items}/{total} steps done.")
    active["progress"] = "\n".join(progress_lines).strip()

    try:
        new_text = _write_journal(active, history, started=meta.get("started", ""))
    except OSError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "path": str(path), "step_id": step_id, "status": status,
            "thread_id": current_thread_id(),
            "size_bytes": len(new_text.encode(config.DEFAULT_ENCODING)),
            "step_ids": extract_step_ids(active.get("plan", ""))}


def clear_task_state() -> Dict[str, Any]:
    """Archive the completed Active Task into Task History.

    The journal file is NEVER deleted (requirement). When there is no active
    task to archive, returns archived=False, so repeated calls are safe.
    """
    _migrate_legacy_file()
    path = task_state_path()
    if not path.exists():
        return {"ok": True, "path": str(path), "thread_id": current_thread_id(),
                "archived": False}
    try:
        current = path.read_text(encoding=config.DEFAULT_ENCODING)
    except OSError as e:
        return {"ok": False, "error": str(e)}
    top = _split_top_sections(current)
    active = _split_active_sections(top.get("active_task", ""))
    history = _parse_history_entries(top.get("task_history", ""))
    if not _has_active_content(active):
        return {"ok": True, "path": str(path), "thread_id": current_thread_id(),
                "archived": False, "history_entries": len(history)}
    meta = _read_active_meta(top.get("active_task", ""))
    history.append(_archive_active_task(active, meta, _now_iso()))
    try:
        _write_journal({}, history, started="")
    except OSError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "path": str(path), "thread_id": current_thread_id(),
            "archived": True, "history_entries": len(history)}


def task_state_for_context(max_history: int = 3) -> Optional[str]:
    """Return a compact block for injection into the LLM context.

    The block starts with the meta info (thread id + journal path) required
    by the prompt, followed by the current Active Task and the most recent
    Task History entries. Returns None when the journal is missing (the
    feature must never break the agent loop). Content is truncated to
    MAX_STATE_CHARS.
    """
    _migrate_legacy_file()
    path = task_state_path()
    if not path.exists():
        return None
    try:
        content = path.read_text(encoding=config.DEFAULT_ENCODING)
    except OSError:
        return None
    if not content.strip():
        return None
    top = _split_top_sections(content)
    header = (
        "CURRENT TASK STATE:\n"
        f"thread_id: {current_thread_id()}\n"
        f"task_state_file: {path}\n"
    )
    parts: List[str] = [
        "## Active Task",
        "",
        top.get("active_task", "").strip() or _NOT_SET_MARKER,
    ]
    entries = _parse_history_entries(top.get("task_history", ""))
    if entries:
        parts.append("")
        parts.append("## Recent Task History")
        parts.append("")
        for entry in entries[-max_history:]:
            parts.append(f"### Completed - {(entry.get('task') or '')[:120]}")
            if entry.get("finished"):
                parts.append(f"- finished: {entry['finished']}")
            if entry.get("completed_steps"):
                parts.append(f"- completed_steps: {entry['completed_steps']}")
            if entry.get("summary"):
                parts.append(f"- summary: {entry['summary'][:1500]}")
            parts.append("")
    body = "\n".join(parts)
    limit = max(200, MAX_STATE_CHARS - len(header))
    if len(body) > limit:
        body = body[:limit] + "\n... [truncated]"
    return header + body
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
