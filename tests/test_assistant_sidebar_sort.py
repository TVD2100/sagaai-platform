# -*- coding: utf-8 -*-
"""tests/test_assistant_sidebar_sort.py - unit tests for core.assistant_nav.

Covers the sidebar ordering rules:
  - the effective order key is the latest dialogue time when a dialogue
    exists, otherwise the assistant creation time (newest first), so a
    freshly created assistant appears at the very top;
  - assistants missing timestamps go last, ordered by name;
  - the visible block is limited to 5 entries.
"""
from datetime import datetime

from core.assistant_nav import (
    DEFAULT_VISIBLE_ASSISTANTS,
    last_dialogue_at,
    sort_assistants,
    split_nav_lists,
    _parse_ts,
)


def _a(aid, name, created="", updated=""):
    return {"id": aid, "name": name, "created_at": created, "updated_at": updated}


def _t(tid, aid, updated="", created=""):
    return {
        "thread_id": tid,
        "assistant_id": aid,
        "type": "chat",
        "updated_at": updated,
        "created_at": created,
        "title": tid,
    }


# ─── timestamp parsing ────────────────────────────────────────────────────

def test_parse_ts_accepts_iso_numeric_datetime_and_junk():
    iso = _parse_ts("2026-01-02T03:04:05")
    assert iso == datetime(2026, 1, 2, 3, 4, 5)

    num = _parse_ts(1767300000)
    assert isinstance(num, datetime)

    dt = datetime(2026, 5, 5, 12, 0, 0)
    assert _parse_ts(dt) == dt

    assert _parse_ts("") is None
    assert _parse_ts(None) is None
    assert _parse_ts(0) is None
    assert _parse_ts("not-a-date") is None


# ─── last_dialogue_at ─────────────────────────────────────────────────────

def test_last_dialogue_at_picks_newest_thread():
    threads = [
        _t("t1", "a1", updated="2026-01-01T00:00:00"),
        _t("t2", "a1", updated="2026-03-01T00:00:00"),
        _t("t3", "a1", updated="2026-02-01T00:00:00"),
        _t("t4", "a2", updated="2026-04-01T00:00:00"),  # other assistant
    ]
    assert last_dialogue_at("a1", threads) == datetime(2026, 3, 1)


def test_last_dialogue_at_filters_by_assistant_id():
    threads = [
        _t("t1", "a1", updated="2026-01-01T00:00:00"),
        _t("t2", "a1", updated="2026-09-01T00:00:00"),
        {"thread_id": "t3", "assistant_id": None, "type": "chat",
         "updated_at": "2026-09-09T00:00:00"},
    ]
    assert last_dialogue_at("a1", threads) == datetime(2026, 9, 1)
    assert last_dialogue_at("a2", threads) is None


def test_last_dialogue_at_falls_back_to_created_at():
    threads = [_t("t1", "a1", updated="", created="2026-02-01T00:00:00")]
    assert last_dialogue_at("a1", threads) == datetime(2026, 2, 1)


def test_last_dialogue_at_empty_inputs():
    assert last_dialogue_at("a1", None) is None
    assert last_dialogue_at("a1", []) is None
    assert last_dialogue_at(None, [_t("t1", "a1", updated="2026-01-01T00:00:00")]) is None


# ─── overall ordering ─────────────────────────────────────────────────────

def test_new_assistant_lands_at_very_top():
    # The task rule: a freshly created assistant (no dialogues yet) must
    # appear at the top even when other assistants have older dialogues.
    assistants = [
        _a("old_no_dial", "Old No Dialog", created="2025-01-01T00:00:00"),
        _a("brand_new", "Brand New", created="2026-06-01T12:00:00"),
        _a("with_dial", "With Dialog", created="2025-01-01T00:00:00"),
    ]
    threads = [_t("t1", "with_dial", updated="2026-05-01T00:00:00")]

    ids = [a["id"] for a in sort_assistants(assistants, threads)]
    assert ids == ["brand_new", "with_dial", "old_no_dial"]


def test_all_assistants_without_dialogues_sorted_by_creation_desc():
    assistants = [
        _a("mid", "Mid", created="2026-02-01T00:00:00"),
        _a("newest", "Newest", created="2026-04-01T00:00:00"),
        _a("oldest", "Oldest", created="2025-12-31T00:00:00"),
    ]
    ids = [a["id"] for a in sort_assistants(assistants, [])]
    assert ids == ["newest", "mid", "oldest"]


def test_assistants_with_dialogues_sorted_by_dialogue_desc():
    assistants = [
        _a("older_dial", "Older", created="2025-01-01T00:00:00"),
        _a("newer_dial", "Newer", created="2025-01-01T00:00:00"),
        _a("silent", "Silent", created="2026-01-01T00:00:00"),
    ]
    threads = [
        _t("t1", "older_dial", updated="2026-01-01T00:00:00"),
        _t("t2", "newer_dial", updated="2026-03-01T00:00:00"),
        _t("t3", "silent", updated="2026-02-01T00:00:00"),
    ]
    ids = [a["id"] for a in sort_assistants(assistants, threads)]
    assert ids == ["newer_dial", "silent", "older_dial"]


def test_dialogue_older_than_creation_still_sorts_first_for_that_assistant():
    # Effective time = max(last dialogue, would-be creation time); in this
    # fixture the dialogue is older than another assistant's creation time,
    # so the newest-creation assistant wins overall.
    assistants = [
        _a("dial_old", "Dialog Old", created="2025-01-01T00:00:00"),
        _a("newer_no_dial", "Newer Silent", created="2026-05-01T00:00:00"),
    ]
    threads = [_t("t1", "dial_old", updated="2026-01-01T00:00:00")]
    ids = [a["id"] for a in sort_assistants(assistants, threads)]
    assert ids == ["newer_no_dial", "dial_old"]


def test_tie_without_dates_falls_back_to_name_and_input_is_not_mutated():
    assistants = [
        _a("z", "Zebra"),
        _a("a", "apple"),
    ]
    original = [dict(a) for a in assistants]
    ids = [a["id"] for a in sort_assistants(assistants, [])]
    assert ids == ["a", "z"]
    assert assistants == original


def test_created_at_fallbacks_to_updated_at():
    assistants = [
        _a("a1", "A", created="", updated="2026-01-01T00:00:00"),
        _a("a2", "B", created="", updated="2025-01-01T00:00:00"),
    ]
    ids = [a["id"] for a in sort_assistants(assistants, [])]
    assert ids == ["a1", "a2"]


# ─── visible block splitting ──────────────────────────────────────────────

def test_split_nav_lists_defaults_to_five():
    assistants = [_a(str(i), f"A{i}") for i in range(7)]
    visible, remaining = split_nav_lists(assistants)
    assert len(visible) == DEFAULT_VISIBLE_ASSISTANTS == 5
    assert len(remaining) == 2
    assert [a["id"] for a in visible] == ["0", "1", "2", "3", "4"]
    assert [a["id"] for a in remaining] == ["5", "6"]


def test_split_nav_lists_respects_custom_count_and_empty_list():
    assistants = [_a(str(i), f"A{i}") for i in range(3)]
    visible, remaining = split_nav_lists(assistants, visible_count=2)
    assert len(visible) == 2
    assert len(remaining) == 1

    visible, remaining = split_nav_lists([], visible_count=2)
    assert visible == []
    assert remaining == []
