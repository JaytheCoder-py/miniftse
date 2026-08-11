"""Unit tests with hand-computed expected values.

Non-negotiable for index arithmetic. A test that asserts the code agrees with itself
proves nothing; every expected value here was worked out on paper first and can be
checked by a reviewer with a calculator.
"""

from __future__ import annotations

import datetime as dt

import pytest

from miniftse.calc.state import Constituent, IndexState
from miniftse.corpactions.engine import CorporateActionEngine, divisor_adjustment
from miniftse.corpactions.events import (
    CashDividend,
    CashMerger,
    Delisting,
    RightsIssue,
    Spinoff,
    Split,
    theoretical_ex_rights_price,
)
from miniftse.types import Country
from miniftse.weighting.capping import CappingError, apply_ucits_5_10_40, cap_weights

D = dt.date(2024, 6, 10)


def make_state(n: int = 3, price: float = 100.0, shares: float = 1000.0) -> IndexState:
    constituents = {
        f"S{i}": Constituent(f"S{i}", price=price, shares=shares, country=Country.US)
        for i in range(n)
    }
    return IndexState.initialise(D, constituents, base_level=1000.0)


# --------------------------------------------------------------------------------------
# TERP
# --------------------------------------------------------------------------------------


class TestTerp:
    def test_one_for_four_at_thirty_percent_discount(self) -> None:
        """Cum price 100, 1-for-4 at a 30% discount.

        Subscription price = 70.  TERP = (4*100 + 1*70) / 5 = 470 / 5 = 94.
        A reviewer can check this on a calculator, which is the point.
        """
        assert theoretical_ex_rights_price(100.0, 70.0, 1, 4) == pytest.approx(94.0)

    def test_one_for_two_at_half_price(self) -> None:
        """Cum 60, sub 30, 1-for-2.  TERP = (2*60 + 1*30) / 3 = 150/3 = 50."""
        assert theoretical_ex_rights_price(60.0, 30.0, 1, 2) == pytest.approx(50.0)

    def test_deep_discount(self) -> None:
        """Cum 200, sub 50, 3-for-1.  TERP = (1*200 + 3*50) / 4 = 350/4 = 87.5."""
        assert theoretical_ex_rights_price(200.0, 50.0, 3, 1) == pytest.approx(87.5)

    def test_no_discount_leaves_price_unchanged(self) -> None:
        assert theoretical_ex_rights_price(100.0, 100.0, 1, 4) == pytest.approx(100.0)

    def test_invalid_ratio_raises(self) -> None:
        with pytest.raises(ValueError):
            theoretical_ex_rights_price(100.0, 70.0, 0, 4)


# --------------------------------------------------------------------------------------
# Divisor
# --------------------------------------------------------------------------------------


