"""
tests.test_auth_access - unit tests for the password access gate (core.auth).

Covers:
  - master switch semantics (disabled => never ask, even if a password is set)
  - settings round-trip through ConfigKV with Fernet encryption at rest
  - form-over-env password priority
  - re-ask interval (default 12h, custom value, expiry)
  - orchestrator-active bypass (any active loop suppresses the gate)
  - require_auth behaviour (login form render / no-op paths)
"""
import os
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_ROOT = os.path.dirname(HERE)
if PKG_ROOT not in sys.path:
    sys.path.insert(0, PKG_ROOT)

from tests._st_mock import install_streamlit_mock, StopRerun  # noqa: E402


class _FakeLoopState:
    """Fake orchestrator loop state carrying just a phase."""

    def __init__(self, phase):
        self.phase = phase


@pytest.fixture()
def auth_env(monkeypatch, tmp_path, isolated_app_modules):
    """Point the app at a throwaway data dir, key file and clean env.

    ``core`` modules are re-imported fresh by the shared
    ``isolated_app_modules`` fixture; ``core.auth`` itself must be imported
    INSIDE an active streamlit mock context (it imports streamlit).
    """
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir, exist_ok=True)
    monkeypatch.setenv("SAGAAI_DATA_DIR", data_dir)
    monkeypatch.setenv("SAGAAI_KEY_FILE", str(tmp_path / "keys" / "encryption_key"))
    monkeypatch.delenv("SAGAAI_AUTH_PASSWORD", raising=False)
    monkeypatch.delenv("SAGAAI_ENCRYPTION_KEY", raising=False)
    yield


def _import_auth():
    """Import core.auth bound to the currently installed streamlit mock."""
    import core.auth as auth
    return auth


def test_disabled_by_default(auth_env):
    """Auth is disabled by default: no password configured, gate closed."""
    with install_streamlit_mock() as st:
        auth = _import_auth()
        st.session_state.update({
            auth._AUTHENTICATED_KEY: False,
            auth._AUTH_SUCCESS_TS_KEY: 0,
        })
        assert auth.load_auth_settings() == {
            "enabled": False, "password": "", "reask_hours": 12.0,
        }
        assert auth.is_auth_enabled() is False
        assert auth.is_authenticated() is True


def test_settings_roundtrip_encrypts_password_at_rest(auth_env):
    """save_auth_settings stores settings; the password never appears in
    plaintext inside the raw ConfigKV rows."""
    with install_streamlit_mock() as st:
        auth = _import_auth()
        st.session_state.update({
            auth._AUTHENTICATED_KEY: False,
            auth._AUTH_SUCCESS_TS_KEY: 0,
        })
        assert auth.save_auth_settings(True, password="s3cret", reask_hours=6)
        settings = auth.load_auth_settings()
        assert settings["enabled"] is True
        assert settings["password"] == "s3cret"
        assert settings["reask_hours"] == 6.0

        # Raw DB rows must not contain the plaintext password.
        from storage.repository import repo_load_config
        raw = repo_load_config()
        assert raw.get("auth_password") != "s3cret"
        assert "s3cret" not in str(raw.get("auth_password", ""))


def test_password_keep_on_none(auth_env):
    """password=None keeps the existing stored password."""
    with install_streamlit_mock() as st:
        auth = _import_auth()
        st.session_state.update({
            auth._AUTHENTICATED_KEY: False,
            auth._AUTH_SUCCESS_TS_KEY: 0,
        })
        auth.save_auth_settings(True, password="first", reask_hours=12)
        auth.save_auth_settings(True, password=None, reask_hours=24)
        settings = auth.load_auth_settings()
        assert settings["password"] == "first"
        assert settings["reask_hours"] == 24.0


def test_disable_switch_hides_configured_password(auth_env):
    """When the switch is OFF, no password is requested even if one is set."""
    with install_streamlit_mock() as st:
        auth = _import_auth()
        st.session_state.update({
            auth._AUTHENTICATED_KEY: False,
            auth._AUTH_SUCCESS_TS_KEY: 0,
        })
        auth.save_auth_settings(True, password="s3cret", reask_hours=12)
        auth.save_auth_settings(False, password=None, reask_hours=12)
        assert auth.load_auth_settings()["password"] == "s3cret"
        assert auth.is_auth_enabled() is False
        assert auth.is_authenticated() is True


