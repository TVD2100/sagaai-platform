# DevAgent Universal Developer - workspace layer.
#
# This module turns DevAgent from "an editor of the SagaAI install" into a
# general-purpose developer that operates on an ARBITRARY target folder
# OR a SINGLE target file.
#
# It is deliberately layered ON TOP of the protected core (config / dev_agent /
# tool_executor / safe_writer / backup_manager) and never modifies it.
# Everything here is plain stdlib + the public DevAgent dispatch surface, so the
# Inviolable Core stays intact.
#
# Responsibilities:
#   1. Target-folder selection (set_workspace) - repoints config.PROJECT_ROOT.
#   2. Single-file mode (set_target_file) - narrows the workspace to one file.
#   3. Folder inspection (scan_folder) - files, languages, and which docs exist.
#   4. Deterministic project map (build_project_map) - structure facts the LLM
#      then enriches with "what each file is responsible for".
#   5. Doc scaffolding (ensure_project_docs) - PROJECT_MAP.md / SPEC.md /
#      ARCHITECTURE.md / README.md created as markdown so users can hand-edit them.
#   6. State report (assess_workspace) - the three pipeline entry states:
#        empty | software_without_docs | software_with_docs
#   7. System-wide backup/restore (snapshot_all / restore_all).
#   8. Recent-workspaces history (list_recent_workspaces) - lets the agent
#      suggest the last 5 projects at the start of a new task.

from __future__ import annotations

import os
import json
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config
from .backup_manager import BackupManager

# ─── File-type filtering constants (formerly in dev_agent.constants) ──────────
TEXT_EXTENSIONS: tuple = (
    ".py", ".md", ".json", ".txt", ".toml", ".cfg", ".ini", ".yaml", ".yml",
    ".html", ".css", ".scss", ".js", ".jsx", ".ts", ".tsx", ".sh",
    ".sql", ".java", ".go", ".rs", ".c", ".cpp", ".h", ".pem",
)

SKIP_DIRS: set = {
    "__pycache__", ".git", "backups", "workspace", "history",
    ".pytest_cache", "node_modules", ".venv", "venv",
    ".dev_agent",
}


# ─── Language / file-type detection ───────────────────────────────────────────
_LANG_BY_EXT: Dict[str, str] = {
    ".py": "Python",
    ".js": "JavaScript", ".jsx": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript",
    ".json": "JSON", ".md": "Markdown", ".txt": "Text",
    ".html": "HTML", ".css": "CSS", ".scss": "CSS",
    ".toml": "TOML", ".cfg": "Config", ".ini": "Config",
    ".yaml": "YAML", ".yml": "YAML", ".sh": "Shell",
    ".sql": "SQL", ".java": "Java", ".go": "Go", ".rs": "Rust",
    ".c": "C", ".cpp": "C++", ".h": "C/C++ header", ".pem": "PEM certificate",
}


def detect_language(path: str) -> str:
    """Return a coarse language label for a file path."""
    return _LANG_BY_EXT.get(Path(path).suffix.lower(), "Other")


# ─── Workspace selection ──────────────────────────────────────────────────────
def set_workspace(path: str) -> Dict[str, Any]:
    """Point DevAgent at a target work folder. Creates it if missing.

    Returns the resolved absolute root and whether it is the SagaAI install.
    Clears any single-file mode.  The chosen folder is recorded in the
    recent-workspaces history so future tasks can suggest it quickly.
    """
    config.TARGET_FILE = None   # switching workspace clears single-file mode

    raw = str(path or "").strip()
    if not raw:
        return {"ok": False, "error": "Empty workspace path."}
    root = Path(raw).expanduser()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "error": f"Cannot create/access folder: {exc}"}

    resolved = config.set_target_root(root)

    # Persist the chosen folder in the recent-workspaces history.
    # Failures here must never break workspace switching.
    try:
        from core.recent_workspaces import add_recent_workspace
        add_recent_workspace(str(resolved))
    except Exception:
        pass

    return {
        "ok": True,
        "root": str(resolved),
        "working_on_install": config.WORKING_ON_INSTALL,
        "backups_dir": str(config.BACKUPS_DIR),
    }


