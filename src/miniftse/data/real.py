"""Build a universe snapshot from real market data, using only free sources.

This is the honest counterpart to `data.synthetic`. It produces the same nine tables,
so `MaterialisedUniverse` loads it and the index engine cannot tell the difference - but
where the synthetic universe is *correct by construction*, this one is correct only
where the free sources happen to be, and the difference is the interesting part.

Sources, and what each is actually good for
-------------------------------------------

============== ================================= ==============================
Table          Source                            Quality
============== ================================= ==============================
securities     SEC ``company_tickers.json``      current registrants only
shares         SEC ``companyconcept`` (dei)      genuinely point-in-time
fundamentals   SEC ``companyconcept`` (us-gaap)  genuinely point-in-time
prices         Yahoo, via yfinance               split-adjusted, survivors only
corp_actions   Yahoo dividends and splits        no spin-offs, no mergers
classifications SEC SIC code, mapped to ICB      crude, and not point-in-time
fx             constant USD + FRED short rate    trivially correct - USD only
============== ================================= ==============================

The SEC data is genuinely good: ``companyconcept`` carries a ``filed`` date on every
fact, so the point-in-time contract in `data.providers.FundamentalProvider` is met
rather than approximated. Yahoo is where it falls apart, in four ways that are recorded
in `DEFECTS` and written into every snapshot's ``config.json`` so no analysis built on
one can claim not to have known.

The defects are not incidental
------------------------------

Three of them change index numbers, and a build on this data is wrong in ways that are
quantifiable but not fixable from free sources:

1. **Survivorship.** ``company_tickers.json`` lists current registrants. Companies
   acquired or delisted during the window are absent, so a ten-year history built here
   holds only the winners. This is the largest single distortion.
2. **Free float is unavailable.** No free source publishes it. Every security is given
   ``free_float_factor = 1.0``, which turns float-adjusted capitalisation weighting into
   full-cap weighting. The methodology in `config.EligibilityConfig` still runs, but its
   float screens can never bind.
3. **Prices are split-adjusted.** Yahoo has no as-traded series - see the
   ``YahooProvider`` docstring in `vendors.py`. Historical market capitalisation
   therefore cannot be reconstructed from price x shares, because the two series sit on
   different bases either side of every split. Historical iShares holdings files do not
   have this problem: each row carries its own as-traded price, and
   ``market_value / quantity`` reproduces it.

Of those three, only the second is a property of free data. The first and third are
properties of *these two sources* - the SEC current-registrant list and Yahoo - and both
dissolve if the universe and prices come from historical iShares holdings files instead.
`NOT_STRUCTURAL` records what was measured and what it would take. Read it before
quoting any of this as a reason a study cannot be run.

The fourth, spin-offs missing from the actions series, is handled: the corporate action
engine never sees the event, so the parent's ex-date drop is read as a price fall - a
one-day total-return error of well over 100bp on a name like GE in 2023.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import shutil
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
SEC_CONCEPT = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/{taxonomy}/{tag}.json"
SEC_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"

SEC_RATE_LIMIT_SECONDS = 0.11
"""The SEC allows 10 requests/second and blocks the IP above it. 0.11s between calls
keeps a single-threaded fetch just under, with margin for clock jitter."""


DEFECTS: dict[str, str] = {
    "survivorship": (
        "The universe comes from the SEC's current registrant list, so companies "
        "acquired or delisted during the window are absent entirely. Returns are "
        "measured on survivors only and are biased upward. This is a limitation of "
        "*this* universe builder, not of free data: see NOT_STRUCTURAL['survivorship']."
    ),
    "no_free_float": (
        "No free source publishes free-float factors. Every security is set to 1.0, "
        "so float-capitalisation weighting degenerates to full-capitalisation "
        "weighting and the free-float eligibility screens cannot bind."
    ),
    "split_adjusted_prices": (
        "Yahoo returns split-adjusted closes even with auto_adjust=False, so the "
        "as-traded price is unrecoverable and historical market capitalisation cannot "
        "be reconstructed from price x shares across a split."
    ),
    "no_spinoffs": (
        "Yahoo's actions series carries dividends and splits only. A spin-off's ex-date "
        "price drop is therefore indistinguishable from a fall in value, and total "
        "return is understated on that day by the value of the distribution."
    ),
    "no_mergers": (
        "Terminal events are absent, so a security that left the index by acquisition "
        "simply stops having prices rather than being removed at a known value."
    ),
    "current_ranking": (
        "Candidates are taken in the SEC file's order, which is by current market "
        "capitalisation. Selecting a historical universe by today's ranking is "
        "look-ahead in the candidate pool. Index *membership* is still decided by the "
        "reconstitution rules at each review date, so the look-ahead does not reach "
        "the weights - but the pool it draws from is not what it would have been."
    ),
    "sic_not_point_in_time": (
        "The SIC code is the issuer's current one, mapped to ICB level 1. Historical "
        "reclassification is invisible, so classification-driven turnover is absent."
    ),
}


NOT_STRUCTURAL: dict[str, str] = {
    "survivorship": (
        "Fixable. Historical iShares holdings files list the constituents as they stood "
        "on the date, delisted names included - Pharmacyclics is in the 2013-06-21 IWM "
        "file at 0.34% weight, nine years after being acquired. EDGAR also keeps the "
        "fundamentals of dead registrants permanently. What is missing is only the join "
        "key: company_tickers.json holds current registrants, so ticker->CIK resolves "
        "83% of a 2024 vintage but 44% of 2013, and recycled tickers make some of those "
        "matches silently wrong (ARRY was Array Biopharma in 2013, Array Technologies "
        "today). Resolving on the SEC name index (cik-lookup-data.txt, ~1.0M names "
        "including former and defunct) plus a check that the filer was actually filing "
        "on the date lifts a 2013 vintage to 94% of names and 92% of weight."
    ),
    "split_adjusted_prices": (
        "Sidesteppable. The defect is Yahoo's, not free data's. iShares holdings files "
        "carry an as-traded price per holding per date, and market_value / quantity "
        "reproduces it (median error zero). Sourcing constituent prices from the "
        "holdings file instead of Yahoo removes the split problem rather than "
        "correcting it."
    ),
    "current_ranking": (
        "Fixable, and by the same change. Taking the candidate pool from today's SEC "
        "ranking is look-ahead; taking it from the holdings file for the review date is "
        "not, because that file *is* what the index held on the day."
    ),
}
"""Defects recorded as structural that turn out not to be.

