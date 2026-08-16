"""Property-based tests: invariants that must hold for every input.

Unit tests check the cases the author thought of. Property tests search for the ones
they did not, and for index arithmetic that difference is where the production bugs
live: nobody writes a unit test for "a rights issue on the same day as a split on a
security whose price is 0.0001".

Each property below is a statement from the Ground Rules, expressed as code.
"""

from __future__ import annotations

import datetime as dt

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from miniftse.calc.state import Constituent, IndexState
from miniftse.corpactions.engine import CorporateActionEngine
from miniftse.corpactions.events import CashDividend, RightsIssue, Split
from miniftse.types import Country
from miniftse.weighting.capping import CappingError, cap_weights, verify_capping

D = dt.date(2024, 6, 10)

prices = st.floats(min_value=0.05, max_value=50_000, allow_nan=False, allow_infinity=False)
share_counts = st.floats(min_value=1_000, max_value=5e10, allow_nan=False, allow_infinity=False)
factors = st.floats(min_value=0.01, max_value=1.0, allow_nan=False, allow_infinity=False)

SETTINGS = settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])


@st.composite
def constituents(draw: st.DrawFn, min_size: int = 2, max_size: int = 12) -> dict[str, Constituent]:
    n = draw(st.integers(min_value=min_size, max_value=max_size))
    return {
        f"S{i}": Constituent(
            security_id=f"S{i}",
            price=draw(prices),
            shares=draw(share_counts),
            free_float_factor=draw(factors),
            capping_factor=1.0,
            fx_rate=draw(
                st.floats(min_value=0.001, max_value=200.0, allow_nan=False, allow_infinity=False)
            ),
            country=Country.US,
        )
        for i in range(n)
    }


@st.composite
def weight_vectors(draw: st.DrawFn, min_size: int = 3, max_size: int = 40) -> dict[str, float]:
    n = draw(st.integers(min_value=min_size, max_value=max_size))
    raw = draw(
        st.lists(
            st.floats(min_value=1e-6, max_value=1e6, allow_nan=False, allow_infinity=False),
            min_size=n,
            max_size=n,
        )
    )
    total = sum(raw)
    assume(total > 0)
    return {f"S{i}": v / total for i, v in enumerate(raw)}


# --------------------------------------------------------------------------------------
# Weights
# --------------------------------------------------------------------------------------


class TestWeightInvariants:
    @given(cs=constituents())
    @SETTINGS
    def test_weights_sum_to_one(self, cs: dict[str, Constituent]) -> None:
        state = IndexState.initialise(D, cs, base_level=1000.0)
        assume(state.total_market_value > 0)
        assert sum(state.weights().values()) == pytest.approx(1.0, abs=1e-9)

    @given(cs=constituents())
    @SETTINGS
    def test_weights_are_non_negative(self, cs: dict[str, Constituent]) -> None:
        state = IndexState.initialise(D, cs, base_level=1000.0)
        assert all(w >= 0 for w in state.weights().values())

    @given(weights=weight_vectors(), cap=st.floats(min_value=0.05, max_value=0.9))
    @SETTINGS
    def test_capping_never_leaves_a_weight_above_the_cap(
        self, weights: dict[str, float], cap: float
    ) -> None:
        assume(cap * len(weights) > 1.0)
        result = cap_weights(weights, cap)
        assert not verify_capping(result.weights, cap)

    @given(weights=weight_vectors(min_size=5), cap=st.floats(min_value=0.1, max_value=0.5))
    @SETTINGS
    def test_capping_is_idempotent(self, weights: dict[str, float], cap: float) -> None:
        """Capping an already-capped vector must change nothing.

        A cap that keeps moving weight on each pass has not converged, it has stopped.
        """
        assume(cap * len(weights) > 1.0)
        once = cap_weights(weights, cap)
        twice = cap_weights(once.weights, cap)
        for name in once.weights:
            assert twice.weights[name] == pytest.approx(once.weights[name], abs=1e-9)

    @given(weights=weight_vectors(min_size=4), cap=st.floats(min_value=0.1, max_value=0.6))
    @SETTINGS
    def test_capping_preserves_relative_order_of_uncapped_names(
        self, weights: dict[str, float], cap: float
    ) -> None:
        """Capping the top must not reorder the rest. If it does, weight is being
        redistributed non-proportionally and the scheme is no longer cap-weighted."""
        assume(cap * len(weights) > 1.0)
        result = cap_weights(weights, cap)
        free = [n for n in weights if n not in result.capped_names]
        assume(len(free) >= 2)
        before = sorted(free, key=lambda n: weights[n])
        after = sorted(free, key=lambda n: result.weights[n])
        assert before == after

    @given(weights=weight_vectors())
    @SETTINGS
    def test_infeasible_capping_raises_rather_than_returning_bad_weights(
        self, weights: dict[str, float]
    ) -> None:
        impossible = 0.5 / len(weights)
        with pytest.raises(CappingError):
            cap_weights(weights, impossible)