class TestDivisor:
    def test_initialisation(self) -> None:
        """3 x 100 x 1000 = 300,000 market value at a base level of 1000
        gives a divisor of exactly 300."""
        state = make_state()
        assert state.total_market_value == pytest.approx(300_000.0)
        assert state.divisor == pytest.approx(300.0)
        assert state.level == pytest.approx(1000.0)

    def test_formula(self) -> None:
        assert divisor_adjustment(300.0, 300_000.0, 317_500.0) == pytest.approx(317.5)

    def test_price_move_does_not_change_the_divisor(self) -> None:
        state = make_state()
        moved = state.replace_constituent(state.constituents["S0"].with_price(110.0))
        assert moved.divisor == pytest.approx(300.0)
        # 310,000 / 300 = 1033.333...
        assert moved.level == pytest.approx(310_000.0 / 300.0)

    def test_split_leaves_divisor_and_level_untouched(self) -> None:
        state = make_state()
        engine = CorporateActionEngine()
        result = engine.apply_event(Split("SP", "S0", D, D, D, ratio=2.0), state)
        c = result.state.constituents["S0"]
        assert c.price == pytest.approx(50.0)
        assert c.shares == pytest.approx(2000.0)
        assert result.state.divisor == pytest.approx(300.0)
        assert result.state.level == pytest.approx(1000.0)

    def test_reverse_split(self) -> None:
        """1-for-8: ratio 0.125, so price x8 and shares /8."""
        state = make_state()
        engine = CorporateActionEngine()
        result = engine.apply_event(Split("RS", "S0", D, D, D, ratio=0.125), state)
        c = result.state.constituents["S0"]
        assert c.price == pytest.approx(800.0)
        assert c.shares == pytest.approx(125.0)
        assert result.state.level == pytest.approx(1000.0)

    def test_rights_issue_moves_divisor_but_not_level(self) -> None:
        """S0: price 100 -> TERP 94, shares 1000 -> 1250.

        New market value = 94*1250 + 100*1000 + 100*1000 = 117,500 + 200,000 = 317,500.
        Divisor 300 -> 300 * 317500/300000 = 317.5.  Level stays at 1000.
        """
        state = make_state()
        engine = CorporateActionEngine()
        event = RightsIssue("R", "S0", D, D, D, subscription_price=70.0,
                            new_shares=1, per_held=4, cum_price=100.0)
        result = engine.apply_event(event, state)
        assert result.state.constituents["S0"].price == pytest.approx(94.0)
        assert result.state.constituents["S0"].shares == pytest.approx(1250.0)
        assert result.state.total_market_value == pytest.approx(317_500.0)
        assert result.state.divisor == pytest.approx(317.5)
        assert result.state.level == pytest.approx(1000.0)

    def test_dividend_moves_level_not_divisor(self) -> None:
        """A 2.00 dividend on one of three equal constituents.

        Market value 300,000 -> 298,000, divisor unchanged, so the level falls to
        298,000/300 = 993.333..., a drop of 2/300 = 0.6667%.
        """
        state = make_state()
        engine = CorporateActionEngine(withholding_tax={"US": 0.30})
        result = engine.apply_event(CashDividend("D", "S0", D, D, D, amount=2.0), state)
        assert result.state.divisor == pytest.approx(300.0)
        assert result.state.level == pytest.approx(298_000.0 / 300.0)
        assert result.state.level / 1000.0 - 1 == pytest.approx(-2.0 / 300.0)

    def test_withholding_tax(self) -> None:
        """Gross 2.00 x 1000 shares = 2,000. US withholding 30% leaves 1,400."""
        state = make_state()
        engine = CorporateActionEngine(withholding_tax={"US": 0.30})
        result = engine.apply_event(CashDividend("D", "S0", D, D, D, amount=2.0), state)
        assert result.cash_distributed == pytest.approx(2000.0)
        assert result.net_cash_distributed == pytest.approx(1400.0)

    def test_uk_has_no_withholding(self) -> None:
        """A UK constituent's net contribution equals its gross - the Module 1
        self-check answer."""
        constituents = {
            "GB0": Constituent("GB0", price=100.0, shares=1000.0, country=Country.GB)
        }
        state = IndexState.initialise(D, constituents, base_level=1000.0)
        engine = CorporateActionEngine(withholding_tax={"GB": 0.0, "US": 0.30})
        result = engine.apply_event(CashDividend("D", "GB0", D, D, D, amount=2.0), state)
        assert result.cash_distributed == pytest.approx(result.net_cash_distributed)

    def test_spinoff_included_preserves_market_value(self) -> None:
        state = make_state()
        engine = CorporateActionEngine()
        event = Spinoff("SP", "S0", D, D, D, spinco_security_id="S0-SPIN",
                        shares_per_parent_share=0.5, value_per_parent_share=20.0,
                        parent_cum_price=100.0, spinco_enters_index=True)
        result = engine.apply_event(event, state)
        assert result.state.constituents["S0"].price == pytest.approx(80.0)
        # Spinco: 20/0.5 = 40 per share, 1000*0.5 = 500 shares, = 20,000.
        spinco = result.state.constituents["S0-SPIN"]
        assert spinco.price == pytest.approx(40.0)
        assert spinco.shares == pytest.approx(500.0)
        assert result.state.total_market_value == pytest.approx(300_000.0)
        assert result.state.level == pytest.approx(1000.0)

    def test_spinoff_excluded_rebases_divisor(self) -> None:
        state = make_state()
        engine = CorporateActionEngine()
        event = Spinoff("SP", "S0", D, D, D, spinco_security_id="S0-SPIN",
                        shares_per_parent_share=0.5, value_per_parent_share=20.0,
                        parent_cum_price=100.0, spinco_enters_index=False)
        result = engine.apply_event(event, state)
        assert "S0-SPIN" not in result.state.constituents
        assert result.state.level == pytest.approx(1000.0)  # continuous
        assert result.state.divisor < 300.0  # value left the index
        assert result.cash_distributed == pytest.approx(20_000.0)

    def test_spinoff_child_inherits_parent_adv(self) -> None:
        import datetime as dt

        from miniftse.calc.state import Constituent, IndexState
        from miniftse.corpactions.engine import CorporateActionEngine
        from miniftse.corpactions.events import Spinoff

        engine = CorporateActionEngine(withholding_tax={})
        parent = Constituent("PARENT", price=100.0, shares=1000.0, adv=8_000_000.0)
        state = IndexState(date=dt.date(2020, 1, 1), divisor=1.0,
                            constituents={"PARENT": parent})
        event = Spinoff(
            event_id="SPIN-1", security_id="PARENT",
            ex_date=dt.date(2020, 1, 1), announcement_date=dt.date(2019, 12, 1),
            pay_date=dt.date(2020, 1, 1),
            spinco_security_id="CHILD",
            shares_per_parent_share=1.0,
            value_per_parent_share=10.0,
            parent_cum_price=100.0,
            spinco_enters_index=True,
        )
        handled = engine._apply_spinoff(event, state)
        assert handled.state.constituents["CHILD"].adv == 8_000_000.0

    def test_cash_merger_recognises_the_premium(self) -> None:
        """S0 taken out at 130 from 100.

        The +30% must flow through the index return before deletion. Level before
        deletion = (130*1000 + 200,000)/300 = 1100. After deletion the level must still
        be 1100, so the premium is kept and the removal is continuous.
        """
        state = make_state()
        engine = CorporateActionEngine()
        result = engine.apply_event(
            CashMerger("M", "S0", D, D, D, cash_per_share=130.0), state)
        assert "S0" not in result.state.constituents
        assert result.state.level == pytest.approx(1100.0)
        assert result.change.realised_return_bps == pytest.approx(1000.0, rel=1e-6)
        assert abs(result.change.level_continuity_error_bps) < 1e-6

    def test_delisting_recognises_the_loss(self) -> None:
        """S0 delisted worthless: level must fall to (0 + 200,000)/300 = 666.67
        and then stay there."""
        state = make_state()
        engine = CorporateActionEngine()
        result = engine.apply_event(
            Delisting("X", "S0", D, D, D, final_price=0.0), state)
        assert "S0" not in result.state.constituents
        assert result.state.level == pytest.approx(200_000.0 / 300.0)
        assert abs(result.change.level_continuity_error_bps) < 1e-6


