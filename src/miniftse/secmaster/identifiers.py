"""Identifier validation, check digits, and normalisation.

Three schemes, three different check-digit algorithms, and a normalisation problem that
causes more join failures than any of them. `BRK.B`, `BRK/B`, `BRK-B` and `BRK B` are
the same listing to a human and four different keys to a `pd.merge`.

Validating identifiers at the data boundary is cheap and catches a whole class of
incident before it reaches the index: a truncated SEDOL, a transposed ISIN, a CUSIP
pasted from a spreadsheet that dropped a leading zero.
"""

from __future__ import annotations

import re
import string
from dataclasses import dataclass

from miniftse.types import Cusip, Isin, Sedol

_ALNUM_VALUE: dict[str, int] = {
    **{c: int(c) for c in string.digits},
    **{c: 10 + i for i, c in enumerate(string.ascii_uppercase)},
}

#: SEDOL deliberately excludes vowels so that generated codes cannot spell words.
_SEDOL_ALLOWED = frozenset(string.digits + "BCDFGHJKLMNPQRSTVWXYZ")
_SEDOL_WEIGHTS = (1, 3, 1, 7, 3, 9, 1)

_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
_SEDOL_RE = re.compile(r"^[0-9BCDFGHJKLMNPQRSTVWXYZ]{6}[0-9]$")
_CUSIP_RE = re.compile(r"^[A-Z0-9*@#]{8}[0-9]$")


class InvalidIdentifierError(ValueError):
    """Raised when an identifier fails structural or check-digit validation."""


# --------------------------------------------------------------------------------------
# Check digits
# --------------------------------------------------------------------------------------


def _luhn_check_digit(digits: str) -> int:
    """Luhn modulus-10, doubling every second digit from the right."""
    total = 0
    for pos, ch in enumerate(reversed(digits)):
        d = int(ch)
        if pos % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return (10 - total % 10) % 10


def isin_check_digit(body: str) -> int:
    """Check digit for the 11-character body of an ISIN (country code + NSIN).

    Letters expand to two digits before the Luhn pass, which is why `US0378331005`
    and a naive digit-only Luhn disagree.
    """
    body = body.upper()
    expanded = "".join(str(_ALNUM_VALUE[c]) for c in body)
    return _luhn_check_digit(expanded)


def sedol_check_digit(body: str) -> int:
    """Weighted modulus-10 over the 6-character SEDOL body."""
    body = body.upper()
    total = sum(_ALNUM_VALUE[c] * w for c, w in zip(body, _SEDOL_WEIGHTS[:6]))
    return (10 - total % 10) % 10


def cusip_check_digit(body: str) -> int:
    """Modified Luhn over the 8-character CUSIP body."""
    body = body.upper()
    extra = {"*": 36, "@": 37, "#": 38}
    total = 0
    for i, ch in enumerate(body):
        v = extra.get(ch, _ALNUM_VALUE.get(ch, -1))
        if v < 0:
            raise InvalidIdentifierError(f"illegal CUSIP character {ch!r}")
        if i % 2 == 1:
            v *= 2
        total += v // 10 + v % 10
    return (10 - total % 10) % 10


# --------------------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------------------


def validate_isin(value: str, *, strict: bool = True) -> bool:
    v = value.strip().upper()
    if not _ISIN_RE.match(v):
        if strict:
            raise InvalidIdentifierError(f"{value!r} is not a well-formed ISIN")
        return False
    if isin_check_digit(v[:11]) != int(v[11]):
        if strict:
            raise InvalidIdentifierError(f"{value!r} has a bad ISIN check digit")
        return False
    return True


def validate_sedol(value: str, *, strict: bool = True) -> bool:
    v = value.strip().upper()
    if not _SEDOL_RE.match(v) or set(v[:6]) - _SEDOL_ALLOWED:
        if strict:
            raise InvalidIdentifierError(f"{value!r} is not a well-formed SEDOL")
        return False
    if sedol_check_digit(v[:6]) != int(v[6]):
        if strict:
            raise InvalidIdentifierError(f"{value!r} has a bad SEDOL check digit")
        return False
    return True