def test_form_password_wins_over_env(auth_env, monkeypatch):
    """The form value (DB) has priority over SAGAAI_AUTH_PASSWORD."""
    with install_streamlit_mock() as st:
        auth = _import_auth()
        monkeypatch.setenv("SAGAAI_AUTH_PASSWORD", "envpass")
        st.session_state.update({
            auth._AUTHENTICATED_KEY: False,
            auth._AUTH_SUCCESS_TS_KEY: 0,
        })
        auth.save_auth_settings(True, password="formpass", reask_hours=12)
        assert auth.resolve_auth_password() == "formpass"
        import hashlib
        assert auth._get_configured_password_hash() == (
            hashlib.sha256(b"formpass").hexdigest()
        )


def test_env_password_used_when_form_empty(auth_env, monkeypatch):
    """Without a form password, the env variable is the fallback."""
    with install_streamlit_mock() as st:
        auth = _import_auth()
        monkeypatch.setenv("SAGAAI_AUTH_PASSWORD", "envpass")
        st.session_state.update({
            auth._AUTHENTICATED_KEY: False,
            auth._AUTH_SUCCESS_TS_KEY: 0,
        })
        auth.save_auth_settings(True, password="", reask_hours=12)
        assert auth.resolve_auth_password() == "envpass"


def test_enabled_requires_a_password(auth_env):
    """Switch ON without any password does not enable the gate."""
    with install_streamlit_mock() as st:
        auth = _import_auth()
        st.session_state.update({
            auth._AUTHENTICATED_KEY: False,
            auth._AUTH_SUCCESS_TS_KEY: 0,
        })
        auth.save_auth_settings(True, password="", reask_hours=12)
        assert auth.is_auth_enabled() is False


def test_reask_interval_expiry(auth_env):
    """After a successful login the session is valid for reask_hours; once
    expired is_authenticated flips back to False."""
    with install_streamlit_mock() as st:
        auth = _import_auth()
        st.session_state.update({
            auth._AUTHENTICATED_KEY: True,
            auth._AUTH_SUCCESS_TS_KEY: 0,
        })
        auth.save_auth_settings(True, password="pass", reask_hours=12)

        # No success timestamp: not authenticated (the flag is reset).
        assert auth.is_authenticated() is False

        st.session_state[auth._AUTH_SUCCESS_TS_KEY] = time.time()
        st.session_state[auth._AUTHENTICATED_KEY] = True
        assert auth.is_authenticated() is True

        # Simulate expiry: success 13 hours ago (default 12h interval).
        st.session_state[auth._AUTH_SUCCESS_TS_KEY] = time.time() - 13 * 3600
        assert auth.is_authenticated() is False
        # The authenticated flag is reset after expiry.
        assert st.session_state[auth._AUTHENTICATED_KEY] is False


def test_custom_reask_interval(auth_env):
    """A custom interval (2h) keeps older logins valid only for 2 hours."""
    with install_streamlit_mock() as st:
        auth = _import_auth()
        st.session_state.update({
            auth._AUTHENTICATED_KEY: True,
            auth._AUTH_SUCCESS_TS_KEY: time.time() - 3 * 3600,
        })
        auth.save_auth_settings(True, password="pass", reask_hours=2)
        assert auth.is_authenticated() is False

        st.session_state[auth._AUTH_SUCCESS_TS_KEY] = time.time() - 1 * 3600
        st.session_state[auth._AUTHENTICATED_KEY] = True
        assert auth.is_authenticated() is True


def test_invalid_reask_hours_fall_back_to_default(auth_env):
    """Garbage/negative stored values fall back to the 12h default."""
    with install_streamlit_mock() as st:
        auth = _import_auth()
        st.session_state.update({
            auth._AUTHENTICATED_KEY: False,
            auth._AUTH_SUCCESS_TS_KEY: 0,
        })
        auth.save_auth_settings(True, password="pass", reask_hours=-5)
        assert auth.load_auth_settings()["reask_hours"] == 12.0

        from storage.repository import repo_save_config, repo_load_config
        cfg = repo_load_config()
        cfg["auth_reask_hours"] = "not-a-number"
        repo_save_config(cfg)
        assert auth.load_auth_settings()["reask_hours"] == 12.0


def test_any_orchestrator_active(auth_env):
    """any_orchestrator_active detects non-terminal loop states."""
    with install_streamlit_mock() as st:
        auth = _import_auth()
        st.session_state.update({
            "unrelated": 1,
            "orch_dev_agent_loop_state": _FakeLoopState("done"),
        })
        assert auth.any_orchestrator_active() is False

        st.session_state["orch_dev_agent_loop_state"] = _FakeLoopState("running")
        assert auth.any_orchestrator_active() is True

        st.session_state["orch_dev_agent_loop_state"] = None
        assert auth.any_orchestrator_active() is False

        st.session_state["orch_custom_slug_loop_state"] = _FakeLoopState("error")
        assert auth.any_orchestrator_active() is False