# --------------------------------------------------------------------------------------
# Capping
# --------------------------------------------------------------------------------------


class TestCapping:
    def test_no_breach_leaves_weights_alone(self) -> None:
        weights = {"A": 0.4, "B": 0.35, "C": 0.25}
        result = cap_weights(weights, 0.5)
        assert result.weights["A"] == pytest.approx(0.4)
        assert result.capped_names == ()

    def test_single_cap_cascades(self) -> None:
        """A=0.6, B=0.3, C=0.1 capped at 0.4.

        Pass 1: A breaches, capped at 0.4. Residual 0.6 splits between B and C in the
        ratio 3:1, giving B = 0.45 and C = 0.15.
        Pass 2: B now breaches at 0.45, capped at 0.4. Residual 0.2 all goes to C.
        Final: A = 0.4, B = 0.4, C = 0.2.

        Capping the largest name pushed the second over the cap. This is the case the
        naive "cap and renormalise everything" loop gets wrong.
        """
        result = cap_weights({"A": 0.6, "B": 0.3, "C": 0.1}, 0.4)
        assert result.weights["A"] == pytest.approx(0.4)
        assert result.weights["B"] == pytest.approx(0.4)
        assert result.weights["C"] == pytest.approx(0.2)
        assert sum(result.weights.values()) == pytest.approx(1.0)
        assert set(result.capped_names) == {"A", "B"}

    def test_cap_below_one_over_n_is_infeasible(self) -> None:
        """Three names cannot hold unit weight at a 0.3 cap: 3 x 0.3 = 0.9 < 1."""
        with pytest.raises(CappingError, match="infeasible"):
            cap_weights({"A": 0.6, "B": 0.3, "C": 0.1}, 0.3)

    def test_cascade(self) -> None:
        """The pathological case: capping the largest pushes the next one over.

        Naive renormalise-everything fails here. All three must end at or below 0.35 and
        the weights must still sum to one.
        """
        result = cap_weights({"A": 0.55, "B": 0.34, "C": 0.06, "D": 0.05}, 0.35)
        assert max(result.weights.values()) <= 0.35 + 1e-9
        assert sum(result.weights.values()) == pytest.approx(1.0)
        assert len(result.capped_names) >= 2

    def test_ucits_limb_one(self) -> None:
        weights = {f"S{i}": w for i, w in enumerate([0.30, 0.20, 0.15] + [0.05] * 7)}
        result = apply_ucits_5_10_40(weights)
        assert max(result.weights.values()) <= 0.10 + 1e-9
        assert sum(result.weights.values()) == pytest.approx(1.0)

    def test_ucits_limb_two(self) -> None:
        """Six names at 8% each sum to 48% above the 5% threshold, breaching the 40%
        aggregate limit. The effective cap must fall until it holds."""
        weights = {f"S{i}": 0.08 for i in range(6)}
        weights.update({f"T{i}": 0.52 / 20 for i in range(20)})
        result = apply_ucits_5_10_40(weights)
        above = sum(w for w in result.weights.values() if w > 0.05 + 1e-9)
        assert above <= 0.40 + 1e-6
        assert sum(result.weights.values()) == pytest.approx(1.0)

    def test_capping_factors_reproduce_weights(self) -> None:
        """The stored factor times the raw weight must give the capped weight - the
        index stores C_i, not the weight, so the two must agree exactly."""
        raw = {"A": 0.6, "B": 0.3, "C": 0.1}
        result = cap_weights(raw, 0.4)
        total = sum(raw.values())
        for name in raw:
            implied = raw[name] * result.factors[name] / total
            assert implied == pytest.approx(result.weights[name])