def set_target_file(file_path: str) -> Dict[str, Any]:
    """Activate single-file mode.

    The workspace is set to the parent directory of the file.
    All scanning/mapping operations will only see this one file.
    """
    raw = str(file_path or "").strip()
    if not raw:
        config.TARGET_FILE = None
        return {"ok": True, "message": "Single-file mode cleared."}

    resolved = Path(raw).expanduser().resolve()
    if not resolved.exists():
        return {"ok": False, "error": f"File not found: {raw}"}
    if resolved.is_dir():
        return {"ok": False, "error": f"Path is a directory, not a file: {raw}. Use set_workspace for folders."}

    # Workspace = parent of the target file
    parent = str(resolved.parent)
    result = set_workspace(parent)
    if not result.get("ok"):
        return result

    config.TARGET_FILE = str(resolved)
    return {
        **result,
        "target_file": str(resolved),
        "single_file_mode": True,
    }


def current_workspace() -> Dict[str, Any]:
    """Report the currently active workspace root and single-file mode status."""
    result: Dict[str, Any] = {
        "ok": True,
        "root": str(config.PROJECT_ROOT),
        "working_on_install": config.WORKING_ON_INSTALL,
    }
    if config.TARGET_FILE:
        result["target_file"] = config.TARGET_FILE
        result["single_file_mode"] = True
    else:
        result["single_file_mode"] = False
    return result


def current_install() -> Dict[str, Any]:
    """Report the SagaAI install root where DevAgent itself lives.

    This is the folder containing the dev_agent package (config.INSTALL_ROOT),
    NOT the active target workspace. Use it for platform-level operations such
    as creating a new project under <install>/apps/<name>.
    """
    return {
        "ok": True,
        "root": str(config.INSTALL_ROOT),
        "apps_dir": str(config.INSTALL_ROOT / "apps"),
        "working_on_install": config.WORKING_ON_INSTALL,
    }


def list_recent_workspaces() -> Dict[str, Any]:
    """Return up to 5 recently used workspace paths (newest first).

    Each entry contains a 1-based index (for the user to pick by number),
    the absolute path, and a short display name (folder basename).
    Paths that no longer exist are filtered out.
    """
    try:
        from core.recent_workspaces import get_recent_workspaces
        recent = get_recent_workspaces()
    except Exception:
        recent = []

    projects: List[Dict[str, Any]] = []
    for i, path in enumerate(recent, start=1):
        p = Path(path)
        projects.append({
            "index": i,
            "path": path,
            "name": p.name or path,
        })

    return {
        "ok": True,
        "count": len(projects),
        "projects": projects,
    }


# ─── Folder inspection ────────────────────────────────────────────────────────
def _iter_project_files(
    base: Path,
    max_depth: int = 6,
    allowed_extensions: Optional[set] = None,
) -> List[Path]:
    """Yield text/code files under base, skipping noise dirs and the runtime dir.

    In single-file mode, returns ONLY the target file (if it is under base),
    REGARDLESS of its extension - the user explicitly chose this file.

    When *allowed_extensions* is given (a set of lowercase suffixes including
    the leading dot), it REPLACES the built-in TEXT_EXTENSIONS allow-list, so
    callers can explicitly search files outside it (e.g. .csv).
    """
    if config.TARGET_FILE:
        tf = Path(config.TARGET_FILE).resolve()
        # Only include if under the current workspace
        try:
            tf.relative_to(base.resolve())
            # In single-file mode, accept ANY file the user pointed at.
            return [tf]
        except ValueError:
            pass
        return []

    # Normal full-workspace scan
    found: List[Path] = []
    root_depth = len(base.resolve().parts)
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        depth = len(Path(dirpath).resolve().parts) - root_depth
        if depth > max_depth:
            dirnames[:] = []
            continue
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            if allowed_extensions is None:
                if fn.endswith(TEXT_EXTENSIONS):
                    found.append(p)
            elif p.suffix.lower() in allowed_extensions:
                found.append(p)
    return found


CONTEXT_LINE_CHARS = 120
"""Maximum chars kept per before/after context line in search results."""


