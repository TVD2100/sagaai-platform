# -*- coding: utf-8 -*-
"""tests/test_updater_ui.py - тесты страницы «Обновления» (ui.pages.updates).

Проверяют рендер страницы и её действия (check / stage / apply / rollback)
под моком Streamlit. Сетевые и файловые операции ядра заменяются mock'ами
в пространстве имён ui.pages.updates, поэтому тесты не касаются ни сети,
ни реального хранилища .dev_agent/updates/.

Порядок важен: страница импортируется ПОД моком Streamlit, а патчи на
атрибуты модуля накладываются ПОСЛЕ импорта (переимпорт чистит старые
патчи). Для наложения патчей используется ExitStack.
"""
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._st_mock import install_streamlit_mock, StopRerun  # noqa: E402


MANIFEST = {
    "app_version": "0.1.0-preview.9",
    "channel": "https://example.test/channel",
    "units": {
        "core": {
            "version": "1.1.0",
            "files": {"core/x.py": {"version": "1.1.0", "sha256": "a" * 64}},
        }
    },
    "selectable": {"content/prompt.md": {"version": "1.0.1", "sha256": "b" * 64}},
}

REPORT_AVAILABLE = {
    "ok": True,
    "error": None,
    "app_version": {
        "local": "0.1.0-preview.2",
        "remote": "0.1.0-preview.9",
        "newer_remote": True,
    },
    "available": [
        {
            "path": "content/prompt.md",
            "action": "update",
            "version": "1.0.1",
            "local_version": None,
            "sha256": "b" * 64,
        },
        {
            "path": "core/x.py",
            "action": "update",
            "version": "1.1.0",
            "local_version": None,
            "sha256": "a" * 64,
        },
    ],
    "up_to_date": False,
    "source": "https://example.test/channel",
}

REPORT_UP_TO_DATE = {
    "ok": True,
    "error": None,
    "app_version": {
        "local": "0.1.0-preview.2",
        "remote": "0.1.0-preview.2",
        "newer_remote": False,
    },
    "available": [],
    "up_to_date": True,
    "source": "https://example.test/channel",
}


# Патчи, общие для всех тестов: перекрывают доступ к реальной сети/диску
# в пространстве имён ui.pages.updates.
COMMON_PATCHES = [
    patch("ui.pages.updates.load_health", return_value=None),
    patch("ui.pages.updates.read_pending", return_value=(None, [])),
    patch("ui.pages.updates.load_state", return_value={"entries": {}, "last_run": {}}),
    patch("ui.pages.updates.list_backup_runs", return_value=[]),
    patch("ui.pages.updates.is_app_running", return_value=True),
]


def _fresh_page():
    """Импортировать страницу под установленным моком Streamlit.

    ui.* вычищаются из sys.modules, чтобы модуль переисполнился и взял
    текущий мок; вызывать ДО наложения патчей на его атрибуты.
    """
    for name in list(sys.modules):
        if name == "ui" or name.startswith("ui."):
            sys.modules.pop(name, None)
    import ui.pages.updates as mod
    return mod


def _enter_patches(root, extra_patches):
    """Наложить все патчи через ExitStack; вернуть (stack, entered_mocks)."""
    stack = ExitStack()
    entered = []
    try:
        stack.enter_context(patch("ui.pages.updates.default_root", return_value=root))
        for base_patch in COMMON_PATCHES:
            stack.enter_context(base_patch)
        for extra_patch in extra_patches:
            entered.append(stack.enter_context(extra_patch))
    except Exception:
        stack.close()
        raise
    return stack, entered


def _render(st, page, **session):
    """Вызвать page_updates() с переданным session_state."""
    st.session_state.update(dict(ui_lang="English", **session))
    try:
        page.page_updates()
    except StopRerun:
        pass


def _button_keys(st):
    """Ключи всех отрендеренных кнопок."""
    return [c[2].get("key") for c in st.calls if c[0] == "button"]


def _button_kwargs(st, key):
    """(args, kwargs) последней отрендеренной кнопки с данным key."""
    for name, args, kwargs in reversed(st.calls):
        if name == "button" and kwargs.get("key") == key:
            return (args, kwargs)
    return None


def test_page_renders_without_report(tmp_path):
    """Без отчёта проверки отображается только кнопка Check."""
    with install_streamlit_mock() as st:
        page = _fresh_page()
        with _enter_patches(str(tmp_path), [])[0]:
            _render(st, page)

    assert "updates_check" in _button_keys(st)
    assert "updates_stage" not in _button_keys(st)
    assert not st.errors
    assert not st.warnings