def validate_cusip(value: str, *, strict: bool = True) -> bool:
    v = value.strip().upper()
    if not _CUSIP_RE.match(v):
        if strict:
            raise InvalidIdentifierError(f"{value!r} is not a well-formed CUSIP")
        return False
    if cusip_check_digit(v[:8]) != int(v[8]):
        if strict:
            raise InvalidIdentifierError(f"{value!r} has a bad CUSIP check digit")
        return False
    return True


# --------------------------------------------------------------------------------------
# Construction
# --------------------------------------------------------------------------------------


def make_isin(country: str, serial: int) -> Isin:
    """Build a structurally valid ISIN. For the synthetic universe only."""
    body = f"{country.upper()[:2]}{serial:09d}"
    return Isin(f"{body}{isin_check_digit(body)}")


def make_sedol(serial: int) -> Sedol:
    body = f"{serial % 1_000_000:06d}"
    return Sedol(f"{body}{sedol_check_digit(body)}")


def make_cusip(serial: int) -> Cusip:
    body = f"{serial % 100_000_000:08d}"
    return Cusip(f"{body}{cusip_check_digit(body)}")


def cusip_to_isin(cusip: str, country: str = "US") -> Isin:
    """A US ISIN is the country code plus the CUSIP plus a fresh check digit.

    The relationship is one-way in practice: not every ISIN embeds a CUSIP.
    """
    validate_cusip(cusip)
    body = f"{country.upper()}{cusip.upper()}"
    return Isin(f"{body}{isin_check_digit(body)}")


# --------------------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------------------

_TICKER_SEPARATORS = re.compile(r"[.\-/_ ]+")


def normalise_ticker(ticker: str, *, keep_suffix: bool = False) -> str:
    """Collapse the share-class separator zoo to a single canonical form.

    `keep_suffix=False` strips the class marker entirely, which is what you want when
    matching an issuer and emphatically not what you want when matching a security -
    `BRK.A` and `BRK.B` are different objects with a four-order-of-magnitude price gap.
    """
    t = ticker.strip().upper()
    if keep_suffix:
        return _TICKER_SEPARATORS.sub(".", t)
    return _TICKER_SEPARATORS.split(t)[0]


def normalise_isin(value: str) -> Isin:
    v = value.strip().upper().replace(" ", "")
    validate_isin(v)
    return Isin(v)


@dataclass(frozen=True, slots=True)
class IdentifierSet:
    """The identifiers attached to one listing, with the level each one keys on.

    The level column is the useful part. An ISIN cannot distinguish two listings of the
    same security, so keying a global index on ISIN silently merges the London and
    Frankfurt lines of the same company - different currency, different calendar,
    different price.
    """

    isin: Isin | None = None
    sedol: Sedol | None = None
    cusip: Cusip | None = None
    ticker: str | None = None
    ric: str | None = None
    perm_id: str | None = None
    figi: str | None = None

    def validate(self) -> None:
        if self.isin is not None:
            validate_isin(self.isin)
        if self.sedol is not None:
            validate_sedol(self.sedol)
        if self.cusip is not None:
            validate_cusip(self.cusip)

    @property
    def primary_key(self) -> str:
        """SEDOL first: it is the only common identifier that keys on the listing.

        This ordering is a design decision, not a convention. See DECISIONS.md D-003.
        """
        for candidate in (self.sedol, self.isin, self.cusip, self.ric, self.ticker):
            if candidate:
                return str(candidate)
        raise InvalidIdentifierError("identifier set is empty")


IDENTIFIER_LEVELS: dict[str, str] = {
    "isin": "security",
    "sedol": "listing",
    "cusip": "security",
    "ticker": "listing",
    "ric": "listing",
    "perm_id": "issuer",
    "figi": "listing",
    "figi_composite": "security",
    "lei": "issuer",
}
"""Which level of the issuer/security/listing hierarchy each identifier keys on.

Consulted by `SecurityMaster.resolve` to decide what a lookup can legitimately return.
Asking for "the listing" given an ISIN is ambiguous by construction, and the master
raises rather than guessing.
"""
