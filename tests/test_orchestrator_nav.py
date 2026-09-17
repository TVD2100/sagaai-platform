# -*- coding: utf-8 -*-
"""tests/test_orchestrator_nav.py - unit tests for core.orchestrator_nav.

Covers the employee-sidebar ordering rules:
  - the effective order key is the latest dialogue time when a dialogue
    exists, otherwise the orchestrator creation time (newest first), so a
    freshly created employee appears at the very top;
  - orchestrators missing timestamps go last, ordered by name;
  - the visible block is limited to DEFAULT_VISIBLE_ORCHESTRATORS (5).
"""
from datetime import datetime

from core.orchestrator_nav import (
    DEFAULT_VISIBLE_ORCHESTRATORS,
    last_used_at,
    sort_orchestrators,
)
from core.assistant_nav import split_nav_lists


def _o(slug, name, created="", updated=""):
    return {"slug": slug, "name": name, "created_at": created, "updated_at": updated}


def _t(tid, slug, updated="", created=""):
    return {
        "thread_id": tid,
        "assistant_id": slug,
        "type": "devagent",
        "updated_at": updated,
        "created_at": created,
        "title": tid,
    }


# ─── last_used_at ─────────────────────────────────────────────────────────

def test_last_used_at_picks_newest_thread():
    threads = [
        _t("t1", "orch1", updated="2026-01-01T00:00:00"),
        _t("t2", "orch1", updated="2026-03-01T00:00:00"),
        _t("t3", "orch1", updated="2026-02-01T00:00:00"),
        _t("t4", "orch2", updated="2026-04-01T00:00:00"),  # other employee
    ]
    assert last_used_at("orch1", threads) == datetime(2026, 3, 1)


def test_last_used_at_empty_inputs():
    assert last_used_at("orch1", None) is None
    assert last_used_at("orch1", []) is None
    assert last_used_at(None, [_t("t1", "orch1", updated="2026-01-01T00:00:00")]) is None


# ─── overall ordering ─────────────────────────────────────────────────────

def test_new_orchestrator_lands_at_very_top():
    orchestrators = [
        _o("old_no_dial", "Old No Dialog", created="2025-01-01T00:00:00"),
        _o("brand_new", "Brand New", created="2026-06-01T12:00:00"),
        _o("with_dial", "With Dialog", created="2025-01-01T00:00:00"),
    ]
    threads = [_t("t1", "with_dial", updated="2026-05-01T00:00:00")]

    slugs = [o["slug"] for o in sort_orchestrators(orchestrators, threads)]
    assert slugs == ["brand_new", "with_dial", "old_no_dial"]


def test_all_orchestrators_without_dialogues_sorted_by_creation_desc():
    orchestrators = [
        _o("mid", "Mid", created="2026-02-01T00:00:00"),
        _o("newest", "Newest", created="2026-04-01T00:00:00"),
        _o("oldest", "Oldest", created="2025-12-31T00:00:00"),
    ]
    slugs = [o["slug"] for o in sort_orchestrators(orchestrators, [])]
    assert slugs == ["newest", "mid", "oldest"]


def test_orchestrators_with_dialogues_sorted_by_dialogue_desc():
    orchestrators = [
        _o("older_dial", "Older", created="2025-01-01T00:00:00"),
        _o("newer_dial", "Newer", created="2025-01-01T00:00:00"),
        _o("silent", "Silent", created="2026-01-01T00:00:00"),
    ]
    threads = [
        _t("t1", "older_dial", updated="2026-01-01T00:00:00"),
        _t("t2", "newer_dial", updated="2026-03-01T00:00:00"),
        _t("t3", "silent", updated="2026-02-01T00:00:00"),
    ]
    slugs = [o["slug"] for o in sort_orchestrators(orchestrators, threads)]
    assert slugs == ["newer_dial", "silent", "older_dial"]


def test_tie_without_dates_falls_back_to_name_and_input_is_not_mutated():
    orchestrators = [
        _o("z", "Zebra"),
        _o("a", "apple"),
    ]
    original = [dict(o) for o in orchestrators]
    slugs = [o["slug"] for o in sort_orchestrators(orchestrators, [])]
    assert slugs == ["a", "z"]
    assert orchestrators == original


def test_created_at_fallbacks_to_updated_at():
    orchestrators = [
        _o("o1", "A", created="", updated="2026-01-01T00:00:00"),
        _o("o2", "B", created="", updated="2025-01-01T00:00:00"),
    ]
    slugs = [o["slug"] for o in sort_orchestrators(orchestrators, [])]
    assert slugs == ["o1", "o2"]


# ─── visible block splitting ──────────────────────────────────────────────

def test_visible_block_defaults_to_five():
    orchestrators = [_o(str(i), f"O{i}") for i in range(7)]
    visible, remaining = split_nav_lists(
        orchestrators, visible_count=DEFAULT_VISIBLE_ORCHESTRATORS
    )
    assert DEFAULT_VISIBLE_ORCHESTRATORS == 5
    assert len(visible) == 5
    assert len(remaining) == 2
