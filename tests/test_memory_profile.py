"""Contract tests of the declarative long-term memory of an employee.

These tests cover the three planes the feature separates: the declaration
(normalisation, refusal of an absolute path), the mechanism (budget of an
``always`` level, refusal to read a ``search`` level whole, one-call append,
rotation) and the prompt block rendered from the declaration.
"""
import json

import pytest

from core import memory_profile as mp

PROFILE = {
    "levels": [
        {"id": "state", "kind": "always", "path": "registry.md",
         "columns": ["date", "project", "status"], "read_lines": 5, "max_rows": 4},
        {"id": "units", "kind": "on_demand", "path": "projects/<key>/card.md"},
        {"id": "history", "kind": "search", "path": "journal.md"},
    ]
}


@pytest.fixture()
def folder(tmp_path):
    """Return an employee folder holding one registry and one journal."""
    root = tmp_path / "employee"
    root.mkdir()
    (root / "registry.md").write_text(
        "# Registry\n"
        "\n"
        "| date | project | status |\n"
        "|---|---|---|\n"
        "| 2026-11-01 | alpha | done |\n"
        "| 2026-11-02 | beta | open |\n",
        encoding="utf-8",
    )
    (root / "journal.md").write_text(
        "# Journal\n\n" + "\n".join(f"- entry {i}" for i in range(40)) + "\n",
        encoding="utf-8",
    )
    return root


def test_normalize_accepts_list_and_object():
    as_list = mp.normalize_profile(PROFILE["levels"])
    as_object = mp.normalize_profile(PROFILE)
    assert as_list == as_object
    ids = [level["id"] for level in as_list["levels"]]
    assert ids == ["state", "units", "history"]


def test_normalize_defaults_kind_to_always():
    profile = mp.normalize_profile([{"id": "state", "path": "registry.md"}])
    assert profile["levels"][0]["kind"] == mp.KIND_ALWAYS


@pytest.mark.parametrize("bad", ["/abs/registry.md", "~/registry.md",
                                 "../registry.md", "a/../../b.md"])
def test_absolute_and_escaping_paths_are_refused(bad):
    with pytest.raises(mp.MemoryProfileError):
        mp.normalize_profile([{"id": "state", "path": bad}])


def test_unknown_kind_and_duplicate_id_are_refused():
    with pytest.raises(mp.MemoryProfileError):
        mp.normalize_profile([{"id": "state", "path": "a.md", "kind": "sometimes"}])
    with pytest.raises(mp.MemoryProfileError):
        mp.normalize_profile([{"id": "state", "path": "a.md"},
                              {"id": "state", "path": "b.md"}])


def test_level_and_column_limits_are_enforced():
    too_many = [{"id": f"l{i}", "path": f"f{i}.md"} for i in range(mp.MAX_LEVELS + 1)]
    with pytest.raises(mp.MemoryProfileError):
        mp.normalize_profile(too_many)
    wide = [f"c{i}" for i in range(mp.MAX_COLUMNS + 1)]
    with pytest.raises(mp.MemoryProfileError):
        mp.normalize_profile([{"id": "state", "path": "a.md", "columns": wide}])


def test_broken_declaration_does_not_raise_from_profile_of():
    assert mp.profile_of({"memory_profile": "not a list"}) == {"levels": []}
    assert mp.profile_of({}) == {"levels": []}
    assert mp.profile_of(None) == {"levels": []}


def test_always_level_honours_its_read_budget(folder):
    profile = mp.normalize_profile(PROFILE)
    result = mp.read_level(folder, mp.get_level(profile, "state"))
    assert result["ok"] is True
    assert result["lines_returned"] == 5
    assert result["truncated"] is True
    assert result["lines_total"] == 6


def test_read_limit_cannot_exceed_the_kind_ceiling(folder):
    profile = mp.normalize_profile(PROFILE)
    result = mp.read_level(folder, mp.get_level(profile, "state"), limit=9999)
    assert result["lines_returned"] <= mp.MAX_READ_LINES


def test_search_level_is_never_read_whole(folder):
    profile = mp.normalize_profile(PROFILE)
    with pytest.raises(mp.MemoryProfileError, match="search-only"):
        mp.read_level(folder, mp.get_level(profile, "history"))


def test_search_returns_hits_without_the_whole_file(folder):
    profile = mp.normalize_profile(PROFILE)
    result = mp.search_level(folder, mp.get_level(profile, "history"), "entry 39")
    assert result["hit_count"] == 1
    assert result["hits"][0]["line"] == 42
    assert result["lines_total"] == 42
    assert "entry 39" in result["hits"][0]["text"]


def test_search_refuses_an_empty_query(folder):
    profile = mp.normalize_profile(PROFILE)
    with pytest.raises(mp.MemoryProfileError, match="empty"):
        mp.search_level(folder, mp.get_level(profile, "history"), "   ")


def test_append_prepends_right_after_the_table_header(folder):
    profile = mp.normalize_profile(PROFILE)
    result = mp.append_entry(folder, mp.get_level(profile, "state"),
                             row={"date": "2026-11-19", "project": "gamma",
                                  "status": "open"})
    assert result["ok"] is True
    lines = (folder / "registry.md").read_text(encoding="utf-8").splitlines()
    assert lines[4] == "| 2026-11-19 | gamma | open |"
    assert any("alpha" in line for line in lines), "append must keep existing rows"
    assert any("beta" in line for line in lines)


def test_append_creates_the_table_of_an_empty_file(folder):
    profile = mp.normalize_profile([{"id": "state", "path": "new.md",
                                     "columns": ["a", "b"]}])
    mp.append_entry(folder, mp.get_level(profile, "state"), row={"a": "1", "b": "2"})
    text = (folder / "new.md").read_text(encoding="utf-8")
    assert "| a | b |" in text
    assert "| 1 | 2 |" in text


