"""Regression tests for tests/_st_mock.py."""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _st_mock import StreamlitMock, install_streamlit_mock


def test_sidebar_children_are_logged():
    mock = StreamlitMock()
    st = types.SimpleNamespace(sidebar=mock.sidebar)
    st.sidebar().title("Explicit Sidebar")
    names = {c[0] for c in mock.calls}
    assert "sidebar.title" in names


def test_sidebar_with_context_logs_widgets_as_top_level():
    with install_streamlit_mock() as m:
        import streamlit as st
        with st.sidebar:
            st.title("Context Title")
    names = {c[0] for c in m.calls}
    assert "title" in names


def test_columns_children_are_logged_with_index_path():
    with install_streamlit_mock() as m:
        import streamlit as st
        cols = st.columns(2)
        cols[0].markdown("Hello")
        cols[1].button("Click")
    names = {c[0] for c in m.calls}
    assert "columns[0].markdown" in names
    assert "columns[1].button" in names


def test_tabs_children_are_logged_with_index_path():
    with install_streamlit_mock() as m:
        import streamlit as st
        tabs = st.tabs(["A", "B"])
        tabs[1].write("tab two")
    names = {c[0] for c in m.calls}
    assert "tabs[1].write" in names


def test_deep_sidebar_columns_chain_is_logged():
    with install_streamlit_mock() as m:
        import streamlit as st
        st.sidebar.columns(1)[0].markdown("nested")
    names = {c[0] for c in m.calls}
    assert "sidebar.columns" in names
    assert "sidebar.columns[0].markdown" in names


def test_empty_container_logs_children():
    with install_streamlit_mock() as m:
        import streamlit as st
        e = st.empty()
        e.markdown("swap")
    names = {c[0] for c in m.calls}
    assert "empty.markdown" in names


def test_context_manager_widgets_still_work():
    with install_streamlit_mock() as m:
        import streamlit as st
        with st.expander("More"):
            st.text("inside")
        with st.container():
            st.caption("cap")
        with st.form("f"):
            st.form_submit_button("Submit")
        with st.spinner("Loading"):
            st.write("spin")
        with st.chat_message("user"):
            st.write("hi")
    names = {c[0] for c in m.calls}
    for expected in ("expander", "text", "container", "caption",
                     "form", "form_submit_button", "spinner", "write",
                     "chat_message"):
        assert expected in names