def test_active_orchestrator_suppresses_auth(auth_env):
    """While any orchestrator runs a task, is_authenticated is True even if
    the re-ask interval is long expired."""
    with install_streamlit_mock() as st:
        auth = _import_auth()
        st.session_state.update({
            auth._AUTHENTICATED_KEY: False,
            auth._AUTH_SUCCESS_TS_KEY: time.time() - 99 * 3600,
            "orch_dev_agent_loop_state": _FakeLoopState("running"),
        })
        auth.save_auth_settings(True, password="pass", reask_hours=12)
        assert auth.is_auth_enabled() is True
        assert auth.is_authenticated() is True


def test_require_auth_noop_when_disabled(auth_env):
    """require_auth is a no-op when the switch is OFF."""
    with install_streamlit_mock() as st:
        auth = _import_auth()
        st.session_state.update({
            auth._AUTHENTICATED_KEY: False,
            auth._AUTH_SUCCESS_TS_KEY: 0,
        })
        auth.save_auth_settings(False, password="pass", reask_hours=12)
        auth.require_auth()  # must not raise / stop
        assert st.rerun_count == 0
        assert not any(c[0] == "form" for c in st.calls)


def test_require_auth_noop_when_orchestrator_active(auth_env):
    """require_auth does not interrupt while an orchestrator is running."""
    with install_streamlit_mock() as st:
        auth = _import_auth()
        st.session_state.update({
            auth._AUTHENTICATED_KEY: False,
            auth._AUTH_SUCCESS_TS_KEY: 0,
            "orch_dev_agent_loop_state": _FakeLoopState("running"),
        })
        auth.save_auth_settings(True, password="pass", reask_hours=12)
        auth.require_auth()  # must not raise / stop
        assert st.rerun_count == 0
        assert not any(c[0] == "form" for c in st.calls)


def test_require_auth_renders_login_form_when_unauthenticated(auth_env):
    """require_auth renders the login form and stops the page."""
    with install_streamlit_mock() as st:
        auth = _import_auth()
        st.session_state.update({
            auth._AUTHENTICATED_KEY: False,
            auth._AUTH_SUCCESS_TS_KEY: 0,
            auth._AUTH_FAILED_KEY: False,
        })
        auth.save_auth_settings(True, password="pass", reask_hours=12)
        with pytest.raises(StopRerun):
            auth.require_auth()
        assert any(c[0] == "text_input" for c in st.calls)
        assert any(c[0] == "form_submit_button" for c in st.calls)


def test_login_form_success_sets_timestamp_and_flag(auth_env):
    """Submitting the correct password through the real form marks the
    session authenticated and records the success timestamp."""
    with install_streamlit_mock() as st:
        auth = _import_auth()
        st.session_state.update({
            "ui_lang": "English",
            auth._AUTHENTICATED_KEY: False,
            auth._AUTH_SUCCESS_TS_KEY: 0,
            auth._AUTH_FAILED_KEY: False,
        })
        auth.save_auth_settings(True, password="pass", reask_hours=12)

        st._text_returns["_sagaai_auth_input"] = "pass"
        st.click(auth.t("auth_sign_in", lang="English"))
        with pytest.raises(StopRerun):
            auth._render_login_form()

        assert st.session_state[auth._AUTHENTICATED_KEY] is True
        assert st.session_state[auth._AUTH_SUCCESS_TS_KEY] > 0
        assert auth.is_authenticated() is True


def test_login_form_wrong_password_shows_error(auth_env):
    """A wrong password leaves the session unauthenticated and shows the
    incorrect-password error."""
    with install_streamlit_mock() as st:
        auth = _import_auth()
        st.session_state.update({
            "ui_lang": "English",
            auth._AUTHENTICATED_KEY: False,
            auth._AUTH_SUCCESS_TS_KEY: 0,
            auth._AUTH_FAILED_KEY: False,
        })
        auth.save_auth_settings(True, password="pass", reask_hours=12)

        st._text_returns["_sagaai_auth_input"] = "wrong"
        st.click(auth.t("auth_sign_in", lang="English"))
        auth._render_login_form()

        assert st.session_state[auth._AUTHENTICATED_KEY] is not True
        assert st.session_state[auth._AUTH_FAILED_KEY] is True
        assert st.errors, "incorrect-password error was not rendered"
