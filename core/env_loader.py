"""
core.env_loader - load env vars from shell profile files.
"""
import os
import re
from pathlib import Path

_EXPORT_RE = re.compile(
    r'^\s*export\s+(\w+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s#]+))'
)

def _parse_shell_exports(profile_path: Path):
    if not profile_path.is_file():
        return {}
    result = {}
    try:
        text = profile_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    for line in text.splitlines():
        m = _EXPORT_RE.match(line)
        if not m:
            continue
        name = m.group(1)
        value = m.group(2) or m.group(3) or m.group(4) or ""
        if name not in os.environ:
            result[name] = value
    return result

def _profile_candidates():
    home = Path.home()
    candidates = [
        home / ".zshrc",
        home / ".zprofile",
        home / ".bashrc",
        home / ".bash_profile",
        home / ".profile",
    ]
    config_dir = os.environ.get("XDG_CONFIG_HOME", str(home / ".config"))
    candidates.append(Path(config_dir) / "zsh" / ".zshrc")
    return candidates

def load_env_from_shell_profiles():
    injected = {}
    for profile_path in _profile_candidates():
        parsed = _parse_shell_exports(profile_path)
        for name, value in parsed.items():
            if name not in os.environ and name not in injected:
                os.environ[name] = value
                injected[name] = value
    return injected
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
