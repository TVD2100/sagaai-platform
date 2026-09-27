# -*- coding: utf-8 -*-
"""tests/test_orchestrator_message_controls.py - per-message controls.

The orchestrator chat feed shows the download/copy panel (MD/TXT download,
Copy MD/TXT) on EVERY plain-prose assistant answer, not only on the final
message. Messages whose content embeds machine tool calls never get the
panel; the live final answer keeps it hidden until the loop finishes.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._st_mock import install_streamlit_mock, StopRerun  # noqa: E402


@pytest.fixture()
def ui_env(monkeypatch, tmp_path):
    """Isolated DATA_DIR + fresh ui.* modules under the streamlit mock."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SAGAAI_DATA_DIR", str(data_dir))
    for m in list(sys.modules):
        if m == "ui" or m.startswith("ui."):
            sys.modules.pop(m, None)
    with install_streamlit_mock() as st:
        def _no_input(*args, **kwargs):
            st._rec("chat_input", args, kwargs)
            return None
        st.chat_input = _no_input
        yield st


def _rerender(st, fn):
    try:
        fn()
    except StopRerun:
        pass


def _orch_dict(slug="custom1"):
    """Minimal orchestrator record for the chat page."""
    return {
        "slug": slug,
        "name": "Custom",
        "description": "",
        "is_builtin": False,
        "prompt_text": "",
        "config": {"strong_service": "DeepSeek", "strong_model": "m1"},
    }


def _prepare_page(ui_env, monkeypatch, slug="custom1", agent_phase=None):
    """Wire get_orchestrator; optionally seed an active loop state."""
    import ui.pages.orchestrator as orch_page

    monkeypatch.setattr(orch_page, "get_orchestrator",
                        lambda s: _orch_dict(slug) if s == slug else None)
    monkeypatch.setattr(orch_page, "_assistant_has_api_key", lambda svc: True)
    ui_env.session_state.update({"ui_lang": "English"})
    if agent_phase is not None:
        from dev_agent.agent_loop import AgentLoopState
        state = AgentLoopState(
            task="", history=[], economy_mode=True, thread_id="",
            economy_cache_enabled=False, economy_cache_multiplier=1,
        )
        state.phase = agent_phase
        ui_env.session_state["orch_" + slug + "_loop_state"] = state
    return orch_page


_MIXED_CONTENT = (
    "Looked at a.py.\n\n"
    "```json\n"
    '{"tool": "read_file", "args": {"path": "a.py"}}\n'
    "```"
)
_PURE_STEP_CONTENT = (
    "```json\n"
    '{"tool": "list_files", "args": {"max_depth": 1}}\n'
    "```"
)


def _sample_history():
    return [
        {"role": "user", "content": "first question", "ts": ""},
        {"role": "assistant", "content": "first plain answer", "ts": ""},
        {"role": "user", "content": "second question", "ts": ""},
        {"role": "assistant", "content": _MIXED_CONTENT, "ts": ""},
        {"role": "user", "content": "third question", "ts": ""},
        {"role": "assistant", "content": _PURE_STEP_CONTENT, "ts": ""},
        {"role": "user", "content": "fourth question", "ts": ""},
        {"role": "assistant", "content": "final plain answer", "ts": ""},
    ]


def _download_keys(st):
    return [c[2].get("key") for c in st.calls if c[0] == "download_button"]


def _clipboard_payloads(st):
    return " ".join(c[1][0] for c in st.calls if c[0] == "html")


def test_controls_on_every_plain_answer(ui_env, monkeypatch):
    """Old-turn and final plain answers each render their own controls."""
    slug = "custom1"
    orch_page = _prepare_page(ui_env, monkeypatch, slug)
    ui_env.session_state[f"orch_{slug}_history"] = _sample_history()

    _rerender(ui_env, lambda: orch_page.page_orchestrator(slug))

    dl_keys = _download_keys(ui_env)
    payloads = _clipboard_payloads(ui_env)
    assert set(dl_keys) == {
        f"orch_dl_md_{slug}_1", f"orch_dl_txt_{slug}_1",
        f"orch_dl_md_{slug}_7", f"orch_dl_txt_{slug}_7",
    }, f"download controls misplaced: {dl_keys}"
    for idx in (1, 7):
        assert f'id="cb_orch_cp_md_{slug}_{idx}"' in payloads, (
            f"Copy MD missing for msg {idx}")
        assert f'id="cb_orch_cp_txt_{slug}_{idx}"' in payloads, (
            f"Copy TXT missing for msg {idx}")

    markdowns = [c[1][0] for c in ui_env.calls if c[0] == "markdown"]
    assert "first plain answer" in markdowns
    assert "final plain answer" in markdowns


def test_tool_call_messages_get_no_controls(ui_env, monkeypatch):
    """A mixed message shows its prose but no controls; a pure tool step
    shows neither prose nor controls (compact caption instead)."""
    from core.i18n import t

    slug = "custom1"
    orch_page = _prepare_page(ui_env, monkeypatch, slug)
    ui_env.session_state[f"orch_{slug}_history"] = _sample_history()

    _rerender(ui_env, lambda: orch_page.page_orchestrator(slug))

    dl_keys = _download_keys(ui_env)
    payloads = _clipboard_payloads(ui_env)
    for idx in (3, 5):
        assert f"orch_dl_md_{slug}_{idx}" not in dl_keys, (
            f"unexpected MD download on tool message {idx}: {dl_keys}")
        assert f"orch_dl_txt_{slug}_{idx}" not in dl_keys, (
            f"unexpected TXT download on tool message {idx}: {dl_keys}")
        assert f'id="cb_orch_cp_md_{slug}_{idx}"' not in payloads
        assert f'id="cb_orch_cp_txt_{slug}_{idx}"' not in payloads

    markdowns = [c[1][0] for c in ui_env.calls if c[0] == "markdown"]
    assert "Looked at a.py." in markdowns
    captions = [c[1][0] for c in ui_env.calls if c[0] == "caption"]
    assert t("devagent_agent_step_compact", lang="English") in captions, (
        f"compact step caption missing: {captions}")


def test_live_final_answer_hides_controls_while_agent_active(ui_env, monkeypatch):
    """While the loop runs only FINISHED answers keep their controls."""
    slug = "custom1"
    orch_page = _prepare_page(ui_env, monkeypatch, slug,
                              agent_phase="calling_llm")
    # An active loop makes the page continue it at the end of the render:
    # replace the step with a no-op so no real LLM call happens (st.rerun
    # then unwinds the render via StopRerun, keeping all recorded calls).
    monkeypatch.setattr(orch_page, "_do_step", lambda **kwargs: None)
    ui_env.session_state[f"orch_{slug}_history"] = _sample_history()

    _rerender(ui_env, lambda: orch_page.page_orchestrator(slug))

    dl_keys = _download_keys(ui_env)
    assert set(dl_keys) == {
        f"orch_dl_md_{slug}_1", f"orch_dl_txt_{slug}_1",
    }, f"finished answers must keep controls while the agent runs: {dl_keys}"
    payloads = _clipboard_payloads(ui_env)
    assert f'id="cb_orch_cp_md_{slug}_1"' in payloads
    assert f'id="cb_orch_cp_txt_{slug}_1"' in payloads
    assert f'id="cb_orch_cp_md_{slug}_7"' not in payloads
    assert f'id="cb_orch_cp_txt_{slug}_7"' not in payloads
