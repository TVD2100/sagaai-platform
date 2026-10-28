"""
core.marketplace.semver - Semantic Versioning 2.0.0 parser and range matcher.

The platform already ships ``core/updater.py`` with a version comparison
helper, but that helper is unsuitable for catalog versions: it compares
pre-release identifiers lexicographically (``rc.10`` sorts below ``rc.9``)
and raises ``ValueError`` when a build suffix is present. The marketplace
needs strict SemVer ordering, so this module implements it independently.

Public API:
  * ``parse(text) -> Version``        - strict parse, raises ``SemverError``
  * ``compare(a, b) -> int``          - -1 / 0 / 1, accepts str or Version
  * ``satisfies(version, spec)``      - range match
  * ``intersect(spec_a, spec_b)``     - combined range or ``None``

Build metadata is ignored for ordering (SemVer 2.0.0, rule 10).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple, Union


class SemverError(ValueError):
    """Raised when a version or a range expression cannot be parsed."""


_VERSION_RE = re.compile(
    r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z.-]+))?"
    r"(?:\+([0-9A-Za-z.-]+))?$"
)

_PARTIAL_RE = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?$")

_IDENT_RE = re.compile(r"^[0-9A-Za-z-]+$")


@dataclass(frozen=True)
class Version:
    """A parsed semantic version.

    ``prerelease`` holds the dot-separated identifiers of the pre-release
    part (empty tuple when absent); numeric identifiers are kept as ``int``
    so that ``rc.10`` correctly compares above ``rc.9``. ``build`` is stored
    for round-tripping only and never participates in ordering.
    """

    major: int
    minor: int
    patch: int
    prerelease: Tuple[Union[int, str], ...] = ()
    build: str = ""

    def __str__(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            base += "-" + ".".join(str(p) for p in self.prerelease)
        if self.build:
            base += "+" + self.build
        return base

    @property
    def is_prerelease(self) -> bool:
        """True when the version carries pre-release identifiers."""
        return bool(self.prerelease)


def _split_identifiers(text: str, *, allow_leading_zero: bool) -> Tuple[Union[int, str], ...]:
    """Split a pre-release/build string into identifiers.

    Numeric identifiers whose canonical form has a leading zero are
    rejected for pre-release parts (SemVer rule 9); build metadata is
    compared by text only, so the same rule is relaxed there.
    """
    parts: List[Union[int, str]] = []
    for raw in text.split("."):
        if not raw or not _IDENT_RE.match(raw):
            raise SemverError(f"Invalid version identifier: {raw!r}")
        if raw.isdigit():
            if not allow_leading_zero and len(raw) > 1 and raw.startswith("0"):
                raise SemverError(f"Numeric identifier with leading zero: {raw!r}")
            parts.append(int(raw))
        else:
            parts.append(raw)
    return tuple(parts)


def parse(text: Union[str, Version]) -> Version:
    """Parse *text* into a :class:`Version`.

    Raises ``SemverError`` for anything that is not a complete
    ``MAJOR.MINOR.PATCH[-pre][+build]`` string. A ``Version`` instance is
    returned unchanged, which lets callers accept both forms.
    """
    if isinstance(text, Version):
        return text
    if not isinstance(text, str):
        raise SemverError(f"Version must be a string, got {type(text).__name__}")
    match = _VERSION_RE.match(text.strip())
    if not match:
        raise SemverError(f"Invalid version: {text!r}")
    major, minor, patch, pre, build = match.groups()
    prerelease: Tuple[Union[int, str], ...] = ()
    if pre:
        prerelease = _split_identifiers(pre, allow_leading_zero=False)
    return Version(int(major), int(minor), int(patch), prerelease, build or "")


def try_parse(text: Union[str, Version]) -> Optional[Version]:
    """Parse *text*, returning ``None`` instead of raising on bad input."""
    try:
        return parse(text)
    except SemverError:
        return None


def _compare_identifier(left: Union[int, str], right: Union[int, str]) -> int:
    """Compare two pre-release identifiers per SemVer rule 11.

    Numeric identifiers always rank below alphanumeric ones; two numeric
    identifiers compare numerically; two alphanumeric ones compare by
    ASCII order.
    """
    left_is_num = isinstance(left, int)
    right_is_num = isinstance(right, int)
    if left_is_num and right_is_num:
        return (left > right) - (left < right)
    if left_is_num:
        return -1
    if right_is_num:
        return 1
    return (str(left) > str(right)) - (str(left) < str(right))


def compare(left: Union[str, Version], right: Union[str, Version]) -> int:
    """Return -1, 0 or 1 comparing two versions.

    A version with a pre-release part ranks below the same version without
    one (``1.0.0-rc.1 < 1.0.0``). Build metadata is ignored.
    """
    a = parse(left)
    b = parse(right)
    for field in ("major", "minor", "patch"):
        av, bv = getattr(a, field), getattr(b, field)
        if av != bv:
            return (av > bv) - (av < bv)
    if not a.prerelease and not b.prerelease:
        return 0
    if not a.prerelease:
        return 1
    if not b.prerelease:
        return -1
    for ai, bi in zip(a.prerelease, b.prerelease):
        result = _compare_identifier(ai, bi)
        if result:
            return result
    return (len(a.prerelease) > len(b.prerelease)) - (len(a.prerelease) < len(b.prerelease))


@dataclass(frozen=True)
class _Bound:
    """One comparison inside a range expression."""

    op: str
    version: Version

    def matches(self, candidate: Version) -> bool:
        """True when *candidate* satisfies this single comparison."""
        result = compare(candidate, self.version)
        if self.op == ">=":
            return result >= 0
        if self.op == ">":
            return result > 0
        if self.op == "<=":
            return result <= 0
        if self.op == "<":
            return result < 0
        return result == 0

    def __str__(self) -> str:
        return f"{self.op}{self.version}"


def _partial_to_version(text: str) -> Version:
    """Turn a possibly partial version (``1``, ``1.2``) into a full one.

    A complete version - including a pre-release part - is parsed as-is so
    that ranges like ``>=1.3.0-alpha <2.0.0`` keep their pre-release bound.
    """
    if _VERSION_RE.match(text):
        return parse(text)
    match = _PARTIAL_RE.match(text)
    if not match:
        raise SemverError(f"Invalid range version: {text!r}")
    major, minor, patch = match.groups()
    return Version(int(major), int(minor or 0), int(patch or 0))


def _expand_bound(op: str, raw_version: str) -> List[_Bound]:
    """Expand a caret/tilde bound into plain comparison bounds.

    ``^1.2.3`` allows changes that do not modify the left-most non-zero
    component, ``~1.2.3`` allows patch-level changes only. Partial caret
    versions (``^1``, ``^0.2``) follow the same rule.
    """
    match = _PARTIAL_RE.match(raw_version)
    if not match:
        raise SemverError(f"Invalid range version: {raw_version!r}")
    major_s, minor_s, patch_s = match.groups()
    major = int(major_s)
    minor = int(minor_s or 0)
    patch = int(patch_s or 0)
    base = Version(major, minor, patch)

    if op == "~":
        upper = Version(major, minor + 1, 0) if minor_s is not None else Version(major + 1, 0, 0)
        return [_Bound(">=", base), _Bound("<", upper)]

    # Caret: the first non-zero component is frozen.
    if major > 0 or (minor_s is None):
        upper = Version(major + 1, 0, 0)
    elif minor > 0 or (patch_s is None):
        upper = Version(0, minor + 1, 0)
    else:
        upper = Version(0, 0, patch + 1)
    return [_Bound(">=", base), _Bound("<", upper)]


def _parse_spec(spec: str) -> List[List[_Bound]]:
    """Parse a range expression into alternatives of comparison bounds.

    ``||`` separates alternatives (any match wins); whitespace separates
    comparisons that must all hold. A bare version means exact equality.
    """
    if spec is None:
        raise SemverError("Range expression is required")
    text = str(spec).strip()
    if not text or text == "*":
        return [[]]

    alternatives: List[List[_Bound]] = []
    for chunk in text.split("||"):
        chunk = chunk.strip()
        bounds: List[_Bound] = []
        if not chunk or chunk == "*":
            alternatives.append(bounds)
            continue
        for token in re.split(r"\s+", chunk):
            if token in ("*", "x", "X"):
                continue
            match = re.match(r"^(>=|<=|>|<|=|\^|~)?(.+)$", token)
            if not match:
                raise SemverError(f"Invalid range token: {token!r}")
            op, raw = match.group(1) or "=", match.group(2).strip()
            if raw.endswith(".*"):
                raw = raw[:-2]
            if op in ("^", "~"):
                bounds.extend(_expand_bound(op, raw))
            elif op == "=":
                if _VERSION_RE.match(raw):
                    bounds.append(_Bound("=", parse(raw)))
                else:
                    bounds.extend(_expand_bound("~", raw))
            else:
                bounds.append(_Bound(op, _partial_to_version(raw)))
        alternatives.append(bounds)
    return alternatives


def satisfies(version: Union[str, Version], spec: str) -> bool:
    """Return True when *version* falls inside the range *spec*.

    Supported range forms: ``>=1.2.0 <2.0.0``, ``^1.2.3``, ``~1.2``,
    ``1.2.3`` (exact), ``*`` (any), with ``||`` separating alternatives.
    A pre-release candidate only matches a spec that explicitly mentions a
    pre-release of the same ``major.minor.patch`` tuple, which mirrors the
    npm behaviour and prevents an accidental update to an unstable build.
    """
    candidate = parse(version)
    alternatives = _parse_spec(spec)
    for bounds in alternatives:
        if not bounds:
            if not candidate.is_prerelease:
                return True
            continue
        if not all(b.matches(candidate) for b in bounds):
            continue
        if candidate.is_prerelease:
            mentioned = any(
                b.version.is_prerelease
                and (b.version.major, b.version.minor, b.version.patch)
                == (candidate.major, candidate.minor, candidate.patch)
                for b in bounds
            )
            if not mentioned:
                continue
        return True
    return False


def intersect(spec_a: str, spec_b: str) -> Optional[str]:
    """Return a spec that satisfies both inputs, or ``None`` if empty.

    The result is a whitespace-joined conjunction of the bounds of both
    specs. When either input has several ``||`` alternatives, every pair is
    combined and the first non-empty pair wins; this keeps the result
    readable without introducing a full SAT-style normalisation.
    """
    alts_a = _parse_spec(spec_a)
    alts_b = _parse_spec(spec_b)
    for bounds_a in alts_a:
        for bounds_b in alts_b:
            combined = list(bounds_a) + list(bounds_b)
            if _has_candidate(combined):
                if not combined:
                    return "*"
                return " ".join(str(b) for b in combined)
    return None


def _has_candidate(bounds: Sequence[_Bound]) -> bool:
    """Cheap emptiness check: probe the bound versions and their edges.

    A range built from ``>=``/``<`` bounds is empty only when the highest
    lower bound is not strictly below the lowest upper bound, so probing the
    bound versions themselves plus a few neighbours is sufficient in
    practice for the ranges the catalog produces.
    """
    if not bounds:
        return True
    probe: List[Version] = []
    for bound in bounds:
        probe.append(bound.version)
        probe.append(Version(bound.version.major, bound.version.minor, bound.version.patch + 1))
        if bound.version.patch > 0:
            probe.append(Version(bound.version.major, bound.version.minor, bound.version.patch - 1))
    return any(all(b.matches(v) for b in bounds) for v in probe)


def max_satisfying(versions: Sequence[str], spec: str) -> Optional[str]:
    """Return the highest version from *versions* that satisfies *spec*."""
    best: Optional[Version] = None
    best_raw = None
    for raw in versions:
        try:
            candidate = parse(raw)
        except SemverError:
            continue
        if not satisfies(candidate, spec):
            continue
        if best is None or compare(candidate, best) > 0:
            best, best_raw = candidate, raw
    return best_raw
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