Kept separate from `DEFECTS` on purpose. `DEFECTS` describes what a snapshot built by
*this module* actually suffers from, and every entry there is still true of it. This
records which of those are properties of the chosen sources rather than of free data,
so the list is not read as a claim that the work is impossible.

The real remaining constraint on deep history is neither identity nor price: XBRL was
phased in between 2009 and 2011 and small caps were in the last wave, so shares
outstanding and public float do not exist before roughly 2011 at any resolution
quality. Constituents, weights and prices reach back to 2008.

Free float stays genuinely unavailable. dei:EntityPublicFloat is a usable proxy -
implied factors land in (0, 1] for 89% of names, median 0.95 - but it lags by a median
364 days, carries a tail of unit errors, and is defined over non-affiliate holdings
rather than the strategic, cross- and locked-up holdings an index provider strips out.
Better than hardcoding 1.0; not the same measure.
"""


class RealDataError(RuntimeError):
    """A fetch failed in a way that would corrupt the snapshot if ignored."""


# --------------------------------------------------------------------------------------
# SIC -> ICB level 1
# --------------------------------------------------------------------------------------


def sic_to_icb(sic: str | int | None) -> str:
    """Map a SEC SIC code to an ICB level-1 industry code.

    Crude by construction: SIC is a 1930s-vintage scheme organised by physical product,
    ICB is organised by revenue source, and the two genuinely disagree - a company that
    manufactures medical instruments (SIC 384) sits in health care under ICB but in
    manufacturing under SIC. The mapping picks the ICB industry a classifier would most
    often land on, and is listed as a defect rather than presented as authoritative.
    """
    if sic is None or str(sic).strip() in {"", "0", "None"}:
        return "50"  # INDUSTRIALS, the least-wrong default for unclassifiable filers
    code = int(str(sic).strip()[:4] or 0)
    major = code // 100

    if code in range(2833, 2837) or major == 80 or code in range(3840, 3852):
        return "20"  # HEALTH_CARE - pharma, health services, medical instruments
    if major == 13 or major == 29 or code in (1381, 1382, 1389):
        return "60"  # ENERGY
    if major == 49:
        return "65"  # UTILITIES
    if major == 48:
        return "15"  # TELECOMMUNICATIONS
    if code == 6798 or major == 65:
        return "35"  # REAL_ESTATE - REITs and property
    if major in range(60, 68):
        return "30"  # FINANCIALS
    if major in (35, 36, 38) or major == 73 or code == 7372:
        return "10"  # TECHNOLOGY
    if major in (10, 12, 14, 24, 26, 28, 30, 32, 33) or major == 34:
        return "55"  # BASIC_MATERIALS
    if major in (1, 2, 7, 9, 20, 21) or major == 54:
        return "45"  # CONSUMER_STAPLES
    if major in (22, 23, 25, 31, 39) or major in range(52, 60) or major in (70, 78, 79):
        return "40"  # CONSUMER_DISCRETIONARY
    if major == 37:
        # 371 motor vehicles is discretionary; 372/376 aerospace is industrial.
        return "40" if code // 10 == 371 else "50"
    return "50"  # INDUSTRIALS


US_GAAP_ITEMS: dict[str, str] = {
    "StockholdersEquity": "BOOK_EQUITY",
    "Assets": "TOTAL_ASSETS",
    "NetIncomeLoss": "NET_INCOME",
    "Revenues": "REVENUE",
}
"""XBRL tag -> the item name `factors/` expects. `BOOK_EQUITY` is the one that matters:
it is the numerator of book-to-price, which is the value signal every factor variant is
built on."""


_MIC_BY_EXCHANGE: dict[str, str] = {
    "Nasdaq": "XNAS",
    "NasdaqGS": "XNAS",
    "NasdaqCM": "XNAS",
    "NasdaqGM": "XNAS",
    "NYSE": "XNYS",
    "NYSE American": "XASE",
    "NYSEAmerican": "XASE",
    "CBOE": "XCBO",
    "OTC": "OTCM",
}


# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RealUniverseConfig:
    """Everything that determines a real snapshot, for the manifest and for caching."""

    n_securities: int = 200
    start: dt.date = dt.date(2016, 1, 4)
    end: dt.date = dt.date(2026, 6, 30)

    tier: str = "clean"
    """``clean`` or ``raw``. The tiers differ only in size: `clean` is small enough that
    every defect is individually resolvable, `raw` is wide enough to be index-like. The
    comparison between two builds is the measurement of what the extra rigour bought."""

    contact: str = "miniftse-research/0.1 (contact: set MINIFTSE_CONTACT)"
    """The SEC requires a declared user agent with real contact details."""

    cache_dir: Path = Path("data/raw/real")
    deposit_rate_series: str = "DGS3MO"
    fallback_deposit_rate: float = 0.02

    min_price_coverage: float = 0.80
    """Fraction of candidates that must return prices before a snapshot may be written.

    Not a style preference - it is the guard against the failure this ETL actually has.
    Yahoo answers a throttled request with "possibly delisted; no price data found", so a
    rate-limited run looks exactly like a universe of delisted companies. Without a floor,
    one surviving ticker out of two hundred is enough to write a snapshot, and that
    snapshot builds an index that is entirely plausible and entirely wrong."""

    price_retries: int = 3
    price_backoff_seconds: float = 20.0
    """Linear backoff between attempts. Yahoo's limiter is per-window, so waiting is the
    only remedy available to an unauthenticated caller."""

    def describe(self) -> dict[str, Any]:
        return {
            "n_securities": self.n_securities,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "tier": self.tier,
            "min_price_coverage": self.min_price_coverage,
        }


# --------------------------------------------------------------------------------------
# The builder
# --------------------------------------------------------------------------------------


@dataclass
class RealUniverseBuilder:
    """Fetches real data and writes a snapshot `MaterialisedUniverse` can load.

    Network access happens here and only here. Everything downstream reads files, which
    is what keeps a real build as reproducible as a synthetic one: re-running a build
    against a kept snapshot cannot produce different numbers, and re-fetching produces a
    different fingerprint rather than silently changing the answer.
    """

    config: RealUniverseConfig = field(default_factory=RealUniverseConfig)
    verbose: bool = True

    _last_sec_call: float = field(default=0.0, repr=False)
    _cache_hits: int = field(default=0, repr=False)
    _fetched: int = field(default=0, repr=False)
    defect_log: dict[str, Any] = field(default_factory=dict, repr=False)

    def log(self, message: str) -> None:
        if self.verbose:
            print(message)

    # ------------------------------------------------------------------ http

    def _cache_path(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode()).hexdigest()[:24]
        return self.config.cache_dir / "http" / f"{digest}.json"

    def _get(self, url: str, throttle: bool = True) -> bytes:
        if throttle:
            elapsed = time.monotonic() - self._last_sec_call
            if elapsed < SEC_RATE_LIMIT_SECONDS:
                time.sleep(SEC_RATE_LIMIT_SECONDS - elapsed)
            self._last_sec_call = time.monotonic()
        request = urllib.request.Request(url, headers={"User-Agent": self.config.contact})
        with urllib.request.urlopen(request, timeout=90) as response:
            data: bytes = response.read()
        return data

    def _get_json(self, url: str) -> Any:
        """Fetch and cache. The cache is what makes this restartable.

        A full fetch is several thousand requests over tens of minutes, and anything that
        interrupts it - a timeout, a dropped connection, a laptop lid - would otherwise
        mean starting from zero. Responses are keyed by URL and never expire: a snapshot
        is supposed to be a fixed observation of the data, so silently picking up revised
        numbers on a re-run is the *wrong* behaviour. Delete the cache directory to
        deliberately re-observe.
        """
        cached = self._cache_path(url)
        if cached.exists():
            self._cache_hits += 1
            return json.loads(cached.read_bytes())
        payload = self._get(url)
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(payload)
        self._fetched += 1
        return json.loads(payload)

    # ------------------------------------------------------------------ 1. candidates

    def candidates(self) -> pd.DataFrame:
        """The candidate pool: the SEC's registrant list, in its own order.

        That order is by current market capitalisation, which is why `n_securities` can
        simply take the head. It is also the `current_ranking` defect - see DEFECTS.
        """
        self.log(f"[1/6] candidate pool: top {self.config.n_securities} SEC registrants")
        raw = self._get_json(SEC_TICKERS)
        rows = [
            {
                "issuer_id": str(entry["cik_str"]).zfill(10),
                "ticker": str(entry["ticker"]).strip().upper(),
                "name": str(entry["title"]).strip(),
                "rank": position,
            }
            for position, entry in enumerate(raw.values())
        ]
        frame = pd.DataFrame(rows)
        # One CIK can carry several tickers - Alphabet's A and C lines share an issuer.
        # Both are kept: they are different securities, which is the whole point of the
        # issuer/security split in `secmaster`.
        return frame.head(self.config.n_securities).reset_index(drop=True)

    # ------------------------------------------------------------------ 2. reference

    def reference(self, candidates: pd.DataFrame) -> dict[str, pd.DataFrame]:
        """Securities, listings, identifiers and classifications from SEC submissions."""
        self.log(f"[2/6] reference data for {len(candidates)} securities")
        securities, listings, identifiers, classifications = [], [], [], []
        failed: list[str] = []

        for n, row in enumerate(candidates.itertuples(), start=1):
            if n % 50 == 0:
                self.log(f"      {n}/{len(candidates)}")
            try:
                meta = self._get_json(SEC_SUBMISSIONS.format(cik=row.issuer_id))
            except (urllib.error.URLError, json.JSONDecodeError, TimeoutError):
                failed.append(row.ticker)
                meta = {}

            icb = sic_to_icb(meta.get("sic"))
            exchanges = meta.get("exchanges") or []
            mic = _MIC_BY_EXCHANGE.get(exchanges[0] if exchanges else "", "XNYS")
            security_id = row.ticker
            listing_id = f"{row.ticker}.{mic}"
            # One CIK with several tickers in the pool means a dual-class issuer.
            dual = bool((candidates["issuer_id"] == row.issuer_id).sum() > 1)

            securities.append(
                {
                    "security_id": security_id,
                    "issuer_id": row.issuer_id,
                    "country": "US",
                    "currency": "USD",
                    "market_status": "DEVELOPED",
                    "icb_industry": icb,
                    "security_type": "ORDINARY",
                    "listing_start": self.config.start,
                    "listing_end": None,
                    "is_dual_class": dual,
                    "foreign_ownership_limit": 1.0,
                }
            )
            listings.append(
                {
                    "listing_id": listing_id,
                    "security_id": security_id,
                    "mic": mic,
                    "currency": "USD",
                    "country": "US",
                    "listing_start": self.config.start,
                    "listing_end": None,
                }
            )
            identifiers.append(
                {
                    "security_id": security_id,
                    "listing_id": listing_id,
                    "isin": None,
                    "sedol": None,
                    "ticker": row.ticker,
                    "valid_from": self.config.start,
                    "valid_to": None,
                }
            )
            classifications.append(
                {
                    "security_id": security_id,
                    "effective_date": self.config.start,
                    "knowledge_date": self.config.start,
                    "icb_industry": icb,
                    "icb_supersector": f"{icb}10",
                }
            )

        if failed:
            self.defect_log["submissions_failed"] = failed
            self.log(f"      ! {len(failed)} submissions lookups failed")

        return {
            "securities": pd.DataFrame(securities),
            "listings": pd.DataFrame(listings),
            "identifiers": pd.DataFrame(identifiers),
            "classifications": pd.DataFrame(classifications),
        }

    # ------------------------------------------------------------------ 3. prices

    def _price_cache_path(self, ticker: str) -> Path:
        # Ticker, start and end all belong in the key: a cached 2016-2026 pull cannot
        # answer a 2020-2021 request, and silently serving it would be a subtle
        # look-ahead bug rather than an obvious miss.
        stamp = f"{ticker}|{self.config.start}|{self.config.end}"
        digest = hashlib.sha256(stamp.encode()).hexdigest()[:16]
        safe = ticker.replace("/", "_").replace("\\", "_")
        return self.config.cache_dir / "prices" / f"{safe}_{digest}.parquet"

    def _fetch_one(self, ticker: str) -> pd.DataFrame | None:
        """One ticker's OHLCV plus actions, cached to parquet. `None` means no data.

        Yahoo is the rate-limited, unauthenticated leg of this pipeline and it fails by
        *lying*: a throttled request comes back as "possibly delisted; no price data
        found" for AAPL. Since that is indistinguishable from a genuine delisting, the
        only safe posture is to cache every success and never treat a miss as fact.
        """
        import yfinance as yf

        cached = self._price_cache_path(ticker)
        if cached.exists():
            self._cache_hits += 1
            frame = pd.read_parquet(cached)
            return None if frame.empty else frame

        frame = yf.download(
            ticker,
            start=self.config.start,
            end=self.config.end,
            auto_adjust=False,
            actions=True,
            progress=False,
            threads=False,
            ignore_tz=True,
        )
        if frame is None or frame.empty:
            return None
        if isinstance(frame.columns, pd.MultiIndex):
            frame.columns = frame.columns.get_level_values(0)
        frame = frame.dropna(subset=["Close"])
        if frame.empty:
            return None

        self._fetched += 1
        cached.parent.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(cached)
        return frame

    def prices_and_actions(
        self,
        candidates: pd.DataFrame,
        listing_ids: dict[str, str],
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Prices and corporate actions from Yahoo, one ticker at a time and cached.

        Returns as-delivered: split-adjusted, survivors only. Tickers that come back
        empty are recorded rather than dropped silently - an empty frame and a genuinely
        untraded security are indistinguishable downstream, and conflating them is how
        survivorship bias becomes invisible.

        Per-ticker rather than batched, because a batch that trips the rate limiter
        loses all forty results at once and caches none of them. One at a time is slower
        on a cold cache and enormously cheaper on the second attempt.
        """
        tickers = list(candidates["ticker"])
        self.log(f"[3/6] prices for {len(tickers)} tickers from Yahoo")

        frames: list[pd.DataFrame] = []
        actions: list[dict[str, Any]] = []
        empty: list[str] = []

        for n, ticker in enumerate(tickers, start=1):
            if n % 25 == 0:
                self.log(
                    f"      {n}/{len(tickers)} ({self._cache_hits} cached, {len(empty)} empty)"
                )

            frame: pd.DataFrame | None = None
            for attempt in range(self.config.price_retries):
                try:
                    frame = self._fetch_one(ticker)
                except Exception as exc:  # noqa: BLE001 - transport errors are retryable
                    self.defect_log.setdefault("price_fetch_errors", []).append(
                        f"{ticker}: {type(exc).__name__}"
                    )
                    frame = None
                if frame is not None:
                    break
                if attempt < self.config.price_retries - 1:
                    # Linear backoff. Yahoo's limiter is per-window, so waiting is the
                    # only remedy; hammering it converts a slow run into a blocked one.
                    time.sleep(self.config.price_backoff_seconds * (attempt + 1))

            if frame is None:
                empty.append(ticker)
                continue

            index = pd.to_datetime(frame.index).date
            frames.append(
                pd.DataFrame(
                    {
                        "security_id": ticker,
                        "listing_id": listing_ids[ticker],
                        "date": index,
                        "close": frame["Close"].to_numpy(dtype=float),
                        "volume": frame.get("Volume", pd.Series(0.0, index=frame.index))
                        .fillna(0.0)
                        .to_numpy(dtype=float),
                        "currency": "USD",
                        "is_suspended": False,
                        "open": frame.get("Open", frame["Close"]).to_numpy(dtype=float),
                        "high": frame.get("High", frame["Close"]).to_numpy(dtype=float),
                        "low": frame.get("Low", frame["Close"]).to_numpy(dtype=float),
                    }
                )
            )
            actions.extend(self._actions_from_frame(ticker, frame))

        if empty:
            self.defect_log["no_price_history"] = empty
            self.log(f"      ! {len(empty)} tickers returned no price history")

        coverage = len(frames) / max(len(tickers), 1)
        if coverage < self.config.min_price_coverage:
            raise RealDataError(
                f"only {len(frames)}/{len(tickers)} candidates ({coverage:.1%}) returned "
                f"prices, below the {self.config.min_price_coverage:.0%} floor.\n\n"
                f"Yahoo reporting a mega-cap as 'possibly delisted' means rate limiting, "
                f"not a delisting - the request was throttled and the error is a lie. "
                f"Wait for the window to clear and re-run: every ticker already fetched "
                f"is cached under {self.config.cache_dir / 'prices'}, so a re-run resumes "
                f"rather than restarting.\n\n"
                f"Refusing to write, because a snapshot covering a fraction of its "
                f"universe still builds an index that looks entirely plausible."
            )

        prices = pd.concat(frames, ignore_index=True)
        prices = prices[
            [
                "security_id",
                "listing_id",
                "date",
                "close",
                "volume",
                "currency",
                "is_suspended",
                "open",
                "high",
                "low",
            ]
        ]
        corp_actions = (
            pd.DataFrame(actions)
            if actions
            else pd.DataFrame(
                columns=[
                    "event_id",
                    "security_id",
                    "event_type",
                    "announcement_date",
                    "ex_date",
                    "pay_date",
                    "payload",
                ]
            )
        )
        return prices, corp_actions

    @staticmethod
    def _actions_from_frame(ticker: str, frame: pd.DataFrame) -> list[dict[str, Any]]:
        """Dividends and splits, in the payload shape `corpactions.events` parses.

        Yahoo gives no announcement date, so it is set to the ex-date. That understates
        the announcement-to-ex gap a real index uses to schedule its own processing, and
        is harmless here only because nothing in this build depends on it.
        """
        events: list[dict[str, Any]] = []
        dates = pd.to_datetime(frame.index).date

        if "Dividends" in frame.columns:
            for when, amount in zip(dates, frame["Dividends"].fillna(0.0), strict=False):
                if amount > 0:
                    events.append(
                        {
                            "event_id": f"DIV-{ticker}-{when}",
                            "security_id": ticker,
                            "event_type": "CASH_DIVIDEND",
                            "announcement_date": when,
                            "ex_date": when,
                            "pay_date": when + dt.timedelta(days=28),
                            "payload": json.dumps(
                                {
                                    "amount": float(amount),
                                    "currency": "USD",
                                    "gross_amount": float(amount),
                                    "is_special": False,
                                }
                            ),
                        }
                    )

        if "Stock Splits" in frame.columns:
            for when, ratio in zip(dates, frame["Stock Splits"].fillna(0.0), strict=False):
                if ratio and ratio > 0:
                    events.append(
                        {
                            "event_id": f"SPL-{ticker}-{when}",
                            "security_id": ticker,
                            "event_type": "SPLIT" if ratio >= 1.0 else "REVERSE_SPLIT",
                            "announcement_date": when,
                            "ex_date": when,
                            "pay_date": when,
                            "payload": json.dumps({"ratio": float(ratio)}),
                        }
                    )
        return events

    # ------------------------------------------------------------------ 4. shares

    def shares(self, candidates: pd.DataFrame) -> pd.DataFrame:
        """Point-in-time shares outstanding from the SEC's ``dei`` cover-page tag.

        This is the one table where the free data is genuinely good. Every fact carries
        the date it was filed, so `effective_date` and `knowledge_date` are both real
        rather than assumed, and the bitemporal contract in `data.providers` is met.

        `free_float_factor` is 1.0 throughout - see the `no_free_float` defect.
        """
        self.log(f"[4/6] shares outstanding for {len(candidates)} securities")
        rows: list[dict[str, Any]] = []
        missing: list[str] = []

        for n, row in enumerate(candidates.itertuples(), start=1):
            if n % 50 == 0:
                self.log(f"      {n}/{len(candidates)}")
            url = SEC_CONCEPT.format(
                cik=row.issuer_id,
                taxonomy="dei",
                tag="EntityCommonStockSharesOutstanding",
            )
            try:
                payload = self._get_json(url)
            except (urllib.error.URLError, json.JSONDecodeError, TimeoutError):
                missing.append(row.ticker)
                continue

            facts = [f for unit in payload.get("units", {}).values() for f in unit]
            for fact in facts:
                filed, value = fact.get("filed"), fact.get("val")
                effective = fact.get("end") or filed
                if not filed or value is None:
                    continue
                rows.append(
                    {
                        "security_id": row.ticker,
                        "effective_date": dt.date.fromisoformat(effective),
                        "knowledge_date": dt.date.fromisoformat(filed),
                        "shares_outstanding": float(value),
                        "free_float_factor": 1.0,
                        "reason": "SEC_COVER_PAGE",
                        "foreign_ownership_limit": 1.0,
                    }
                )

        if missing:
            self.defect_log["no_share_count"] = missing
            self.log(f"      ! {len(missing)} securities had no share count")

        if not rows:
            raise RealDataError("no share counts fetched - cannot weight an index")

        frame = pd.DataFrame(rows)
        # A security with no shares row before the base date can never be weighted, so
        # back-fill the earliest known count to the start. Recorded as an assumption
        # rather than done quietly: it treats the first observed count as if it had
        # applied since the base date, which is wrong across any pre-base buyback.
        earliest = frame.sort_values("knowledge_date").groupby("security_id").first()
        backfill = [
            {
                "security_id": security_id,
                "effective_date": self.config.start,
                "knowledge_date": self.config.start,
                "shares_outstanding": float(record["shares_outstanding"]),
                "free_float_factor": 1.0,
                "reason": "BACKFILL_TO_BASE",
                "foreign_ownership_limit": 1.0,
            }
            for security_id, record in earliest.iterrows()
            if record["knowledge_date"] > self.config.start
        ]
        self.defect_log["shares_backfilled_to_base"] = len(backfill)
        return pd.concat([frame, pd.DataFrame(backfill)], ignore_index=True)

    # ------------------------------------------------------------------ 5. fundamentals

    def fundamentals(self, candidates: pd.DataFrame) -> pd.DataFrame:
        """Point-in-time fundamentals from ``companyconcept``.

        Genuinely point-in-time, for the same reason as the share counts: every fact
        carries its filing date, so a restatement appears as a second row with a later
        `filed_date` rather than overwriting the first.
        """
        self.log(f"[5/6] fundamentals for {len(candidates)} securities")
        rows: list[dict[str, Any]] = []

        for n, row in enumerate(candidates.itertuples(), start=1):
            if n % 25 == 0:
                self.log(f"      {n}/{len(candidates)}")
            for tag, item in US_GAAP_ITEMS.items():
                url = SEC_CONCEPT.format(cik=row.issuer_id, taxonomy="us-gaap", tag=tag)
                try:
                    payload = self._get_json(url)
                except (urllib.error.URLError, json.JSONDecodeError, TimeoutError):
                    continue
                seen: set[tuple[str, str]] = set()
                for unit, facts in payload.get("units", {}).items():
                    if unit != "USD":
                        continue
                    for fact in facts:
                        filed, end, value = (fact.get("filed"), fact.get("end"), fact.get("val"))
                        if not filed or not end or value is None:
                            continue
                        key = (end, filed)
                        if key in seen:
                            continue
                        seen.add(key)
                        rows.append(
                            {
                                "security_id": row.ticker,
                                "item": item,
                                "period_end": dt.date.fromisoformat(end),
                                "filed_date": dt.date.fromisoformat(filed),
                                "value": float(value),
                                "currency": "USD",
                                "is_restatement": False,
                            }
                        )

        if not rows:
            self.defect_log["no_fundamentals"] = True
            return pd.DataFrame(
                columns=[
                    "security_id",
                    "item",
                    "period_end",
                    "filed_date",
                    "value",
                    "currency",
                    "is_restatement",
                ]
            )

        frame = pd.DataFrame(rows)
        # The second and later filings of the same (security, item, period) are
        # restatements by definition. Flagging them is what makes the restatement
        # studies in `quality/` runnable on real data.
        frame = frame.sort_values(["security_id", "item", "period_end", "filed_date"]).reset_index(
            drop=True
        )
        frame["is_restatement"] = frame.duplicated(
            subset=["security_id", "item", "period_end"], keep="first"
        )
        return frame

    # ------------------------------------------------------------------ 6. fx

    def fx(self, calendar: list[dt.date]) -> pd.DataFrame:
        """USD against itself, plus a real short rate.

        The universe is US-only, so FX is the identity and the currency-hedged variant
        is degenerate. The deposit rate is still fetched for real, because the hedged
        calculation reads it and a made-up number there would quietly propagate into a
        published figure.
        """
        self.log("[6/6] FX and deposit rates")
        rate_by_date: dict[dt.date, float] = {}
        try:
            raw = self._get(FRED_CSV.format(series=self.config.deposit_rate_series), throttle=False)
            fred = pd.read_csv(io.StringIO(raw.decode()))
            date_col, value_col = fred.columns[0], fred.columns[1]
            fred[date_col] = pd.to_datetime(fred[date_col]).dt.date
            fred[value_col] = pd.to_numeric(fred[value_col], errors="coerce") / 100.0
            rate_by_date = dict(zip(fred[date_col], fred[value_col], strict=False))
            self.log(f"      {self.config.deposit_rate_series}: {len(rate_by_date)} obs")
        except Exception as exc:  # noqa: BLE001 - a missing rate is a defect, not a crash
            self.defect_log["deposit_rate_source"] = f"{type(exc).__name__}: {exc}"
            self.log(
                f"      ! FRED unavailable, using flat {self.config.fallback_deposit_rate:.2%}"
            )

        last = self.config.fallback_deposit_rate
        rows = []
        for when in calendar:
            value = rate_by_date.get(when)
            if value is not None and value == value:  # not NaN
                last = float(value)
            rows.append(
                {"date": when, "base": "USD", "quote": "USD", "rate": 1.0, "deposit_rate": last}
            )
        return pd.DataFrame(rows)

    # ------------------------------------------------------------------ assemble

    def build(self, dest: Path) -> Path:
        """Fetch everything and write a snapshot. Returns the snapshot directory."""
        started = time.perf_counter()
        dest = Path(dest)

        # Everything is assembled into a staging directory and swapped in at the end.
        # Writing in place cost a known-good snapshot once already: a rate-limited run
        # got far enough to overwrite `prices.parquet` before anything noticed it had
        # one security in it. A snapshot is an artefact other work depends on, so a
        # failed rebuild must leave the previous one exactly as it was.
        staging = dest.parent / f".{dest.name}.staging"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True, exist_ok=True)

        candidates = self.candidates()
        reference = self.reference(candidates)
        # Prices must carry the same `listing_id` the listings table uses, or the two
        # tables silently fail to join and `get_prices(listing_ids=...)` matches nothing.
        listing_ids = dict(
            zip(
                reference["listings"]["security_id"],
                reference["listings"]["listing_id"],
                strict=True,
            )
        )
        prices, corp_actions = self.prices_and_actions(candidates, listing_ids)

        # Anything with no prices cannot be in the index. Drop it from every reference
        # table so the snapshot is internally consistent - a securities row with no
        # price panel is exactly the kind of silent inconsistency the quality checks in
        # `quality/rules.py` are there to catch, and shipping one would be self-inflicted.
        traded = set(prices["security_id"].unique())
        dropped = sorted(set(candidates["ticker"]) - traded)
        if dropped:
            self.defect_log["dropped_no_prices"] = dropped
        candidates = candidates[candidates["ticker"].isin(traded)].reset_index(drop=True)
        for name, table in reference.items():
            reference[name] = table[table["security_id"].isin(traded)].reset_index(drop=True)

        shares = self.shares(candidates)
        shares = shares[shares["security_id"].isin(traded)].reset_index(drop=True)
        fundamentals = self.fundamentals(candidates)
        if not fundamentals.empty:
            fundamentals = fundamentals[fundamentals["security_id"].isin(traded)].reset_index(
                drop=True
            )

        calendar = sorted(prices["date"].unique())
        fx = self.fx(list(calendar))

        tables: dict[str, pd.DataFrame] = {
            "prices": prices,
            "shares": shares,
            "corp_actions": corp_actions,
            "fundamentals": fundamentals,
            "fx": fx,
            **reference,
        }
        for name, table in tables.items():
            out = table.copy()
            for column in out.columns:
                if column in {
                    "date",
                    "effective_date",
                    "knowledge_date",
                    "filed_date",
                    "period_end",
                    "announcement_date",
                    "ex_date",
                    "pay_date",
                    "listing_start",
                    "listing_end",
                    "valid_from",
                    "valid_to",
                }:
                    out[column] = pd.to_datetime(out[column], errors="coerce")
            out.to_parquet(staging / f"{name}.parquet", index=False)

        meta = {
            "name": f"real-{self.config.tier}",
            "source": "SEC EDGAR (reference, shares, fundamentals) + Yahoo (prices, "
            "actions) + FRED (deposit rate)",
            "config": self.config.describe(),
            "start": str(min(calendar)),
            "end": str(max(calendar)),
            "built_at": dt.datetime.now(dt.UTC).isoformat(),
            "securities": int(len(reference["securities"])),
            "price_rows": int(len(prices)),
            "provenance": {
                "defects": DEFECTS,
                "observed": self.defect_log,
            },
        }
        (staging / "config.json").write_text(json.dumps(meta, indent=2, default=str))

        # Swap. The old snapshot is kept until the new one is in place, and is only
        # removed once the rename has succeeded.
        superseded = dest.parent / f".{dest.name}.superseded"
        if superseded.exists():
            shutil.rmtree(superseded)
        if dest.exists():
            dest.rename(superseded)
        try:
            staging.rename(dest)
        except OSError:
            if superseded.exists() and not dest.exists():
                superseded.rename(dest)
            raise
        if superseded.exists():
            shutil.rmtree(superseded)

        self.log(
            f"\nsnapshot written to {dest}\n"
            f"  {len(reference['securities'])} securities, {len(prices):,} price rows, "
            f"{len(corp_actions):,} corporate actions, {len(fundamentals):,} facts\n"
            f"  {min(calendar)} to {max(calendar)}, "
            f"{time.perf_counter() - started:.0f}s "
            f"({self._fetched} fetched, {self._cache_hits} cached)"
        )
        return dest


__all__ = [
    "DEFECTS",
    "RealDataError",
    "RealUniverseBuilder",
    "RealUniverseConfig",
    "sic_to_icb",
]
