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
