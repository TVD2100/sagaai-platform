"""
Shared pieces of the command modules: confirmation and small lookups.

Two rules live here because every dangerous command needs them and each one
implementing its own version is how they end up disagreeing:

- **Confirmation.** A destructive command asks interactively when a person is
  present and demands ``--yes`` when nobody is (a cron job, a pipe, CI). The
  difference is decided by ``stdin.isatty()``: reading from a pipe that nobody
  fills would block forever, and defaulting to "yes" would defeat the flag.
- **The install root.** The folder the platform runs from, resolved from the
  core rather than from the current directory: a command must work from any
  working directory the user happens to be in.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Callable, Dict

from sagaai.errors import CliError, config_error

#: What a command says when it refuses to run without an explicit answer.
NO_CONFIRMATION_HINT = "re-run with --yes to confirm"


def is_interactive() -> bool:
    """True when a person can answer a question right now."""
    try:
        return bool(sys.stdin.isatty())
    except Exception:  # pragma: no cover - a closed or replaced stdin
        return False


def confirm(question: str, *, assume_yes: bool = False) -> bool:
    """Ask for confirmation; raise a CONFIG error when it cannot be obtained.

    ``assume_yes`` is the ``--yes`` flag. Without it an interactive terminal
    gets one ``y/N`` question; a non-interactive one gets a refusal with the
    documented hint, because there is nobody to answer and guessing "yes" is
    exactly the failure mode the flag exists to prevent.
    """
    if assume_yes:
        return True
    if not is_interactive():
        raise config_error("confirmation_required",
                           "this command changes the installation and needs "
                           "confirmation, but no terminal is available",
                           hint=NO_CONFIRMATION_HINT)
    sys.stderr.write(question.rstrip() + " [y/N]: ")
    sys.stderr.flush()
    try:
        answer = sys.stdin.readline().strip().lower()
    except (KeyboardInterrupt, EOFError):
        answer = ""
    if answer not in ("y", "yes", "д", "да"):
        raise config_error("cancelled_by_user", "the operation was cancelled",
                           hint="nothing was changed")
    return True


def install_root() -> str:
    """Return the installation root the platform runs from.

    ``SAGAAI_INSTALL_ROOT`` overrides the location for a CI run or a test
    that must not touch the checkout it imports from: a command that replaces
    files in the installation cannot be exercised against the working tree.
    Without the override the answer comes from the core, so a command works
    from any working directory the user happens to be in.
    """
    override = str(os.environ.get("SAGAAI_INSTALL_ROOT") or "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    from core.paths import BASE_DIR
    return str(BASE_DIR)


def platform_version() -> str:
    """Return the platform version, or an empty string."""
    try:
        from core.version import __version__
        return str(__version__)
    except Exception:  # pragma: no cover - version is informational
        return ""


def app_running(root: str = "") -> bool:
    """True when the platform (UI or API) holds its running marker.

    ``core.updater.is_app_running`` answers for the Streamlit process; the API
    process is detected by its instance lock. A database restore or a file
    replacement under a live process is the exact scenario these guards
    exist for, so both are checked.
    """
    try:
        from core.updater import is_app_running
        if is_app_running(root or install_root()):
            return True
    except Exception:  # pragma: no cover - the marker is best effort
        pass
    try:
        import json

        from server.instance_lock import lock_path
        path = lock_path()
        if not path.exists():
            return False
        raw = path.read_text(encoding="utf-8").strip()
        holder = json.loads(raw) if raw else {}
        pid = holder.get("pid") if isinstance(holder, dict) else None
        if not isinstance(pid, int) or pid <= 0:
            return False
        import os
        os.kill(pid, 0) if os.name == "posix" else None
        return True
    except (OSError, ValueError, ImportError):
        return False
    except Exception:  # pragma: no cover - defensive
        return False


def refuse_when_running(root: str = "", *, force: bool = False,
                        what: str = "this operation") -> None:
    """Raise a CONFLICT error when the platform is live and *force* is off."""
    if force or not app_running(root):
        return
    raise CliError(
        "app_running",
        "the platform is running; %s would replace files it has loaded" % what,
        exit_code=3,
        hint="stop the interface and the API first, or add --force",
    )


def employee_names() -> Dict[str, str]:
    """Return ``{slug: name}`` of the installed employees, or an empty dict."""
    try:
        from core.gateway import entities
        cards = entities.list_orchestrators()
    except Exception:  # pragma: no cover - the listing is informational
        return {}
    return {str(card.slug): str(card.name or card.slug) for card in cards}


def call(action: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """Call a core function, translating a gateway refusal into a CliError.

    The translation happens at this boundary rather than inside every command:
    a command that had to know about HTTP statuses would be doing the
    transport's job.
    """
    try:
        return action(*args, **kwargs)
    except CliError:
        raise
    except Exception as exc:  # noqa: BLE001 - translated below
        if all(hasattr(exc, attribute) for attribute in ("code", "message", "status")):
            from sagaai.errors import from_gateway
            raise from_gateway(exc) from exc
        raise
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