# --------------------------------------------------------------------------------------
# ADV roll-forward
# --------------------------------------------------------------------------------------


def test_to_constituent_carries_adv() -> None:
    from miniftse.calc.fx import FxTable
    from miniftse.calc.index import ConstituentSpec, IndexCalculator
    from miniftse.config import global_all_cap

    calc = IndexCalculator(
        config=global_all_cap(), fx=FxTable(base="USD"),
        engine=CorporateActionEngine(withholding_tax={}),
    )
    spec = ConstituentSpec(
        security_id="S1", shares=1000.0, free_float_factor=1.0, adv=5_000_000.0,
    )
    c = calc._to_constituent(spec, price=10.0, date=dt.date(2020, 1, 1))
    assert c.adv == 5_000_000.0


def test_mark_preserves_adv() -> None:
    import pandas as pd

    from miniftse.calc.fx import FxTable
    from miniftse.calc.index import IndexCalculator, _PriceBook
    from miniftse.config import global_all_cap

    calc = IndexCalculator(
        config=global_all_cap(), fx=FxTable(base="USD"),
        engine=CorporateActionEngine(withholding_tax={}),
    )
    state = IndexState(
        date=dt.date(2020, 1, 1), divisor=1.0,
        constituents={"S1": Constituent("S1", price=10.0, shares=1000.0, adv=5_000_000.0)},
    )
    book = _PriceBook(pd.DataFrame({
        "security_id": ["S1"], "date": [dt.date(2020, 1, 2)], "close": [11.0],
    }))
    rolled = calc._mark(state, dt.date(2020, 1, 2), book)
    assert rolled.constituents["S1"].adv == 5_000_000.0
    assert rolled.constituents["S1"].price == 11.0