# --------------------------------------------------------------------------------------
# Divisor
# --------------------------------------------------------------------------------------


class TestDivisorInvariants:
    @given(cs=constituents(), multiplier=st.floats(min_value=0.5, max_value=2.0))
    @SETTINGS
    def test_pure_price_moves_never_change_the_divisor(
        self, cs: dict[str, Constituent], multiplier: float
    ) -> None:
        state = IndexState.initialise(D, cs, base_level=1000.0)
        moved = state
        for c in state.constituents.values():
            moved = moved.replace_constituent(c.with_price(c.price * multiplier))
        assert moved.divisor == state.divisor
        assert moved.level == pytest.approx(state.level * multiplier, rel=1e-9)

    @given(cs=constituents(), ratio=st.sampled_from([2.0, 3.0, 4.0, 5.0, 10.0, 0.1, 0.25]))
    @SETTINGS
    def test_splits_leave_the_index_level_continuous(
        self, cs: dict[str, Constituent], ratio: float
    ) -> None:
        state = IndexState.initialise(D, cs, base_level=1000.0)
        target = next(iter(cs))
        engine = CorporateActionEngine()
        result = engine.apply_event(Split("SP", target, D, D, D, ratio=ratio), state)
        assert result.state.divisor == pytest.approx(state.divisor, rel=1e-12)
        assert result.state.level == pytest.approx(state.level, rel=1e-9)

    @given(
        cs=constituents(),
        new_shares=st.integers(min_value=1, max_value=5),
        per_held=st.integers(min_value=1, max_value=10),
        discount=st.floats(min_value=0.05, max_value=0.60),
    )
    @SETTINGS
    def test_rights_issues_leave_the_index_level_continuous(
        self, cs: dict[str, Constituent], new_shares: int, per_held: int, discount: float
    ) -> None:
        state = IndexState.initialise(D, cs, base_level=1000.0)
        target = next(iter(cs))
        cum = state.constituents[target].price
        engine = CorporateActionEngine()
        event = RightsIssue(
            "R",
            target,
            D,
            D,
            D,
            subscription_price=cum * (1 - discount),
            new_shares=new_shares,
            per_held=per_held,
            cum_price=cum,
        )
        result = engine.apply_event(event, state)
        assert result.state.level == pytest.approx(state.level, rel=1e-8)

    @given(cs=constituents(), amount_fraction=st.floats(min_value=0.001, max_value=0.10))
    @SETTINGS
    def test_dividends_lower_the_level_and_leave_the_divisor(
        self, cs: dict[str, Constituent], amount_fraction: float
    ) -> None:
        state = IndexState.initialise(D, cs, base_level=1000.0)
        target = next(iter(cs))
        amount = state.constituents[target].price * amount_fraction
        engine = CorporateActionEngine()
        result = engine.apply_event(CashDividend("D", target, D, D, D, amount=amount), state)
        assert result.state.divisor == state.divisor
        assert result.state.level <= state.level + 1e-9
        assert result.cash_distributed >= 0.0

    @given(cs=constituents(min_size=3))
    @SETTINGS
    def test_divisor_stays_positive(self, cs: dict[str, Constituent]) -> None:
        state = IndexState.initialise(D, cs, base_level=1000.0)
        assert state.divisor > 0

    @given(cs=constituents())
    @SETTINGS
    def test_level_equals_market_value_over_divisor(self, cs: dict[str, Constituent]) -> None:
        """The defining identity. Everything else is downstream of it."""
        state = IndexState.initialise(D, cs, base_level=1000.0)
        assert state.level * state.divisor == pytest.approx(state.total_market_value, rel=1e-9)