def test_check_button_fetches_manifest_and_reports_up_to_date(tmp_path):
    """Клик Check вызывает fetch_manifest + check_updates и кладёт отчёт в state."""
    with install_streamlit_mock() as st:
        st.click("updates_check")
        page = _fresh_page()
        stack, mocks = _enter_patches(
            str(tmp_path),
            [
                patch("ui.pages.updates.fetch_manifest", return_value=(MANIFEST, None)),
                patch("ui.pages.updates.check_updates", return_value=REPORT_UP_TO_DATE),
            ],
        )
        with stack:
            _render(st, page)
    fetch_mock, check_mock = mocks

    fetch_mock.assert_called_once()
    check_mock.assert_called_once()
    assert st.session_state["updates_manifest"] == MANIFEST
    assert st.session_state["updates_report"] == REPORT_UP_TO_DATE
    assert "updates_stage" not in _button_keys(st)


def test_check_error_shows_error_message(tmp_path):
    """Ошибка проверки показывается через st.error."""
    with install_streamlit_mock() as st:
        st.click("updates_check")
        page = _fresh_page()
        stack, _mocks = _enter_patches(
            str(tmp_path),
            [patch("ui.pages.updates.fetch_manifest", return_value=(None, "network down"))],
        )
        with stack:
            _render(st, page)

    assert any("network down" in msg for msg in st.errors)
    assert st.session_state["updates_manifest"] is None
    assert st.session_state["updates_report"] == {"ok": False, "error": "network down"}


def test_stage_sends_selected_paths(tmp_path):
    """Клик Download передаёт в stage_updates отмеченные файлы и выбранный канал."""
    with install_streamlit_mock() as st:
        st.click("updates_stage")
        page = _fresh_page()
        stack, mocks = _enter_patches(
            str(tmp_path),
            [
                patch(
                    "ui.pages.updates.stage_updates",
                    return_value={
                        "ok": True,
                        "staged": ["content/prompt.md"],
                        "pending_path": "p",
                    },
                ),
            ],
        )
        with stack:
            _render(
                st,
                page,
                updates_manifest=MANIFEST,
                updates_report=REPORT_AVAILABLE,
                updates_channel="https://example.test/channel",
                **{"upd_sel_content/prompt.md": True, "upd_unit_core": False},
            )
    stage_mock = mocks[0]

    stage_mock.assert_called_once()
    kwargs = stage_mock.call_args.kwargs
    assert kwargs["selection"] == ["content/prompt.md"]
    assert kwargs["manifest"] == MANIFEST
    assert kwargs["channel"] == "https://example.test/channel"


def test_stage_unit_checkbox_selects_all_unit_files(tmp_path):
    """Отмеченный пакет передаёт в stage все его файлы."""
    with install_streamlit_mock() as st:
        st.click("updates_stage")
        page = _fresh_page()
        stack, mocks = _enter_patches(
            str(tmp_path),
            [
                patch(
                    "ui.pages.updates.stage_updates",
                    return_value={
                        "ok": True,
                        "staged": ["core/x.py"],
                        "pending_path": "p",
                    },
                ),
            ],
        )
        with stack:
            _render(
                st,
                page,
                updates_manifest=MANIFEST,
                updates_report=REPORT_AVAILABLE,
                updates_channel="https://example.test/channel",
                **{"upd_sel_content/prompt.md": False, "upd_unit_core": True},
            )
    stage_mock = mocks[0]

    stage_mock.assert_called_once()
    assert stage_mock.call_args.kwargs["selection"] == ["core/x.py"]


def test_stage_without_selection_warns_and_does_not_call_stage(tmp_path):
    """Без выбранных файлов Download показывает предупреждение и ничего не качает."""
    with install_streamlit_mock() as st:
        st.click("updates_stage")
        page = _fresh_page()
        stack, mocks = _enter_patches(
            str(tmp_path),
            [patch("ui.pages.updates.stage_updates")],
        )
        with stack:
            _render(
                st,
                page,
                updates_manifest=MANIFEST,
                updates_report=REPORT_AVAILABLE,
                **{"upd_sel_content/prompt.md": False, "upd_unit_core": False},
            )
    stage_mock = mocks[0]

    stage_mock.assert_not_called()
    assert st.warnings


def test_apply_requires_confirmation_while_app_is_running(tmp_path):
    """При живом приложении клик Apply без подтверждения не вызывает apply_updates."""
    pending = (
        {"files": {"content/prompt.md": {"version": "1.0.1", "sha256": "b" * 64}}},
        [],
    )
    with install_streamlit_mock() as st:
        st.click("updates_apply")
        page = _fresh_page()
        stack, mocks = _enter_patches(
            str(tmp_path),
            [
                patch("ui.pages.updates.read_pending", return_value=pending),
                patch("ui.pages.updates.apply_updates"),
            ],
        )
        with stack:
            _render(st, page)
    apply_mock = mocks[1]

    apply_mock.assert_not_called()
    hit = _button_kwargs(st, "updates_apply")
    assert hit is not None
    assert hit[1].get("disabled") is not True
    assert st.warnings


