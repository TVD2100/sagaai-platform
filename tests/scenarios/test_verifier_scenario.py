# -*- coding: utf-8 -*-
"""tests/scenarios/test_verifier_scenario.py - проверка результата делегирования.

Given сотрудник с назначенным верификатором и веер из трёх исполнителей
When родитель проверяет один результат через verify_result
Then он получает структурный вердикт, а порядок веера не меняется.

Сценарий проверяет связку, которую не видит ни один модульный тест: настройка
(её пишет страница настроек) -> каталог инструментов и блок промпта (их видит
модель) -> запуск верификатора в дереве родителя -> вердикт, разобранный из
ответа. Верификатор видит только результат: если бы он получал транскрипт,
проверка превратилась бы в согласие с исполнителем.
"""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from storage.db import reset_devagent_engine, reset_engine  # noqa: E402

PARENT = "scenario_parent"
WORKER = "scenario_worker"
CHECKER = "scenario_checker"


@pytest.fixture
def platform(tmp_path, monkeypatch):
    """Изолированный каталог данных: строки оркестраторов и журналы."""
    monkeypatch.setenv("SAGAAI_DATA_DIR", str(tmp_path))

    import core.paths as paths_mod
    for attr, value in (("DATA_DIR", str(tmp_path)),
                        ("DB_PATH", str(tmp_path / "sagaai.db")),
                        ("DEVAGENT_DB_PATH", str(tmp_path / "devagent.db")),
                        ("HISTORY_DIR", str(tmp_path / "history")),
                        ("SYSTEM_PROMPTS_DIR", str(tmp_path / "system_prompts"))):
        monkeypatch.setattr(paths_mod, attr, value, raising=False)

    import dev_agent.config as dagent_config
    journal_dir = tmp_path / "task_states"
    journal_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(dagent_config, "TASK_STATES_DIR", journal_dir,
                        raising=False)
    monkeypatch.setattr(dagent_config, "WORKSPACE_SELECTED", True,
                        raising=False)

    reset_engine()
    reset_devagent_engine()
    yield tmp_path
    reset_engine()
    reset_devagent_engine()


@pytest.fixture
def employees(platform):
    """Родитель, исполнитель и назначенный верификатор."""
    from core.orchestrators import (create_orchestrator, set_allowed_agents,
                                    set_verifier_agent)

    for slug, name in ((PARENT, "Scenario Parent"),
                       (WORKER, "Scenario Worker"),
                       (CHECKER, "Scenario Checker")):
        assert create_orchestrator(slug, name, "scenario") is not None
    # Право запускать исполнителя и право проверять - разные права.
    assert set_allowed_agents(PARENT, [WORKER]) is True
    assert set_verifier_agent(PARENT, CHECKER) is True
    return {"parent": PARENT, "worker": WORKER, "checker": CHECKER}


def _service(platform, runner):
    """Настоящий сервис оркестрации с подменённым исполнителем детей."""
    from core.orchestration.service import OrchestrationService

    service = OrchestrationService()
    service.set_store_root(str(platform / "runs"))
    service.set_child_runner(runner)
    return service


def _dispatcher(slug):
    """Поверхность диспетчера, которую читает фаза веера."""
    class _D:
        child_disabled_tools = set()

        def _current_orchestrator_slug(self):
            return slug

    return _D()


def _answer(verdict="passed", reasons=None, checked=None):
    return "Проверил сам.\n" + json.dumps({
        "verdict": verdict,
        "reasons": list(reasons or []),
        "checked": list(checked or ["отчёт"]),
    }, ensure_ascii=False)


# ─── сценарий ─────────────────────────────────────────────────────────────


