"""
tests.scenarios.test_access_scenarios - end-to-end scenarios for the
"Password access" (Доступы) feature.

Scenarios (given -> when -> then):
  1. Happy path: enable access, set password via the form, login, stay
     authenticated for the interval, then get re-asked after it expires.
  2. Orchestrator busy: while any orchestrator runs a task, no password is
     requested even after the interval expires.
  3. Master switch OFF: a configured password is never requested.
  4. Form-over-env priority: both configured, the form password wins.
  5. Error state: password/confirmation mismatch does not change settings.
"""
import os
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_ROOT = os.path.dirname(os.path.dirname(HERE))
if PKG_ROOT not in sys.path:
    sys.path.insert(0, PKG_ROOT)

from tests._st_mock import install_streamlit_mock, StopRerun  # noqa: E402


class _FakeLoopState:
    """Fake orchestrator loop state carrying just a phase."""

    def __init__(self, phase):
        self.phase = phase


@pytest.fixture()
def access_env(monkeypatch, tmp_path, isolated_app_modules):
    """Isolated DATA_DIR + encryption key; auth imported inside a mock."""
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir, exist_ok=True)
    monkeypatch.setenv("SAGAAI_DATA_DIR", data_dir)
    monkeypatch.setenv("SAGAAI_KEY_FILE", str(tmp_path / "keys" / "encryption_key"))
    monkeypatch.delenv("SAGAAI_AUTH_PASSWORD", raising=False)
    monkeypatch.delenv("SAGAAI_ENCRYPTION_KEY", raising=False)
    yield


def _import_auth():
    import core.auth as auth
    return auth


def test_scenario_happy_path_enable_login_reask(access_env):
    """Given access is enabled with a password and 12h interval,
    when the user logs in successfully,
    then the session stays authenticated until the interval expires,
    after which the password is requested again."""
    with install_streamlit_mock() as st:
        auth = _import_auth()
        st.session_state.update({
            "ui_lang": "English",
            auth._AUTHENTICATED_KEY: False,
            auth._AUTH_SUCCESS_TS_KEY: 0,
            auth._AUTH_FAILED_KEY: False,
        })
        assert auth.save_auth_settings(True, password="secret12", reask_hours=12)

        assert auth.is_auth_enabled() is True
        assert auth.is_authenticated() is False
        with pytest.raises(StopRerun):
            auth.require_auth()

        st._text_returns["_sagaai_auth_input"] = "secret12"
        st.click(auth.t("auth_sign_in", lang="English"))
        with pytest.raises(StopRerun):
            auth._render_login_form()
        assert st.session_state[auth._AUTHENTICATED_KEY] is True

        assert auth.is_authenticated() is True
        auth.require_auth()  # no StopRerun expected

        st.session_state[auth._AUTH_SUCCESS_TS_KEY] = time.time() - 13 * 3600
        assert auth.is_authenticated() is False
        with pytest.raises(StopRerun):
            auth.require_auth()


def test_scenario_orchestrator_busy_no_auth_prompt(access_env):
    """Given access is enabled and the interval has long expired,
    when an orchestrator is running a task,
    then no password is requested."""
    with install_streamlit_mock() as st:
        auth = _import_auth()
        st.session_state.update({
            auth._AUTHENTICATED_KEY: False,
            auth._AUTH_SUCCESS_TS_KEY: time.time() - 99 * 3600,
            "orch_dev_agent_loop_state": _FakeLoopState("running"),
        })
        auth.save_auth_settings(True, password="secret12", reask_hours=12)

        assert auth.is_authenticated() is True
        auth.require_auth()
        form_calls = [c for c in st.calls if c[0] == "form"]
        assert not form_calls

        st.session_state["orch_dev_agent_loop_state"] = _FakeLoopState("done")
        with pytest.raises(StopRerun):
            auth.require_auth()


def test_scenario_switch_off_never_asks(access_env):
    """Given a password is configured but the switch is OFF,
    when the page is opened,
    then the password is never requested."""
    with install_streamlit_mock() as st:
        auth = _import_auth()
        st.session_state.update({
            auth._AUTHENTICATED_KEY: False,
            auth._AUTH_SUCCESS_TS_KEY: 0,
        })
        auth.save_auth_settings(True, password="secret12", reask_hours=12)
        auth.save_auth_settings(False, password=None, reask_hours=12)

        assert auth.is_auth_enabled() is False
        assert auth.is_authenticated() is True
        auth.require_auth()
        assert not any(c[0] == "form" for c in st.calls)


def test_scenario_form_password_beats_env(access_env, monkeypatch):
    """Given both the form and the env define a password,
    when the user logs in,
    then only the form password is accepted."""
    with install_streamlit_mock() as st:
        auth = _import_auth()
        monkeypatch.setenv("SAGAAI_AUTH_PASSWORD", "envpass")
        st.session_state.update({
            "ui_lang": "English",
            auth._AUTHENTICATED_KEY: False,
            auth._AUTH_SUCCESS_TS_KEY: 0,
            auth._AUTH_FAILED_KEY: False,
        })
        auth.save_auth_settings(True, password="formpass", reask_hours=12)

        assert auth.resolve_auth_password() == "formpass"

        st._text_returns["_sagaai_auth_input"] = "envpass"
        st.click(auth.t("auth_sign_in", lang="English"))
        auth._render_login_form()
        assert st.session_state[auth._AUTHENTICATED_KEY] is not True
        assert st.session_state[auth._AUTH_FAILED_KEY] is True

        st.errors.clear()
        st.session_state[auth._AUTH_FAILED_KEY] = False
        st._text_returns["_sagaai_auth_input"] = "formpass"
        st.click(auth.t("auth_sign_in", lang="English"))
        with pytest.raises(StopRerun):
            auth._render_login_form()
        assert st.session_state[auth._AUTHENTICATED_KEY] is True


def test_scenario_password_mismatch_keeps_settings(access_env):
    """Given access is enabled,
    when the user submits mismatching password fields,
    then nothing is saved and an error is shown."""
    with install_streamlit_mock() as st:
        auth = _import_auth()
        st.session_state.update({
            "ui_lang": "English",
            auth._AUTHENTICATED_KEY: False,
            auth._AUTH_SUCCESS_TS_KEY: 0,
        })
        auth.save_auth_settings(True, password="oldpass", reask_hours=12)

        from ui.pages.access import page_access
        st.session_state["access_enabled"] = True
        st._text_returns["access_password_input"] = "newpass1"
        st._text_returns["access_password_confirm_input"] = "newpass2"
        st._number_returns["access_reask_hours_input"] = 7
        st.click(auth.t("access_save_btn", lang="English"))
        page_access()

        assert st.errors, "mismatch error was not shown"
        settings = auth.load_auth_settings()
        assert settings["password"] == "oldpass"
        assert settings["reask_hours"] == 12.0
