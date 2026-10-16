# -*- coding: utf-8 -*-
"""core.folder_dialog - native OS folder picker for the Streamlit UI.

Streamlit has no built-in directory chooser widget, so the platform opens
the operating system's own folder dialog and returns the chosen absolute
path. Two backends are supported:

  * ``tkinter.filedialog.askdirectory`` - cross-platform (Windows, macOS,
    Linux with a GUI). On Windows this is the native shell dialog; on
    official python.org builds ``tkinter`` ships with the interpreter.
  * ``osascript`` "choose folder" - macOS fallback used when the tkinter
    child fails for any reason (e.g. a Python build without Tk).

The dialog is ALWAYS opened in a short-lived child process, never inside
the Streamlit server process:

  * a crash or hang in the GUI toolkit cannot take the web app down;
  * the child is killed by ``subprocess`` timeout, so a dialog left open
    forever cannot block the UI thread;
  * the result travels through a temporary UTF-8 file instead of stdout,
    which keeps non-ASCII paths (cyrillic, spaces, emoji) intact on
    Windows console encodings (cp1251/cp866).

When no GUI is available (headless server, service session, missing Tk,
explicit opt-out) every function degrades gracefully: ``pick_folder()``
returns ``None`` and the UI falls back to the manual path input.

Public API:
    native_dialog_available() -> bool
    pick_folder(initial_dir=None, title=None, timeout=180.0) -> str | None
    reset_availability_cache() -> None
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

# Environment switch: set to "1" to force the manual path input everywhere
# (useful for CI, headless servers and users who dislike native dialogs).
DISABLE_ENV_VAR = "SAGAAI_DISABLE_FOLDER_DIALOG"

# Default timeout: a user may browse folders for a while, but an orphaned
# dialog must never block the app indefinitely.
DEFAULT_TIMEOUT = 180.0

# Cached verdict of the GUI availability probe (None = not probed yet).
_AVAILABLE_CACHE: Optional[bool] = None


# ── Child-process programs ────────────────────────────────────────────────────

# argv: <out_json_path> <initial_dir> <title>
# Writes {"path": ...} on success, {"cancelled": true} when the user closes
# the dialog without choosing, {"error": ...} when the toolkit is unusable.
_TK_CHILD_CODE = (
    "import json, sys\n"
    "out, initial, title = sys.argv[1], sys.argv[2], sys.argv[3]\n"
    "def emit(payload):\n"
    "    with open(out, 'w', encoding='utf-8') as fh:\n"
    "        json.dump(payload, fh)\n"
    "try:\n"
    "    import tkinter\n"
    "    from tkinter import filedialog\n"
    "    root = tkinter.Tk()\n"
    "    root.withdraw()\n"
    "    try:\n"
    "        root.attributes('-topmost', True)\n"
    "    except Exception:\n"
    "        pass\n"
    "    kwargs = {}\n"
    "    if title:\n"
    "        kwargs['title'] = title\n"
    "    if initial and __import__('os').path.isdir(initial):\n"
    "        kwargs['initialdir'] = initial\n"
    "    chosen = filedialog.askdirectory(**kwargs)\n"
    "    try:\n"
    "        root.destroy()\n"
    "    except Exception:\n"
    "        pass\n"
    "    emit({'path': chosen} if chosen else {'cancelled': True})\n"
    "except Exception as exc:\n"
    "    emit({'error': str(exc)})\n"
)


def _probe_child_code() -> str:
    """Program used to probe whether tkinter can be imported at all."""
    return "import tkinter; import tkinter.filedialog"


# ── Availability probe ───────────────────────────────────────────────────────

def native_dialog_available(force_recheck: bool = False) -> bool:
    """Return True when a native folder dialog can plausibly be opened.

    The verdict is cached for the process lifetime: probing spawns a child
    interpreter, which is far too expensive to repeat on every Streamlit
    rerun. Call ``reset_availability_cache()`` to drop the cached value.

    The probe only checks that a usable dialog BACKEND exists (tkinter on
    any OS, or ``osascript`` on macOS). It never opens a window, so it is
    safe to call while rendering a page.
    """
    global _AVAILABLE_CACHE
    if _AVAILABLE_CACHE is not None and not force_recheck:
        return _AVAILABLE_CACHE

    if os.environ.get(DISABLE_ENV_VAR) == "1":
        _AVAILABLE_CACHE = False
        return False

    available = False
    if sys.platform == "darwin" and shutil.which("osascript"):
        # macOS always has a native fallback, even without Tk.
        available = True
    else:
        try:
            probe = subprocess.run(
                [sys.executable, "-c", _probe_child_code()],
                capture_output=True, text=True, timeout=30,
            )
            available = probe.returncode == 0
        except (OSError, subprocess.SubprocessError):
            available = False

    _AVAILABLE_CACHE = available
    return available


def reset_availability_cache() -> None:
    """Forget the cached availability verdict (used by tests)."""
    global _AVAILABLE_CACHE
    _AVAILABLE_CACHE = None


# ── Picker ───────────────────────────────────────────────────────────────────

def _run_tk_dialog(out_path: str, initial_dir: Optional[str],
                   title: Optional[str], timeout: float) -> dict:
    """Open the tkinter dialog in a child process and read its verdict."""
    try:
        subprocess.run(
            [sys.executable, "-c", _TK_CHILD_CODE, out_path,
             str(initial_dir or ""), str(title or "")],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"error": "timeout"}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"error": str(exc)}
    return _read_child_verdict(out_path)


def _read_child_verdict(out_path: str) -> dict:
    """Read and parse the child's JSON verdict, tolerating a crash."""
    try:
        with open(out_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {"error": "no result"}
    if not isinstance(data, dict):
        return {"error": "bad result"}
    return data


def _osascript_available() -> bool:
    """True when the macOS ``choose folder`` fallback can be used."""
    return sys.platform == "darwin" and bool(shutil.which("osascript"))


def _run_osascript_dialog(initial_dir: Optional[str],
                          title: Optional[str], timeout: float) -> dict:
    """macOS fallback: ``osascript -e 'choose folder ...'``.

    Returns the same verdict shape as the tkinter backend. A non-zero exit
    status means the user cancelled (or Finder refused the request).
    """
    prompt = (title or "Выберите папку").replace('"', "'")
    script = f'POSIX path of (choose folder with prompt "{prompt}"'
    if initial_dir and os.path.isdir(initial_dir):
        script += f' default location POSIX file "{str(initial_dir).replace(chr(34), chr(39))}"'
    script += ")"
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"error": "timeout"}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"error": str(exc)}
    if proc.returncode != 0:
        return {"cancelled": True}
    return {"path": (proc.stdout or "").strip()}


