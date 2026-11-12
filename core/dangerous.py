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

Context-aware exceptions (v3.2):
  - subprocess.run with a LIST argument and a command that belongs to an
    ordinary build, test or version-control cycle is allowed (interpreters,
    test runners, package managers, git, common file utilities, the local
    database clients of the project). The list form rules out shell
    injection; the command NAME is then checked against a deny list of
    binaries that destroy data or reach beyond this machine.
  - a Python comment is removed before the shell rules run: a comment that
    mentions a forbidden command is documentation, not an instruction.
  - os.system with simple, non-piped, safe commands is allowed
    (e.g. os.system('echo done')).
  - open(..., 'w') on a temporary, test or scratch path is allowed.
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

# The set of commands a normal build, test or inspection cycle needs. It is
# NOT a security boundary and is not treated as one: the boundary is the deny
# list below plus the isolation of the profile. Keeping an allow list instead
# meant that every ordinary tool - a test runner, a version-control command,
# a language runtime - needed a confirmation, and a delegated child, which
# has nobody to ask, was refused outright. That is the failure this set is
# sized against: the work an agent is asked to do is allowed, while anything
# that destroys data or reaches beyond this machine still asks.
_SAFE_SUBPROCESS_COMMANDS: Set[str] = {
    # Inspection
    "echo", "ls", "dir", "pwd", "which", "where", "uname", "hostname",
    "true", "false", "cat", "head", "tail", "wc", "grep", "rg", "find",
    "stat", "file", "du", "df", "sort", "uniq", "diff", "tree", "date",
    "basename", "dirname", "realpath", "sleep",
    # Files inside the project: creating, copying, moving, packing
    "mkdir", "touch", "cp", "mv", "ln", "tar", "zip", "unzip",
    "gzip", "gunzip", "sed", "awk", "tr", "cut", "xargs", "tee",
    # Language runtimes, test runners and linters: the ordinary work here.
    # The versioned interpreter names appear explicitly because a project
    # pins one (``python3``, ``pip3``); a dotted version suffix such as
    # ``python3.14`` is reduced to its stem before the lookup.
    "python", "python3", "pip", "pip3", "pytest", "node", "npm", "npx",
    "yarn", "pnpm",
    "ruff", "black", "mypy", "flake8", "isort", "coverage", "tox",
    "php", "composer", "go", "cargo", "rustc", "make", "cmake",
    "java", "javac", "mvn", "gradle", "dotnet", "perl", "ruby", "bundle",
    # Version control
    "git",
    # Local database clients of the project
    "sqlite3", "mysql", "mariadb", "mysqldump", "mariadb-dump", "psql",
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
    # Network access. The child process has no boundary of its own, so any
    # outbound call is a potential exfiltration path for secrets it can read.
    DangerRule(
        r"\b(?:socket\.(?:socket|create_connection)|urllib\.request|requests\."
        r"(?:get|post|put|patch|delete|head|request|Session)|httpx\.|aiohttp\.)",
        "Сетевой запрос из выполняемого кода. Позволяет отправить наружу всё, что доступно процессу: переменные окружения, файлы проекта, ключи.",
    ),
    # Direct imports of the process-spawning API: the call site then has no
    # ``subprocess.`` prefix for the rule above to anchor on.
    DangerRule(
        r"\bfrom\s+(?:subprocess|os|asyncio|multiprocessing)\s+import\s+"
        r"(?:run|call|check_call|check_output|Popen|popen|system|spawn\w*|fork|exec\w*)",
        "Прямой импорт функций запуска процессов. Даёт те же возможности, что и обращение через модуль, но без префикса, по которому проверяется код.",
    ),
    # Alternative process-spawning APIs that the subprocess rule does not cover.
    DangerRule(
        r"\b(?:os\.popen|asyncio\.create_subprocess_(?:exec|shell)|os\.posix_spawn|pty\.spawn)\s*\(",
        "Запуск внешней программы через альтернативный API. Даёт те же возможности, что и subprocess, и не ограничен проверкой команд.",
    ),
    # Native code and dynamic import: an escape from every static check.
    DangerRule(
        r"\b(?:ctypes\.(?:CDLL|cdll|PyDLL|windll)|importlib\.import_module\s*\(|__import__\s*\()",
        "Загрузка нативного кода или динамический импорт по строке. Обходит статический анализ: что именно выполнится, заранее неизвестно.",
    ),
    # File writes through pathlib, which the open(...) rule does not see.
    # ``replace`` is deliberately absent: it is also the name of the most
    # common string method, so the rule fired on ``"a".replace("a", "b")``
    # and blocked ordinary formatting work.
    DangerRule(
        r"\.(?:write_text|write_bytes|unlink|rmdir|rename)\s*\(",
        "Запись или удаление файла через pathlib. Может перезаписать существующий файл или безвозвратно удалить данные.",
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


_SUBPROCESS_CALL_RE = re.compile(
    r"(?:subprocess|Popen)\s*\.\s*(?:run|call|check_call|check_output|Popen|popen)\s*\(",
    re.IGNORECASE,
)
_LIST_COMMAND_RE = re.compile(r"\s*\[\s*['\"]([^'\"]+)['\"]")


def _normalize_command(cmd_name: str) -> str:
    """Return the bare, lowercase program name of a subprocess command.

    ``/usr/bin/LS`` and ``ls`` name the same program; a case-sensitive
    comparison made the verdict depend on how the path happened to be
    written.
    """
    name = str(cmd_name or "").strip().strip("'\"")
    base = name.rsplit("/", 1)[-1] if "/" in name else name
    return base.lower()


def _command_is_safe(base: str) -> bool:
    """Return True when *base* names a command an ordinary cycle needs.

    A dotted version suffix is reduced to its stem first: ``python3.14`` is
    the interpreter a project pins, and the entry list holds ``python3``.
    A name whose digits are part of the program name (``sqlite3``) is looked
    up as written, so the reduction never widens the list by accident.
    """
    if base in _SAFE_SUBPROCESS_COMMANDS:
        return True
    head, dot, tail = base.partition(".")
    if dot and head and tail.replace(".", "").isdigit():
        return head in _SAFE_SUBPROCESS_COMMANDS
    return False


def _iter_subprocess_calls(code: str) -> List[bool]:
    """Return one flag per subprocess call: True when that call is safe.

    Every call site is judged on its own. A whitelisted binary earlier in the
    snippet no longer vouches for a dangerous binary later in the same
    snippet: the caller treats the snippet as dangerous when at least one
    call is unsafe, and the reasons list names that call.
    """
    results: List[bool] = []
    for match in _SUBPROCESS_CALL_RE.finditer(code):
        args_match = _LIST_COMMAND_RE.match(code, match.end())
        if args_match is None:
            results.append(False)   # string form or unknown shape
            continue
        cmd_name = args_match.group(1)
        base = _normalize_command(cmd_name)
        if base in _ALWAYS_DANGEROUS_COMMANDS:
            results.append(False)
            continue
        results.append(_command_is_safe(base))
    return results


def _check_python_rules(code: str) -> List[str]:
    """Return the reasons for matched Python rules.

    A safe-pattern match excuses only the call it was written for. The rule is
    reported when at least one occurrence is NOT excused, so a harmless call
    cannot vouch for a dangerous one elsewhere in the same snippet.
    """
    reasons: List[str] = []
    for pattern_compiled, safe_compiled, rule in _PYTHON_SAFE:
        matches = list(pattern_compiled.finditer(code))
        if not matches:
            continue
        if safe_compiled is None:
            reasons.append(rule.reason)
            continue
        if "subprocess" in rule.pattern.lower():
            calls = _iter_subprocess_calls(code)
            if calls and all(calls):
                continue
            reasons.append(rule.reason)
            continue
        # os.system()/open(): every occurrence must be excused by its own
        # safe shape, otherwise the rule stays.
        excused = sum(1 for _ in safe_compiled.finditer(code))
        if excused >= len(matches):
            continue
        reasons.append(rule.reason)
    return reasons


def _strip_python_comments(code: str) -> str:
    """Return *code* with whole-line Python comments removed.

    A comment that describes a forbidden command ("do not run rm -rf here")
    is documentation. Matching the shell rules against it made the analysis
    refuse a snippet for the text of its own warning. Only WHOLE-LINE
    comments are removed: a ``#`` inside a string literal is data, and
    rewriting code before the analysis would risk hiding a real payload in
    the very text the rules inspect.
    """
    kept = []
    for line in str(code or "").splitlines():
        if line.lstrip().startswith("#"):
            continue
        kept.append(line)
    return "\n".join(kept)


def _check_shell_rules(code: str) -> List[str]:
    """Return the list of human-readable reasons for matched shell rules."""
    reasons: List[str] = []
    analysed = _strip_python_comments(code)
    for pattern, rule in _SHELL_COMPILED:
        if pattern.search(analysed):
            reasons.append(rule.reason)
    return reasons


#: Upper bound on the code text handed to the rules. It exists so a
#: pathological payload cannot stall the loop, and it is far above any
#: realistic snippet: padding the head of a script must never hide the rest.
ANALYSIS_LIMIT_CHARS = 1_000_000

#: Upper bound on the code text echoed back for display. Analysis always sees
#: the full text; only the preview is bounded.
DISPLAY_SNIPPET_CHARS = 20_000


def assess_code(code: str, tool: str = "") -> DangerAssessment:
    """Assess whether a code snippet is potentially dangerous.

    The rules run over the WHOLE text: a long prologue must not push a
    dangerous tail out of view. Only the preview carried back to the UI is
    bounded, and it is explicitly marked when shortened, so the user is never
    shown a shortened snippet as if it were the code being executed.

    Returns a DangerAssessment with:
      - dangerous: True if any rule matched
      - reasons:   human-readable explanations (maybe several)
      - tool:      which tool was called (run_code / run_test)
      - code_snippet: the analysed code, bounded for display
    """
    full = str(code or "")
    analysed = full[:ANALYSIS_LIMIT_CHARS]
    reasons = _check_shell_rules(analysed)
    # Additive: Python rules also apply (the code may be Python that spawns shells)
    reasons.extend(_check_python_rules(analysed))
    # Deduplicate while preserving order
    seen: set = set()
    unique_reasons = [r for r in reasons if not (r in seen or seen.add(r))]
    snippet = full[:DISPLAY_SNIPPET_CHARS]
    if len(full) > DISPLAY_SNIPPET_CHARS:
        snippet += (
            f"\n\n... [показаны первые {DISPLAY_SNIPPET_CHARS} из {len(full)} символов; "
            "анализ выполнен по всему тексту]"
        )
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
        return DangerAssessment(dangerous=False, reasons=[], tool=tool,
                                code_snippet=str(code or "")[:DISPLAY_SNIPPET_CHARS])
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
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
