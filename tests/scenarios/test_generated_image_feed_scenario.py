# -*- coding: utf-8 -*-
'''tests/scenarios/test_generated_image_feed_scenario.py - feed scenarios
for the generated image shown immediately in the orchestrator chat.

The picture must be visible at once: the image block renders in the feed
itself, OUTSIDE the collapsed tool-result expander. Scenarios (given ->
when -> then) are driven through the real feed entry point
ui.pages.orchestrator._render_chat_tab on the streamlit mock.

  1. happy path - picture and download button at expander depth 0, after
     the collapsed result block has closed;
  2. edge case - the saved file vanished: an actionable caption replaces
     the picture, no broken controls;
  3. error state - the generation failed (HTTP 403): the failure text sits
     in the collapsed result block, no image controls.
'''
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests._st_mock import install_streamlit_mock, StopRerun  # noqa: E402

JPEG_BYTES = bytes([255, 216, 255, 224]) + bytes(16)
SLUG = 'imgfeed'
NL = chr(10)
TOOL_BLOCK = json.dumps({'tool': 'generate_image',
                         'args': {'prompt': 'a red cat'}})


@pytest.fixture()
def ui_env(monkeypatch, tmp_path):
    '''Streamlit mock + isolated DATA_DIR + freshly imported ui.* modules.'''
    data_dir = tmp_path / 'data'
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv('SAGAAI_DATA_DIR', str(data_dir))
    for m in list(sys.modules):
        if m == 'ui' or m.startswith('ui.'):
            sys.modules.pop(m, None)
    with install_streamlit_mock() as st:
        yield st


def _image_result(path, ok=True):
    '''Build the generate_image tool result the agent loop emits.'''
    if not ok:
        return {'ok': False,
                'error': 'HTTP 403: image generation role required'}
    return {'ok': True, 'path': str(path), 'mime': 'image/jpeg',
            'service': 'YandexAI', 'model': 'aliceai-image-art-3.0'}


FENCE = chr(96) * 3


def _assistant_history(result):
    '''Dialog with an assistant turn carrying the generate_image events.'''
    fenced = FENCE + 'json' + NL + TOOL_BLOCK + NL + FENCE
    events = [{'type': 'tool_call', 'tool': 'generate_image',
               'args': {'prompt': 'a red cat'}},
              {'type': 'tool_result', 'tool': 'generate_image',
               'result': result}]
    return [
        {'role': 'user', 'content': 'Draw a red cat',
         'ts': '2026-01-01T00:00:00'},
        {'role': 'assistant', 'content': fenced, '_events': events,
         'ts': '2026-01-01T00:00:01'},
    ]


def _setup_feed(ui_env, monkeypatch, history):
    '''Wire the orchestrator page seams and seed the chat feed state.'''
    import ui.pages.orchestrator as orch_page

    orch = {'slug': SLUG, 'name': 'DevAgent', 'description': '',
            'is_builtin': True, 'prompt_text': '',
            'config': {'strong_service': 'Svc', 'strong_model': 'm1'}}
    monkeypatch.setattr(orch_page, 'get_orchestrator', lambda slug: orch)
    monkeypatch.setattr(orch_page, '_assistant_has_api_key', lambda svc: True)
    monkeypatch.setattr(
        orch_page, 'build_assistant_dicts',
        lambda slug: ({'service': 'Svc', 'model': 'm1', 'temperature': 0.5,
                       'text': 'p', 'max_tokens': 0}, {}))
    monkeypatch.setattr(
        orch_page, 'check_context',
        lambda *a, **k: {'total_tokens': 10, 'ok': True,
                         'limit': 1000, 'excess_chars': 0})
    monkeypatch.setattr(orch_page, 'sum_thread_tokens', lambda msgs: (0, 0, 0))
    ui_env.session_state.update({
        'ui_lang': 'English',
        'orch_' + SLUG + '_history': history,
        'orch_' + SLUG + '_loop_state': None,
        'orch_' + SLUG + '_thread_id': None,
        'orch_' + SLUG + '_web_search': False,
        'orch_' + SLUG + '_economy_mode': False,
        'orch_' + SLUG + '_safety_mode': True,
        'orch_' + SLUG + '_attached': [],
        'orch_' + SLUG + '_upload_counter': 0,
        'orch_' + SLUG + '_saved_msg_count': len(history),
        'orch_' + SLUG + '_dispatcher': None,
        'orch_' + SLUG + '_scroll_to': None,
    })
    ui_env.chat_input = lambda *a, **k: ''
    return orch_page