def pick_folder(initial_dir: Optional[str] = None,
                title: Optional[str] = None,
                timeout: float = DEFAULT_TIMEOUT) -> Optional[str]:
    """Ask the operating system for a folder and return its absolute path.

    Parameters
    ----------
    initial_dir : Optional[str]
        Folder the dialog opens in (used when it exists).
    title : Optional[str]
        Dialog caption, already localised by the caller.
    timeout : float
        Hard limit in seconds; a dialog left open longer than this is
        abandoned and ``None`` is returned so the UI keeps responding.

    Returns
    -------
    Optional[str]
        The resolved absolute folder path, or ``None`` when the user
        cancelled, no GUI is available, or the backend failed. The caller
        must treat ``None`` as "nothing was picked" and fall back to the
        manual path input.
    """
    if os.environ.get(DISABLE_ENV_VAR) == "1":
        return None

    fd, out_path = tempfile.mkstemp(prefix="sagaai_folder_", suffix=".json")
    os.close(fd)
    try:
        verdict = _run_tk_dialog(out_path, initial_dir, title, timeout)
        # A failed tkinter backend on macOS still has the native fallback;
        # a plain cancel never falls through to it (that would reopen a
        # dialog the user just dismissed).
        if verdict.get("error") and _osascript_available():
            verdict = _run_osascript_dialog(initial_dir, title, timeout)

        chosen = str(verdict.get("path") or "").strip()
        if not chosen:
            return None
        resolved = Path(chosen).expanduser()
        if not resolved.is_dir():
            return None
        return str(resolved.resolve())
    finally:
        try:
            os.remove(out_path)
        except OSError:
            pass
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
