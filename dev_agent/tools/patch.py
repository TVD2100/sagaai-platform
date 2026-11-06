"""Surgical anchor-based edits inside an existing file."""
from __future__ import annotations

from typing import Any, Dict

from .deps import ToolMixin
from .text_utils import _exact_spans, _fuzzy_find_matches, _line_of, _normalize_line_endings, _suggest_anchor_lines

# Diagnostics of a failed propose_file forwarded to the caller unchanged, so
# the agent can tell a syntax error from an empty-write rejection.
_RESULT_PASSTHROUGH_KEYS = (
    "syntax_error",
    "errors",
    "verified",
    "wrote_file",
    "is_new",
    "size_before",
    "size_after",
    "backup_version",
    "diff",
    "draft_path",
)


def _coerce_edits(edits: Any, path: str):
    """Normalise the *edits* argument into a list of edit dicts.

    Accepts either a ready list or a JSON-encoded string carrying one. Returns
    ``(edits, None)`` on success and ``(None, error_result)`` otherwise; the
    error text is part of the tool contract and must not drift.
    """
    if isinstance(edits, str):
        import json as _json
        try:
            parsed_edits = _json.loads(edits)
        except Exception:
            return None, {"ok": False, "path": path,
                          "error": "edits is a string but not valid JSON: " + edits[:80]}
        if not isinstance(parsed_edits, list):
            return None, {"ok": False, "path": path,
                          "error": "edits JSON must decode to a list"}
        edits = parsed_edits
    if not isinstance(edits, list):
        return None, {"ok": False, "path": path, "error": "edits must be a list"}
    return edits, None


def _apply_one_edit(working: str, edit: Any, index: int, total: int,
                    fuzzy: bool, path: str, details: list):
    """Apply a single edit to *working*.

    Returns ``(new_text, detail, was_fuzzy, None)`` on success and
    ``(working, None, False, error_result)`` when the edit is rejected. Every
    rejection carries the accumulated *details* and the 1-based edit index, so
    the caller knows which entry of a batch failed.
    """
    label = "edits[" + str(index) + "/" + str(total) + "]"
    if not isinstance(edit, dict):
        return working, None, False, {"ok": False, "path": path,
                                      "error": label + " is not a dict",
                                      "details": details}
    old = edit.get("old", "")
    new = edit.get("new", "")
    if not isinstance(old, str) or old == "":
        return working, None, False, {
            "ok": False, "path": path,
            "error": label + ".old must be a non-empty string",
            "details": details}
    if not isinstance(new, str):
        return working, None, False, {
            "ok": False, "path": path,
            "error": label + ".new must be a string",
            "details": details}
    old = _normalize_line_endings(old)
    new = _normalize_line_endings(new)
    if old == "<END>":
        if working and not working.endswith("\n"):
            working += "\n"
        working += new
        return working, {"edit": index, "replaced": True, "mode": "append"}, False, None

    occurrence = edit.get("occurrence", 1)
    try:
        occurrence = int(occurrence)
    except (TypeError, ValueError):
        return working, None, False, {
            "ok": False, "path": path,
            "error": label + ".occurrence must be an integer",
            "details": details}

    count = working.count(old)
    if count > 0:
        positions = _exact_spans(working, old)
    else:
        positions = _fuzzy_find_matches(working, old) if fuzzy else []

    if len(positions) == 0:
        snippet = old[:80] + ("..." if len(old) > 80 else "")
        result = {"ok": False, "path": path,
                  "error": label + ".old not found in file; snippet: " + snippet,
                  "details": details}
        suggestions = _suggest_anchor_lines(old, working)
        if suggestions:
            result["suggestions"] = suggestions
        return working, None, False, result

    if len(positions) > 1 and "occurrence" not in edit:
        occ_list = [{
            "occurrence": idx,
            "line": _line_of(working, s, e),
        } for idx, (s, e) in enumerate(positions, start=1)]
        return working, None, False, {
            "ok": False, "path": path,
            "error": (
                label + ".old matches " + str(len(positions)) +
                " time(s); pass occurrence (1-" + str(len(positions)) +
                ") or extend the snippet"
            ),
            "occurrences": occ_list,
            "details": details}
    if occurrence < 1 or occurrence > len(positions):
        return working, None, False, {
            "ok": False, "path": path,
            "error": label + ".occurrence out of range 1-" + str(len(positions)),
            "details": details}

    start_pos, end_pos = positions[occurrence - 1]
    was_fuzzy = (count == 0)
    working = working[:start_pos] + new + working[end_pos:]
    detail = {"edit": index, "replaced": True}
    if was_fuzzy:
        detail["fuzzy"] = True
    return working, detail, was_fuzzy, None


