"""The security master: bitemporal identifier resolution over the three-level hierarchy.

The contract that makes this useful is narrow and strict:

    ``resolve(identifier, as_of)`` returns what that identifier meant **on that date**,
    or raises. It never returns today's meaning for a historical date, and it never
    guesses when an identifier is ambiguous at the level requested.

Everything else follows from that. A ticker recycled from a delisted company to a new
IPO resolves to two different securities depending on the date. An ISIN maps to a
security but not to a listing, so asking it for a listing raises rather than picking one
arbitrarily.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

import pandas as pd

from miniftse.secmaster.identifiers import (
    IDENTIFIER_LEVELS,
    IdentifierSet,
    normalise_ticker,
)
from miniftse.secmaster.model import (
    IdentifierMapping,
    Issuer,
    Listing,
    ResolvedSecurity,
    Security,
)
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


class SecurityNotFoundError(KeyError):
    """No mapping for this identifier on this date."""


class AmbiguousIdentifierError(ValueError):
    """The identifier resolves to more than one object at the level requested.

    Raised rather than resolved. An ISIN covering three listings is not a defect in the
    data - it is a defect in the question, and answering it arbitrarily produces an
    index that holds the wrong line in the wrong currency.
    """


@dataclass
class SecurityMaster:
    """In-memory master with as-of resolution.

    Built from frames rather than a live database so it can be constructed from any
    provider, snapshotted into a run manifest, and diffed between vintages.
    """

    issuers: dict[IssuerId, Issuer] = field(default_factory=dict)
    securities: dict[SecurityId, Security] = field(default_factory=dict)
    listings: dict[ListingId, Listing] = field(default_factory=dict)
    mappings: list[IdentifierMapping] = field(default_factory=list)

    _by_value: dict[tuple[str, str], list[IdentifierMapping]] = field(
        default_factory=lambda: defaultdict(list), repr=False
    )
    _by_security: dict[SecurityId, list[ListingId]] = field(
        default_factory=lambda: defaultdict(list), repr=False
    )
    _by_issuer: dict[IssuerId, list[SecurityId]] = field(
        default_factory=lambda: defaultdict(list), repr=False
    )

    # ---------------------------------------------------------------- construction

    def add_issuer(self, issuer: Issuer) -> None:
        self.issuers[issuer.issuer_id] = issuer

    def add_security(self, security: Security) -> None:
        self.securities[security.security_id] = security
        if security.security_id not in self._by_issuer[security.issuer_id]:
            self._by_issuer[security.issuer_id].append(security.security_id)

    def add_listing(self, listing: Listing) -> None:
        self.listings[listing.listing_id] = listing
        if listing.listing_id not in self._by_security[listing.security_id]:
            self._by_security[listing.security_id].append(listing.listing_id)

    def add_mapping(self, mapping: IdentifierMapping) -> None:
        """Insert a mapping, closing any open interval it supersedes.

        The auto-close is the whole value of a Type-2 dimension. Without it, a recycled
        ticker has two open rows and every as-of lookup after the reassignment is
        ambiguous.
        """
        key = (mapping.identifier_type, self._normalise(mapping.identifier_type,
                                                        mapping.identifier_value))
        existing = self._by_value[key]
        for i, prior in enumerate(existing):
            if prior.valid_to is None and prior.valid_from < mapping.valid_from:
                closed = IdentifierMapping(
                    identifier_type=prior.identifier_type,
                    identifier_value=prior.identifier_value,
                    security_id=prior.security_id,
                    listing_id=prior.listing_id,
                    valid_from=prior.valid_from,
                    valid_to=mapping.valid_from,
                )
                existing[i] = closed
                self.mappings[self.mappings.index(prior)] = closed
        existing.append(mapping)
        self.mappings.append(mapping)

    @staticmethod
    def _normalise(id_type: str, value: str) -> str:
        if id_type == "ticker":
            return normalise_ticker(value, keep_suffix=True)
        return value.strip().upper()

    # ---------------------------------------------------------------- resolution

    def resolve(
        self,
        identifier: str,
        as_of: dt.date,
        *,
        id_type: str | None = None,
        level: str = "listing",
    ) -> ResolvedSecurity:
        """Resolve an identifier as it stood on `as_of`.

        `id_type=None` tries every scheme in a fixed precedence order, most specific
        first. Explicit is better: an unqualified 7-character string could be a SEDOL or
        a ticker, and the guess is not always right.
        """
        candidates = self._candidates(identifier, as_of, id_type)
        if not candidates:
            raise SecurityNotFoundError(
                f"{identifier!r} ({id_type or 'any type'}) has no mapping valid on {as_of}"
            )

        matched_type, matched = candidates[0]
        security_ids = {m.security_id for _, m in candidates}
        if len(security_ids) > 1:
            raise AmbiguousIdentifierError(
                f"{identifier!r} maps to {len(security_ids)} securities on {as_of}: "
                f"{sorted(security_ids)}"
            )

        security = self.securities.get(matched.security_id)
        if security is None:
            raise SecurityNotFoundError(f"security {matched.security_id} is not in the master")
        issuer = self.issuers.get(security.issuer_id)
        if issuer is None:
            raise SecurityNotFoundError(f"issuer {security.issuer_id} is not in the master")

        listing = self._pick_listing(matched, security.security_id, as_of, matched_type, level)
        return ResolvedSecurity(as_of=as_of, issuer=issuer, security=security,
                                listing=listing, matched_on=matched_type)

    def _candidates(
        self, identifier: str, as_of: dt.date, id_type: str | None
    ) -> list[tuple[str, IdentifierMapping]]:
        types: Sequence[str] = (
            [id_type] if id_type else ["sedol", "isin", "cusip", "ric", "figi", "ticker"]
        )
        found: list[tuple[str, IdentifierMapping]] = []
        for t in types:
            key = (t, self._normalise(t, identifier))
            for m in self._by_value.get(key, []):
                if m.covers(as_of):
                    found.append((t, m))
            if found:
                break  # most specific scheme wins
        return found

    def _pick_listing(
        self,
        mapping: IdentifierMapping,
        security_id: SecurityId,
        as_of: dt.date,
        matched_type: str,
        level: str,
    ) -> Listing:
        if mapping.listing_id is not None:
            listing = self.listings.get(mapping.listing_id)
            if listing is None:
                raise SecurityNotFoundError(f"listing {mapping.listing_id} is not in the master")
            return listing

        active = [
            self.listings[lid]
            for lid in self._by_security.get(security_id, [])
            if self.listings[lid].is_active(as_of)
        ]
        if not active:
            raise SecurityNotFoundError(
                f"security {security_id} has no active listing on {as_of}"
            )
        if len(active) == 1:
            return active[0]

        primary = [x for x in active if x.is_primary_listing]
        if level == "listing" and len(primary) != 1:
            # This is the ISIN case: the identifier keys on the security, and the
            # security has several lines. Refuse rather than pick.
            raise AmbiguousIdentifierError(
                f"{matched_type} keys on the "
                f"{IDENTIFIER_LEVELS.get(matched_type, 'security')} level; "
                f"security {security_id} has {len(active)} active listings on {as_of} "
                f"({[str(x.listing_id) for x in active]}). Resolve with a listing-level "
                f"identifier such as a SEDOL, or request level='security'."
            )
        return primary[0] if primary else active[0]

    # ---------------------------------------------------------------- navigation

    def listings_for_security(self, security_id: SecurityId, as_of: dt.date) -> list[Listing]:
        return [
            self.listings[lid]
            for lid in self._by_security.get(security_id, [])
            if self.listings[lid].is_active(as_of)
        ]

    def securities_for_issuer(self, issuer_id: IssuerId) -> list[Security]:
        return [self.securities[sid] for sid in self._by_issuer.get(issuer_id, [])]

    def issuer_of(self, security_id: SecurityId) -> Issuer:
        return self.issuers[self.securities[security_id].issuer_id]

    def sibling_lines(self, security_id: SecurityId) -> list[Security]:
        """Other share classes of the same issuer.

        This is what makes issuer-level capping possible. A 10% issuer cap on Alphabet
        has to see GOOGL and GOOG as one object.
        """
        sec = self.securities[security_id]
        return [s for s in self.securities_for_issuer(sec.issuer_id)
                if s.security_id != security_id]

    def multiline_issuers(self) -> dict[IssuerId, list[SecurityId]]:
        return {iss: sids for iss, sids in self._by_issuer.items() if len(sids) > 1}

    # ---------------------------------------------------------------- history

    def identifier_history(self, identifier: str, id_type: str) -> list[IdentifierMapping]:
        key = (id_type, self._normalise(id_type, identifier))
        return sorted(self._by_value.get(key, []), key=lambda m: m.valid_from)

    def as_of_snapshot(self, as_of: dt.date) -> pd.DataFrame:
        """Every active listing on a date, flattened for joining to market data."""
        rows = []
        for listing in self.listings.values():
            if not listing.is_active(as_of):
                continue
            sec = self.securities.get(listing.security_id)
            if sec is None:
                continue
            iss = self.issuers.get(sec.issuer_id)
            rows.append({
                "listing_id": str(listing.listing_id),
                "security_id": str(sec.security_id),
                "issuer_id": str(sec.issuer_id),
                "currency": str(listing.currency),
                "country": str(iss.nationality if iss else listing.country),
                "market_status": str(iss.market_status) if iss else None,
                "icb_industry": str(sec.icb_industry),
                "security_type": str(sec.security_type),
                "shares_outstanding": sec.shares_outstanding,
                "free_float_factor": sec.free_float_factor,
                "foreign_ownership_limit": sec.foreign_ownership_limit,
                "investable_factor": sec.investable_factor,
                "is_primary_line": sec.is_primary_line,
                "isin": listing.identifiers.isin,
                "sedol": listing.identifiers.sedol,
            })
        return pd.DataFrame(rows)

    # ---------------------------------------------------------------- loading

    @classmethod
    def from_provider(cls, provider: object, as_of: dt.date | None = None) -> SecurityMaster:
        """Build a master from anything implementing the reference Protocols."""
        master = cls()
        securities = provider.get_securities()  # type: ignore[attr-defined]
        listings = provider.get_listings()  # type: ignore[attr-defined]
        identifiers = provider.get_identifier_map()  # type: ignore[attr-defined]

        as_of = as_of or dt.date.today()
        shares = provider.get_shares(None, as_of)  # type: ignore[attr-defined]
        share_lookup = shares.set_index("security_id")[
            ["shares_outstanding", "free_float_factor", "foreign_ownership_limit"]
        ].to_dict("index")

        for row in securities.itertuples(index=False):
            iid = IssuerId(str(row.issuer_id))
            if iid not in master.issuers:
                country = Country(str(row.country))
                master.add_issuer(Issuer(
                    issuer_id=iid, name=f"Issuer {iid}",
                    country_of_incorporation=country, country_of_domicile=country,
                    nationality=country, market_status=MarketStatus(str(row.market_status)),
                ))
            sh = share_lookup.get(str(row.security_id), {})
            master.add_security(Security(
                security_id=SecurityId(str(row.security_id)),
                issuer_id=iid, name=f"Security {row.security_id}",
                security_type=SecurityType(str(row.security_type)),
                currency=Currency(str(row.currency)),
                icb_industry=IcbIndustry(str(row.icb_industry)),
                is_primary_line=True,
                shares_outstanding=float(sh.get("shares_outstanding", 0.0) or 0.0),
                free_float_factor=float(sh.get("free_float_factor", 1.0) or 1.0),
                foreign_ownership_limit=float(
                    sh.get("foreign_ownership_limit", getattr(row, "foreign_ownership_limit", 1.0))
                    or 1.0
                ),
            ))

        ident_by_listing = {
            str(r.listing_id): r for r in identifiers.itertuples(index=False)
        }
        for row in listings.itertuples(index=False):
            ident = ident_by_listing.get(str(row.listing_id))
            master.add_listing(Listing(
                listing_id=ListingId(str(row.listing_id)),
                security_id=SecurityId(str(row.security_id)),
                mic=str(row.mic), currency=Currency(str(row.currency)),
                country=Country(str(row.country)), is_primary_listing=True,
                listing_start=row.listing_start, listing_end=row.listing_end,
                identifiers=IdentifierSet(
                    isin=ident.isin if ident is not None else None,
                    sedol=ident.sedol if ident is not None else None,
                    ticker=ident.ticker if ident is not None else None,
                ),
            ))

        for r in identifiers.itertuples(index=False):
            for id_type in ("isin", "sedol", "ticker"):
                value = getattr(r, id_type, None)
                if not value:
                    continue
                master.add_mapping(IdentifierMapping(
                    identifier_type=id_type, identifier_value=str(value),
                    security_id=SecurityId(str(r.security_id)),
                    # Only listing-level identifiers pin a listing. An ISIN does not,
                    # which is exactly what makes it ambiguous for a multi-listed name.
                    listing_id=(ListingId(str(r.listing_id))
                                if IDENTIFIER_LEVELS.get(id_type) == "listing" else None),
                    valid_from=r.valid_from, valid_to=r.valid_to,
                ))
        return master

    def validate(self) -> list[str]:
        """Referential integrity and identifier-format problems, as a list of findings.

        Returns rather than raises: a master with three broken rows out of fifty thousand
        should be reported and triaged, not made unloadable.
        """
        problems: list[str] = []
        for sec in self.securities.values():
            if sec.issuer_id not in self.issuers:
                problems.append(f"security {sec.security_id} references missing issuer "
                                f"{sec.issuer_id}")
            if not 0.0 <= sec.free_float_factor <= 1.0:
                problems.append(f"security {sec.security_id} has free float "
                                f"{sec.free_float_factor} outside [0, 1]")
        for lst in self.listings.values():
            if lst.security_id not in self.securities:
                problems.append(f"listing {lst.listing_id} references missing security "
                                f"{lst.security_id}")
            try:
                lst.identifiers.validate()
            except ValueError as exc:
                problems.append(f"listing {lst.listing_id}: {exc}")

        overlaps = self._overlapping_mappings()
        problems.extend(
            f"identifier {t}:{v} has overlapping validity intervals on {n} rows"
            for (t, v), n in overlaps.items()
        )
        return problems

    def _overlapping_mappings(self) -> dict[tuple[str, str], int]:
        out: dict[tuple[str, str], int] = {}
        for key, maps in self._by_value.items():
            ordered = sorted(maps, key=lambda m: m.valid_from)
            for a, b in zip(ordered, ordered[1:]):
                if a.valid_to is None or a.valid_to > b.valid_from:
                    out[key] = out.get(key, 1) + 1
        return out

    def __len__(self) -> int:
        return len(self.securities)

    def summary(self) -> dict[str, int]:
        return {
            "issuers": len(self.issuers),
            "securities": len(self.securities),
            "listings": len(self.listings),
            "mappings": len(self.mappings),
            "multiline_issuers": len(self.multiline_issuers()),
        }


def build_from_frames(
    issuers: Iterable[Issuer],
    securities: Iterable[Security],
    listings: Iterable[Listing],
    mappings: Iterable[IdentifierMapping],
) -> SecurityMaster:
    master = SecurityMaster()
    for i in issuers:
        master.add_issuer(i)
    for s in securities:
        master.add_security(s)
    for lst in listings:
        master.add_listing(lst)
    for m in sorted(mappings, key=lambda x: x.valid_from):
        master.add_mapping(m)
    return master
