"""
test_marketplace_semver.py - SemVer 2.0.0 parsing, ordering and ranges.

The catalog relies on strict SemVer: the platform helper in core.updater
compares pre-release identifiers lexicographically (``rc.10 < rc.9``) and
raises on a build suffix, so the marketplace ships its own implementation.
These tests pin the behaviours that helper gets wrong.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.marketplace import semver


class TestParsing:
    def test_parses_full_version(self):
        parsed = semver.parse("1.2.3")
        assert (parsed.major, parsed.minor, parsed.patch) == (1, 2, 3)
        assert parsed.prerelease == ()
        assert parsed.build == ""

    def test_parses_prerelease_and_build(self):
        parsed = semver.parse("1.0.0-rc.1+build.5")
        assert parsed.prerelease == ("rc", 1)
        assert parsed.build == "build.5"
        assert str(parsed) == "1.0.0-rc.1+build.5"

    def test_strips_leading_v(self):
        assert semver.parse("v2.0.0").major == 2

    @pytest.mark.parametrize("bad", ["1.0", "1", "", "a.b.c", "1.0.0.0", "01.0.0", "1.0.0-"])
    def test_rejects_invalid(self, bad):
        with pytest.raises(semver.SemverError):
            semver.parse(bad)

    def test_try_parse_returns_none(self):
        assert semver.try_parse("nonsense") is None
        assert semver.try_parse("1.2.3") is not None

    def test_accepts_version_object(self):
        parsed = semver.parse("1.0.0")
        assert semver.parse(parsed) is parsed


class TestOrdering:
    def test_major_minor_patch(self):
        assert semver.compare("1.0.0", "2.0.0") < 0
        assert semver.compare("2.1.0", "2.0.0") > 0
        assert semver.compare("1.2.3", "1.2.3") == 0

    def test_prerelease_ranks_below_release(self):
        assert semver.compare("1.0.0-rc.1", "1.0.0") < 0
        assert semver.compare("1.0.0", "1.0.0-rc.1") > 0

    def test_numeric_prerelease_identifiers_compare_numerically(self):
        # The platform's helper gets this wrong: rc.10 must beat rc.9.
        assert semver.compare("1.0.0-rc.9", "1.0.0-rc.10") < 0
        assert semver.compare("1.0.0-rc.10", "1.0.0-rc.9") > 0

    def test_more_identifiers_win_when_prefix_equal(self):
        assert semver.compare("1.0.0-alpha", "1.0.0-alpha.1") < 0

    def test_alpha_beta_ordering(self):
        assert semver.compare("1.0.0-alpha.beta", "1.0.0-beta") < 0

    def test_numeric_identifiers_rank_below_alphanumeric(self):
        assert semver.compare("1.0.0-1", "1.0.0-alpha") < 0

    def test_build_metadata_is_ignored(self):
        assert semver.compare("1.0.0+build1", "1.0.0+build2") == 0
        assert semver.compare("1.0.0+x", "1.0.0") == 0


class TestRanges:
    def test_exact_match(self):
        assert semver.satisfies("1.2.3", "1.2.3")
        assert not semver.satisfies("1.2.4", "1.2.3")

    def test_comparison_conjunction(self):
        assert semver.satisfies("1.5.0", ">=1.2.0 <2.0.0")
        assert not semver.satisfies("2.0.0", ">=1.2.0 <2.0.0")
        assert not semver.satisfies("1.1.9", ">=1.2.0 <2.0.0")

    def test_caret_allows_minor_and_patch(self):
        assert semver.satisfies("1.2.9", "^1.2.3")
        assert semver.satisfies("1.9.0", "^1.2.3")
        assert not semver.satisfies("2.0.0", "^1.2.3")

    def test_caret_on_zero_major_freezes_minor(self):
        assert semver.satisfies("0.2.5", "^0.2.3")
        assert not semver.satisfies("0.3.0", "^0.2.3")

    def test_caret_on_zero_zero_freezes_patch(self):
        assert semver.satisfies("0.0.3", "^0.0.3")
        assert not semver.satisfies("0.0.4", "^0.0.3")

    def test_tilde_allows_patch_only(self):
        assert semver.satisfies("1.2.9", "~1.2.3")
        assert not semver.satisfies("1.3.0", "~1.2.3")

    def test_wildcard_and_empty(self):
        assert semver.satisfies("9.9.9", "*")
        assert semver.satisfies("9.9.9", "")

    def test_alternatives(self):
        assert semver.satisfies("3.0.0", "^1.0.0 || ^3.0.0")
        assert not semver.satisfies("2.0.0", "^1.0.0 || ^3.0.0")

    def test_prerelease_needs_explicit_bound(self):
        # A pre-release must not slip into a range that never mentions one:
        # that would silently offer an unstable build as an update.
        assert not semver.satisfies("1.3.0-rc.1", ">=1.2.0 <2.0.0")
        assert semver.satisfies("1.3.0-rc.1", ">=1.3.0-alpha <2.0.0")

    def test_invalid_range_raises(self):
        with pytest.raises(semver.SemverError):
            semver.satisfies("1.0.0", ">=nonsense")


class TestIntersection:
    def test_overlapping_ranges_produce_a_spec(self):
        combined = semver.intersect(">=1.0.0", "<2.0.0")
        assert combined is not None
        assert semver.satisfies("1.5.0", combined)
        assert not semver.satisfies("2.5.0", combined)

    def test_disjoint_ranges_return_none(self):
        assert semver.intersect(">=2.0.0", "<1.0.0") is None

    def test_wildcard_intersection_keeps_the_other_side(self):
        combined = semver.intersect("*", "^1.2.0")
        assert combined is not None
        assert semver.satisfies("1.5.0", combined)
        assert not semver.satisfies("2.0.0", combined)


class TestMaxSatisfying:
    def test_picks_highest_matching(self):
        versions = ["1.0.0", "1.2.0", "1.9.3", "2.0.0"]
        assert semver.max_satisfying(versions, "^1.0.0") == "1.9.3"

    def test_ignores_unparsable(self):
        assert semver.max_satisfying(["nonsense", "1.0.1"], "*") == "1.0.1"

    def test_returns_none_when_nothing_matches(self):
        assert semver.max_satisfying(["3.0.0"], "^1.0.0") is None
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
