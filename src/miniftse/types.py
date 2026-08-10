"""Domain primitives.

Everything here is a `float` or a `str` to the interpreter and something specific to a
human. That gap is where production index bugs live: a free-float factor multiplied by a
capping factor is meaningful, a price multiplied by a divisor is not, and nothing in
`float * float` will tell you which one you just wrote.

`NewType` costs nothing at runtime and makes `mypy --strict` enforce the distinction.
"""

from __future__ import annotations

import datetime as dt
from enum import Enum, StrEnum
from typing import Final, NewType

# --------------------------------------------------------------------------------------
# Identifier types
# --------------------------------------------------------------------------------------

IssuerId = NewType("IssuerId", str)
"""Legal entity. One per company, stable across renames and redomiciles."""

SecurityId = NewType("SecurityId", str)
"""Share class. Alphabet A and Alphabet C are two SecurityIds under one IssuerId."""

ListingId = NewType("ListingId", str)
"""Security on a venue. This is what an index actually holds - it fixes the price,
the currency and the trading calendar."""

Isin = NewType("Isin", str)
Sedol = NewType("Sedol", str)
Cusip = NewType("Cusip", str)
Ric = NewType("Ric", str)
PermId = NewType("PermId", str)
Ticker = NewType("Ticker", str)

# --------------------------------------------------------------------------------------
# Quantity types
# --------------------------------------------------------------------------------------

Price = NewType("Price", float)
"""Traded price, in the listing's own currency. Never adjusted in place."""

Shares = NewType("Shares", float)
"""Shares in issue for a security, as at a point in time."""

Weight = NewType("Weight", float)
"""Portfolio or index weight. Fractional, so 0.05 is five percent."""

FloatFactor = NewType("FloatFactor", float)
"""Investable proportion of shares in issue, in [0, 1], after removing strategic
holdings and applying any foreign ownership limit."""

CappingFactor = NewType("CappingFactor", float)
"""Multiplicative factor applied post-float to enforce concentration limits. Unlike a
float factor this is an index-design artefact, not a fact about the company."""

Divisor = NewType("Divisor", float)
"""The denominator that absorbs every non-market change in index market value, so the
published level stays continuous. Has no economic meaning on its own."""

FxRate = NewType("FxRate", float)
"""Units of base currency per one unit of quote currency."""

BasisPoints = NewType("BasisPoints", float)

EPSILON: Final[float] = 1e-12
"""Comparison tolerance for weight and divisor arithmetic."""

WEIGHT_SUM_TOLERANCE: Final[float] = 1e-9
"""How far the sum of weights may drift from 1.0 before we treat it as a defect."""


# --------------------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------------------


class Currency(StrEnum):
    """ISO 4217 currencies used by the reference universe."""

    USD = "USD"
    GBP = "GBP"
    EUR = "EUR"
    JPY = "JPY"
    CHF = "CHF"
    CAD = "CAD"
    AUD = "AUD"
    HKD = "HKD"
    SEK = "SEK"
    KRW = "KRW"


class Country(StrEnum):
    """ISO 3166-1 alpha-2. Country of *nationality* as assigned by the index, which is
    a judgement, not a lookup - see `secmaster.nationality`."""

    US = "US"
    GB = "GB"
    DE = "DE"
    FR = "FR"
    JP = "JP"
    CH = "CH"
    CA = "CA"
    AU = "AU"
    HK = "HK"
    SE = "SE"
    NL = "NL"
    IE = "IE"
    KR = "KR"
    KY = "KY"  # Cayman - incorporation domicile of convenience, rarely the nationality
    BM = "BM"  # Bermuda - likewise


class MarketStatus(StrEnum):
    """Development classification. Drives eligibility thresholds and index family."""

    DEVELOPED = "DEVELOPED"
    ADVANCED_EMERGING = "ADVANCED_EMERGING"
    SECONDARY_EMERGING = "SECONDARY_EMERGING"
    FRONTIER = "FRONTIER"


class SecurityType(StrEnum):
    """Instrument form. Index eligibility rules exclude several of these outright."""

    ORDINARY = "ORDINARY"
    PREFERRED = "PREFERRED"
    ADR = "ADR"
    GDR = "GDR"
    REIT = "REIT"
    UNIT_TRUST = "UNIT_TRUST"
    INVESTMENT_TRUST = "INVESTMENT_TRUST"
    WARRANT = "WARRANT"
    CONVERTIBLE = "CONVERTIBLE"


class SizeBand(StrEnum):
    """Capitalisation band. Membership is sticky - see `universe.banding` for buffers."""

    LARGE = "LARGE"
    MID = "MID"
    SMALL = "SMALL"
    MICRO = "MICRO"
    INELIGIBLE = "INELIGIBLE"


class ReturnType(StrEnum):
    """Price return, gross total return, net total return.

    NTR applies withholding tax at the rate of the *issuer's* country of domicile, and
    represents the position of a notional non-resident institutional investor who cannot
    reclaim treaty relief. It is not any real investor's after-tax return.
    """

    PRICE = "PR"
    GROSS_TOTAL = "GTR"
    NET_TOTAL = "NTR"


class IcbIndustry(StrEnum):
    """ICB level 1. FTSE Russell's classification scheme; the analogue of GICS sectors.

    Reclassification is a real source of index turnover, which is why it is modelled as
    a point-in-time attribute rather than a static one.
    """

    TECHNOLOGY = "10"
    TELECOMMUNICATIONS = "15"
    HEALTH_CARE = "20"
    FINANCIALS = "30"
    REAL_ESTATE = "35"
    CONSUMER_DISCRETIONARY = "40"
    CONSUMER_STAPLES = "45"
    INDUSTRIALS = "50"
    BASIC_MATERIALS = "55"
    ENERGY = "60"
    UTILITIES = "65"


class Severity(Enum):
    """Validation outcome severity. `BLOCK` stops publication; `ESCALATE` stops
    publication *and* pages a human."""

    INFO = 10
    WARN = 20
    BLOCK = 30
    ESCALATE = 40

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Severity):
            return NotImplemented
        return self.value < other.value

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, Severity):
            return NotImplemented
        return self.value >= other.value


# --------------------------------------------------------------------------------------
# Calendar helpers
# --------------------------------------------------------------------------------------

Date = dt.date


def is_month_end(d: Date) -> bool:
    return (d + dt.timedelta(days=1)).month != d.month


def quarter_of(d: Date) -> int:
    return (d.month - 1) // 3 + 1
