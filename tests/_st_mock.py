"""
A lightweight Streamlit mock so we can execute ui/app.py and page render
functions headlessly (Streamlit itself is not installed in this environment).

It records widget calls, supports a controllable session_state, and lets a test
simulate a button click by pre-seeding which button keys/labels should return
True on the next render pass. A StopRerun exception models st.rerun().
"""
from __future__ import annotations
import sys
import types
from contextlib import contextmanager


class StopRerun(Exception):
    """Raised by mock st.rerun() / st.stop() to unwind the current render."""


class _SessionState(dict):
    # allow attribute access like real st.session_state
    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError as e:
            raise AttributeError(k) from e

    def __setattr__(self, k, v):
        self[k] = v


class StreamlitMock(types.ModuleType):
    def __init__(self):
        super().__init__("streamlit")
        self.session_state = _SessionState()
        self.calls = []                 # list of (fn_name, args, kwargs)
        self._buttons_true = set()      # keys/labels that should return True once
        self._selectbox_returns = {}    # key -> value to return
        self._text_returns = {}         # key -> value
        self._date_returns = {}         # key -> date value to return
        self.rerun_count = 0
        self.errors = []                # st.error messages
        self.warnings = []              # st.warning messages
        self.query_params = {}          # dict-like st.query_params
        self.bottom = _NullCtx(self, "bottom")  # st.bottom is a container, not callable

    # ---- helpers for tests ------------------------------------------------
    def _rec(self, name, args, kwargs):
        self.calls.append((name, args, kwargs))

    def click(self, *keys_or_labels):
        self._buttons_true = set(keys_or_labels)

    def reset_clicks(self):
        self._buttons_true = set()

    # ---- generic catch-all widget factory ---------------------------------
    def __getattr__(self, name):
        # Only called for attributes not set in __init__.
        def _stub(*args, **kwargs):
            self._rec(name, args, kwargs)
            return _NullCtx(self, name)
        return _stub

    # ---- explicitly modelled widgets --------------------------------------
    def button(self, label="", *args, **kwargs):
        self._rec("button", (label,), kwargs)
        key = kwargs.get("key")
        return (key in self._buttons_true) or (label in self._buttons_true)

    def form_submit_button(self, label="", *args, **kwargs):
        self._rec("form_submit_button", (label,), kwargs)
        key = kwargs.get("key")
        return (key in self._buttons_true) or (label in self._buttons_true)

    def selectbox(self, label="", options=None, *args, **kwargs):
        if options is not None:
            kwargs = dict(kwargs, options=options)
        self._rec("selectbox", (label,), kwargs)
        key = kwargs.get("key")
        if key in self._selectbox_returns:
            return self._selectbox_returns[key]
        idx = kwargs.get("index", 0) or 0
        opts = list(options or [])
        return opts[idx] if opts and idx < len(opts) else (opts[0] if opts else None)

    def text_input(self, label="", value="", *args, **kwargs):
        self._rec("text_input", (label,), kwargs)
        key = kwargs.get("key")
        return self._text_returns.get(key, kwargs.get("value", value))

    def text_area(self, label="", value="", *args, **kwargs):
        self._rec("text_area", (label,), kwargs)
        key = kwargs.get("key")
        return self._text_returns.get(key, kwargs.get("value", value))

    def number_input(self, label="", *args, **kwargs):
        self._rec("number_input", (label,), kwargs)
        return kwargs.get("value", kwargs.get("min_value", 0))

    def date_input(self, label="", value=None, *args, **kwargs):
        self._rec("date_input", (label,), kwargs)
        key = kwargs.get("key")
        return self._date_returns.get(key, kwargs.get("value", value))

    def slider(self, label="", *args, **kwargs):
        self._rec("slider", (label,), kwargs)
        return kwargs.get("value", kwargs.get("min_value", 0))

    def file_uploader(self, label="", *args, **kwargs):
        self._rec("file_uploader", (label,), kwargs)
        return None

    def checkbox(self, label="", *args, **kwargs):
        key = kwargs.get("key")
        value = kwargs.get("value", False)
        if key is not None:
            if key not in self.session_state:
                self.session_state[key] = value
            value = self.session_state[key]
        # Record the EFFECTIVE value (like real Streamlit after the widget
        # reads its stateful slot) so tests can assert kwargs["value"].
        rec_kwargs = dict(kwargs)
        rec_kwargs["value"] = value
        self._rec("checkbox", (label,), rec_kwargs)
        return value

    def multiselect(self, label="", options=None, *args, **kwargs):
        if options is not None:
            kwargs = dict(kwargs, options=options)
        self._rec("multiselect", (label,), kwargs)
        opts = list(options or [])
        default = kwargs.get("default") or []
        return list(default) if default else (opts[:1] if opts else [])

    def radio(self, label="", options=None, *args, **kwargs):
        self._rec("radio", (label,), kwargs)
        opts = list(options or [])
        return opts[0] if opts else None

    def error(self, msg="", *a, **k):
        self._rec("error", (msg,), k)
        self.errors.append(msg)

    def warning(self, msg="", *a, **k):
        self._rec("warning", (msg,), k)
        self.warnings.append(msg)

    def rerun(self, *a, **k):
        self.rerun_count += 1
        raise StopRerun()

    def stop(self, *a, **k):
        raise StopRerun()

    # context-manager widgets
    def sidebar(self):  # property-like; real st.sidebar is an object, handled in install_streamlit_mock
        return _NullCtx(self, "sidebar")

    def columns(self, spec, *a, **k):
        n = spec if isinstance(spec, int) else len(spec)
        self._rec("columns", (spec,), k)
        return [_NullCtx(self, f"columns[{i}]") for i in range(n)]

    def tabs(self, labels, *a, **k):
        self._rec("tabs", (labels,), k)
        return [_NullCtx(self, f"tabs[{i}]") for i in range(len(labels))]

    def expander(self, *a, **k):
        self._rec("expander", a, k)
        return _NullCtx(self, "expander")

    def container(self, *a, **k):
        self._rec("container", a, k)
        return _NullCtx(self, "container")

    def form(self, *a, **k):
        self._rec("form", a, k)
        return _NullCtx(self, "form")

    def spinner(self, *a, **k):
        self._rec("spinner", a, k)
        return _NullCtx(self, "spinner")

    def chat_message(self, *a, **k):
        self._rec("chat_message", a, k)
        return _NullCtx(self, "chat_message")

    def empty(self, *a, **k):
        self._rec("empty", a, k)
        return _NullCtx(self, "empty")