def _coerce_nonneg_int(value, default=0):
    """Coerce *value* to a non-negative int, falling back to *default*."""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def search_in_files(
    query: str,
    subdir: Optional[str] = None,
    path: Optional[str] = None,
    files: Optional[List[str]] = None,
    extensions: Optional[List[str]] = None,
    regex: bool = False,
    case_sensitive: bool = False,
    max_results: int = 100,
    context_before: int = 0,
    context_after: int = 0,
) -> Dict[str, Any]:
    """Search text files in the workspace for *query*.

    A codebase-exploration tool that replaces ad-hoc subprocess grep. The
    query is treated as a literal substring by default; pass ``regex=True``
    to interpret it as a regular expression. Only text files are scanned,
    noise directories are skipped, and single-file mode narrows the scan to
    the target file.

    Returns matching ``path``, ``line`` and ``text`` (trimmed to 200 chars),
    capped at *max_results*. ``truncated`` reports whether the cap was hit.
    ``files_skipped_large`` / ``files_unreadable`` report files skipped due to
    the size limit or undecodable content (UTF-8 or cp1251).

    *path* narrows the scan: a FILE path scans exactly that one file
    (extension filtering is ignored for an explicit file); a DIRECTORY path
    acts like *subdir*. When both are given, *path* takes precedence.

    *files* narrows the scan to an explicit LIST of relative file paths
    (e.g. ``files=["app.py", "tests/test_x.py"]``). Only those files are
    scanned, extension filtering is ignored for them, and every path must
    exist, be a file and stay inside the workspace. *files* takes precedence
    over *path* and *subdir*.

    When *context_before* / *context_after* are > 0, each match additionally
    carries ``before`` / ``after`` lists with the surrounding lines (trimmed to
    CONTEXT_LINE_CHARS chars). Invalid context values coerce to 0.
    """
    if query is None or str(query) == "":
        return {"ok": False, "error": "Missing required argument 'query'."}

    try:
        max_results = max(1, int(max_results))
    except (TypeError, ValueError):
        max_results = 100

    context_before = _coerce_nonneg_int(context_before)
    context_after = _coerce_nonneg_int(context_after)

    base = config.PROJECT_ROOT.resolve()
    scan_root = base
    explicit_file: Optional[Path] = None
    explicit_files: List[Path] = []
    if files is not None:
        if isinstance(files, str):
            files = [files]
        if not isinstance(files, (list, tuple)) or not files:
            return {"ok": False, "error": "'files' must be a non-empty list of file paths."}
        for rel_f in files:
            if not isinstance(rel_f, str) or not str(rel_f).strip():
                return {"ok": False, "error": "'files' must contain non-empty file paths."}
            candidate = (base / str(rel_f)).resolve()
            try:
                candidate.relative_to(base)
            except ValueError:
                return {"ok": False, "error": f"Path escapes project root: {rel_f}"}
            if not candidate.exists():
                return {"ok": False, "error": f"File not found: {rel_f}"}
            if not candidate.is_file():
                return {"ok": False, "error": f"Path is not a file: {rel_f}"}
            explicit_files.append(candidate)
    elif path:
        candidate = (base / str(path)).resolve()
        try:
            candidate.relative_to(base)
        except ValueError:
            return {"ok": False, "error": f"Path escapes project root: {path}"}
        if not candidate.exists():
            return {"ok": False, "error": f"Path not found: {path}"}
        if candidate.is_file():
            explicit_file = candidate
        else:
            scan_root = candidate
    elif subdir:
        candidate = (base / str(subdir)).resolve()
        try:
            candidate.relative_to(base)
        except ValueError:
            return {"ok": False, "error": f"Path escapes project root: {subdir}"}
        if not candidate.exists():
            return {"ok": False, "error": f"Subdir not found: {subdir}"}
        if not candidate.is_dir():
            return {"ok": False, "error": f"Subdir is not a directory: {subdir}"}
        scan_root = candidate

    if isinstance(extensions, str):
        extensions = [extensions]
    allowed_ext: Optional[set] = None
    if extensions:
        allowed_ext = {
            e.lower() if e.startswith(".") else f".{e.lower()}"
            for e in extensions if isinstance(e, str) and e
        }
        if not allowed_ext:
            allowed_ext = None

    # An explicit allow-list OVERRIDES the built-in TEXT_EXTENSIONS set, so
    # files outside it (e.g. .csv) can be searched when the caller asks.
    if explicit_files:
        scan_files = explicit_files
    elif explicit_file is not None:
        scan_files = [explicit_file]
    else:
        scan_files = _iter_project_files(scan_root, allowed_extensions=allowed_ext)

    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        pattern = re.compile(str(query), flags)
    except re.error as exc:
        return {"ok": False, "error": f"Invalid regex: {exc}"}
    if not regex:
        pattern = re.compile(re.escape(str(query)), flags)

    results: List[Dict[str, Any]] = []
    files_scanned = 0
    files_skipped_large = 0
    files_unreadable = 0
    truncated = False
    for f in scan_files:
        files_scanned += 1
        try:
            if f.stat().st_size > config.MAX_FILE_SIZE_BYTES:
                files_skipped_large += 1
                continue
        except OSError:
            files_unreadable += 1
            continue
        try:
            text = f.read_text(encoding=config.DEFAULT_ENCODING)
        except UnicodeDecodeError:
            try:
                text = f.read_text(encoding="cp1251")
            except (OSError, UnicodeDecodeError):
                files_unreadable += 1
                continue
        except OSError:
            files_unreadable += 1
            continue
        lines_all = text.splitlines()
        for idx, raw_line in enumerate(lines_all):
            line_no = idx + 1
            if not pattern.search(raw_line):
                continue
            try:
                rel = config.to_project_relative(f)
            except ValueError:
                rel = str(f)
            entry = {
                "path": rel,
                "line": line_no,
                "text": raw_line.strip()[:200],
            }
            if context_before:
                entry["before"] = [
                    lines_all[j].strip()[:CONTEXT_LINE_CHARS]
                    for j in range(max(0, idx - context_before), idx)
                ]
            if context_after:
                entry["after"] = [
                    lines_all[j].strip()[:CONTEXT_LINE_CHARS]
                    for j in range(idx + 1, min(len(lines_all), idx + 1 + context_after))
                ]
            results.append(entry)
            if len(results) >= max_results:
                truncated = True
                break
        if truncated:
            break

    return {
        "ok": True,
        "query": str(query),
        "regex": bool(regex),
        "case_sensitive": bool(case_sensitive),
        "files_scanned": files_scanned,
        "files_skipped_large": files_skipped_large,
        "files_unreadable": files_unreadable,
        "match_count": len(results),
        "truncated": truncated,
        "results": results,
    }


