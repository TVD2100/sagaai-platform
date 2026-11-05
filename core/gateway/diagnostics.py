"""
core.gateway.diagnostics - one honest report about an installation.

The report answers the questions a person asks when something does not work,
and it answers them WITHOUT changing anything: which version is running, are
the dependencies installed, do the databases exist and is their schema
current, can the process write where it must, where does the encryption key
come from, is the API usable, is anything watching the schedule clock.

Two decisions shape the module.

- **It never writes.** A diagnostic that seeds defaults or creates a database
  reports the state it just created, not the state it was asked about. Every
  probe is read-only: ``sqlite3`` connections use ``mode=ro`` and a missing
  file is a finding, not something to fix.
- **A missing installation is not an error.** A clean machine has no
  databases, no token and no key - that is the normal state before the first
  start. Those checks report ``not_initialized`` (a warning); an ``error`` is
  reserved for a state the platform cannot run in at all.

Every check returns the same shape so the CLI can print it and a machine can
read it:

    {"status": "ok" | "warn" | "error", "summary": str, ...details}
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from typing import Any, Dict, Optional

# ─── Status vocabulary ──────────────────────────────────────────────────────

OK = "ok"
WARN = "warn"
ERROR = "error"


def _check(status: str, summary: str, **details: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"status": status, "summary": summary}
    payload.update(details)
    return payload


def _readonly(path: str) -> Optional[sqlite3.Connection]:
    """Open *path* read-only, or return None when it cannot be opened.

    ``mode=ro`` is the whole point: a probe must not create a missing file,
    run a journal recovery into it or leave a ``-wal`` behind in a folder the
    caller only asked about.
    """
    if not path or not os.path.isfile(path):
        return None
    try:
        uri = "file:%s?mode=ro" % path.replace("?", "%3f")
        return sqlite3.connect(uri, uri=True, timeout=5.0)
    except Exception:
        return None


def _scalar(conn: sqlite3.Connection, sql: str, default: Any = None) -> Any:
    try:
        row = conn.execute(sql).fetchone()
    except Exception:
        return default
    if row is None:
        return default
    return row[0]


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return bool(_scalar(conn,
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='%s'"
                        % name.replace("'", "''"), 0))


# ─── Identity ───────────────────────────────────────────────────────────────


def _version() -> str:
    try:
        from core.version import __version__
        return str(__version__)
    except Exception:  # pragma: no cover - version is informational
        return "0.0.0"


def check_version() -> Dict[str, Any]:
    """Report the platform version and where it was read from."""
    version = _version()
    status = OK if version and version != "0.0.0" else WARN
    return _check(status, version,
                  version=version,
                  module="core.version")


def check_python() -> Dict[str, Any]:
    """Report the interpreter the command runs under.

    This is the first thing to look at when the UI works and the CLI does not:
    a different ``python`` resolves a different set of packages.
    """
    version = "%d.%d.%d" % sys.version_info[:3]
    ok = sys.version_info >= (3, 12)
    return _check(OK if ok else ERROR, version,
                  version=version, executable=sys.executable,
                  implementation=sys.implementation.name)


def check_paths() -> Dict[str, Any]:
    """Report the base and data folders and the two database files."""
    try:
        from core.paths import BASE_DIR, DATA_DIR, DB_PATH, DEVAGENT_DB_PATH
    except Exception as exc:  # pragma: no cover - defensive
        return _check(ERROR, "core.paths is unreadable: %s" % exc)
    files = {}
    for name, path in (("sagaai.db", DB_PATH), ("devagent.db", DEVAGENT_DB_PATH)):
        exists = os.path.isfile(path)
        files[name] = {"path": path, "exists": exists,
                       "size": os.path.getsize(path) if exists else 0}
    missing = [name for name, item in files.items() if not item["exists"]]
    status = OK if not missing else WARN
    summary = ("both databases present" if not missing
               else "not initialized: " + ", ".join(missing))
    return _check(status, summary, base_dir=BASE_DIR, data_dir=DATA_DIR,
                  files=files)


def check_dependencies() -> Dict[str, Any]:
    """Audit installed libraries against the platform registry.

    A missing OPTIONAL library is a warning: the feature that needs it is
    unavailable, the platform still starts. A missing REQUIRED one is an
    error, because the next command will fail with an ImportError.
    """
    try:
        from core.dependency_check import check_dependencies as _audit
        report = _audit() or {}
    except Exception as exc:  # pragma: no cover - defensive
        return _check(ERROR, "the dependency audit failed: %s" % exc)
    missing = list(report.get("missing") or [])
    outdated = list(report.get("outdated") or [])
    required_missing = [item for item in missing if item.get("required")]
    if required_missing:
        status = ERROR
        summary = "missing required: " + ", ".join(
            str(item.get("package") or "") for item in required_missing)
    elif missing or outdated:
        status = WARN
        parts = []
        if missing:
            parts.append("missing " + ", ".join(
                str(item.get("package") or "") for item in missing))
        if outdated:
            parts.append("outdated " + ", ".join(
                str(item.get("package") or "") for item in outdated))
        summary = "; ".join(parts)
    else:
        status = OK
        summary = "all dependencies are installed and current"
    return _check(status, summary, missing=missing, outdated=outdated,
                  required_missing=[str(i.get("package") or "")
                                    for i in required_missing])


# ─── Databases ──────────────────────────────────────────────────────────────


#: Tables a current schema must have. Mirrors ``health.REQUIRED_TABLES``; kept
#: as a local constant so a diagnostic does not import the readiness probe and
#: its process-wide cache.
REQUIRED_TABLES = ("threads", "assistants", "messages", "config_kv",
                   "orchestrators")


def _database_file(name: str, path: str) -> Dict[str, Any]:
    """Describe one database file and the tables it holds."""
    exists = os.path.isfile(path)
    if not exists:
        return {
            "name": name,
            "path": path,
            "exists": False,
            "size": 0,
            "user_version": None,
            "revision": "",
            "revision_head": _expected_revision(),
            "revision_behind": 0,
            "tables": [],
            "missing_tables": list(REQUIRED_TABLES),
            "state": "not_initialized",
        }
    info: Dict[str, Any] = {
        "name": name,
        "path": path,
        "exists": True,
        "size": os.path.getsize(path),
        "user_version": None,
        "revision": "",
        "revision_head": _expected_revision(),
        "revision_behind": 0,
        "tables": [],
        "missing_tables": [],
        "state": "ok",
    }
    conn = _readonly(path)
    if conn is None:
        info["state"] = "unreadable"
        return info
    try:
        info["user_version"] = _scalar(conn, "PRAGMA user_version")
        info["journal_mode"] = str(_scalar(conn, "PRAGMA journal_mode") or "")
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        info["tables"] = [str(row[0]) for row in rows if row and row[0]]
        info["missing_tables"] = [t for t in REQUIRED_TABLES
                                  if not _table_exists(conn, t)]
    finally:
        try:
            conn.close()
        except Exception:  # pragma: no cover - closing is best effort
            pass
    _fill_revision(info)
    if info["missing_tables"]:
        info["state"] = "incomplete"
    elif info.get("revision_behind"):
        info["state"] = "outdated"
    return info


def _expected_revision() -> str:
    """Return the revision this build expects, or ``''`` when unreadable.

    A diagnostic must not create or migrate a database, so the head of the
    chain is read from the revision files alone.
    """
    try:
        from storage import migrations
        return str(migrations.head_revision() or "")
    except Exception:  # pragma: no cover - the chain is part of the install
        return ""


def _fill_revision(info: Dict[str, Any]) -> None:
    """Record the applied revision of one database in *info*.

    ``revision_status`` opens the file read-only; a failure leaves the empty
    values in place, which the summary reports as "not versioned" rather than
    turning a diagnostic into an exception.
    """
    try:
        from storage import migrations
        status = migrations.revision_status(info["path"])
    except Exception:  # pragma: no cover - defensive
        return
    info["revision"] = str(status.get("current") or "")
    info["revision_head"] = str(status.get("head") or "") or info["revision_head"]
    info["revision_behind"] = int(status.get("behind") or 0)
    info["versioned"] = bool(status.get("versioned"))


def check_databases() -> Dict[str, Any]:
    """Report both databases: presence, size, schema and journal mode.

    The main database carries the owner's assistants, settings and the
    marketplace registry; the agent database carries the dialogs. A missing
    file is reported as ``not_initialized`` - the state of a machine that has
    never started the platform - and is a warning, not a failure.
    """
    try:
        from core.paths import DB_PATH, DEVAGENT_DB_PATH
    except Exception as exc:  # pragma: no cover - defensive
        return _check(ERROR, "core.paths is unreadable: %s" % exc)
    main = _database_file("sagaai.db", DB_PATH)
    agent = _database_file("devagent.db", DEVAGENT_DB_PATH)
    items = {"sagaai.db": main, "devagent.db": agent}
    unreadable = [item["name"] for item in items.values()
                  if item["state"] == "unreadable"]
    incomplete = [item["name"] for item in items.values()
                  if item["state"] == "incomplete"]
    outdated = [item["name"] for item in items.values()
                if item["state"] == "outdated"]
    missing = [item["name"] for item in items.values()
               if item["state"] == "not_initialized"]
    if unreadable:
        status = ERROR
        summary = "unreadable: " + ", ".join(unreadable)
    elif incomplete:
        status = WARN
        summary = "schema incomplete: " + ", ".join(incomplete) + \
                  " (run: sagaai migrate)"
    elif outdated:
        status = WARN
        summary = "schema revision behind: " + ", ".join(outdated) + \
                  " (run: sagaai migrate)"
    elif missing:
        status = WARN
        summary = "not initialized: " + ", ".join(missing) + \
                  " (run: sagaai migrate)"
    else:
        status = OK
        summary = "both databases are present and current"
    return _check(status, summary, databases=items,
                  unreadable=unreadable, incomplete=incomplete,
                  outdated=outdated, missing=missing)


def check_schema_revision() -> Dict[str, Any]:
    """Report the schema revision of both databases against the expected one.

    This is the check that turns the history into something an operator can
    read: ``current`` is the revision recorded in the database, ``head`` is
    the newest revision this build ships. Equal means the installation is at
    the schema the code expects; behind means the next start will migrate it.

    A database without ``alembic_version`` is reported as unversioned rather
    than broken: it is an installation created before the history existed, and
    the baseline revision adopts it on the next migration.
    """
    try:
        from core.paths import DB_PATH, DEVAGENT_DB_PATH
    except Exception as exc:  # pragma: no cover - defensive
        return _check(ERROR, "core.paths is unreadable: %s" % exc)
    head = _expected_revision()
    items: Dict[str, Any] = {}
    for name, path in (("sagaai.db", DB_PATH), ("devagent.db", DEVAGENT_DB_PATH)):
        item = _database_file(name, path)
        items[name] = {
            "exists": item["exists"],
            "state": item["state"],
            "current": item.get("revision") or "",
            "head": item.get("revision_head") or head,
            "behind": int(item.get("revision_behind") or 0),
            "versioned": bool(item.get("versioned")),
        }
    behind = [name for name, item in items.items() if item["behind"]]
    unversioned = [name for name, item in items.items()
                   if item["exists"] and not item["versioned"]]
    missing = [name for name, item in items.items() if not item["exists"]]
    if behind:
        status = WARN
        summary = "revision behind head %s: %s (run: sagaai migrate)" % (
            head, ", ".join(behind))
    elif unversioned:
        status = WARN
        summary = ("schema history not adopted yet: " + ", ".join(unversioned)
                   + " (run: sagaai migrate)")
    elif missing:
        status = WARN
        summary = ("schema history not applied yet: " + ", ".join(missing)
                   + " (run: sagaai migrate)")
    else:
        status = OK
        summary = "both databases are at revision %s" % (head or "unknown")
    return _check(status, summary, head=head, databases=items,
                  behind=behind, unversioned=unversioned, missing=missing)


# ─── Permissions and the encryption key ─────────────────────────────────────


def check_permissions() -> Dict[str, Any]:
    """Report whether the process can write where the platform writes.

    The probe writes a tiny temporary file and removes it: an ``os.access``
    check answers a different question (it uses the real uid, ignores an
    ACL-only grant and lies on network filesystems), and a read-only data
    folder produces the least obvious failure of all - settings that silently
    fail to save.
    """
    try:
        from core.paths import DATA_DIR
    except Exception as exc:  # pragma: no cover - defensive
        return _check(ERROR, "core.paths is unreadable: %s" % exc)
    probe = os.path.join(DATA_DIR, ".sagaai_write_probe_%d" % os.getpid())
    writable = False
    error = ""
    try:
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("ok")
        writable = True
    except OSError as exc:
        error = str(exc)
    finally:
        if os.path.exists(probe):
            try:
                os.remove(probe)
            except OSError:
                pass
    status = OK if writable else ERROR
    summary = ("data folder is writable" if writable
               else "data folder is NOT writable: " + error)
    return _check(status, summary, data_dir=DATA_DIR, writable=writable,
                  error=error)


def check_encryption_key() -> Dict[str, Any]:
    """Report where the Fernet key comes from and whether it is protected.

    Three sources, in the order ``core.crypto`` resolves them: the
    environment variable, the key file, or a new key that will be generated on
    the first write. The last one is a warning for a reason: a key that does
    not exist yet means every secret saved so far is unreadable, which happens
    when the key file was moved or lost.
    """
    env_name = "SAGAAI_ENCRYPTION_KEY"
    file_env = "SAGAAI_KEY_FILE"
    try:
        from core.crypto import get_key_file_path
        key_path = str(get_key_file_path())
    except Exception as exc:  # pragma: no cover - defensive
        return _check(ERROR, "the key path is unavailable: %s" % exc)
    from_env = bool(str(os.environ.get(env_name) or "").strip())
    exists = os.path.isfile(key_path)
    source = "env" if from_env else ("file" if exists else "missing")
    size = os.path.getsize(key_path) if exists else 0
    mode = ""
    mode_ok = True
    if exists and os.name == "posix":
        try:
            mode = oct(os.stat(key_path).st_mode & 0o777)
            mode_ok = (os.stat(key_path).st_mode & 0o077) == 0
        except OSError:
            mode = ""
    if source == "missing":
        status = WARN
        summary = ("no key yet: a new one is generated on the first secret write"
                   " (secrets saved with a lost key cannot be decrypted)")
    elif not mode_ok:
        status = WARN
        summary = "key file is readable by other users (mode %s, expected 0600)" % mode
    else:
        status = OK
        summary = "key source: " + source
    return _check(status, summary, source=source, path=key_path,
                  exists=exists, size=size, mode=mode, mode_ok=mode_ok,
                  env_var=env_name, env_file_var=file_env)


# ─── API tokens ─────────────────────────────────────────────────────────────


def _read_config_kv() -> Dict[str, Any]:
    """Read the settings table in READ-ONLY mode; {} when it is absent.

    ``core.config.load_config`` and ``core.api_token_store`` both go through
    ``storage.repository``, whose import builds the SQLAlchemy engines and
    therefore CREATES and migrates the database. A diagnostic that did that
    would report a state it had just invented. The settings row is a plain
    JSON value per key, so reading it directly keeps the probe honest.
    """
    try:
        from core.paths import DB_PATH
    except Exception:  # pragma: no cover - defensive
        return {}
    conn = _readonly(DB_PATH)
    if conn is None:
        return {}
    try:
        if not _table_exists(conn, "config_kv"):
            return {}
        rows = conn.execute("SELECT key, value FROM config_kv").fetchall()
    except Exception:  # pragma: no cover - defensive
        return {}
    finally:
        try:
            conn.close()
        except Exception:  # pragma: no cover - closing is best effort
            pass
    import json
    payload: Dict[str, Any] = {}
    for key, raw in rows:
        try:
            payload[str(key)] = json.loads(raw)
        except (TypeError, ValueError):
            payload[str(key)] = raw
    return payload


def check_tokens() -> Dict[str, Any]:
    """Report the API switch, the bind address and the token inventory.

    The check answers one question a caller cannot answer from outside: is the
    API usable AT ALL? ``enabled`` without a usable token is a switched-on
    service that refuses every request, and a token without ``enabled`` is the
    reverse. Either state is a warning with a concrete next step.

    The record SHAPES and the expiry rule come from ``core.api_token_store``
    (pure functions with no storage access); only the read itself is done
    here, because the store's own readers would create the database.
    """
    try:
        from core import api_token_store as store
    except Exception as exc:  # pragma: no cover - defensive
        return _check(ERROR, "the token store is unreadable: %s" % exc)
    config = _read_config_kv()
    raw_records = config.get(store.CONFIG_KEY)
    if isinstance(raw_records, str):
        import json
        try:
            raw_records = json.loads(raw_records)
        except ValueError:
            raw_records = []
    records = [dict(item) for item in raw_records or [] if isinstance(item, dict)]
    tokens = [store.public_view(record) for record in records]
    enabled = bool(config.get(store.ENABLED_KEY, False))
    expired = [item for item in tokens if item.get("expired")]
    usable = [item for item in tokens if not item.get("expired")]
    if enabled and usable:
        status = OK
        summary = "API is enabled with %d usable token(s)" % len(usable)
    elif enabled and not usable:
        status = WARN
        summary = "API is enabled but no usable token exists (run: sagaai token create)"
    elif not enabled and usable:
        status = WARN
        summary = "tokens exist but the API is switched off (turn it on in Access)"
    else:
        status = OK
        summary = "API is switched off and no token exists"
    return _check(status, summary, enabled=enabled,
                  available=bool(enabled) and bool(usable),
                  host=str(config.get(store.HOST_KEY) or "127.0.0.1"),
                  port=int(config.get(store.PORT_KEY) or 8765),
                  allow_remote=bool(config.get(store.REMOTE_KEY, False)),
                  token_count=len(tokens), expired_count=len(expired),
                  tokens=tokens)


# ─── Scheduler ──────────────────────────────────────────────────────────────


def check_scheduler() -> Dict[str, Any]:
    """Report the schedule rules and the age of the scheduler heartbeat.

    The scheduler is an in-process thread of the UI: it fires while the
    interface is open and dies with it. A stale heartbeat is therefore the
    honest description of "no scheduled run will fire right now", not a
    failure - and the report says exactly that.
    """
    rules = 0
    error = ""
    try:
        from core.paths import DB_PATH
        conn = _readonly(DB_PATH)
        if conn is not None:
            try:
                if _table_exists(conn, "schedules"):
                    rules = int(_scalar(conn, "SELECT COUNT(*) FROM schedules", 0) or 0)
            finally:
                conn.close()
    except Exception as exc:  # pragma: no cover - defensive
        error = str(exc)
    heartbeat_age: Optional[float] = None
    heartbeat_at = ""
    try:
        # The heartbeat file lives under the orchestration store of the
        # INSTALLATION. Its path is computed from ``core.paths.BASE_DIR``
        # instead of imported: both ``core.schedules.daemon`` and the run
        # store pull in modules whose import creates the database this probe
        # is inspecting, and a diagnostic must not change what it measures.
        from core.paths import BASE_DIR
        stamp_path = os.path.join(BASE_DIR, ".dev_agent", "orchestration",
                                  "scheduler", "heartbeat")
        with open(stamp_path, "r", encoding="utf-8") as handle:
            stamp = int(str(handle.read() or "0").strip() or 0)
        if stamp:
            heartbeat_age = max(0.0, time.time() - float(stamp))
            heartbeat_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(stamp)))
    except (OSError, ValueError):
        heartbeat_age = None
    except Exception as exc:  # pragma: no cover - defensive
        error = error or str(exc)
    alive = heartbeat_age is not None and heartbeat_age <= 60.0
    if error:
        status = WARN
        summary = "scheduler state is partially unavailable: " + error
    elif rules == 0:
        status = OK
        summary = "no schedules are configured"
    elif alive:
        status = OK
        summary = "%d schedule(s); the scheduler is running" % rules
    else:
        status = WARN
        summary = ("%d schedule(s) will not fire: the scheduler runs only while"
                   " the interface is open" % rules)
    return _check(status, summary, rules=rules, alive=alive,
                  heartbeat_at=heartbeat_at, heartbeat_age_s=heartbeat_age)


# ─── Aggregate ──────────────────────────────────────────────────────────────


#: The checks a report always runs, in the order the CLI prints them.
CHECKS = (
    ("version", check_version),
    ("python", check_python),
    ("paths", check_paths),
    ("dependencies", check_dependencies),
    ("databases", check_databases),
    ("schema", check_schema_revision),
    ("permissions", check_permissions),
    ("encryption_key", check_encryption_key),
    ("tokens", check_tokens),
    ("scheduler", check_scheduler),
)


def doctor_report() -> Dict[str, Any]:
    """Run every check and return one report.

    ``status`` is ``error`` when any check failed, ``warn`` when any check
    warned and ``ok`` otherwise; ``exit_ok`` is the single boolean a transport
    turns into an exit code, so the rule lives here and not in each caller.
    """
    checks: Dict[str, Any] = {}
    for name, probe in CHECKS:
        try:
            checks[name] = probe()
        except Exception as exc:  # pragma: no cover - a probe never breaks the report
            checks[name] = _check(ERROR, "check crashed: %s" % exc)
    errors = [name for name, item in checks.items() if item.get("status") == ERROR]
    warnings = [name for name, item in checks.items() if item.get("status") == WARN]
    if errors:
        status = ERROR
    elif warnings:
        status = WARN
    else:
        status = OK
    return {
        "status": status,
        "exit_ok": not errors,
        "version": _version(),
        "errors": errors,
        "warnings": warnings,
        "checks": checks,
    }
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