# --------------------------------------------------------------------------------------
# Returns
# --------------------------------------------------------------------------------------


class TestReturnInvariants:
    @given(
        cs=constituents(),
        dividends=st.lists(st.floats(min_value=0.0, max_value=0.02), min_size=1, max_size=6),
    )
    @SETTINGS
    def test_total_return_is_at_least_price_return(
        self, cs: dict[str, Constituent], dividends: list[float]
    ) -> None:
        """With non-negative dividends, GTR cannot fall below PR over any period."""
        state = IndexState.initialise(D, cs, base_level=1000.0)
        engine = CorporateActionEngine(withholding_tax={"US": 0.30})
        pr = gtr = 1000.0
        prev = state.level

        for i, fraction in enumerate(dividends):
            target = list(cs)[i % len(cs)]
            amount = state.constituents[target].price * fraction
            result = engine.apply_event(
                CashDividend(f"D{i}", target, D, D, D, amount=amount), state
            )
            state = result.state
            pr = state.level
            points = result.cash_distributed / state.divisor
            gtr *= (pr + points) / prev if prev > 0 else 1.0
            prev = pr

        assert gtr >= pr - 1e-6

    @given(
        cs=constituents(),
        fraction=st.floats(min_value=0.001, max_value=0.05),
        rate=st.floats(min_value=0.0, max_value=0.5),
    )
    @SETTINGS
    def test_net_return_never_exceeds_gross(
        self, cs: dict[str, Constituent], fraction: float, rate: float
    ) -> None:
        state = IndexState.initialise(D, cs, base_level=1000.0)
        engine = CorporateActionEngine(withholding_tax={"US": rate})
        target = next(iter(cs))
        amount = state.constituents[target].price * fraction
        result = engine.apply_event(CashDividend("D", target, D, D, D, amount=amount), state)
        assert result.net_cash_distributed <= result.cash_distributed + 1e-9
        assert result.net_cash_distributed >= 0.0


# --------------------------------------------------------------------------------------
# Identifiers
# --------------------------------------------------------------------------------------


class TestIdentifierInvariants:
    @given(serial=st.integers(min_value=0, max_value=999_999_999))
    @SETTINGS
    def test_generated_isins_validate(self, serial: int) -> None:
        from miniftse.secmaster.identifiers import make_isin, validate_isin

        assert validate_isin(make_isin("US", serial))

    @given(serial=st.integers(min_value=0, max_value=999_999))
    @SETTINGS
    def test_generated_sedols_validate(self, serial: int) -> None:
        from miniftse.secmaster.identifiers import make_sedol, validate_sedol

        assert validate_sedol(make_sedol(serial))

    @given(serial=st.integers(min_value=0, max_value=99_999_999))
    @SETTINGS
    def test_generated_cusips_validate(self, serial: int) -> None:
        from miniftse.secmaster.identifiers import make_cusip, validate_cusip

        assert validate_cusip(make_cusip(serial))

    @given(serial=st.integers(min_value=0, max_value=99_999_998))
    @SETTINGS
    def test_check_digit_catches_a_corrupted_identifier(self, serial: int) -> None:
        """Changing the last body digit must break the check digit.

        This is the whole reason check digits exist, and asserting it means the
        validator is doing work rather than accepting everything.
        """
        from miniftse.secmaster.identifiers import make_isin, validate_isin

        good = make_isin("US", serial)
        digit = int(good[10])
        corrupted = good[:10] + str((digit + 1) % 10) + good[11]
        assert not validate_isin(corrupted, strict=False)