class PatchTools(ToolMixin):
    """Surgical anchor-based edits inside an existing file."""

    def _read_patch_target(self, path: str):
        """Resolve, guard and read the file a patch is about to change.

        Returns ``(payload, None)`` with the working text, the original text
        and whether CRLF line endings were normalised, or ``(None, error)``
        when the file cannot be patched at all.
        """
        try:
            src = self.deps().config.resolve_in_project(path)
        except ValueError as e:
            return None, {"ok": False, "path": path, "error": str(e)}
        if not src.exists():
            return None, {"ok": False, "path": path, "error": f"File not found: {path}"}
        if self.deps().config.is_protected(path):
            return None, {"ok": False, "path": path,
                          "error": f"Protected file, cannot patch: {path}"}
        try:
            original = src.read_text(encoding=self.deps().config.DEFAULT_ENCODING)
        except UnicodeDecodeError as e:
            return None, {
                "ok": False,
                "path": path,
                "error": (
                    f"File is not valid UTF-8 text (decode error at byte "
                    f"{e.start}). apply_patch operates on UTF-8 text files; "
                    "re-encode the file to UTF-8 first or use run_code/"
                    "propose_file for other encodings."
                ),
            }
        except Exception as e:
            return None, {"ok": False, "path": path,
                          "error": "read failed: " + str(e)}

        normalized_endings = False
        try:
            normalized_endings = b"\r" in src.read_bytes()
        except Exception:
            pass
        working = _normalize_line_endings(original) if normalized_endings else original
        return {"working": working, "original": original,
                "normalized_endings": normalized_endings}, None

    def _commit_patch(self, path: str, working: str, original: str, note: str,
                      auto_apply: bool, details: list, fuzzy_count: int,
                      normalized_endings: bool) -> Dict[str, Any]:
        """Write the patched text through the standard rewrite pipeline.

        The write goes through ``propose_file``, so backups, syntax validation
        and manual-mode staging behave exactly as a full rewrite does.
        """
        from ..safe_writer import render_diff
        try:
            result = self.propose_file(path=path, content=working, note=note,
                                       auto_apply=auto_apply)
        except Exception as e:
            return {"ok": False, "path": path,
                    "error": "write failed: " + str(e),
                    "details": details}
        if not result.get("ok"):
            err_res = {
                "ok": False,
                "path": path,
                "error": result.get("error", "propose_file failed"),
                "details": details,
            }
            for key in _RESULT_PASSTHROUGH_KEYS:
                if key in result:
                    err_res[key] = result[key]
            return err_res
        if not result.get("applied"):
            return {"ok": True, "path": self.deps().config.to_project_relative(path),
                    "applied": False,
                    "staged": True,
                    "verified": False,
                    "replacements": len(details),
                    "fuzzy_count": fuzzy_count,
                    "normalized_line_endings": normalized_endings,
                    "details": details,
                    "new_text": working,
                    "diff": result.get("diff"),
                    "error": result.get("error", "patch staged only; not applied")
                    if result.get("error") else None,
                    "backup_version": result.get("backup_version")}
        rel = self.deps().config.to_project_relative(path)
        return {"ok": True, "path": rel,
                "applied": True,
                "verified": True,
                "replacements": len(details),
                "fuzzy_count": fuzzy_count,
                "normalized_line_endings": normalized_endings,
                "details": details,
                "diff": render_diff(original, working, rel),
                "backup_version": result.get("backup_version")}

    def apply_patch(self, path: str, edits: list, note: str = "",
                     auto_apply: bool = True, fuzzy: bool = True) -> Dict[str, Any]:
        """Apply surgical text replacements to a file.

        Each entry in *edits* is a dict:
            {"old": str, "new": str, ["occurrence": int]}
        - old: snippet to find. When the EXACT snippet is missing and *fuzzy*
          is True (default), a whitespace-tolerant search is used: line
          indentation and horizontal spacing may differ from the file, and
          CRLF line endings are normalised to LF. *new* is still inserted
          exactly as given. Set fuzzy=False for strict byte-exact matching.
        - new: replacement text.
        - occurrence: 1-based index of the match to replace when `old` matches
          more than once. Without it, an ambiguous match is rejected with
          match counts and line numbers. Missing anchors additionally return
          'suggestions' (up to 3 closest lines) so the caller can fix the
          anchor quickly.

        Edits are applied sequentially; the final text is written through the
        standard full-rewrite pipeline (backup -> write -> verify). Returns
        {'ok': True, 'path', 'applied', 'replacements', 'details', 'diff',
        'fuzzy_count', 'normalized_line_endings'} on success (note: 'applied'
        is False when the write was only staged in manual mode - the caller
        must wait for user approval, exactly like propose_file), or
        {'ok': False, 'error', ['suggestions'], ...} on failure. The file is
        NEVER modified on failure.
        """
        coerced, error = _coerce_edits(edits, path)
        if error is not None:
            return error
        target, error = self._read_patch_target(path)
        if error is not None:
            return error

        working = target["working"]
        details: list = []
        fuzzy_count = 0
        total_edits = len(coerced)
        for i, edit in enumerate(coerced, start=1):
            working, detail, was_fuzzy, error = _apply_one_edit(
                working, edit, i, total_edits, fuzzy, path, details)
            if error is not None:
                return error
            if was_fuzzy:
                fuzzy_count += 1
            details.append(detail)

        return self._commit_patch(
            path, working, target["original"], note, auto_apply,
            details, fuzzy_count, target["normalized_endings"])


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