def test_apply_calls_applier_with_confirmation_while_running(tmp_path):
    """При живом приложении подтверждённый Apply вызывает apply_updates(force=True)."""
    pending = (
        {"files": {"content/prompt.md": {"version": "1.0.1", "sha256": "b" * 64}}},
        [],
    )
    with install_streamlit_mock() as st:
        st.click("updates_apply")
        page = _fresh_page()
        stack, mocks = _enter_patches(
            str(tmp_path),
            [
                patch("ui.pages.updates.read_pending", return_value=pending),
                patch(
                    "ui.pages.updates.apply_updates",
                    return_value={
                        "ok": True,
                        "applied": ["content/prompt.md"],
                        "failed": False,
                        "error": None,
                        "logs": [],
                        "health_path": "h",
                    },
                ),
            ],
        )
        with stack:
            _render(st, page, updates_apply_confirm=True)
    apply_mock = mocks[1]

    apply_mock.assert_called_once_with(str(tmp_path), force=True)


def test_apply_calls_applier_when_app_is_stopped(tmp_path):
    """При остановленном приложении клик Apply вызывает apply_updates."""
    pending = (
        {"files": {"content/prompt.md": {"version": "1.0.1", "sha256": "b" * 64}}},
        [],
    )
    with install_streamlit_mock() as st:
        st.click("updates_apply")
        page = _fresh_page()
        stack, mocks = _enter_patches(
            str(tmp_path),
            [
                patch("ui.pages.updates.read_pending", return_value=pending),
                patch("ui.pages.updates.is_app_running", return_value=False),
                patch(
                    "ui.pages.updates.apply_updates",
                    return_value={
                        "ok": True,
                        "applied": ["content/prompt.md"],
                        "failed": False,
                        "error": None,
                        "logs": [],
                        "health_path": "h",
                    },
                ),
            ],
        )
        with stack:
            _render(st, page)
    apply_mock = mocks[2]

    apply_mock.assert_called_once_with(str(tmp_path), force=False)



def test_apply_without_confirmation_warns(tmp_path):
    """При живом приложении Apply без чекбокса показывает st.warning."""
    pending = (
        {"files": {"content/prompt.md": {"version": "1.0.1", "sha256": "b" * 64}}},
        [],
    )
    with install_streamlit_mock() as st:
        st.click("updates_apply")
        page = _fresh_page()
        stack, mocks = _enter_patches(
            str(tmp_path),
            [
                patch("ui.pages.updates.read_pending", return_value=pending),
                patch("ui.pages.updates.apply_updates"),
            ],
        )
        with stack:
            _render(st, page)
    apply_mock = mocks[1]

    apply_mock.assert_not_called()
    assert st.warnings


def test_rollback_calls_rollback_updates(tmp_path):
    """Клик Rollback вызывает rollback_updates при остановленном приложении."""
    with install_streamlit_mock() as st:
        st.click("updates_rollback")
        page = _fresh_page()
        stack, mocks = _enter_patches(
            str(tmp_path),
            [
                patch(
                    "ui.pages.updates.load_state",
                    return_value={"entries": {"a": {}}, "last_run": {"ok": True, "at": "t"}},
                ),
                patch("ui.pages.updates.list_backup_runs", return_value=["20260101T000000Z"]),
                patch("ui.pages.updates.is_app_running", return_value=False),
                patch(
                    "ui.pages.updates.rollback_updates",
                    return_value={"ok": True, "restored": ["a"], "error": None},
                ),
            ],
        )
        with stack:
            _render(st, page)
    rollback_mock = mocks[3]

    rollback_mock.assert_called_once_with(str(tmp_path), force=False)


def test_rollback_requires_confirmation_while_app_is_running(tmp_path):
    """При живом приложении клик Rollback без подтверждения не вызывает rollback_updates."""
    with install_streamlit_mock() as st:
        st.click("updates_rollback")
        page = _fresh_page()
        stack, mocks = _enter_patches(
            str(tmp_path),
            [
                patch("ui.pages.updates.list_backup_runs", return_value=["20260101T000000Z"]),
                patch("ui.pages.updates.rollback_updates"),
            ],
        )
        with stack:
            _render(st, page)
    rollback_mock = mocks[1]

    rollback_mock.assert_not_called()
    hit = _button_kwargs(st, "updates_rollback")
    assert hit is not None
    assert hit[1].get("disabled") is not True
    assert st.warnings


def test_health_error_is_shown(tmp_path):
    """Неудачное применение при старте показывается через st.warning."""
    with install_streamlit_mock() as st:
        page = _fresh_page()
        stack, _mocks = _enter_patches(
            str(tmp_path),
            [patch(
                "ui.pages.updates.load_health",
                return_value={"ok": False, "error": "boom"},
            )],
        )
        with stack:
            _render(st, page)

    assert any("boom" in msg for msg in st.warnings)
