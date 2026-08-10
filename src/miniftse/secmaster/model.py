"""The issuer / security / listing hierarchy.

Three levels, because index rules operate at three different levels and collapsing them
is the root cause of a large share of index data incidents:

* **Issuer** - the legal entity. Concentration limits and issuer-level exposure apply here.
* **Security** - the share class. Free float, dividends and fundamentals attach here.
* **Listing** - the security on a venue. Price, currency and trading calendar come from
  here, and this is the thing an index actually holds.

The classic failure: applying a 10% cap to Alphabet by capping GOOGL at 10% and GOOG at
10%, giving the issuer 20%. The cap is an issuer-level constraint enforced on
listing-level weights, and the code has to be able to say so.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from miniftse.secmaster.identifiers import IdentifierSet
from miniftse.types import (
    Country,
    Currency,
    IcbIndustry,
    IssuerId,
    ListingId,
    MarketStatus,
    SecurityId,
    SecurityType,
)


@dataclass(frozen=True, slots=True)
class Issuer:
    """A legal entity."""

    issuer_id: IssuerId
    name: str
    country_of_incorporation: Country
    country_of_domicile: Country
    nationality: Country
    """The index's assigned nationality, which may equal neither of the above.
    See `secmaster.nationality` - this is a judgement with a published rule, not a
    lookup, and it determines index family membership and therefore capital flows."""

    market_status: MarketStatus
    lei: str | None = None
    perm_id: str | None = None

    @property
    def is_offshore_incorporated(self) -> bool:
        """Incorporated in a domicile of convenience. Not disqualifying, but it means
        incorporation carries no information about where the company really is, so the
        nationality rule must lean on other evidence."""
        return self.country_of_incorporation in {Country.KY, Country.BM}


@dataclass(frozen=True, slots=True)
class Security:
    """A share class."""

    security_id: SecurityId
    issuer_id: IssuerId
    name: str
    security_type: SecurityType
    currency: Currency
    icb_industry: IcbIndustry
    is_primary_line: bool = True
    """False for a secondary share class. Some index families include every line;
    others take only the primary. Both are defensible; the rule must be published."""

    votes_per_share: float = 1.0
    shares_outstanding: float = 0.0
    free_float_factor: float = 1.0
    foreign_ownership_limit: float = 1.0

    @property
    def investable_factor(self) -> float:
        """Free float after any foreign ownership limit.

        The FOL binds independently of free float: a name can be 80% free-floating and
        still capped at 30% for foreign investors, and it is the smaller of the two
        that a global index can actually buy.
        """
        return min(self.free_float_factor, self.foreign_ownership_limit)

    @property
    def investable_shares(self) -> float:
        return self.shares_outstanding * self.investable_factor


@dataclass(frozen=True, slots=True)
class Listing:
    """A security trading on a venue."""

    listing_id: ListingId
    security_id: SecurityId
    mic: str
    currency: Currency
    country: Country
    is_primary_listing: bool = True
    listing_start: dt.date | None = None
    listing_end: dt.date | None = None
    identifiers: IdentifierSet = field(default_factory=IdentifierSet)

    def is_active(self, as_of: dt.date) -> bool:
        if self.listing_start is not None and as_of < self.listing_start:
            return False
        return not (self.listing_end is not None and as_of >= self.listing_end)


@dataclass(frozen=True, slots=True)
class IdentifierMapping:
    """One identifier's validity interval - a Type-2 slowly changing dimension row.

    `valid_to` is exclusive and `None` means still current. Tickers get recycled, ISINs
    change on redomicile, and SEDOLs change when a line moves market, so every mapping
    is temporary until proven otherwise.
    """

    identifier_type: str
    identifier_value: str
    security_id: SecurityId
    listing_id: ListingId | None
    valid_from: dt.date
    valid_to: dt.date | None = None

    def covers(self, as_of: dt.date) -> bool:
        return self.valid_from <= as_of and (self.valid_to is None or as_of < self.valid_to)


@dataclass(frozen=True, slots=True)
class ResolvedSecurity:
    """The full picture for one security at one instant, as the master resolved it."""

    as_of: dt.date
    issuer: Issuer
    security: Security
    listing: Listing
    matched_on: str
    """Which identifier type produced the match. Recorded because a match on ticker is
    far weaker evidence than a match on SEDOL, and an incident investigation needs to
    know which one it was."""

    @property
    def keys(self) -> dict[str, str]:
        return {
            "issuer_id": str(self.issuer.issuer_id),
            "security_id": str(self.security.security_id),
            "listing_id": str(self.listing.listing_id),
        }