def test_fan_out_then_verify_the_chosen_result(employees, platform):
    """Веер из трёх -> проверка одного -> вердикт passed, порядок цел."""
    import core.orchestrators as orch_mod
    from core.orchestration import verify as V
    from core.orchestration.contracts import AgentBrief, AgentRunResult
    from dev_agent import agent_loop as L

    seen_briefs = []

    def runner(**kwargs):
        brief = kwargs.get("brief") or AgentBrief(goal="")
        seen_briefs.append(brief)
        if str(brief.goal).startswith(V.VERIFIER_GOAL_PREFIX):
            return AgentRunResult(run_id=kwargs["run_id"],
                                  status="done", stop_reason="completed",
                                  summary=_answer("passed", [], ["audit.md"]),
                                  steps_used=1)
        return AgentRunResult(run_id=kwargs["run_id"], status="done",
                              stop_reason="completed",
                              summary="готово: " + str(brief.goal), steps_used=1)

    service = _service(platform, runner)
    state = L.AgentLoopState(task="веер", thread_id="thread-scenario",
                             run_id="run-scenario")
    dispatcher = _dispatcher(PARENT)

    # When: веер из трёх независимых исполнителей.
    calls = [{"tool": "spawn_agent",
              "args": {"agent_slug": WORKER,
                       "request": {"goal": "аудит части %d" % i}}}
             for i in range(3)]
    shuffled = _shuffle(calls)
    results = L.execute_delegation_calls(shuffled, state=state,
                                         dispatcher=dispatcher,
                                         service=service)

    assert [r["ok"] for r in results] == [True, True, True]
    assert [r["result"]["summary"] for r in results] == [
        "готово: " + str(c["args"]["request"]["goal"]) for c in shuffled]
    chosen = results[1]["result"]["run_id"]

    # Then: проверка одного результата даёт вердикт, а не четвёртый результат.
    verdicts = L.execute_delegation_calls(
        [{"tool": "verify_result",
          "args": {"run_id": chosen,
                   "criteria": "отчёт перечисляет все находки аудита"}}],
        state=state, dispatcher=dispatcher, service=service)

    assert len(verdicts) == 1
    assert verdicts[0]["ok"] is True
    verdict = verdicts[0]["verdict"]
    assert verdict["verdict"] == "passed"
    assert verdict["target_run_id"] == chosen
    assert verdict["verifier_run_id"] not in [r["result"]["run_id"] for r in results]

    # Верификатор видит РЕЗУЛЬТАТ: в его задании есть сводка и критерии, но
    # нет транскрипта исполнителя.
    verifier_brief = seen_briefs[-1]
    text = verifier_brief.goal + "\n" + verifier_brief.brief
    assert "готово: аудит части" in text
    assert "отчёт перечисляет все находки аудита" in text
    assert "messages" not in text.lower()

    # Настройка дошла до модели: инструмент и правило есть в её поверхности.
    from core.orchestrator_tools import build_tool_catalog
    from core.orchestrators import _extend_prompt_with_delegation

    names = [t["name"] for t in build_tool_catalog(PARENT, include_connections=False)]
    assert "verify_result" in names
    assert "## Verifying delegated work" in _extend_prompt_with_delegation("BASE", PARENT)


def test_verdict_failed_when_reasons_contradict_the_pass(employees, platform):
    """Исполнитель отработал, но верификатор перечислил замечания -> failed.

    Это правило защищает от верификатора, который подписывает результат и в
    том же ответе перечисляет его дефекты.
    """
    import core.orchestrators as orch_mod
    from core.orchestration import verify as V
    from core.orchestration.contracts import AgentBrief, AgentRunResult
    from dev_agent import agent_loop as L

    def runner(**kwargs):
        brief = kwargs.get("brief") or AgentBrief(goal="")
        if str(brief.goal).startswith(V.VERIFIER_GOAL_PREFIX):
            return AgentRunResult(run_id=kwargs["run_id"], status="done",
                                  stop_reason="completed",
                                  summary=_answer("passed",
                                                  ["в отчёте нет выводов"]),
                                  steps_used=1)
        return AgentRunResult(run_id=kwargs["run_id"], status="done",
                              stop_reason="completed", summary="готово",
                              steps_used=1)

    service = _service(platform, runner)
    state = L.AgentLoopState(task="веер", thread_id="thread-scenario-2",
                             run_id="run-scenario-2")
    dispatcher = _dispatcher(PARENT)

    results = L.execute_delegation_calls(
        [{"tool": "spawn_agent",
          "args": {"agent_slug": WORKER,
                   "request": {"goal": "написать отчёт"}}}],
        state=state, dispatcher=dispatcher, service=service)
    target = results[0]["result"]["run_id"]

    verdicts = L.execute_delegation_calls(
        [{"tool": "verify_result",
          "args": {"run_id": target, "criteria": "в отчёте есть выводы"}}],
        state=state, dispatcher=dispatcher, service=service)

    assert verdicts[0]["ok"] is True
    assert verdicts[0]["verdict"]["verdict"] == "failed"
    assert any("выводов" in r for r in verdicts[0]["verdict"]["reasons"])


def test_without_a_verifier_the_result_stays_unchecked(employees, platform):
    """Верификатор не назначен: инструмента нет, а результаты не проверены.

    Молчаливая подстановка «кого-нибудь» запрещена - иначе пользователь
    считал бы результат проверенным.
    """
    from core.orchestration import tools as T
    from core.orchestrators import set_verifier_agent
    from core.orchestrator_tools import build_tool_catalog
    from core.orchestrators import _extend_prompt_with_delegation

    assert set_verifier_agent(PARENT, "") is True

    names = [t["name"] for t in build_tool_catalog(PARENT, include_connections=False)]
    assert "verify_result" not in names
    assert "## Verifying delegated work" not in _extend_prompt_with_delegation(
        "BASE", PARENT)

    refusal = T.verify_result("some-run", "criteria")

    assert refusal["ok"] is False
    assert refusal["error_code"] == "verifier_not_configured"


def _shuffle(calls):
    """Переставить вызовы так, чтобы позиционный контракт был проверяем."""
    return [calls[2], calls[0], calls[1]]
