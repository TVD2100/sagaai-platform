# -*- coding: utf-8 -*-
"""tests/scenarios/test_orchestrator_devagent_scenarios.py - user-level
scenario tests for the DevAgent orchestrator (employee) toolchain.

Scenarios (given -> when -> then), walking the public DevAgent entry point
``dev_agent.universal_agent.UniversalDevAgent.dispatch`` - exactly the way
the LLM calls these tools - plus the public core CRUD API for the import
gate:

  Scenario 1 - full employee lifecycle via the dispatcher: create with a
               human slug, auto-named instruction id, custom function that
               is callable through the dispatcher, prompt update, reload,
               duplicate rejection, and delete that purges folder + cache.
  Scenario 2 - boundary failures: empty/meaningless slugs are rejected;
               unknown tool arguments produce a structured error with
               ``unknown_args`` + ``suggestion``; reload/delete of a missing
               orchestrator fail; nothing is created.
  Scenario 3 - import validation gate: a path-traversal slug is rejected,
               a normal export is imported under its normalized slug.
  Scenario 4 - the personal folder is the source of truth: a hand-edited
               orchestrator.json / system_prompt.md / instructions/*.md become
               visible after ``reload_orchestrator`` (system_prompt.md wins
               over the bundle json when both are present).
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from storage.db import reset_engine, reset_devagent_engine


@pytest.fixture
def isolated_data_dir():
    """Temporary DATA_DIR that isolates orchestrator folders from real DB."""
    tmp = tempfile.mkdtemp(prefix="sagaai_scen_orch_")
    old_data_dir = os.environ.get("SAGAAI_DATA_DIR")
    os.environ["SAGAAI_DATA_DIR"] = tmp

    import core.paths
    old_values = {}
    for attr in ("DATA_DIR", "DB_PATH", "DEVAGENT_DB_PATH", "HISTORY_DIR", "SYSTEM_PROMPTS_DIR"):
        old_values[attr] = getattr(core.paths, attr, None)

    core.paths.DATA_DIR = tmp
    core.paths.DB_PATH = os.path.join(tmp, "sagaai.db")
    core.paths.DEVAGENT_DB_PATH = os.path.join(tmp, "devagent.db")
    core.paths.HISTORY_DIR = os.path.join(tmp, "history")
    core.paths.SYSTEM_PROMPTS_DIR = os.path.join(tmp, "system_prompts")

    reset_engine()
    reset_devagent_engine()

    yield tmp

    reset_engine()
    reset_devagent_engine()

    if old_data_dir:
        os.environ["SAGAAI_DATA_DIR"] = old_data_dir
    else:
        os.environ.pop("SAGAAI_DATA_DIR", None)

    for attr in ("DATA_DIR", "DB_PATH", "DEVAGENT_DB_PATH", "HISTORY_DIR", "SYSTEM_PROMPTS_DIR"):
        if old_values.get(attr) is not None:
            setattr(core.paths, attr, old_values[attr])

    shutil.rmtree(tmp, ignore_errors=True)


def test_scenario_full_employee_lifecycle_via_devagent_dispatch(isolated_data_dir):
    """Scenario 1: a DevAgent creates and manages a complete employee.

    Given an empty platform (fresh DATA_DIR),
    when  the dispatcher runs create_orchestrator with a human slug, then
          saves an auto-id instruction, a custom function, updates the
          prompt, attaches the orchestrator, calls the custom function and
          reloads from the folder,
    then  the slug is normalized ('review_bot'), the instruction id is an
          8-hex string that can be read back, the custom function answers
          through the dispatcher, a duplicate slug is rejected, and delete
          removes the folder and the instruction cache.
    """
    from dev_agent.universal_agent import UniversalDevAgent

    agent = UniversalDevAgent()

    created = agent.dispatch("create_orchestrator", {
        "slug": "Review Bot", "name": "Review Bot", "prompt_text": "initial",
    })
    assert created["ok"] is True, created
    assert created["slug"] == "review_bot"
    slug = created["slug"]

    instr = agent.dispatch("save_orchestrator_instruction", {
        "slug": slug, "name": "Style Guide", "prompt_text": "use single quotes",
    })
    assert instr["ok"] is True, instr
    iid = instr["instruction_id"]
    assert isinstance(iid, str) and len(iid) == 8

    fn_code = "def invoke(**kw):\n    return {'ok': True, 'n': kw.get('n', 0) * 2}\n"
    fn = agent.dispatch("save_orchestrator_function", {
        "slug": slug, "name": "double", "code": fn_code,
    })
    assert fn["ok"] is True, fn

    upd = agent.dispatch("update_orchestrator", {"slug": slug, "prompt_text": "final prompt"})
    assert upd["ok"] is True, upd

    agent.attach_orchestrator(slug)
    call = agent.dispatch("double", {"n": 21})
    assert call == {"ok": True, "n": 42}

    reloaded = agent.dispatch("reload_orchestrator", {"slug": slug})
    assert reloaded["ok"] is True, reloaded
    assert reloaded["action"] == "updated"

    from core.orchestrators import get_orchestrator
    orch = get_orchestrator(slug)
    assert orch["name"] == "Review Bot"
    assert orch["prompt_text"] == "final prompt"

    got_instr = agent.dispatch("get_orchestrator_instruction", {
        "slug": slug, "instruction_id": iid,
    })
    assert got_instr["ok"] is True, got_instr
    assert got_instr["instruction"]["text"] == "use single quotes"

    dup = agent.dispatch("create_orchestrator", {"slug": "REVIEW_BOT", "name": "dup"})
    assert dup["ok"] is False

    deleted = agent.dispatch("delete_orchestrator", {"slug": slug})
    assert deleted["ok"] is True, deleted

    from core.orchestrator_folders import orchestrator_folder_exists
    from storage.repository import repo_list_orchestrator_instructions
    assert not orchestrator_folder_exists(slug)
    assert repo_list_orchestrator_instructions(slug) == []
    assert agent.dispatch("get_orchestrator", {"slug": slug})["ok"] is False


def test_scenario_boundary_failures_are_rejected_cleanly(isolated_data_dir):
    """Scenario 2: invalid inputs fail loud and leave no state behind.

    Given an empty platform,
    when  the dispatcher is asked to create an orchestrator from an empty or
          meaningless slug, to update one with an unknown argument, or to
          reload/delete a nonexistent slug,
    then  every call returns ok=False, the unknown-argument error carries
          ``unknown_args`` and a ``suggestion``, and no orchestrator exists.
    """
    from dev_agent.universal_agent import UniversalDevAgent

    agent = UniversalDevAgent()

    for bad_slug in ("", "   ", "!!!", "___"):
        res = agent.dispatch("create_orchestrator", {"slug": bad_slug, "name": "X"})
        assert res["ok"] is False, f"slug={bad_slug!r} -> {res}"

    unknown = agent.dispatch("update_orchestrator", {"slug": "nope", "foo": 1})
    assert unknown["ok"] is False
    assert unknown.get("unknown_args") == ["foo"]
    assert "suggestion" in unknown

    assert agent.dispatch("reload_orchestrator", {"slug": "ghost"})["ok"] is False
    assert agent.dispatch("delete_orchestrator", {"slug": "ghost"})["ok"] is False
    assert agent.dispatch("save_orchestrator_instruction",
                          {"slug": "ghost", "name": ""})["ok"] is False

    listing = agent.dispatch("list_orchestrators", {})
    assert listing["ok"] is True
    assert listing["count"] == 0


def test_scenario_import_validation_gate(isolated_data_dir):
    """Scenario 3: importing an export with a traversal slug is refused.

    Given an empty platform,
    when  an export dict with slug '../evil' is imported, then a normal
          export ('MyBot') is imported,
    then  the traversal import is rejected, the regular import lands under
          the normalized slug with its folder + prompt, and cleanup removes
          it again.
    """
    from core.orchestrators import import_orchestrator, delete_orchestrator, get_orchestrator_by_slug

    bad = import_orchestrator({"format": "sagaai_orchestrator/v1", "slug": "../evil", "name": "Evil"})
    assert bad["ok"] is False

    good = import_orchestrator({
        "format": "sagaai_orchestrator/v1",
        "slug": "MyBot", "name": "My Bot", "prompt_text": "prompt",
    })
    assert good["ok"] is True, good.get("error")
    assert good["slug"] == "mybot"
    assert get_orchestrator_by_slug("mybot") is not None

    delete_orchestrator("mybot")
    assert get_orchestrator_by_slug("mybot") is None


def test_scenario_folder_is_source_of_truth(isolated_data_dir):
    """Scenario 4: hand edits to the personal folder go live after reload.

    Given an employee created through the dispatcher,
    when  the user hand-edits orchestrator.json (name), system_prompt.md
          (prompt) and drops instructions/manual.md into the folder, then
          calls reload_orchestrator via the dispatcher,
    then  get_orchestrator returns the hand-edited name and the prompt from
          system_prompt.md (the md file wins over the bundle json), and the
          hand-written instruction is readable through the dispatcher.
    """
    from dev_agent.universal_agent import UniversalDevAgent

    agent = UniversalDevAgent()
    created = agent.dispatch("create_orchestrator", {
        "slug": "folder_truth", "name": "Old Name", "prompt_text": "old prompt",
    })
    assert created["ok"] is True, created
    slug = created["slug"]

    from core.orchestrator_folders import get_orchestrator_dir
    folder = get_orchestrator_dir(slug)

    with open(os.path.join(folder, "orchestrator.json"), "w", encoding="utf-8") as f:
        json.dump({
            "slug": slug, "name": "Hand Edited", "description": "by user",
            "prompt_text": "stale-from-json", "config": {}, "tools": [],
            "max_steps": 50, "auto_apply": False,
        }, f, ensure_ascii=False, indent=2)
    with open(os.path.join(folder, "system_prompt.md"), "w", encoding="utf-8") as f:
        f.write("hand prompt")
    with open(os.path.join(folder, "instructions", "manual.md"), "w", encoding="utf-8") as f:
        f.write("---\nid: manual\nname: Manual Guide\n---\n\nManual body\n")

    reloaded = agent.dispatch("reload_orchestrator", {"slug": slug})
    assert reloaded["ok"] is True, reloaded
    assert reloaded["action"] == "updated"

    got = agent.dispatch("get_orchestrator", {"slug": slug})
    assert got["ok"] is True, got
    orch = got["orchestrator"]
    assert orch["name"] == "Hand Edited"
    assert orch["prompt_text"] == "hand prompt"  # md wins over bundle json

    got_instr = agent.dispatch("get_orchestrator_instruction", {
        "slug": slug, "instruction_id": "manual",
    })
    assert got_instr["ok"] is True, got_instr
    assert got_instr["instruction"]["name"] == "Manual Guide"

    agent.dispatch("delete_orchestrator", {"slug": slug})