def test_append_validates_the_record_against_the_columns(folder):
    profile = mp.normalize_profile(PROFILE)
    level = mp.get_level(profile, "state")
    with pytest.raises(mp.MemoryProfileError, match="missing status"):
        mp.append_entry(folder, level, row={"date": "d", "project": "p"})
    with pytest.raises(mp.MemoryProfileError, match="unexpected"):
        mp.append_entry(folder, level, row={"date": "d", "project": "p",
                                            "status": "s", "extra": "x"})


def test_append_refuses_a_structured_cell(folder):
    profile = mp.normalize_profile(PROFILE)
    with pytest.raises(mp.MemoryProfileError, match="scalar"):
        mp.append_entry(folder, mp.get_level(profile, "state"),
                        row={"date": "d", "project": [1, 2], "status": "s"})


def test_append_refuses_too_long_a_cell(folder):
    profile = mp.normalize_profile(PROFILE)
    with pytest.raises(mp.MemoryProfileError, match="limit"):
        mp.append_entry(folder, mp.get_level(profile, "state"),
                        row={"date": "d", "project": "x" * (mp.MAX_VALUE_CHARS + 1),
                             "status": "s"})


def test_template_level_refuses_read_and_append(folder):
    profile = mp.normalize_profile(PROFILE)
    level = mp.get_level(profile, "units")
    with pytest.raises(mp.MemoryProfileError, match="family"):
        mp.read_level(folder, level)
    with pytest.raises(mp.MemoryProfileError, match="family"):
        mp.append_entry(folder, level, text="x")


def test_free_text_level_prepends_after_the_title(folder):
    profile = mp.normalize_profile([{"id": "notes", "path": "notes.md"}])
    level = mp.get_level(profile, "notes")
    (folder / "notes.md").write_text("# Notes\n\nolder\n", encoding="utf-8")
    mp.append_entry(folder, level, text="## 2026-11-19 - newer")
    text = (folder / "notes.md").read_text(encoding="utf-8")
    assert text.index("newer") < text.index("older")


def test_free_text_level_requires_a_body(folder):
    profile = mp.normalize_profile([{"id": "notes", "path": "notes.md"}])
    with pytest.raises(mp.MemoryProfileError, match="free text"):
        mp.append_entry(folder, mp.get_level(profile, "notes"), text="  ")


def test_rotate_moves_the_tail_and_keeps_the_head(folder):
    profile = mp.normalize_profile(PROFILE)
    level = mp.get_level(profile, "state")
    result = mp.rotate_level(folder, level)
    assert result["rotated"] is True
    assert result["kept"] == 4
    kept = (folder / "registry.md").read_text(encoding="utf-8").splitlines()
    assert len(kept) == 4
    archives = list((folder / "archive").glob("registry-*.md"))
    assert len(archives) == 1
    assert "alpha" in archives[0].read_text(encoding="utf-8")


def test_rotate_is_a_no_op_below_the_limit(folder):
    profile = mp.normalize_profile(PROFILE)
    level = mp.get_level(profile, "state")
    level["max_rows"] = 100
    result = mp.rotate_level(folder, level)
    assert result["rotated"] is False
    assert result["moved"] == 0


def test_rotate_without_max_rows_is_refused(folder):
    profile = mp.normalize_profile(PROFILE)
    level = mp.get_level(profile, "state")
    level.pop("max_rows", None)
    with pytest.raises(mp.MemoryProfileError, match="max_rows"):
        mp.rotate_level(folder, level)


def test_prompt_block_lists_levels_with_their_policy(folder):
    profile = mp.normalize_profile(PROFILE)
    block = mp.render_block(profile, str(folder))
    assert "## Memory profile" in block
    assert "`state`" in block and "always" in block
    assert "budget 5 lines" in block
    assert "`units`" in block and "on_demand" in block
    assert "`history`" in block and "search" in block
    assert "memory_append" in block
    assert "memory_search" in block
    assert str(folder) in block
    assert "Never write a filesystem path" in block
    assert "never widens tool" in block


def test_prompt_block_of_a_child_forbids_role_registries(folder):
    profile = mp.normalize_profile(PROFILE)
    block = mp.render_block(profile, str(folder), is_child=True)
    assert "delegated subagent" in block
    assert "must not write" in block
    assert "`history`" not in block


def test_prompt_block_is_empty_without_levels():
    assert mp.render_block(mp.normalize_profile(None), "/tmp/x") == ""


def test_level_status_reports_without_reading_the_body(folder):
    profile = mp.normalize_profile(PROFILE)
    status = mp.level_status(folder, mp.get_level(profile, "state"))
    assert status["exists"] is True
    assert status["lines"] == 6
    assert status["columns"] == ["date", "project", "status"]


def test_status_of_missing_file_is_honest(folder):
    profile = mp.normalize_profile([{"id": "state", "path": "absent.md"}])
    status = mp.level_status(folder, mp.get_level(profile, "state"))
    assert status["exists"] is False
    assert status["lines"] == 0


def test_status_of_a_family_level_needs_no_file(folder):
    profile = mp.normalize_profile(PROFILE)
    status = mp.level_status(folder, mp.get_level(profile, "units"))
    assert status["template"] is True
    assert status["exists"] is None


def test_declaration_round_trips_through_json():
    config = {"memory_profile": json.loads(json.dumps(PROFILE))}
    profile = mp.profile_of(config)
    assert len(profile["levels"]) == 3
    assert mp.get_level(profile, "state")["columns"] == ["date", "project", "status"]


# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