def scan_folder() -> Dict[str, Any]:
    """Inspect the active workspace: files, languages, and which docs exist.
    In single-file mode, returns only the target file.
    """
    base = config.PROJECT_ROOT.resolve()
    files = _iter_project_files(base)

    file_rows: List[Dict[str, Any]] = []
    lang_counts: Dict[str, int] = {}
    for f in files:
        try:
            rel = config.to_project_relative(f)
        except ValueError:
            continue
        lang = detect_language(rel)
        lang_counts[lang] = lang_counts.get(lang, 0) + 1
        try:
            size = f.stat().st_size
        except OSError:
            size = 0
        file_rows.append({"path": rel, "lang": lang, "size_bytes": size})

    docs_present = {
        name: (base / name).exists() for name in config.PROJECT_DOC_NAMES
    }
    code_rows = [r for r in file_rows if r["path"] not in config.PROJECT_DOC_NAMES]

    result: Dict[str, Any] = {
        "ok": True,
        "root": str(base),
        "total_files": len(file_rows),
        "code_files": len(code_rows),
        "languages": lang_counts,
        "docs_present": docs_present,
        "files": file_rows,
    }
    if config.TARGET_FILE:
        result["single_file_mode"] = True
        result["target_file"] = config.TARGET_FILE
    return result


