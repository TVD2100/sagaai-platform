# -*- coding: utf-8 -*-
"""core/dependency_check.py - runtime dependency registry and version audit.

The platform ships a requirements.txt, but a user may run it inside an older
virtualenv or forget to install the optional document/SSH helpers. On the
first cold start the UI calls :func:`check_dependencies` and warns about
packages that are missing or older than the supported minimum, so the user
can run ``pip install -r requirements.txt`` (or ``pip install -U <pkg>``)
before hitting a confusing ImportError deep inside a feature.

Public API:
  DEPENDENCIES          - registry: import name -> {pip, min, required}
  check_dependencies()  - -> {"ok": bool, "missing": [...], "outdated": [...]}
  parse_version(v)      - version string -> comparable int tuple
  version_lt(a, b)      - True when a < b (numeric component comparison)
  missing_packages(r)   - pip names of the missing entries
  outdated_lines(r)     - "pkg installed -> required" strings

The module is stdlib-only and never imports the audited packages itself: it
probes ``importlib.util.find_spec`` and the installed distribution metadata,
so a broken or absent package cannot make importing this module fail.
"""
import importlib.util
from importlib import metadata as importlib_metadata


# import name -> {"pip": distribution name, "min": minimum version, "required": bool}
# Mirrors requirements.txt; the consistency test keeps both in sync.
DEPENDENCIES = {
    # Core runtime (the app cannot start without these).
    "streamlit":    {"pip": "streamlit",    "min": "1.66.0", "required": True},
    "requests":     {"pip": "requests",     "min": "2.28",   "required": True},
    "sqlalchemy":   {"pip": "SQLAlchemy",   "min": "2.0",    "required": True},
    "cryptography": {"pip": "cryptography", "min": "41.0",   "required": True},
    # Optional document parsing and utilities.
    "pypdf":        {"pip": "pypdf",        "min": "3.0",    "required": False},
    "docx":         {"pip": "python-docx",  "min": "0.8",    "required": False},
    "pptx":         {"pip": "python-pptx",  "min": "0.6",    "required": False},
    "openpyxl":     {"pip": "openpyxl",     "min": "3.1",    "required": False},
    "pdfplumber":   {"pip": "pdfplumber",   "min": "0.6",    "required": False},
    "tiktoken":     {"pip": "tiktoken",     "min": "0.5",    "required": False},
    "markdown":     {"pip": "markdown",     "min": "3.0",    "required": False},
    "reportlab":    {"pip": "reportlab",    "min": "4.0",    "required": False},
    # Optional Selenium rendering of web pages (url_fetch tool).
    "selenium":     {"pip": "selenium",     "min": "4.10",   "required": False},
    # Optional SSH connector.
    "paramiko":     {"pip": "paramiko",     "min": "3.0",    "required": False},
}


# Developer-only packages (not audited at runtime; listed for the consistency test).
DEV_DEPENDENCIES = {
    "pytest": {"pip": "pytest", "min": "7.0", "required": False},
}


def parse_version(value):
    """Return a comparable tuple of leading integer components of *value*.

    "1.66.0" -> (1, 66, 0); "2.28" -> (2, 28); pre-release/build suffixes
    ("1.2.3rc1", "2.0.0.post1") are truncated at the first non-digit; an
    unparsable value yields an empty tuple.
    """
    if not isinstance(value, str):
        return ()
    parts = []
    for chunk in value.strip().split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def version_lt(a, b):
    """True when version *a* is strictly older than version *b*.

    Missing components are padded with zeros, so ``1.66`` == ``1.66.0``.
    """
    ta, tb = parse_version(a), parse_version(b)
    if not ta or not tb:
        return False
    n = max(len(ta), len(tb))
    ta = ta + (0,) * (n - len(ta))
    tb = tb + (0,) * (n - len(tb))
    return ta < tb


def _installed_version(pip_name):
    """Return the installed distribution version, or None when unknown."""
    try:
        return importlib_metadata.version(pip_name)
    except Exception:
        return None


def _importable(import_name):
    """Return True when *import_name* can be found by the import system."""
    try:
        return importlib.util.find_spec(import_name) is not None
    except Exception:
        return False


def check_dependencies(registry=None):
    """Audit installed dependencies against the registry.

    Returns ``{"ok": bool, "missing": [...], "outdated": [...]}``:
      * ``missing``  - ``{"package": pip_name, "required": bool}`` when the
        distribution is neither importable nor present in metadata;
      * ``outdated`` - ``{"package", "installed", "required"}`` when the
        installed version is older than the registry minimum.

    ``registry`` defaults to :data:`DEPENDENCIES`; pass a custom mapping in
    tests. Any probing error is treated conservatively (package skipped, not
    reported) so a broken install never blocks the UI.
    """
    deps = DEPENDENCIES if registry is None else registry
    missing = []
    outdated = []
    for import_name, spec in deps.items():
        pip_name = spec.get("pip", import_name)
        required_min = spec.get("min")
        installed = _installed_version(pip_name)
        if installed is None and not _importable(import_name):
            missing.append({
                "package": pip_name,
                "required": bool(spec.get("required")),
            })
            continue
        if installed and required_min and version_lt(installed, required_min):
            outdated.append({
                "package": pip_name,
                "installed": installed,
                "required": required_min,
            })
    missing.sort(key=lambda d: d["package"].lower())
    outdated.sort(key=lambda d: d["package"].lower())
    return {"ok": not missing and not outdated,
            "missing": missing,
            "outdated": outdated}


def missing_packages(report):
    """Pip names of the missing entries of a :func:`check_dependencies` report."""
    return [d.get("package", "") for d in (report or {}).get("missing", [])]


def outdated_lines(report):
    """'package installed -> required' strings of an outdated report."""
    return [
        "%s %s -> %s" % (d.get("package", ""),
                          d.get("installed", "?"), d.get("required", "?"))
        for d in (report or {}).get("outdated", [])
    ]
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
