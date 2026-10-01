# -*- coding: utf-8 -*-
'''tests/test_orchestrator_image_result.py - generated image in tool results.

A successful ``generate_image`` tool_result renders the saved image
(st.image) plus a download button in the chat feed ITSELF - at expander
depth 0, right after the collapsed tool-result block - so the picture is
visible without an extra click. A missing file renders an actionable
caption; failed results and other tools render nothing image-specific.
'''
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._st_mock import install_streamlit_mock  # noqa: E402

JPEG_BYTES = b'\xff\xd8\xff\xe0' + b'\x00' * 16


@pytest.fixture()
def ui_env(monkeypatch, tmp_path):
    '''Isolated DATA_DIR + fresh ui.* modules under the streamlit mock.'''
    data_dir = tmp_path / 'data'
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv('SAGAAI_DATA_DIR', str(data_dir))
    for m in list(sys.modules):
        if m == 'ui' or m.startswith('ui.'):
            sys.modules.pop(m, None)
    with install_streamlit_mock() as st:
        yield st


def _write_image(path: Path) -> Path:
    '''Create a tiny JPEG-like file and return its path.'''
    path.write_bytes(JPEG_BYTES)
    return path


def _event(path, ok=True, tool='generate_image', mime='image/jpeg'):
    '''Build a tool_result event shaped like the agent loop emits it.'''
    result = {'ok': ok, 'path': str(path), 'mime': mime,
              'service': 'YandexAI', 'model': 'aliceai-image-art-3.0'}
    return {'type': 'tool_result', 'tool': tool, 'result': result}


def _track_expander_depth(st) -> dict:
    '''Wrap expander/image/download_button to record nesting depth.

    Returns ``image_depths``/``download_depths`` (the expander depth at
each call; 0 == the chat feed itself) and ``seq`` - an ordered event log
    with entries ('expander' | 'expander_exit' | 'image' |
    'download_button', label, depth).
    '''
    depth = {'cur': 0}
    seq = []
    real_expander = st.expander

    def expander(*args, **kwargs):
        header = args[0] if args else ''
        seq.append(('expander', header, depth['cur']))
        ctx = real_expander(*args, **kwargs)

        class _Ctx:
            def __enter__(self_inner):
                depth['cur'] += 1
                return ctx.__enter__()

            def __exit__(self_inner, *exc):
                depth['cur'] -= 1
                seq.append(('expander_exit', header, depth['cur']))
                return ctx.__exit__(*exc)

        return _Ctx()

    st.expander = expander

    orig_image = st.image
    image_depths = []

    def image(*args, **kwargs):
        label = args[0] if args else ''
        image_depths.append(depth['cur'])
        seq.append(('image', label, depth['cur']))
        return orig_image(*args, **kwargs)

    st.image = image

    orig_dl = st.download_button
    download_depths = []

    def download_button(*args, **kwargs):
        label = kwargs.get('file_name', '') or (args[0] if args else '')
        download_depths.append(depth['cur'])
        seq.append(('download_button', label, depth['cur']))
        return orig_dl(*args, **kwargs)

    st.download_button = download_button
    return {'image_depths': image_depths,
            'download_depths': download_depths, 'seq': seq}


def test_successful_result_shows_image_and_download(ui_env, tmp_path):
    '''The image is rendered with st.image and offered for download.'''
    from ui.pages import orchestrator as orch_page

    img = _write_image(tmp_path / 'generated_image_1.jpeg')
    orch_page._render_tool_result(_event(img), 'English')

    image_calls = [c for c in ui_env.calls if c[0] == 'image']
    assert len(image_calls) == 1, ui_env.calls
    assert image_calls[0][1][0] == str(img)

    dl_calls = [c for c in ui_env.calls if c[0] == 'download_button']
    assert len(dl_calls) == 1, ui_env.calls
    kwargs = dl_calls[0][2]
    assert kwargs['data'] == JPEG_BYTES
    assert kwargs['file_name'] == 'generated_image_1.jpeg'
    assert kwargs['mime'] == 'image/jpeg'
    assert 'generated_image_1.jpeg' in kwargs['key']

    captions = [c[1][0] for c in ui_env.calls if c[0] == 'caption']
    assert any('Generated image' in c for c in captions), captions


def test_image_and_download_render_outside_collapsed_expander(ui_env, tmp_path):
    '''The picture belongs to the feed, not to the collapsed result block:
    st.image and the download button render at expander depth 0, after the
    tool-result expander has closed.'''
    from ui.pages import orchestrator as orch_page

    tracker = _track_expander_depth(ui_env)
    img = _write_image(tmp_path / 'generated_image_2.jpeg')
    orch_page._render_tool_result(_event(img), 'English')

    assert tracker['image_depths'] == [0], tracker['seq']
    assert tracker['download_depths'] == [0], tracker['seq']
    kinds = [ev[0] for ev in tracker['seq']]
    assert 'expander_exit' in kinds, tracker['seq']
    assert kinds.index('expander_exit') < kinds.index('image'), tracker['seq']
    assert kinds.index('expander_exit') < kinds.index('download_button'), tracker['seq']


def test_missing_file_renders_caption_only(ui_env, tmp_path):
    '''A vanished image file yields a caption, not a broken render.'''
    from ui.pages import orchestrator as orch_page

    missing = tmp_path / 'gone.jpeg'
    orch_page._render_tool_result(_event(missing), 'English')

    assert not [c for c in ui_env.calls if c[0] == 'image']
    assert not [c for c in ui_env.calls if c[0] == 'download_button']
    captions = [c[1][0] for c in ui_env.calls if c[0] == 'caption']
    assert any('not found' in c and 'gone.jpeg' in c for c in captions), captions


def test_failed_result_and_other_tools_render_no_image(ui_env, tmp_path):
    '''Only successful generate_image results get the image block.'''
    from ui.pages import orchestrator as orch_page

    img = _write_image(tmp_path / 'pic.jpeg')
    orch_page._render_tool_result(_event(img, ok=False), 'English')
    orch_page._render_tool_result(
        {'type': 'tool_result', 'tool': 'propose_file',
         'result': {'ok': True, 'path': str(img)}}, 'English')

    assert not [c for c in ui_env.calls if c[0] == 'image']
    assert not [c for c in ui_env.calls if c[0] == 'download_button']