def assess_workspace() -> Dict[str, Any]:
    """Classify the workspace into one of three pipeline entry states.

    In single-file mode, returns a special state description.
    """
    scan = scan_folder()
    code_files = scan["code_files"]
    docs = scan["docs_present"]
    has_map = docs.get("PROJECT_MAP.md", False)
    has_spec_or_arch = docs.get("SPEC.md", False) or docs.get("ARCHITECTURE.md", False)

    if config.TARGET_FILE:
        state = "single_file"
    elif code_files == 0 and not has_map:
        state = "empty"
    elif has_map and has_spec_or_arch:
        state = "software_with_docs"
    else:
        state = "software_without_docs"

    result: Dict[str, Any] = {
        "ok": True,
        "state": state,
        "root": scan["root"],
        "code_files": code_files,
        "languages": scan["languages"],
        "docs_present": docs,
    }
    if config.TARGET_FILE:
        result["target_file"] = config.TARGET_FILE
        result["single_file_mode"] = True
    return result


# ─── Deterministic project map ────────────────────────────────────────────────
def _python_symbols(text: str) -> List[Dict[str, Any]]:
    """Extract top-level def/class names with line numbers from Python source."""
    symbols: List[Dict[str, Any]] = []
    for i, ln in enumerate(text.split("\n"), start=1):
        stripped = ln.rstrip()
        if stripped.startswith("def ") or stripped.startswith("class "):
            name = stripped.split("(")[0]
            name = name.replace("def ", "").replace("class ", "").strip(": ")
            kind = "class" if stripped.startswith("class ") else "func"
            symbols.append({"line": i, "name": name, "kind": kind})
    return symbols


def _python_imports(text: str) -> List[str]:
    """Collect imported module roots from Python source (for relationship hints)."""
    roots: set = set()
    for ln in text.split("\n"):
        s = ln.strip()
        if s.startswith("import "):
            mod = s[len("import "):].split(",")[0].split(" as ")[0].strip()
            roots.add(mod.split(".")[0])
        elif s.startswith("from ") and " import " in s:
            mod = s[len("from "):].split(" import ")[0].strip()
            roots.add(mod.lstrip(".").split(".")[0] or ".")
    return sorted(r for r in roots if r)


def build_project_map() -> Dict[str, Any]:
    """Build a DETERMINISTIC structural map of the workspace.

    In single-file mode, maps only the target file.
    """
    scan = scan_folder()
    base = config.PROJECT_ROOT.resolve()
    local_modules = {
        Path(r["path"]).stem for r in scan["files"] if r["lang"] == "Python"
    }

    entries: List[Dict[str, Any]] = []
    for row in scan["files"]:
        rel = row["path"]
        if rel in config.PROJECT_DOC_NAMES:
            continue
        entry: Dict[str, Any] = {
            "path": rel,
            "lang": row["lang"],
            "size_bytes": row["size_bytes"],
        }
        if row["lang"] == "Python":
            try:
                text = (base / rel).read_text(encoding=config.DEFAULT_ENCODING)
            except (OSError, UnicodeDecodeError):
                text = ""
            syms = _python_symbols(text)
            imports = _python_imports(text)
            entry["symbols"] = syms[:40]
            entry["symbol_count"] = len(syms)
            entry["depends_on"] = sorted(
                m for m in imports if m in local_modules and m != Path(rel).stem
            )
        entries.append(entry)

    return {
        "ok": True,
        "root": str(base),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "file_count": len(entries),
        "languages": scan["languages"],
        "entries": entries,
    }