def _render_feed(st, orch_page):
    '''Run the real feed render; st.rerun() unwinds through StopRerun.'''
    try:
        orch_page._render_chat_tab(SLUG, 'English')
    except StopRerun:
        pass


def _track_depth(st):
    '''Record expander depth of image/download_button calls (0 == feed).'''
    depth = {'cur': 0}
    events = []
    real_expander = st.expander

    def expander(*args, **kwargs):
        ctx = real_expander(*args, **kwargs)

        class _Ctx:
            def __enter__(inner):
                depth['cur'] += 1
                return ctx.__enter__()

            def __exit__(inner, *exc):
                depth['cur'] -= 1
                return ctx.__exit__(*exc)

        return _Ctx()

    st.expander = expander

    def wrap(name):
        original = getattr(st, name)

        def wrapper(*args, **kwargs):
            events.append((name, depth['cur']))
            return original(*args, **kwargs)

        setattr(st, name, wrapper)

    wrap('image')
    wrap('download_button')
    return events


# --- Scenario 1 - happy path -------------------------------------------------

def test_scenario_image_visible_in_feed_without_expanding(ui_env, monkeypatch, tmp_path):
    '''Given a successful generate_image tool result in the assistant turn,
    when  the chat feed renders,
    then  the picture and the download button appear at expander depth 0,
          after the collapsed result block has closed.'''
    img = tmp_path / 'generated_cat.jpeg'
    img.write_bytes(JPEG_BYTES)
    orch_page = _setup_feed(ui_env, monkeypatch,
                            _assistant_history(_image_result(img)))
    events = _track_depth(ui_env)

    _render_feed(ui_env, orch_page)

    shots = [ev for ev in events if ev[0] == 'image']
    assert shots == [('image', 0)], events
    dls = [ev for ev in events if ev[0] == 'download_button']
    assert dls and all(ev[1] == 0 for ev in dls), events

    image_calls = [c for c in ui_env.calls if c[0] == 'image']
    assert any(c[1] and c[1][0] == str(img) for c in image_calls), image_calls
    dl_calls = [c for c in ui_env.calls if c[0] == 'download_button'
                and c[2].get('file_name') == 'generated_cat.jpeg']
    assert dl_calls, ui_env.calls

    headers = [c[1][0] if c[1] else '' for c in ui_env.calls if c[0] == 'expander']
    assert any('generate_image' in str(h) for h in headers), headers

    captions = [c[1][0] for c in ui_env.calls if c[0] == 'caption']
    assert any('Generated image' in str(c) for c in captions), captions


# --- Scenario 2 - edge case: the saved file vanished --------------------------

def test_scenario_missing_file_keeps_feed_alive(ui_env, monkeypatch, tmp_path):
    '''Given a successful result whose file is gone from disk,
    when  the feed renders,
    then  an actionable caption replaces the picture and no image or
          download controls appear.'''
    missing = tmp_path / 'gone_cat.jpeg'
    orch_page = _setup_feed(ui_env, monkeypatch,
                            _assistant_history(_image_result(missing)))

    _render_feed(ui_env, orch_page)

    assert not [c for c in ui_env.calls if c[0] == 'image']
    assert not [c for c in ui_env.calls if c[0] == 'download_button']
    captions = [c[1][0] for c in ui_env.calls if c[0] == 'caption']
    assert any('not found' in str(c) and 'gone_cat.jpeg' in str(c)
               for c in captions), captions


# --- Scenario 3 - error state: generation failed ------------------------------

def test_scenario_failed_generation_shows_error_no_controls(ui_env, monkeypatch, tmp_path):
    '''Given a failed generate_image result (HTTP 403),
    when  the feed renders,
    then  the failure text is visible in the collapsed result block and no
          image controls appear.'''
    orch_page = _setup_feed(ui_env, monkeypatch,
                            _assistant_history(_image_result(None, ok=False)))

    _render_feed(ui_env, orch_page)

    assert not [c for c in ui_env.calls if c[0] == 'image']
    assert not [c for c in ui_env.calls if c[0] == 'download_button']
    headers = [c[1][0] if c[1] else '' for c in ui_env.calls if c[0] == 'expander']
    assert any('403' in str(h) for h in headers), headers
