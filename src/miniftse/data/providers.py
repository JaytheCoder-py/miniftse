"""Provider interfaces and the canonical table schemas.

Research code binds to these Protocols, never to a vendor. Swapping the synthetic
generator for yfinance, or yfinance for `lseg.data`, must not require touching anything
in `calc/`, `factors/` or `risk/`. That separation is the whole point: at an index
provider the data source changes over a product's life and the methodology must not.

Schema conventions, applied everywhere:

* **Tidy long frames.** Ragged panels are the normal case - securities list, delist and
  suspend - and a wide frame forces you to invent a value for cells that should not
  exist. Pivot at the point of use.
* **Prices are as traded.** Never adjusted in place. Adjustment factors are derived from
  the corporate action table on demand, so history is immutable and reproducible.
* **Bitemporal where it matters.** Any table carrying a fact that gets restated has both
  `effective_date` (the period the fact describes) and `knowledge_date` (when it could
  first have been known). Querying one without the other is the standard look-ahead bug.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Protocol, runtime_checkable

import pandas as pd

# --------------------------------------------------------------------------------------
# Canonical schemas
# --------------------------------------------------------------------------------------

PRICE_SCHEMA: dict[str, str] = {
    "listing_id": "str",
    "date": "date",
    "open": "float64",
    "high": "float64",
    "low": "float64",
    "close": "float64",
    "volume": "float64",
    "currency": "str",
    "is_suspended": "bool",
}

SHARES_SCHEMA: dict[str, str] = {
    "security_id": "str",
    "effective_date": "date",
    "knowledge_date": "date",
    "shares_outstanding": "float64",
    "free_float_factor": "float64",
    "foreign_ownership_limit": "float64",
}

FUNDAMENTAL_SCHEMA: dict[str, str] = {
    "security_id": "str",
    "item": "str",
    "period_end": "date",
    "filed_date": "date",
    "value": "float64",
    "currency": "str",
}

FX_SCHEMA: dict[str, str] = {
    "date": "date",
    "base": "str",
    "quote": "str",
    "rate": "float64",
}

CORP_ACTION_SCHEMA: dict[str, str] = {
    "event_id": "str",
    "security_id": "str",
    "event_type": "str",
    "announcement_date": "date",
    "ex_date": "date",
    "pay_date": "date",
    "payload": "json",
}

CLASSIFICATION_SCHEMA: dict[str, str] = {
    "security_id": "str",
    "effective_date": "date",
    "knowledge_date": "date",
    "icb_industry": "str",
    "icb_supersector": "str",
}


# --------------------------------------------------------------------------------------
# Protocols
# --------------------------------------------------------------------------------------


@runtime_checkable
class PriceProvider(Protocol):
    """As-traded prices. No adjustment applied, ever."""

    def get_prices(
        self,
        listing_ids: list[str] | None,
        start: dt.date,
        end: dt.date,
    ) -> pd.DataFrame:
        """Return a frame conforming to `PRICE_SCHEMA`.

        `listing_ids=None` means the full universe, including securities that had
        delisted before `end`. A provider that silently drops delisted names is
        survivorship-biased and unusable for index work.
        """
        ...


@runtime_checkable
class SharesProvider(Protocol):
    """Shares in issue, free float and any foreign ownership limit."""

    def get_shares(
        self,
        security_ids: list[str] | None,
        as_of: dt.date,
    ) -> pd.DataFrame:
        """Latest row per security with `knowledge_date <= as_of`."""
        ...

    def get_shares_history(
        self,
        security_ids: list[str] | None,
        start: dt.date,
        end: dt.date,
    ) -> pd.DataFrame: ...


@runtime_checkable
class FundamentalProvider(Protocol):
    """Point-in-time company fundamentals."""

    def get_fundamentals(
        self,
        security_ids: list[str] | None,
        items: list[str],
        as_of: dt.date,
        max_staleness_days: int = 550,
    ) -> pd.DataFrame:
        """Most recent filing per (security, item) known on `as_of`.

        The contract that matters: no row where `filed_date > as_of` may ever be
        returned, regardless of `period_end`. That single rule is what separates a
        research platform from a backtest that quietly cheats.
        """
        ...


@runtime_checkable
class CorpActionProvider(Protocol):
    def get_corp_actions(
        self,
        security_ids: list[str] | None,
        start: dt.date,
        end: dt.date,
    ) -> pd.DataFrame: ...


@runtime_checkable
class FxProvider(Protocol):
    def get_fx(
        self, base: str, quotes: list[str], start: dt.date, end: dt.date
    ) -> pd.DataFrame: ...

    def get_deposit_rates(
        self, currencies: list[str], start: dt.date, end: dt.date
    ) -> pd.DataFrame:
        """Short rates, used to synthesise forwards via covered interest parity for the
        currency-hedged index variant."""
        ...


@runtime_checkable
class ClassificationProvider(Protocol):
    def get_classifications(
        self, security_ids: list[str] | None, as_of: dt.date
    ) -> pd.DataFrame: ...


@runtime_checkable
class ReferenceProvider(Protocol):
    """The security master tables themselves."""

    def get_issuers(self) -> pd.DataFrame: ...
    def get_securities(self) -> pd.DataFrame: ...
    def get_listings(self) -> pd.DataFrame: ...
    def get_identifier_map(self) -> pd.DataFrame: ...


@runtime_checkable
class MarketDataProvider(
    PriceProvider,
    SharesProvider,
    FundamentalProvider,
    CorpActionProvider,
    FxProvider,
    ClassificationProvider,
    ReferenceProvider,
    Protocol,
):
    """Everything the index engine needs from the outside world.

    A single composite Protocol rather than seven constructor arguments. Implementations
    may compose several underlying vendors; the engine does not care.
    """

    @property
    def name(self) -> str: ...


@runtime_checkable
class UniverseData(MarketDataProvider, Protocol):
    """A complete, self-consistent universe an index can be built from.

    `MarketDataProvider` says what you can *ask* - as-of queries, one security at a
    time. This says what you can *hold*: the whole panel, the calendar it spans, and an
    identity you can pin a regression test to.

    The distinction is not academic. `IndexCalculator.run` walks a full price panel day
    by day; asking a provider for one date at a time would be both wrong (the calculator
    needs to see delistings coming) and unusably slow. Before this Protocol existed the
    pipeline got that panel by reaching into `SyntheticUniverse._generated`, which meant
    the swappability documented at the top of this module was not real.

    Implementations: `data.synthetic.SyntheticUniverse` (generates), and
    `data.materialised.MaterialisedUniverse` (loads a parquet snapshot, whether that
    snapshot came from the generator or from `data.real`).
    """

    @property
    def prices(self) -> pd.DataFrame:
        """Full price panel, `PRICE_SCHEMA`, every security including delisted ones."""
        ...

    @property
    def shares(self) -> pd.DataFrame:
        """Full bitemporal shares history, `SHARES_SCHEMA`."""
        ...

    @property
    def corp_actions(self) -> pd.DataFrame:
        """Every event, `CORP_ACTION_SCHEMA`."""
        ...

    @property
    def fundamentals(self) -> pd.DataFrame:
        """Full filing history, `FUNDAMENTAL_SCHEMA`, carrying `filed_date`."""
        ...

    @property
    def fx_rates(self) -> pd.DataFrame:
        """Full FX panel, `FX_SCHEMA` plus `deposit_rate`."""
        ...

    @property
    def fingerprint(self) -> str:
        """Stable identity of the data, recorded in the run manifest.

        A generator hashes its config; a snapshot hashes its files. Either way, two
        builds carrying the same fingerprint were built from the same numbers - which
        is the property an audit years later actually needs.
        """
        ...

    @property
    def calendar(self) -> pd.DatetimeIndex: ...

    @property
    def start(self) -> dt.date: ...

    @property
    def end(self) -> dt.date: ...

    def summary(self) -> dict[str, object]: ...

    def materialise(self, path: Path) -> dict[str, Path]:
        """Write every table to parquet, in the layout `MaterialisedUniverse` reads."""
        ...


__all__ = [
    "CLASSIFICATION_SCHEMA",
    "CORP_ACTION_SCHEMA",
    "FUNDAMENTAL_SCHEMA",
    "FX_SCHEMA",
    "PRICE_SCHEMA",
    "SHARES_SCHEMA",
    "ClassificationProvider",
    "CorpActionProvider",
    "FundamentalProvider",
    "FxProvider",
    "MarketDataProvider",
    "PriceProvider",
    "ReferenceProvider",
    "SharesProvider",
    "UniverseData",
]
