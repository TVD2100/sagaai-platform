"""
core.dangerous - classification of potentially dangerous operations
and human-readable explanations for the confirmation UI.

When DevAgent (or any orchestrator) wants to run code or shell commands
that may harm the system, the agent loop must NOT execute them silently.
Instead, it stops and asks the user for explicit permission, showing
WHY the operation is considered dangerous.

The classifier is heuristic: it looks for known dangerous patterns in
shell commands and Python code. It is intentionally conservative -
when in doubt, it requests confirmation. A "false positive" only costs
the user one click; a false negative could destroy data.

Danger levels:
  - "safe"       - no confirmation required
  - "confirm"    - confirmation required, with explanation
  - "blocked"    - confirmation is NOT enough (not used yet; reserved)

Context-aware exceptions (v3.1):
  - subprocess.run with a LIST argument and a safe command is allowed
    (e.g. subprocess.run(['node', 'test.js'], ...) - no shell injection).
  - os.system with simple, non-piped, safe commands is allowed
    (e.g. os.system('echo done')).
  - open(..., 'w') on non-system, non-protected paths is allowed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Set


@dataclass
class DangerRule:
    """A single dangerous-pattern rule with its explanation."""
    pattern: str          # regex pattern (case-insensitive unless prefixed (?-i))
    reason: str           # human-readable explanation in Russian
    action: str = "confirm"  # "confirm" | "block" (reserved)
    # If safe_pattern is set and matches, the rule is suppressed.
    safe_pattern: Optional[str] = None


# ─── Shell command rules ─────────────────────────────────────────────────────

_SHELL_RULES: List[DangerRule] = [
    # Destructive filesystem operations
    DangerRule(
        r"\brm\s+(-[a-z]*r[a-z]*\s+|[a-z]*r[a-z]*\s+.*/)",
        "Рекурсивное удаление файлов (rm -r / rm -rf). Это необратимо уничтожает данные - случайная ошибка в пути может стереть важные файлы или всю папку проекта.",
    ),
    DangerRule(
        r"\brm\s+(-f|--force)\b",
        "Принудительное удаление файлов (rm -f) без запроса подтверждения. Ошибочная команда может безвозвратно удалить нужные данные.",
    ),
    DangerRule(
        r"\bmkfs(\s|\.|\b)",
        "Форматирование диска (mkfs). Полностью стирает все данные на указанном разделе или устройстве.",
    ),
    DangerRule(
        r"\bdd\s+.*of=/",
        "Запись напрямую в устройство (dd of=/dev/...). Может перезаписать диск, раздел или загрузочную область, уничтожив данные без возможности восстановления.",
    ),
    DangerRule(
        r">\s*/dev/sd[a-z]+",
        "Запись напрямую в дисковое устройство. Может уничтожить файловую систему или все данные на диске.",
    ),
    DangerRule(
        r"\bchmod\s+777\s+/",
        "Изменение прав на корневой каталог (chmod 777 /). Делает всю файловую систему доступной для записи всем пользователям - серьёзная уязвимость.",
    ),
    DangerRule(
        r"\bchown\s+.*\s+/",
        "Изменение владельца корневого каталога (chown /). Может нарушить работу всей операционной системы.",
    ),
    DangerRule(
        r"\bshred\s+",
        "Безвозвратное затирание файлов (shred). Данные уничтожаются навсегда и не могут быть восстановлены.",
    ),
    DangerRule(
        r"\bformat\s+[a-z]:",
        "Форматирование диска (Windows format). Полностью стирает данные с указанного диска.",
    ),
    DangerRule(
        r"\bdeltree\s+",
        "Рекурсивное удаление каталога (deltree, Windows). Удаляет папку и всё её содержимое безвозвратно.",
    ),
    # Package installation / system modification
    DangerRule(
        r"\bpip\s+install\b",
        "Установка Python-пакетов (pip install). Изменяет окружение и может загрузить вредоносный или несовместимый код. Проверьте имя пакета, прежде чем разрешить.",
    ),
    DangerRule(
        r"\b(?:apt-get|apt|yum|dnf|brew|pacman)\s+(?:install|remove|purge|update|upgrade)\b",
        "Изменение системного окружения (установка, удаление или обновление системных пакетов). Может повлиять на работу всей системы.",
    ),
    DangerRule(
        r"\b(?:systemctl|service|rc-service)\s+(?:stop|start|restart|kill|disable|enable)\b",
        "Управление системными службами. Может нарушить работу сервисов и операционной системы.",
    ),
    DangerRule(
        r"\b(curl|wget)\s+.*\|\s*(ba)?sh\b",
        "Выполнение скрипта, скачанного из интернета (curl | bash). Крайне опасно: вы выполняете код, который не видели и не проверяли.",
    ),
    DangerRule(
        r"\bsudo\b",
        "Выполнение команды с правами администратора (sudo). Даёт программе полный доступ к системе - последствия могут быть необратимыми.",
    ),
    # Network / remote operations
    DangerRule(
        r"\b(?:nmap|masscan|nikto|hydra|aircrack-ng)\b",
        "Сканирование сети или подбор паролей. Такие действия могут быть незаконными и расцениваются как атака.",
    ),
    DangerRule(
        r"\bssh\s+.*(?:@|root@)",
        "Подключение по SSH к удалённому серверу. Может предоставить доступ к внешним системам или вызвать нежелательные действия.",
    ),
    DangerRule(
        r"\b(?:iptables|nftables|ufw)\s+",
        "Изменение правил сетевого экрана (firewall). Может отрезать доступ к серверу или открыть опасные порты.",
    ),
]


# ─── Python code rules ───────────────────────────────────────────────────────

# Known safe commands that are harmless when run via subprocess
_SAFE_SUBPROCESS_COMMANDS: Set[str] = {
    "python", "python3", "node", "npm", "npx", "pytest",
    "echo", "cat", "ls", "dir", "pwd", "touch", "mkdir",
    "git", "make", "cmake", "cargo", "go", "rustc",
    "pip", "pip3", "poetry", "conda", "virtualenv", "venv",
    "black", "ruff", "mypy", "flake8", "pylint",
    "docker", "kubectl", "helm", "terraform",
    "java", "javac", "mvn", "gradle",
    "docker-compose", "env", "printenv", "which", "where",
    "type", "test", "true", "false", "uname", "hostname",
}

# Commands that are NEVER safe, even in list form
_ALWAYS_DANGEROUS_COMMANDS: Set[str] = {
    "rm", "shred", "dd", "mkfs", "format", "deltree",
    "chmod", "chown", "chgrp",
    "sudo", "su", "su-",
    "curl", "wget", "nc", "netcat", "telnet",
    "nmap", "masscan", "nikto", "hydra", "aircrack-ng",
    "ssh", "scp", "sftp", "rsync",
    "iptables", "nftables", "ufw", "pfctl",
    "systemctl", "service", "rc-service", "initctl",
    "reboot", "shutdown", "halt", "poweroff",
    "kill", "killall", "pkill", "xkill",
    "apt-get", "apt", "yum", "dnf", "brew", "pacman", "zypper",
}


_PYTHON_RULES: List[DangerRule] = [
    DangerRule(
        r"\b(?:shutil\.)?rmtree\s*\(",
        "shutil.rmtree() - рекурсивное удаление папки в Python. Безвозвратно удаляет все файлы внутри, включая важные данные проекта.",
    ),
    DangerRule(
        r"\b(?:os\.)?(?:remove|unlink)\s*\(",
        "Удаление файла через os.remove()/os.unlink(). Если путь указан неверно, данные будут потеряны безвозвратно.",
    ),
    DangerRule(
        r"\bos\.system\s*\(",
        "Выполнение системной команды через os.system(). Это запускает произвольную shell-команду - эквивалентно выполнению её в терминале.",
        safe_pattern=r"os\.system\(['\"](?:echo|print|date|hostname|whoami|pwd|env|type|which)\b",
    ),
    DangerRule(
        r"\b(?:subprocess|Popen)\s*\.\s*(?:call|run|check_call|check_output|Popen|popen)\s*\(",
        "Запуск внешнего процесса через subprocess. Позволяет выполнить произвольную команду операционной системы.",
        safe_pattern=r"(?:subprocess|Popen)\s*\.\s*(?:run|call|check_call|check_output)\s*\("
                     r"\s*\[",  # list argument = no shell injection; command is checked later
    ),
    DangerRule(
        r"\b(?:eval|exec)\s*\(",
        "Выполнение кода из строки через eval()/exec(). Позволяет выполнить произвольный код - опасно, если строка получена из ненадёжного источника.",
    ),
    DangerRule(
        r"\b(?:pickle|shelve)\s*\.\s*(?:load|loads)\s*\(",
        "Десериализация pickle/shelve. Загрузка pickle из ненадёжного источника может выполнить произвольный код при распаковке.",
    ),
    DangerRule(
        r"\b(?:os\.)?(?:chmod|chown)\s*\(",
        "Изменение прав/владельца файлов через os.chmod()/os.chown(). Может ослабить защиту системы.",
    ),
    DangerRule(
        r"\bopen\s*\(\s*['\"][^'\"]*['\"]\s*,\s*['\"]w['\"]\s*\)",
        "Открытие файла в режиме записи (open(file, 'w')). Может перезаписать существующий файл, уничтожив его содержимое.",
        safe_pattern=r"\bopen\s*\(\s*['\"](?:tmp|temp|test|draft|scratch)[a-zA-Z0-9_.:/\-]*['\"]\s*,\s*['\"]w['\"]\s*\)",
    ),
    DangerRule(
        r"\b(?:glob|os\.walk)\s*\s+",
        "Обход файловой системы с потенциальным удалением/изменением найденных файлов. Проверьте, что именно будет затронуто.",
    ),
]


# ─── Compiled regexes ────────────────────────────────────────────────────────

_SHELL_COMPILED: List[tuple[re.Pattern, DangerRule]] = [
    (re.compile(r.pattern, re.IGNORECASE), r) for r in _SHELL_RULES
]

_PYTHON_COMPILED: List[tuple[re.Pattern, DangerRule]] = [
    (re.compile(r.pattern, re.IGNORECASE), r) for r in _PYTHON_RULES
]

# Also compile safe_pattern for each rule that has one.
_PYTHON_SAFE: List[tuple[re.Pattern, Optional[re.Pattern], DangerRule]] = [
    (re.compile(r.pattern, re.IGNORECASE),
     re.compile(r.safe_pattern, re.IGNORECASE) if r.safe_pattern else None,
     r)
    for r in _PYTHON_RULES
]


# ─── Tools that always require confirmation (unless explicitly allowed) ─────

# run_code executes arbitrary code by design, so it always requires
# confirmation when dangerous content is detected.
ALWAYS_CONFIRM_TOOLS = {"run_code"}

# run_test only requires confirmation if the code contains dangerous patterns.
CONDITIONAL_CONFIRM_TOOLS = {"run_test"}


@dataclass
class DangerAssessment:
    """Result of a danger assessment."""
    dangerous: bool
    reasons: List[str]   # human-readable explanations
    tool: str = ""
    code_snippet: str = ""  # truncated (or full) code shown in the UI

    def to_dict(self) -> dict:
        return {
            "dangerous": self.dangerous,
            "reasons": self.reasons,
            "tool": self.tool,
            "code_snippet": self.code_snippet,
        }


def _is_subprocess_command_safe(code: str) -> bool:
    """Check whether a subprocess call uses a safe command in list form.

    Returns True if the command is safe (list argument + safe command name),
    False if the command is dangerous or unknown.
    """
    # Try to extract the command list from subprocess.run(['cmd', ...], ...)
    m = re.search(
        r"(?:subprocess|Popen)\s*\.\s*(?:run|call|check_call|check_output)\s*\("
        r"\s*\[\s*['\"]([^'\"]+)['\"]",
        code, re.IGNORECASE,
    )
    if not m:
        return False
    cmd_name = m.group(1)
    # Get the base command name (last part of path)
    base = cmd_name.rsplit("/", 1)[-1] if "/" in cmd_name else cmd_name
    # Dangerous commands are never safe, regardless of arguments
    if base in _ALWAYS_DANGEROUS_COMMANDS:
        return False
    # Check if it's in the known-safe set
    if base in _SAFE_SUBPROCESS_COMMANDS:
        return True
    return False


def _check_python_rules(code: str) -> List[str]:
    """Return the list of human-readable reasons for matched Python rules,
    respecting safe_pattern exceptions.
    """
    reasons: List[str] = []
    for pattern_compiled, safe_compiled, rule in _PYTHON_SAFE:
        if not pattern_compiled.search(code):
            continue
        # If a safe_pattern exists and matches, suppress this rule.
        if safe_compiled is not None and safe_compiled.search(code):
            # Additional check for subprocess: verify the command is actually safe.
            if "subprocess" in rule.pattern.lower():
                if _is_subprocess_command_safe(code):
                    continue
            # For os.system and open, safe_pattern match alone is sufficient.
            continue
        reasons.append(rule.reason)
    return reasons


def _check_shell_rules(code: str) -> List[str]:
    """Return the list of human-readable reasons for matched shell rules."""
    reasons: List[str] = []
    for pattern, rule in _SHELL_COMPILED:
        if pattern.search(code):
            reasons.append(rule.reason)
    return reasons


def assess_code(code: str, tool: str = "") -> DangerAssessment:
    """Assess whether a code snippet is potentially dangerous.

    Returns a DangerAssessment with:
      - dangerous: True if any rule matched
      - reasons:   human-readable explanations (maybe several)
      - tool:      which tool was called (run_code / run_test)
      - code_snippet: the original code (truncated to 2000 chars for display)
    """
    snippet = str(code or "")[:2000]
    reasons = _check_shell_rules(snippet)
    # Additive: Python rules also apply (the code may be Python that spawns shells)
    reasons.extend(_check_python_rules(snippet))
    # Deduplicate while preserving order
    seen: set = set()
    unique_reasons = [r for r in reasons if not (r in seen or seen.add(r))]
    return DangerAssessment(
        dangerous=bool(unique_reasons),
        reasons=unique_reasons,
        tool=tool,
        code_snippet=snippet,
    )


def tool_needs_confirmation(tool: str, code: str) -> DangerAssessment:
    """Decide whether the given tool call needs user confirmation.

    Returns a DangerAssessment; ``dangerous=True`` means confirmation is required.

    Rules:
      - ``run_code``: ALWAYS assessed; dangerous if any pattern matches.
      - ``run_test``: assessed the same way; dangerous if any pattern matches.
      - Other tools: never dangerous by this module.
    """
    if tool not in ("run_code", "run_test"):
        return DangerAssessment(dangerous=False, reasons=[], tool=tool, code_snippet=str(code or "")[:2000])
    return assess_code(str(code or ""), tool=tool)


def format_reasons_for_ui(reasons: List[str]) -> str:
    """Format the danger reasons for display in the confirmation dialog."""
    if not reasons:
        return ""
    lines = ["⚠️ **Почему это считается опасным:**"]
    for r in reasons:
        lines.append(f"- {r}")
    return "\n".join(lines)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