class _NullCtx:
    """Acts as a no-op context manager AND a no-op callable/attr holder.

    Attribute access returns a callable bound to the dotted child path
    (e.g. ``sidebar.title``); calling it logs that path into the owning
    mock's ``calls`` list and returns another ``_NullCtx`` for deeper chains.
    """
    def __init__(self, mock=None, path=""):
        self._mock = mock
        self._path = path

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __call__(self, *a, **k):
        if self._mock is not None and self._path:
            self._mock._rec(self._path, a, k)
        return _NullCtx(self._mock, self._path)

    def __getattr__(self, name):
        child_path = f"{self._path}.{name}" if self._path else name

        def _stub(*a, **k):
            if self._mock is not None:
                self._mock._rec(child_path, a, k)
            if name in ("columns", "tabs"):
                spec = a[0] if a else (k.get("spec") or k.get("labels"))
                if name == "columns":
                    n = spec if isinstance(spec, int) else len(spec or [])
                else:
                    n = len(spec or [])
                return [_NullCtx(self._mock, f"{child_path}[{i}]") for i in range(n)]
            return _NullCtx(self._mock, child_path)

        return _stub


def _make_components_stub(mock):
    """Build lightweight streamlit.components(+) stubs bound to *mock*."""
    comp = types.ModuleType("streamlit.components")
    v1 = types.ModuleType("streamlit.components.v1")
    v1.html = lambda *a, **k: mock._rec("components.v1.html", a, k)
    comp.v1 = v1
    return comp


@contextmanager
def install_streamlit_mock():
    """Install the mock into sys.modules for the duration of the context.

    Saves the original ``streamlit``, ``streamlit.components`` and
    ``streamlit.components.v1`` modules FIRST, so the mock (which replaces
    them) never shadows the real package after the context exits.
    """
    names = ("streamlit", "streamlit.components", "streamlit.components.v1")
    saved = {name: sys.modules.get(name) for name in names}

    mock = StreamlitMock()
    mock.sidebar = _NullCtx(mock, "sidebar")   # st.sidebar must behave like a context manager
    components = _make_components_stub(mock)
    mock.components = components

    sys.modules["streamlit"] = mock
    sys.modules["streamlit.components"] = components
    sys.modules["streamlit.components.v1"] = components.v1
    try:
        yield mock
    finally:
        for name in names:
            if saved[name] is not None:
                sys.modules[name] = saved[name]
            else:
                sys.modules.pop(name, None)
