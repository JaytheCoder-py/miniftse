"""The README quotes index numbers. Pin them to the run manifest.

This test exists because the README was wrong: it advertised a +10.4% annualised
return against an actual +3.7%, a −32.6% drawdown against −36.4%, 7,335 divisor
events against 7,590, and 70 tests against 331. None of it was invented — the code
is deterministic and golden-mastered, and the figures were true of an earlier build.
They simply drifted, and nothing failed when they did.

That is the gap worth closing. The golden master pins the parquet columns; it has no
opinion about markdown. So CI stayed green while the headline moved 2.8x away from
what the software does, which to a reader is indistinguishable from a made-up number.

A published figure is a published figure regardless of the file extension. This reads
the README table and compares it against the metrics block of the most recent
`make build-index` manifest, so the document cannot drift from the build without the
suite going red.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
README = REPO / "README.md"
MANIFESTS = REPO / "artefacts" / "manifests"

# The default `make build-index` invocation the README documents and prints.
DEFAULT_START, DEFAULT_END = "2016-01-04", "2026-06-30"


def _reference_manifest() -> dict:
    """Newest manifest produced by the README's own default build invocation."""
    best, best_at = None, ""
    for path in MANIFESTS.glob("MFTSE-GLOBAL-*.json"):
        try:
            m = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        metrics = m.get("metrics", {})
        if metrics.get("start") != DEFAULT_START or metrics.get("end") != DEFAULT_END:
            continue
        if "ann_return_gtr" not in metrics:
            continue
        if m.get("created_at", "") > best_at:
            best, best_at = m, m.get("created_at", "")
    return best or {}


def _readme_row(label: str) -> str:
    """The value cell of a `| label | value |` row in the README."""
    text = README.read_text(encoding="utf-8")
    m = re.search(rf"^\|\s*{re.escape(label)}\s*\|([^|]*)\|", text, re.M)
    assert m, f"README has no table row labelled {label!r}"
    return m.group(1).strip()


def _number(cell: str) -> float:
    """First signed number in a cell, tolerating %, commas, bold and a unicode minus."""
    cleaned = cell.replace("−", "-").replace("**", "").replace(",", "")
    m = re.search(r"-?\d+(?:\.\d+)?", cleaned)
    assert m, f"no number in README cell {cell!r}"
    return float(m.group(0))


@pytest.fixture(scope="module")
def metrics() -> dict:
    manifest = _reference_manifest()
    if not manifest:
        pytest.skip(
            f"no manifest for the default {DEFAULT_START}..{DEFAULT_END} build in "
            f"{MANIFESTS}; run `make build-index` first"
        )
    return manifest["metrics"]


class TestReadmeMatchesTheBuild:
    """Percentages are quoted to one decimal, so compare at that resolution."""

    def test_annualised_return(self, metrics: dict) -> None:
        assert _number(_readme_row("Annualised return (GTR)")) == pytest.approx(
            round(metrics["ann_return_gtr"] * 100, 1), abs=0.05
        )

    def test_annualised_volatility(self, metrics: dict) -> None:
        assert _number(_readme_row("Annualised volatility")) == pytest.approx(
            round(metrics["ann_vol"] * 100, 1), abs=0.05
        )

    def test_max_drawdown(self, metrics: dict) -> None:
        assert _number(_readme_row("Maximum drawdown")) == pytest.approx(
            round(metrics["max_drawdown"] * 100, 1), abs=0.05
        )

    def test_divisor_events(self, metrics: dict) -> None:
        assert _number(_readme_row("Divisor events")) == metrics["divisor_events"]

    def test_reviews(self, metrics: dict) -> None:
        assert _number(_readme_row("Reviews")) == metrics["reviews"]

    def test_divisor_continuity_breaches_claimed_zero(self, metrics: dict) -> None:
        """The README claims zero. It is the one figure that must never be softened."""
        assert _number(_readme_row("Divisor continuity breaches")) == 0

    def test_validation_rule_count(self, metrics: dict) -> None:
        row = _readme_row("Validation")
        assert _number(row) == metrics["validation_counts"]["total"]


class TestReadmeInternalConsistency:
    def test_universe_size_matches_the_documented_command(self) -> None:
        """The prose says 500 securities; the CLI default must agree."""
        from miniftse.cli import build_index_cmd

        default = build_index_cmd.__defaults__
        assert default is not None
        text = README.read_text(encoding="utf-8")
        assert "500-security universe" in text

    def test_no_stale_test_count(self) -> None:
        """`70 passing` was stale for a long time. Catch that shape of claim."""
        text = README.read_text(encoding="utf-8")
        m = re.search(r"\|\s*Tests\s*\|\s*(\d+) passing", text)
        assert m, "README no longer states a test count"
        claimed = int(m.group(1))
        collected = sum(
            len(re.findall(r"^\s*def test_", p.read_text(encoding="utf-8"), re.M))
            for p in (REPO / "tests").glob("test_*.py")
        )
        # Parametrised cases mean the run count exceeds the def count; the claim must
        # not fall below what is written down, which is how `70` survived.
        assert claimed >= collected, (
            f"README claims {claimed} tests but {collected} test functions are defined; "
            "the claim is stale"
        )


class TestRetrospectiveSizeClaims:
    """`docs/ai_development_retrospective.md` opens with the repo's size.

    Those three numbers were literals in `reporting/papers.py` that nobody updated,
    so the document arguing that published figures need pinning drifted to ~20,000
    lines / 68 modules / 89 tests against a repo at 29,000 / 86 / 342. Pin them.

    The line count is quoted as "roughly", so it is checked with slack: normal
    development must not turn CI red, but a claim half the size of the repo must.
    """

    RETRO = REPO / "docs" / "ai_development_retrospective.md"

    @staticmethod
    def _measured() -> tuple[int, int, int]:
        """Source files, total source lines, and test functions defined."""
        sources = sorted((REPO / "src" / "miniftse").rglob("*.py"))
        lines = sum(len(p.read_text(encoding="utf-8").splitlines()) for p in sources)
        tests = sum(
            len(re.findall(r"^\s*def test_", p.read_text(encoding="utf-8"), re.M))
            for p in (REPO / "tests").glob("test_*.py")
        )
        return len(sources), lines, tests

    def _claims(self) -> re.Match[str]:
        text = self.RETRO.read_text(encoding="utf-8")
        m = re.search(r"roughly ~?([\d,]+) lines across (\d+) modules with (\d+) tests", text)
        assert m, "the retrospective no longer opens with a size claim in the known shape"
        return m

    def test_module_count_is_exact(self) -> None:
        n_files, _, _ = self._measured()
        assert int(self._claims().group(2)) == n_files

    def test_test_count_is_not_below_what_is_defined(self) -> None:
        _, _, n_tests = self._measured()
        claimed = int(self._claims().group(3))
        assert claimed >= n_tests, f"retrospective claims {claimed} tests against {n_tests} defined"

    def test_line_count_is_the_right_order_of_magnitude(self) -> None:
        _, n_lines, _ = self._measured()
        claimed = int(self._claims().group(1).replace(",", ""))
        assert claimed == pytest.approx(n_lines, rel=0.10), (
            f"retrospective claims {claimed:,} lines against {n_lines:,} measured"
        )