# --------------------------------------------------------------------------------------
# Size banding - determinism
# --------------------------------------------------------------------------------------


class TestBandingDeterminism:
    """Band membership must be a function of the universe, not of how it was assembled
    or which machine summed it. See DECISIONS.md D-014 and Ground Rules 2.1.1/8.3.

    These are permutation tests, not smoke tests: each one re-runs the real
    `assign_bands` over inputs that differ *only* in summation order, and demands
    identical output.
    """

    @staticmethod
    def _caps_with_ties() -> dict[str, float]:
        """A universe carrying several exactly-equal float market caps, summing to 1000.

        Ties are the case that exposes ordering: a stable sort leaves equal-sized names
        in dict-iteration order, so permuting the input silently permutes the ranking
        and moves the cumulative percentile of everything after them. BBB and CCC tie
        at 200, and the twenty tail names all tie at 15.

        Ranked, the cumulative percentiles are 0.300, 0.500, 0.700, 0.715, 0.730 ... up
        to 1.000. That is chosen, not incidental: CCC closes at exactly the 70% cut, and
        T00 lands 1.5 percentage points past it - inside the 2-point buffer - so the
        fixture exercises the cutoff tie-break and both sides of the buffer test.
        """
        caps = {"AAA": 300.0, "BBB": 200.0, "CCC": 200.0}
        caps.update({f"T{i:02d}": 15.0 for i in range(20)})
        return caps

    def test_cumulative_percentile_is_invariant_under_input_permutation(self) -> None:
        """The property the September 2024 divergence violated.

        The same securities with the same caps, presented in 200 different dict orders,
        must produce the same cumulative percentile for every security - bit for bit,
        not approximately. Plain `==` on the float is deliberate: `pytest.approx` here
        would pass on exactly the drift this test exists to forbid.
        """
        import random

        from miniftse.config import BandingConfig
        from miniftse.universe.banding import assign_bands

        caps = self._caps_with_ties()
        config = BandingConfig(apply_buffers=False)
        baseline = assign_bands(caps, config)

        rng = random.Random(20260811)
        for _ in range(200):
            keys = list(caps)
            rng.shuffle(keys)
            permuted = assign_bands({k: caps[k] for k in keys}, config)

            assert set(permuted) == set(baseline)
            for sec_id, assignment in baseline.items():
                assert permuted[sec_id].cumulative_pct == assignment.cumulative_pct
                assert permuted[sec_id].band == assignment.band

    def test_exact_cumulative_matches_prefix_fsum_and_ignores_association(self) -> None:
        """`_exact_cumulative` is the correctly rounded sum of every prefix.

        Two assertions, because they are different claims. First: it agrees exactly
        with the O(n^2) `math.fsum` over each slice - the reference definition of
        "correctly rounded prefix". Second: on values chosen so that naive
        left-to-right addition loses the small terms entirely, it still recovers them.
        That is what makes the result independent of how the additions were associated,
        which is the freedom numpy uses (block size, SIMD width, BLAS build) and the
        reason a cumulative percentile computed with `np.cumsum` was not reproducible.
        """
        import math

        from miniftse.universe.banding import _exact_cumulative

        catastrophic = [1e16, 1.0, 1.0, 1.0, -1e16, 1.0]
        assert _exact_cumulative(catastrophic) == [
            math.fsum(catastrophic[: i + 1]) for i in range(len(catastrophic))
        ]
        # Naive accumulation swallows the three unit terms against 1e16; the exact
        # running sum does not.
        naive = 0.0
        for value in catastrophic:
            naive += value
        assert naive == 1.0
        assert _exact_cumulative(catastrophic)[-1] == 4.0

    def test_exact_cumulative_matches_prefix_fsum_over_random_inputs(self) -> None:
        """The bit-identity claim again, but randomised rather than hand-picked.

        One curated cancellation case proves the algorithm handles the case its author
        thought of. This runs the same equality - `_exact_cumulative(v)[i] ==
        math.fsum(v[:i+1])` for every prefix - over 200 random inputs of varying length
        and magnitude, which is the claim the docstring on `_exact_cumulative` actually
        makes. Seeded, so a failure is reproducible.

        Two populations, because they fail differently: positive-only values spanning
        many orders of magnitude (a real universe of market caps, where small names are
        lost against the running total) and mixed-sign values (adversarial cancellation,
        where the running total can collapse toward zero and lose everything).
        """
        import math
        import random

        from miniftse.universe.banding import _exact_cumulative

        rng = random.Random(20260811)
        for mixed_sign in (False, True):
            for _ in range(100):
                n = rng.randint(1, 400)
                values = [
                    (rng.uniform(-1.0, 1.0) if mixed_sign else rng.uniform(0.0, 1.0))
                    * 10.0 ** rng.randint(-6, 12)
                    for _ in range(n)
                ]
                assert _exact_cumulative(values) == [
                    math.fsum(values[: i + 1]) for i in range(n)
                ], f"prefix mismatch at n={n}, mixed_sign={mixed_sign}"

    def test_equal_caps_are_ranked_by_security_id_not_insertion_order(self) -> None:
        """The written-down tie-break: equal float market caps rank by ascending
        security id. Without it the ranking is whatever the caller's dict iterated.
        """
        from miniftse.universe.banding import _ordered

        forwards = _ordered({"ZZZ": 100.0, "AAA": 100.0, "MMM": 100.0})
        backwards = _ordered({"MMM": 100.0, "AAA": 100.0, "ZZZ": 100.0})

        assert [sec_id for sec_id, _ in forwards] == ["AAA", "MMM", "ZZZ"]
        assert forwards == backwards

    def test_security_exactly_on_the_cutoff_lands_in_the_closing_band(self) -> None:
        """The documented tie-break for the boundary case itself (Ground Rules 2.1.1): a
        security whose quantised cumulative percentile is exactly a cutoff is in the
        band that cutoff closes - the larger band.

        The universe is built so the cumulative share after ON_CUT is exactly 0.70:
        600 + 100 = 700 of 1000, with every tail name smaller than ON_CUT so it really
        does rank second. Under the old bare comparison against an `np.cumsum` output
        this was the case that could land either side; here it is required to be LARGE,
        and required to stay LARGE when the input order changes.
        """
        from miniftse.config import BandingConfig
        from miniftse.types import SizeBand
        from miniftse.universe.banding import assign_bands

        caps = {
            "BIG": 600.0, "ON_CUT": 100.0,
            "T0": 90.0, "T1": 90.0, "T2": 90.0, "T3": 30.0,
        }
        config = BandingConfig(apply_buffers=False)

        for order in (list(caps), sorted(caps), sorted(caps, reverse=True)):
            bands = assign_bands({k: caps[k] for k in order}, config)
            assert bands["ON_CUT"].cumulative_pct == 0.70
            assert bands["ON_CUT"].band is SizeBand.LARGE

    def test_cumulative_percentile_is_quantised_to_the_documented_precision(self) -> None:
        """Every reported `cumulative_pct` is the quantised value the rule was decided
        on, so a diagnostic can never disagree with the assignment it explains.
        """
        from miniftse.config import BandingConfig
        from miniftse.universe.banding import CUMULATIVE_PCT_DECIMALS, assign_bands

        assert CUMULATIVE_PCT_DECIMALS == 12
        caps = {f"S{i:03d}": 1.0 / (i + 3) for i in range(37)}
        for assignment in assign_bands(caps, BandingConfig(apply_buffers=False)).values():
            pct = assignment.cumulative_pct
            assert pct == round(pct, CUMULATIVE_PCT_DECIMALS)

    def test_incumbent_exactly_one_buffer_width_out_is_held(self) -> None:
        """The buffer edge carried the same unquantised-comparison exposure as the
        cutoff, so it gets the same rule (Ground Rules 8.3): "more than" the buffer
        width moves a constituent, and exactly the buffer width does not.

        ON_EDGE closes at a cumulative 0.72, exactly `buffer_width` past the 0.70 Large
        cut, so a hard cut-off makes it Mid and the buffer must hold it Large. Nudging
        it a hair further out must move it - otherwise this would pass on a buffer that
        simply never releases anything.
        """
        from miniftse.config import BandingConfig
        from miniftse.types import SizeBand
        from miniftse.universe.banding import assign_bands

        config = BandingConfig(buffer_width=0.02, apply_buffers=True)
        previous = {"BIG": SizeBand.LARGE, "ON_EDGE": SizeBand.LARGE}

        # 690 + 30 = 720 of 1000: ON_EDGE closes at exactly 0.72, one buffer width past
        # the 0.70 cut. The tail is split into names smaller than ON_EDGE so it ranks
        # second rather than behind them.
        held_caps: dict[str, float] = {"BIG": 690.0, "ON_EDGE": 30.0}
        held_caps.update({f"T{i:02d}": 28.0 for i in range(10)})
        on_edge = assign_bands(held_caps, config, previous)["ON_EDGE"]
        assert on_edge.cumulative_pct == 0.72
        assert on_edge.band is SizeBand.LARGE
        assert on_edge.held_by_buffer

        # 690 + 31 = 721 of 1000: more than one buffer width out, so it moves.
        moved_caps: dict[str, float] = {"BIG": 690.0, "ON_EDGE": 31.0, "T09": 27.0}
        moved_caps.update({f"T{i:02d}": 28.0 for i in range(9)})
        past_edge = assign_bands(moved_caps, config, previous)["ON_EDGE"]
        assert past_edge.cumulative_pct == 0.721
        assert past_edge.band is SizeBand.MID
        assert not past_edge.held_by_buffer

    def test_band_assignment_is_invariant_under_permutation_with_buffers_on(self) -> None:
        """The permutation invariant must survive the buffer path too - `_within_buffer`
        reads the same cumulative percentile the cutoff test does.
        """
        import random

        from miniftse.config import BandingConfig
        from miniftse.types import SizeBand
        from miniftse.universe.banding import assign_bands

        caps = self._caps_with_ties()
        config = BandingConfig(apply_buffers=True)
        previous = {k: SizeBand.LARGE for k in caps}
        baseline = assign_bands(caps, config, previous)
        assert any(a.held_by_buffer for a in baseline.values()), (
            "fixture must exercise the buffer path for this test to mean anything"
        )

        rng = random.Random(11082026)
        for _ in range(100):
            keys = list(caps)
            rng.shuffle(keys)
            permuted = assign_bands({k: caps[k] for k in keys}, config, previous)
            for sec_id, assignment in baseline.items():
                assert permuted[sec_id].cumulative_pct == assignment.cumulative_pct
                assert permuted[sec_id].band == assignment.band
                assert permuted[sec_id].held_by_buffer == assignment.held_by_buffer