def render_project_map_markdown(
    project_map: Dict[str, Any],
    responsibilities: Optional[Dict[str, str]] = None,
) -> str:
    """Render PROJECT_MAP.md from deterministic facts + optional LLM prose."""
    responsibilities = responsibilities or {}
    lines: List[str] = []
    lines.append("# Карта проекта (PROJECT_MAP)")
    lines.append("")
    lines.append(
        "Автоматически поддерживается DevAgent. Структура - детерминированная, "
        "описания назначения файлов - генерируются моделью. Вы можете править "
        "этот файл вручную; при следующей доработке DevAgent учтёт ваши правки."
    )
    lines.append("")
    lines.append(f"- Обновлено: `{project_map.get('generated_at', '')}`")
    lines.append(f"- Файлов: **{project_map.get('file_count', 0)}**")
    langs = project_map.get("languages", {})
    if langs:
        lang_str = ", ".join(f"{k}: {v}" for k, v in sorted(langs.items()))
        lines.append(f"- Языки: {lang_str}")
    lines.append("")
    lines.append("## Файлы и назначение")
    lines.append("")
    lines.append("| Файл | Язык | Назначение | Зависит от |")
    lines.append("| --- | --- | --- | --- |")
    for e in project_map.get("entries", []):
        path = e["path"]
        resp = responsibilities.get(path, "_(описание не задано)_")
        deps = ", ".join(e.get("depends_on", [])) or "-"
        lines.append(f"| `{path}` | {e['lang']} | {resp} | {deps} |")
    lines.append("")

    py = [e for e in project_map.get("entries", []) if e.get("lang") == "Python"]
    if py:
        lines.append("## Структура Python-модулей")
        lines.append("")
        for e in py:
            syms = e.get("symbols", [])
            if not syms:
                continue
            lines.append(f"### `{e['path']}`")
            for s in syms:
                lines.append(f"- `{s['name']}` ({s['kind']}, строка {s['line']})")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def default_spec_markdown(task: str = "") -> str:
    """Scaffold SPEC.md (requirements). The LLM fills sections during pipeline."""
    return (
        "# Спецификация требований (SPEC)\n\n"
        "Документ поддерживается DevAgent и редактируется пользователем.\n\n"
        "## Назначение системы\n\n_(описание появится после первой задачи)_\n\n"
        "## Функциональные требования\n\n"
        + (f"- {task}\n\n" if task else "_(пока не заданы)_\n\n")
        + "## Нефункциональные требования\n\n_(пока не заданы)_\n\n"
        "## Ограничения\n\n_(пока не заданы)_\n"
    )


def default_architecture_markdown() -> str:
    """Scaffold ARCHITECTURE.md. The LLM fills sections during pipeline."""
    return (
        "# Архитектурное описание (ARCHITECTURE)\n\n"
        "Документ поддерживается DevAgent и редактируется пользователем.\n\n"
        "## Обзор\n\n_(описание появится после анализа проекта)_\n\n"
        "## Компоненты\n\n_(см. PROJECT_MAP.md)_\n\n"
        "## Поток данных\n\n_(пока не задан)_\n\n"
        "## Решения и принципы\n\n_(пока не заданы)_\n"
    )


def default_readme_markdown() -> str:
    """Scaffold README.md - user-facing documentation for the project."""
    return (
        "# README\n\n"
        "Документ поддерживается DevAgent и редактируется пользователем.\n\n"
        "## Описание проекта\n\n"
        "_(краткое описание проекта появится здесь после первой задачи)_\n\n"
        "## Установка и запуск\n\n"
        "_(инструкции по установке, настройке и запуску проекта)_\n\n"
        "## Использование\n\n"
        "_(основные сценарии использования, примеры)_\n\n"
        "## Структура проекта\n\n"
        "_(см. PROJECT_MAP.md)_\n\n"
        "## Зависимости\n\n"
        "_(внешние библиотеки и инструменты)_\n"
    )


def _backup_before_overwrite(dest: Path) -> None:
    """Best-effort backup of *dest* before it is overwritten.

    Swallows all exceptions - a backup failure must not prevent the write.
    """
    if not dest.exists():
        return
    try:
        bm = BackupManager()
        bm.create_backup(str(dest), note="pre-doc-overwrite backup")
    except Exception:
        pass


def write_project_map(responsibilities: Dict[str, str]) -> Dict[str, Any]:
    """Build and write PROJECT_MAP.md with LLM-supplied responsibility descriptions."""
    pmap = build_project_map()
    if not pmap.get("ok"):
        return pmap
    md = render_project_map_markdown(pmap, responsibilities)
    dest = config.PROJECT_MAP_FILE
    _backup_before_overwrite(dest)
    try:
        dest.write_text(md, encoding=config.DEFAULT_ENCODING)
    except OSError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "path": str(dest), "size_bytes": len(md.encode(config.DEFAULT_ENCODING))}


