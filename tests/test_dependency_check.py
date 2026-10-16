# -*- coding: utf-8 -*-
"""tests/test_dependency_check.py - runtime dependency audit.

Covers core.dependency_check: version parsing/comparison, the registry
contract and the check report with a stubbed importlib.metadata so missing,
outdated and up-to-date packages are all exercised deterministically.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core.dependency_check as dc  # noqa: E402


# ── parse_version / version_lt ───────────────────────────────────────────────

def test_parse_version_components():
    assert dc.parse_version("1.66.0") == (1, 66, 0)
    assert dc.parse_version("2.28") == (2, 28)
    assert dc.parse_version("3") == (3,)


def test_parse_version_truncates_suffixes():
    """Pre-release/build suffixes are truncated at the first non-digit."""
    assert dc.parse_version("1.2.3rc1") == (1, 2, 3)
    assert dc.parse_version("2.0.0.post1") == (2, 0, 0)


def test_parse_version_unparsable_is_empty():
    assert dc.parse_version("foo") == ()
    assert dc.parse_version("") == ()
    assert dc.parse_version(None) == ()


def test_version_lt_basic_ordering():
    assert dc.version_lt("1.65.0", "1.66.0") is True
    assert dc.version_lt("1.66.0", "1.66.0") is False
    assert dc.version_lt("1.67.0", "1.66.0") is False


def test_version_lt_pads_missing_components():
    """1.66 == 1.66.0 - the shorter form is not 'older'."""
    assert dc.version_lt("1.66", "1.66.0") is False
    assert dc.version_lt("1.66", "1.66.1") is True


def test_version_lt_unparsable_is_false():
    """An unusable version never claims to be outdated."""
    assert dc.version_lt("weird", "1.0") is False
    assert dc.version_lt("1.0", "weird") is False


# ── registry contract ────────────────────────────────────────────────────────

def test_registry_has_expected_shape():
    assert dc.DEPENDENCIES, "registry must not be empty"
    for import_name, spec in dc.DEPENDENCIES.items():
        assert isinstance(import_name, str) and import_name
        assert isinstance(spec.get("pip"), str) and spec["pip"]
        assert isinstance(spec.get("min"), str) and spec["min"]
        assert isinstance(spec.get("required"), bool)


def test_streamlit_is_required_with_min_1_66():
    """The chat input inline upload needs Streamlit >= 1.66."""
    spec = dc.DEPENDENCIES["streamlit"]
    assert spec["required"] is True
    assert spec["min"] == "1.66.0"


def test_core_runtime_packages_are_required():
    for name in ("streamlit", "requests", "sqlalchemy", "cryptography"):
        assert dc.DEPENDENCIES[name]["required"] is True, name


def test_doc_and_ssh_packages_are_optional():
    for name in ("docx", "pptx", "openpyxl", "pypdf", "pdfplumber",
                 "markdown", "reportlab", "paramiko", "tiktoken"):
        assert dc.DEPENDENCIES[name]["required"] is False, name


# ── check_dependencies ───────────────────────────────────────────────────────

def _stub_versions(monkeypatch, mapping):
    """Patch importlib.metadata.version to answer from *mapping* only."""
    def _version(pip_name):
        if pip_name in mapping:
            return mapping[pip_name]
        raise dc.importlib_metadata.PackageNotFoundError(pip_name)
    monkeypatch.setattr(dc.importlib_metadata, "version", _version)


def test_check_all_up_to_date(monkeypatch):
    registry = {"foo": {"pip": "foo", "min": "1.0", "required": True}}
    _stub_versions(monkeypatch, {"foo": "1.5"})
    report = dc.check_dependencies(registry)
    assert report["ok"] is True
    assert report["missing"] == []
    assert report["outdated"] == []


def test_check_detects_outdated(monkeypatch):
    registry = {"foo": {"pip": "foo", "min": "2.0", "required": True}}
    _stub_versions(monkeypatch, {"foo": "1.9"})
    report = dc.check_dependencies(registry)
    assert report["ok"] is False
    assert report["missing"] == []
    assert report["outdated"] == [
        {"package": "foo", "installed": "1.9", "required": "2.0"}
    ]


def test_check_detects_missing(monkeypatch):
    registry = {"foo": {"pip": "foo", "min": "1.0", "required": False}}
    _stub_versions(monkeypatch, {})
    # No metadata AND not importable (find_spec stub returns None).
    monkeypatch.setattr(dc, "_importable", lambda name: False)
    report = dc.check_dependencies(registry)
    assert report["ok"] is False
    assert report["missing"] == [{"package": "foo", "required": False}]
    assert report["outdated"] == []


def test_importable_without_metadata_is_not_missing(monkeypatch):
    """An editable/dev install may lack metadata but still import."""
    registry = {"foo": {"pip": "foo", "min": "1.0", "required": True}}
    _stub_versions(monkeypatch, {})
    monkeypatch.setattr(dc, "_importable", lambda name: True)
    report = dc.check_dependencies(registry)
    assert report["missing"] == []
    # Without a known version it also must not be flagged as outdated.
    assert report["outdated"] == []
    assert report["ok"] is True


def test_check_report_is_sorted(monkeypatch):
    registry = {
        "zed": {"pip": "zed", "min": "1.0", "required": False},
        "abc": {"pip": "abc", "min": "1.0", "required": False},
    }
    _stub_versions(monkeypatch, {})
    monkeypatch.setattr(dc, "_importable", lambda name: False)
    report = dc.check_dependencies(registry)
    assert [m["package"] for m in report["missing"]] == ["abc", "zed"]


def test_missing_packages_helper():
    report = {"missing": [{"package": "a", "required": True},
                          {"package": "b", "required": False}]}
    assert dc.missing_packages(report) == ["a", "b"]
    assert dc.missing_packages(None) == []


def test_outdated_lines_helper():
    report = {"outdated": [{"package": "a", "installed": "1.0", "required": "2.0"}]}
    assert dc.outdated_lines(report) == ["a 1.0 -> 2.0"]
    assert dc.outdated_lines({}) == []


def test_default_registry_returns_report_shape(monkeypatch):
    """check_dependencies() with no args audits the real registry."""
    _stub_versions(monkeypatch, {})
    monkeypatch.setattr(dc, "_importable", lambda name: True)
    report = dc.check_dependencies()
    assert set(report) == {"ok", "missing", "outdated"}
    assert report["ok"] is True


# ── consistency with requirements.txt ────────────────────────────────────────

def _parse_requirements():
    """Return {pip_name_lower: specifier} from the project requirements.txt."""
    root = Path(__file__).resolve().parent.parent
    entries = {}
    for raw in (root / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        for sep in (">=", "==", "<=", "~=", ">", "<", "!="):
            if sep in line:
                name, _, ver = line.partition(sep)
                entries[name.strip().lower()] = sep + ver.strip()
                break
        else:
            entries[line.lower()] = ""
    return entries


def test_requirements_pins_streamlit_minimum_not_exact():
    """streamlit must be a >= minimum pin, not an exact == pin."""
    reqs = _parse_requirements()
    assert "streamlit" in reqs
    assert reqs["streamlit"] == ">=1.66.0"


def test_registry_matches_requirements():
    """Every runtime registry package is declared in requirements.txt with
    the same minimum version."""
    reqs = _parse_requirements()
    for spec in dc.DEPENDENCIES.values():
        pip_lower = spec["pip"].lower()
        assert pip_lower in reqs, f"{spec['pip']} missing from requirements.txt"
        assert reqs[pip_lower] == ">=" + spec["min"], (
            f"{spec['pip']}: requirements {reqs[pip_lower]!r} "
            f"!= registry >= {spec['min']}"
        )


def test_dev_dependencies_match_requirements():
    reqs = _parse_requirements()
    for spec in dc.DEV_DEPENDENCIES.values():
        pip_lower = spec["pip"].lower()
        assert pip_lower in reqs, f"{spec['pip']} missing from requirements.txt"
        assert reqs[pip_lower] == ">=" + spec["min"]