def write_doc(doc: str, content: Optional[str] = None) -> Dict[str, Any]:
    """Write SPEC.md, ARCHITECTURE.md, or README.md. If no content, scaffold defaults."""
    if doc == "spec":
        target = config.SPEC_FILE
        text = content if content is not None else default_spec_markdown()
    elif doc == "architecture":
        target = config.ARCHITECTURE_FILE
        text = content if content is not None else default_architecture_markdown()
    elif doc == "readme":
        target = config.README_FILE
        text = content if content is not None else default_readme_markdown()
    else:
        return {"ok": False, "error": f"Unknown doc type: {doc}. Use 'spec', 'architecture', or 'readme'."}
    _backup_before_overwrite(target)
    try:
        target.write_text(text, encoding=config.DEFAULT_ENCODING)
    except OSError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "path": str(target), "size_bytes": len(text.encode(config.DEFAULT_ENCODING))}


def read_doc(doc: str) -> Dict[str, Any]:
    """Read a managed project document (map, spec, architecture, changelog, readme)."""
    mapping = {
        "map": config.PROJECT_MAP_FILE,
        "spec": config.SPEC_FILE,
        "architecture": config.ARCHITECTURE_FILE,
        "changelog": config.CHANGELOG_FILE,
        "readme": config.README_FILE,
    }
    target = mapping.get(doc)
    if target is None:
        return {"ok": False, "error": f"Unknown doc: {doc}. Use map|spec|architecture|changelog|readme."}
    if not target.exists():
        return {"ok": False, "exists": False, "error": f"Document not found: {doc}"}
    try:
        content = target.read_text(encoding=config.DEFAULT_ENCODING)
    except OSError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "exists": True, "doc": doc, "path": str(target), "content": content}


# ─── System-wide backup / restore ─────────────────────────────────────────────
def _snapshot_dir() -> Path:
    return config.BACKUPS_DIR / "_snapshots"


@dataclass
class SnapshotInfo:
    id: str
    timestamp: str
    note: str
    file_count: int


def snapshot_all(note: str = "") -> Dict[str, Any]:
    """Create a full-system snapshot: back up every project file via BackupManager."""
    bm = BackupManager()
    scan = scan_folder()
    snap_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    manifest: Dict[str, Any] = {
        "id": snap_id,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": note,
        "files": {},
    }
    errors: List[str] = []
    for row in scan["files"]:
        path = row["path"]
        try:
            entry = bm.create_backup(path, note=f"snapshot {snap_id}")
            manifest["files"][path] = entry.version
        except Exception as e:
            errors.append(f"{path}: {e}")
    snap_dir = _snapshot_dir()
    snap_dir.mkdir(parents=True, exist_ok=True)
    (snap_dir / f"{snap_id}.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding=config.DEFAULT_ENCODING,
    )
    return {
        "ok": True,
        "snapshot_id": snap_id,
        "file_count": len(manifest["files"]),
        "errors": errors,
    }


def list_snapshots() -> Dict[str, Any]:
    """List recorded full-system snapshots, newest first."""
    snap_dir = _snapshot_dir()
    out: List[Dict[str, Any]] = []
    if snap_dir.exists():
        for f in sorted(snap_dir.glob("*.json"), reverse=True):
            try:
                m = json.loads(f.read_text(encoding=config.DEFAULT_ENCODING))
            except (OSError, json.JSONDecodeError):
                continue
            out.append({
                "id": m.get("id"),
                "timestamp": m.get("timestamp"),
                "note": m.get("note", ""),
                "file_count": len(m.get("files", {})),
            })
    return {"ok": True, "snapshots": out}


def restore_all(snapshot_id: str) -> Dict[str, Any]:
    """Restore every file recorded in a full-system snapshot."""
    bm = BackupManager()
    snap_file = _snapshot_dir() / f"{snapshot_id}.json"
    if not snap_file.exists():
        return {"ok": False, "error": f"Snapshot not found: {snapshot_id}"}
    manifest = json.loads(snap_file.read_text(encoding=config.DEFAULT_ENCODING))
    restored: List[str] = []
    errors: List[str] = []
    for path, version in manifest.get("files", {}).items():
        try:
            bm.restore_backup(path, version=version)
            restored.append(path)
        except Exception as e:
            errors.append(f"{path}: {e}")
    return {
        "ok": True,
        "snapshot_id": snapshot_id,
        "restored": restored,
        "errors": errors,
    }
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
