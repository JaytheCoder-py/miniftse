"""Real-world provider implementations behind the same Protocols.

The synthetic universe is the default because it is reproducible. These adapters exist
so the identical index engine, factor library and risk model can be pointed at genuine
data without a line changing upstream of `data/`.

Included:

* ``YFinanceProvider``   - prices. Survivorship-biased and adjusted in place; the class
                           documents its own defects rather than pretending otherwise.
* ``EdgarProvider``      - genuinely point-in-time US fundamentals from the SEC's
                           Financial Statement Data Sets, which carry a filing date.
* ``ISharesProvider``    - daily ETF holdings, the best free proxy for real index
                           membership, with ISIN/SEDOL/CUSIP attached.
* ``LsegDataProvider``   - a deliberate stub. It shows the call shapes of the LSEG Data
                           Library so the wiring is obvious, and raises rather than
                           silently returning empty frames when unlicensed.
* ``CompositeProvider``  - route each Protocol method to a different backend, which is
                           what a real platform does: prices from one vendor,
                           fundamentals from another, reference from a third.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from miniftse.data.providers import MarketDataProvider

USER_AGENT = "miniftse-research/0.1 (contact: set MINIFTSE_CONTACT)"
"""The SEC requires a declared user agent with contact details on EDGAR requests and
rate-limits to 10 requests/second. Ignoring either gets the IP blocked."""


class ProviderUnavailableError(RuntimeError):
    """Raised when a provider cannot serve a request - unlicensed, offline, or the
    identifier is unknown. Never return an empty frame for this: an empty frame is
    indistinguishable from 'this security genuinely had no data', and that ambiguity
    is how survivorship bias gets into a backtest unnoticed."""


# --------------------------------------------------------------------------------------


@dataclass
class YFinanceProvider:
    """Prices from Yahoo, with its limitations stated in the type rather than the docs.

    Known defects, all of which matter for index work and none of which this class can
    fix:

    1. **Split adjustment is unavoidable.** Even ``auto_adjust=False`` returns a
       split-adjusted close. There is no path to the actually-traded price, so
       historical market capitalisation cannot be reconstructed.
    2. **No delisted securities.** A request for an acquired ticker returns an empty
       frame, so any universe built here is survivorship-biased by construction.
    3. **Spin-offs are not in the actions series.** The parent's ex-date price drop
       appears as a large negative return with no corresponding distribution.
    4. **No free float, no shares history worth trusting, no point-in-time anything.**

    Use for prototyping. `set_strict(True)` makes the class refuse index-critical
    requests instead of quietly answering them badly.
    """

    strict: bool = False
    _cache: dict[str, pd.DataFrame] = field(default_factory=dict, repr=False)

    @property
    def name(self) -> str:
        return "yfinance"

    def set_strict(self, strict: bool) -> None:
        self.strict = strict

    def get_prices(
        self, listing_ids: list[str] | None, start: dt.date, end: dt.date
    ) -> pd.DataFrame:
        if listing_ids is None:
            raise ProviderUnavailableError(
                "yfinance has no universe concept - it cannot enumerate securities, "
                "only answer for tickers you already know. That absence is precisely "
                "the survivorship problem."
            )
        if self.strict:
            raise ProviderUnavailableError(
                "strict mode: yfinance prices are split-adjusted in place and cannot "
                "support a divisor-based index calculation"
            )

        import yfinance as yf

        frames: list[pd.DataFrame] = []
        missing: list[str] = []
        for ticker in listing_ids:
            key = f"{ticker}:{start}:{end}"
            if key in self._cache:
                hist = self._cache[key]
            else:
                hist = yf.Ticker(ticker).history(
                    start=start, end=end, auto_adjust=False, actions=True
                )
                self._cache[key] = hist
            if hist.empty:
                missing.append(ticker)
                continue
            df = hist.reset_index()
            df.columns = [str(c).lower().replace(" ", "_") for c in df.columns]
            df["date"] = pd.to_datetime(df["date"], utc=True).dt.date
            df["security_id"] = ticker
            df["listing_id"] = ticker
            df["currency"] = "USD"
            df["is_suspended"] = False
            frames.append(
                df[["security_id", "listing_id", "date", "open", "high", "low",
                    "close", "volume", "currency", "is_suspended"]]
            )

        if missing:
            # Surfaced, never swallowed. These are the delisted names.
            print(f"yfinance returned nothing for {len(missing)} tickers: {missing}")
        if not frames:
            raise ProviderUnavailableError(f"no data for any of {listing_ids}")
        return pd.concat(frames, ignore_index=True)

    def get_corp_actions(
        self, security_ids: list[str] | None, start: dt.date, end: dt.date
    ) -> pd.DataFrame:
        """Dividends and splits only. Spin-offs, rights issues and mergers are absent,
        which is why the Module 1 memo can demonstrate a >100bp single-day error."""
        if security_ids is None:
            raise ProviderUnavailableError("yfinance cannot enumerate a universe")

        import yfinance as yf

        rows: list[dict[str, Any]] = []
        for ticker in security_ids:
            t = yf.Ticker(ticker)
            try:
                actions = t.actions
            except Exception as exc:  # noqa: BLE001
                raise ProviderUnavailableError(f"{ticker}: {exc}") from exc
            if actions is None or actions.empty:
                continue
            for idx, row in actions.iterrows():
                d = pd.Timestamp(idx).date()
                if not (start <= d <= end):
                    continue
                if row.get("Dividends", 0):
                    rows.append({
                        "event_id": f"YF-DIV-{ticker}-{d}", "security_id": ticker,
                        "event_type": "CASH_DIVIDEND", "announcement_date": d,
                        "ex_date": d, "pay_date": d,
                        "payload": json.dumps({"amount": float(row["Dividends"]),
                                               "currency": "USD", "is_special": False}),
                    })
                if row.get("Stock Splits", 0):
                    rows.append({
                        "event_id": f"YF-SPL-{ticker}-{d}", "security_id": ticker,
                        "event_type": "SPLIT", "announcement_date": d,
                        "ex_date": d, "pay_date": d,
                        "payload": json.dumps({"ratio": float(row["Stock Splits"])}),
                    })
        return pd.DataFrame(rows)


# --------------------------------------------------------------------------------------


    # ------------------------------------------------------------------ reference

    def get_shares(self, security_ids: list[str] | None, as_of: dt.date) -> pd.DataFrame:
        """Shares outstanding from Yahoo's share-count series.

        Real data, with two caveats that make it unusable for index weighting on its
        own. The series is sparse and irregularly dated, so `as_of` resolves to the last
        observation on or before the date rather than to a filing. And there is **no
        free float at all** — the factor is returned as 1.0, which would weight a
        company whose shares are 70% state-owned as if all of them were buyable.
        """
        if security_ids is None:
            raise ProviderUnavailableError("yfinance cannot enumerate a universe")

        import yfinance as yf

        rows: list[dict[str, Any]] = []
        for ticker in security_ids:
            try:
                series = yf.Ticker(ticker).get_shares_full(
                    start=as_of - dt.timedelta(days=730), end=as_of)
            except Exception as exc:  # noqa: BLE001
                raise ProviderUnavailableError(f"{ticker}: {exc}") from exc
            if series is None or len(series) == 0:
                continue
            frame = series.to_frame("shares_outstanding").reset_index()
            frame.columns = ["effective_date", "shares_outstanding"]
            frame["effective_date"] = pd.to_datetime(
                frame["effective_date"], utc=True).dt.date
            latest = frame[frame["effective_date"] <= as_of].tail(1)
            if latest.empty:
                continue
            rows.append({
                "security_id": ticker,
                "effective_date": latest["effective_date"].iloc[0],
                # No filing date is published with the series, so knowledge date is set
                # equal to effective date. That is optimistic and is a real PIT hole.
                "knowledge_date": latest["effective_date"].iloc[0],
                "shares_outstanding": float(latest["shares_outstanding"].iloc[0]),
                "free_float_factor": 1.0,
                "foreign_ownership_limit": 1.0,
            })
        if not rows:
            raise ProviderUnavailableError(f"no share data for {security_ids}")
        return pd.DataFrame(rows)

    def get_shares_history(self, security_ids: list[str] | None, start: dt.date,
                           end: dt.date) -> pd.DataFrame:
        if security_ids is None:
            raise ProviderUnavailableError("yfinance cannot enumerate a universe")

        import yfinance as yf

        frames: list[pd.DataFrame] = []
        for ticker in security_ids:
            series = yf.Ticker(ticker).get_shares_full(start=start, end=end)
            if series is None or len(series) == 0:
                continue
            frame = series.to_frame("shares_outstanding").reset_index()
            frame.columns = ["effective_date", "shares_outstanding"]
            frame["effective_date"] = pd.to_datetime(
                frame["effective_date"], utc=True).dt.date
            frame["knowledge_date"] = frame["effective_date"]
            frame["security_id"] = ticker
            frame["free_float_factor"] = 1.0
            frame["foreign_ownership_limit"] = 1.0
            frames.append(frame)
        if not frames:
            raise ProviderUnavailableError("no share history available")
        return pd.concat(frames, ignore_index=True)

    def get_fx(self, base: str, quotes: list[str], start: dt.date, end: dt.date
               ) -> pd.DataFrame:
        """FX from Yahoo's currency pairs (`GBPUSD=X` and friends).

        Returned in the repo's convention: units of base per one unit of quote. Yahoo
        quotes `QUOTEBASE=X` as base-per-quote already, so no inversion is needed — but
        the pair that does *not* exist is fetched inverted and flipped, and getting that
        backwards is a silent error that looks like a factor return.
        """
        import yfinance as yf

        frames: list[pd.DataFrame] = []
        for quote in quotes:
            if quote == base:
                dates = pd.bdate_range(start, end)
                frames.append(pd.DataFrame({
                    "date": [d.date() for d in dates], "base": base, "quote": quote,
                    "rate": 1.0,
                }))
                continue
            hist = yf.Ticker(f"{quote}{base}=X").history(start=start, end=end)
            inverted = False
            if hist.empty:
                hist = yf.Ticker(f"{base}{quote}=X").history(start=start, end=end)
                inverted = True
            if hist.empty:
                raise ProviderUnavailableError(f"no FX series for {quote}/{base}")
            rate = hist["Close"]
            if inverted:
                rate = 1.0 / rate
            frames.append(pd.DataFrame({
                "date": pd.to_datetime(rate.index, utc=True).date,
                "base": base, "quote": quote, "rate": rate.to_numpy(dtype=float),
            }))
        return pd.concat(frames, ignore_index=True)

    def get_deposit_rates(self, currencies: list[str], start: dt.date, end: dt.date
                          ) -> pd.DataFrame:
        raise ProviderUnavailableError(
            "yfinance publishes no deposit or money-market rates, so covered interest "
            "parity forwards cannot be synthesised from it. Use FRED for USD/EUR/GBP "
            "short rates, or a quoted forward curve."
        )

    def get_classifications(self, security_ids: list[str] | None, as_of: dt.date
                            ) -> pd.DataFrame:
        """Sector from Yahoo's profile data.

        Not ICB, and not point-in-time: `info` returns the classification as it stands
        *today*, so a company reclassified in 2022 appears to have always been in its
        current sector. Industry reclassification is a genuine source of index turnover,
        and this source cannot see it.
        """
        if security_ids is None:
            raise ProviderUnavailableError("yfinance cannot enumerate a universe")

        import yfinance as yf

        rows: list[dict[str, Any]] = []
        for ticker in security_ids:
            try:
                info = yf.Ticker(ticker).info or {}
            except Exception:  # noqa: BLE001
                continue
            sector = str(info.get("sector") or "UNKNOWN")
            rows.append({
                "security_id": ticker,
                "effective_date": as_of, "knowledge_date": as_of,
                "icb_industry": YAHOO_SECTOR_TO_ICB.get(sector, "UNKNOWN"),
                "icb_supersector": sector,
            })
        if not rows:
            raise ProviderUnavailableError("no classification data available")
        return pd.DataFrame(rows)

    def get_fundamentals(self, security_ids: list[str] | None, items: list[str],
                         as_of: dt.date, max_staleness_days: int = 550) -> pd.DataFrame:
        """Refused, deliberately.

        Yahoo publishes quarterly financials but no filing dates, so there is no way to
        know what was public on `as_of`. Serving them would silently backdate every
        restatement to the period end and inflate any value factor built on book equity.

        This is the one place where returning nothing is worse than raising: an empty
        frame reads as "this company has no fundamentals", and the caller carries on.
        """
        raise ProviderUnavailableError(
            "yfinance fundamentals carry no filing date, so they cannot be made "
            "point-in-time. Use EdgarProvider, which does. See DECISIONS.md D-004."
        )

    def get_issuers(self) -> pd.DataFrame:
        raise ProviderUnavailableError(
            "yfinance has no issuer concept - it is keyed on ticker, which is a "
            "listing-level display label. Use the security master."
        )

    def get_securities(self) -> pd.DataFrame:
        raise ProviderUnavailableError("yfinance cannot enumerate a universe")

    def get_listings(self) -> pd.DataFrame:
        raise ProviderUnavailableError("yfinance cannot enumerate a universe")

    def get_identifier_map(self) -> pd.DataFrame:
        raise ProviderUnavailableError(
            "yfinance exposes only tickers. A ticker is not an identifier: it is "
            "recycled, market-specific, and carries no issuer or security level."
        )


#: Yahoo's sector taxonomy mapped onto ICB level 1 codes. Approximate by nature - the
#: two schemes disagree on where to put several kinds of company, and that disagreement
#: is itself a source of index turnover when a provider switches classification systems.
YAHOO_SECTOR_TO_ICB: dict[str, str] = {
    "Technology": "10",
    "Communication Services": "15",
    "Healthcare": "20",
    "Financial Services": "30",
    "Real Estate": "35",
    "Consumer Cyclical": "40",
    "Consumer Defensive": "45",
    "Industrials": "50",
    "Basic Materials": "55",
    "Energy": "60",
    "Utilities": "65",
}


@dataclass
class EdgarProvider:
    """Point-in-time US fundamentals from the SEC Financial Statement Data Sets.

    Genuinely PIT, which is what makes it worth the parsing effort: each row carries
    ``adsh`` (the accession number), ``period``, and ``filed``. Restatements appear as a
    second submission for the same period with a later ``filed``, exactly the shape the
    research layer needs to handle.

    Quarterly ZIPs at ``https://www.sec.gov/files/dera/data/financial-statement-data-sets/``.
    """

    cache_dir: Path = Path("data/raw/edgar")
    contact: str = USER_AGENT

    BASE = "https://www.sec.gov/files/dera/data/financial-statement-data-sets"

    #: US-GAAP XBRL tags mapped to this project's canonical item names.
    TAG_MAP: dict[str, str] = field(
        default_factory=lambda: {
            "StockholdersEquity": "BOOK_EQUITY",
            "NetIncomeLoss": "NET_INCOME",
            "Revenues": "REVENUE",
            "RevenueFromContractWithCustomerExcludingAssessedTax": "REVENUE",
            "Assets": "TOTAL_ASSETS",
            "GrossProfit": "GROSS_PROFIT",
            "NetCashProvidedByUsedInOperatingActivities": "OPERATING_CASHFLOW",
            "PaymentsToAcquirePropertyPlantAndEquipment": "CAPEX",
            "PaymentsOfDividends": "DIVIDENDS_PAID",
        }
    )

    @property
    def name(self) -> str:
        return "sec-edgar"

    def download_quarter(self, year: int, quarter: int) -> Path:
        """Fetch and cache one quarterly ZIP."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        dest = self.cache_dir / f"{year}q{quarter}.zip"
        if dest.exists():
            return dest
        url = f"{self.BASE}/{year}q{quarter}.zip"
        resp = requests.get(url, headers={"User-Agent": self.contact}, timeout=120)
        resp.raise_for_status()
        dest.write_bytes(resp.content)
        return dest

    def parse_quarter(self, year: int, quarter: int) -> pd.DataFrame:
        """Join ``sub.txt`` (submissions, carrying the filing date) to ``num.txt``
        (the numeric facts) and map XBRL tags to canonical items."""
        path = self.download_quarter(year, quarter)
        with zipfile.ZipFile(path) as zf:
            sub = pd.read_csv(
                io.BytesIO(zf.read("sub.txt")), sep="\t",
                usecols=["adsh", "cik", "name", "filed", "period", "fy", "fp"],
                dtype={"adsh": str, "cik": int}, low_memory=False,
            )
            num = pd.read_csv(
                io.BytesIO(zf.read("num.txt")), sep="\t",
                usecols=["adsh", "tag", "ddate", "qtrs", "uom", "value"],
                dtype={"adsh": str, "tag": str}, low_memory=False,
            )

        num = num[num["tag"].isin(self.TAG_MAP) & (num["uom"] == "USD")]
        merged = num.merge(sub, on="adsh", how="inner")
        merged["item"] = merged["tag"].map(self.TAG_MAP)
        merged["filed_date"] = pd.to_datetime(merged["filed"], format="%Y%m%d").dt.date
        merged["period_end"] = pd.to_datetime(
            merged["ddate"], format="%Y%m%d", errors="coerce").dt.date
        merged["security_id"] = merged["cik"].astype(str).str.zfill(10)
        merged["currency"] = "USD"
        out = merged[
            ["security_id", "item", "period_end", "filed_date", "value", "currency", "adsh"]
        ].dropna(subset=["period_end", "value"])
        return out.drop_duplicates(
            subset=["security_id", "item", "period_end", "adsh"]).reset_index(drop=True)

    def build_pit_store(self, start_year: int, end_year: int, dest: Path) -> Path:
        """Partition several years of filings to parquet, partitioned by filing year so
        the ``filed_date <= as_of`` predicate can be pushed down."""
        dest.mkdir(parents=True, exist_ok=True)
        frames = [
            self.parse_quarter(y, q)
            for y in range(start_year, end_year + 1)
            for q in (1, 2, 3, 4)
        ]
        all_data = pd.concat(frames, ignore_index=True)
        all_data["filed_year"] = pd.to_datetime(all_data["filed_date"]).dt.year
        all_data.to_parquet(dest, partition_cols=["filed_year"], index=False)
        return dest

    def get_fundamentals(
        self, security_ids: list[str] | None, items: list[str], as_of: dt.date,
        max_staleness_days: int = 550,
    ) -> pd.DataFrame:
        store = self.cache_dir / "pit.parquet"
        if not store.exists():
            raise ProviderUnavailableError(
                f"no PIT store at {store} - run build_pit_store() first"
            )
        df = pd.read_parquet(store)
        known = df[(df["filed_date"] <= as_of) & (df["item"].isin(items))]
        known = known[known["period_end"] >= as_of - dt.timedelta(days=max_staleness_days)]
        if security_ids is not None:
            known = known[known["security_id"].isin(security_ids)]
        return (
            known.sort_values(["security_id", "item", "period_end", "filed_date"])
            .groupby(["security_id", "item"], as_index=False).last().reset_index(drop=True)
        )


# --------------------------------------------------------------------------------------


    # ------------------------------------------------------------------ reference

    COMPANY_TICKERS = "https://www.sec.gov/files/company_tickers.json"

    def fetch_company_tickers(self) -> pd.DataFrame:
        """The SEC's CIK-to-ticker map: the free backbone of a US security master.

        CIK is an **issuer**-level identifier and is permanent, which makes it a far
        better key than a ticker. One CIK can carry several tickers — Alphabet's two
        share classes share a CIK — which is exactly the issuer/security distinction a
        ticker-keyed dataset cannot express.
        """
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        dest = self.cache_dir / "company_tickers.json"
        if not dest.exists():
            resp = requests.get(self.COMPANY_TICKERS,
                                headers={"User-Agent": self.contact}, timeout=60)
            resp.raise_for_status()
            dest.write_bytes(resp.content)
        raw = json.loads(dest.read_text(encoding="utf-8"))
        frame = pd.DataFrame(list(raw.values()))
        frame["security_id"] = frame["cik_str"].astype(str).str.zfill(10)
        return frame.rename(columns={"cik_str": "cik", "title": "name"})

    def get_issuers(self) -> pd.DataFrame:
        frame = self.fetch_company_tickers()
        return (
            frame[["security_id", "name"]]
            .drop_duplicates("security_id")
            .rename(columns={"security_id": "issuer_id"})
            .assign(country="US", market_status="DEVELOPED")
            .reset_index(drop=True)
        )

    def get_securities(self) -> pd.DataFrame:
        frame = self.fetch_company_tickers()
        # Several tickers under one CIK means several share classes under one issuer.
        counts = frame.groupby("security_id")["ticker"].transform("size")
        return frame.assign(
            issuer_id=frame["security_id"], currency="USD", country="US",
            market_status="DEVELOPED", icb_industry="UNKNOWN",
            security_type="ORDINARY", is_dual_class=counts > 1,
            foreign_ownership_limit=1.0, listing_start=None, listing_end=None,
        )[["security_id", "issuer_id", "name", "ticker", "country", "currency",
           "market_status", "icb_industry", "security_type", "is_dual_class",
           "foreign_ownership_limit", "listing_start", "listing_end"]]

    def get_listings(self) -> pd.DataFrame:
        frame = self.fetch_company_tickers()
        exchange = frame.get("exchange", pd.Series("XNAS", index=frame.index))
        return pd.DataFrame({
            "listing_id": frame["ticker"], "security_id": frame["security_id"],
            "mic": exchange.fillna("XNAS"), "currency": "USD", "country": "US",
            "listing_start": None, "listing_end": None,
        })

    def get_identifier_map(self) -> pd.DataFrame:
        frame = self.fetch_company_tickers()
        return pd.DataFrame({
            "security_id": frame["security_id"], "listing_id": frame["ticker"],
            "ticker": frame["ticker"], "cik": frame["security_id"],
            "isin": None, "sedol": None,
            "valid_from": dt.date(1993, 1, 1), "valid_to": None,
        })

    def get_prices(self, listing_ids: list[str] | None, start: dt.date, end: dt.date
                   ) -> pd.DataFrame:
        raise ProviderUnavailableError(
            "EDGAR carries filings, not market data. Compose it with a price provider "
            "via CompositeProvider - that separation is the normal shape of a real "
            "platform, not a limitation."
        )

    def get_corp_actions(self, security_ids: list[str] | None, start: dt.date,
                         end: dt.date) -> pd.DataFrame:
        raise ProviderUnavailableError(
            "corporate action detail is not in the Financial Statement Data Sets. "
            "8-K and S-1 filings describe them in prose, which is a structured-"
            "extraction problem - see agents.extraction."
        )

    def get_shares(self, security_ids: list[str] | None, as_of: dt.date) -> pd.DataFrame:
        """Shares outstanding from the cover page of the latest filing.

        Genuinely point-in-time, unlike every free alternative: the count is as stated
        on a filing with a known date. Still no free float — that is a commercial data
        product and there is no free substitute.
        """
        store = self.cache_dir / "pit.parquet"
        if not store.exists():
            raise ProviderUnavailableError(f"no PIT store at {store}")
        df = pd.read_parquet(store)
        shares = df[(df["item"] == "SHARES_OUTSTANDING") & (df["filed_date"] <= as_of)]
        if security_ids is not None:
            shares = shares[shares["security_id"].isin(security_ids)]
        if shares.empty:
            raise ProviderUnavailableError("no share counts in the PIT store")
        latest = (
            shares.sort_values(["security_id", "period_end", "filed_date"])
            .groupby("security_id", as_index=False).last()
        )
        return latest.assign(
            effective_date=latest["period_end"], knowledge_date=latest["filed_date"],
            shares_outstanding=latest["value"], free_float_factor=1.0,
            foreign_ownership_limit=1.0,
        )[["security_id", "effective_date", "knowledge_date", "shares_outstanding",
           "free_float_factor", "foreign_ownership_limit"]]

    def get_shares_history(self, security_ids: list[str] | None, start: dt.date,
                           end: dt.date) -> pd.DataFrame:
        store = self.cache_dir / "pit.parquet"
        if not store.exists():
            raise ProviderUnavailableError(f"no PIT store at {store}")
        df = pd.read_parquet(store)
        shares = df[(df["item"] == "SHARES_OUTSTANDING")
                    & (df["period_end"] >= start) & (df["period_end"] <= end)]
        if security_ids is not None:
            shares = shares[shares["security_id"].isin(security_ids)]
        return shares.assign(
            effective_date=shares["period_end"], knowledge_date=shares["filed_date"],
            shares_outstanding=shares["value"], free_float_factor=1.0,
            foreign_ownership_limit=1.0,
        )[["security_id", "effective_date", "knowledge_date", "shares_outstanding",
           "free_float_factor", "foreign_ownership_limit"]]

    def get_fx(self, base: str, quotes: list[str], start: dt.date, end: dt.date
               ) -> pd.DataFrame:
        raise ProviderUnavailableError("EDGAR publishes no exchange rates")

    def get_deposit_rates(self, currencies: list[str], start: dt.date, end: dt.date
                          ) -> pd.DataFrame:
        raise ProviderUnavailableError("EDGAR publishes no interest rates")

    def get_classifications(self, security_ids: list[str] | None, as_of: dt.date
                            ) -> pd.DataFrame:
        """SIC code from the submissions file, mapped onto ICB level 1.

        SIC is coarse and dated — it was designed in the 1930s and has no concept of a
        software company — but it is genuinely point-in-time and free, which no other
        classification is.
        """
        frame = self.fetch_company_tickers()
        rows = [{
            "security_id": r.security_id, "effective_date": as_of,
            "knowledge_date": as_of,
            "icb_industry": SIC_TO_ICB.get(str(getattr(r, "sic", ""))[:2], "UNKNOWN"),
            "icb_supersector": str(getattr(r, "sic", "")),
        } for r in frame.itertuples(index=False)]
        out = pd.DataFrame(rows)
        if security_ids is not None:
            out = out[out["security_id"].isin(security_ids)]
        return out.reset_index(drop=True)


#: SIC division prefix -> ICB level 1. Lossy, and honestly so: SIC predates most of the
#: economy it is being asked to classify.
SIC_TO_ICB: dict[str, str] = {
    "73": "10", "35": "10", "36": "10", "48": "15", "28": "20", "80": "20",
    "60": "30", "61": "30", "62": "30", "63": "30", "65": "35", "59": "40",
    "58": "40", "20": "45", "54": "45", "37": "50", "34": "50", "33": "55",
    "29": "60", "13": "60", "49": "65",
}


@dataclass
class ISharesProvider:
    """Daily ETF holdings from iShares - the best free proxy for real index membership.

    The CSVs carry ISIN, SEDOL and CUSIP alongside weight, shares and market value,
    which makes them usable for two things the synthetic universe cannot teach:
    reverse-engineering implied free-float factors, and measuring real reconstitution
    turnover.

    Archive these daily. The historical files are not retrievable, so the archive you
    did not start last year is the study you cannot run today.
    """

    cache_dir: Path = Path("data/raw/ishares")

    FUNDS: dict[str, str] = field(
        default_factory=lambda: {
            "IWB": "239707/ishares-russell-1000-etf",
            "IWM": "239710/ishares-russell-2000-etf",
            "IVV": "239726/ishares-core-sp-500-etf",
            "IEFA": "244049/ishares-core-msci-eafe-etf",
            "IEMG": "244050/ishares-core-msci-emerging-markets-etf",
        }
    )

    @property
    def name(self) -> str:
        return "ishares"

    def holdings_url(self, ticker: str) -> str:
        slug = self.FUNDS[ticker]
        fund_id, name = slug.split("/", 1)
        return (
            f"https://www.ishares.com/us/products/{fund_id}/{name}/1467271812596.ajax"
            f"?fileType=csv&fileName={ticker}_holdings&dataType=fund"
        )

    def fetch_holdings(self, ticker: str, as_of: dt.date | None = None) -> pd.DataFrame:
        """Download today's holdings and cache under the retrieval date.

        The cache key is the *retrieval* date, not a date you can choose: the endpoint
        only ever serves the current file.
        """
        as_of = as_of or dt.date.today()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        dest = self.cache_dir / f"{ticker}_{as_of.isoformat()}.csv"

        if not dest.exists():
            resp = requests.get(
                self.holdings_url(ticker), headers={"User-Agent": USER_AGENT}, timeout=60
            )
            resp.raise_for_status()
            dest.write_bytes(resp.content)

        text = dest.read_text(encoding="utf-8", errors="replace")
        # The file carries a variable-length preamble before the real header row.
        lines = text.splitlines()
        header_ix = next(
            (i for i, line in enumerate(lines) if line.lstrip('"').startswith("Ticker")), 0
        )
        df = pd.read_csv(io.StringIO("\n".join(lines[header_ix:])), thousands=",")
        df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
        df["fund"] = ticker
        df["as_of"] = as_of
        return df

    def archive_all(self, as_of: dt.date | None = None) -> dict[str, Path]:
        """Snapshot every configured fund. Wire this to a daily scheduled job."""
        out: dict[str, Path] = {}
        as_of = as_of or dt.date.today()
        for ticker in self.FUNDS:
            self.fetch_holdings(ticker, as_of)
            out[ticker] = self.cache_dir / f"{ticker}_{as_of.isoformat()}.csv"
        return out

    def implied_float_factors(self, ticker: str, prices: pd.DataFrame,
                              shares_outstanding: pd.DataFrame) -> pd.DataFrame:
        """Back out each holding's implied free-float factor from its index weight.

        If the index is float-weighted then weight_i is proportional to
        ``price_i * shares_i * float_i``, so dividing the observed weight by the
        full-cap weight recovers ``float_i`` up to a constant. Normalising by the
        maximum implied value pins that constant on the assumption that at least one
        constituent is fully free-floating - crude, but it puts the rest on a
        defensible relative scale.
        """
        h = self.fetch_holdings(ticker)
        h = h[h["asset_class"].astype(str).str.upper().eq("EQUITY")] if "asset_class" \
            in h.columns else h
        merged = (
            h.merge(prices, left_on="ticker", right_on="security_id", how="inner")
            .merge(shares_outstanding, on="security_id", how="inner")
        )
        merged["full_cap"] = merged["close"] * merged["shares_outstanding"]
        merged["full_cap_weight"] = merged["full_cap"] / merged["full_cap"].sum()
        merged["index_weight"] = merged["weight_(%)"] / 100.0
        merged["implied_float_ratio"] = merged["index_weight"] / merged["full_cap_weight"]
        merged["implied_float"] = (
            merged["implied_float_ratio"] / merged["implied_float_ratio"].max()
        ).clip(0.0, 1.0)
        return merged[
            ["ticker", "name", "index_weight", "full_cap_weight", "implied_float"]
        ].sort_values("index_weight", ascending=False).reset_index(drop=True)

    @staticmethod
    def reconstitution_diff(before: pd.DataFrame, after: pd.DataFrame) -> dict[str, Any]:
        """Additions, deletions, weight drift and one-way turnover between two snapshots.

        The attribution is the useful part: total turnover splits into the part caused
        by names entering and leaving, and the part caused by surviving names being
        reweighted. A client asking "why did the index turn over 4.2%?" is asking for
        this decomposition, not the headline.
        """
        b = before.set_index("ticker")["weight_(%)"] / 100.0
        a = after.set_index("ticker")["weight_(%)"] / 100.0
        all_ix = b.index.union(a.index)
        b, a = b.reindex(all_ix).fillna(0.0), a.reindex(all_ix).fillna(0.0)

        additions = all_ix[(b == 0) & (a > 0)]
        deletions = all_ix[(b > 0) & (a == 0)]
        survivors = all_ix[(b > 0) & (a > 0)]

        return {
            "n_additions": int(len(additions)),
            "n_deletions": int(len(deletions)),
            "additions_weight": float(a[additions].sum()),
            "deletions_weight": float(b[deletions].sum()),
            "survivor_drift": float((a[survivors] - b[survivors]).abs().sum() / 2),
            "one_way_turnover": float((a - b).abs().sum() / 2),
            "additions": list(additions),
            "deletions": list(deletions),
        }


# --------------------------------------------------------------------------------------


@dataclass
class LsegDataProvider:
    """Stub for the LSEG Data Library (``lseg.data``, formerly ``refinitiv.data``).

    Unlicensed here, and deliberately not faked. The value of the class is that it
    fixes the *shape* of the integration, so the equivalence between what was built on
    free data and what would run in production is explicit rather than hand-waved:

    ======================  ==================================  =========================
    Concept                 LSEG source                         Free equivalent used here
    ======================  ==================================  =========================
    Price history           Datastream ``P`` / ``RI``           yfinance close
    Total return index      Datastream ``RI``                   computed in ``calc.index``
    Shares outstanding      Worldscope ``WC05301``              synthetic / EDGAR
    Book equity             Worldscope ``WC03501``              EDGAR StockholdersEquity
    Net income              Worldscope ``WC01751``              EDGAR NetIncomeLoss
    Revenue                 Worldscope ``WC01001``              EDGAR Revenues
    Total assets            Worldscope ``WC02999``              EDGAR Assets
    Consensus EPS           IBES summary tape                   not available free
    Estimate revisions      IBES detail history                 not available free
    Free float              Datastream ``WC08001`` / FTSE       inferred from iShares
    Entity identifiers      PermID                              PermID (free API)
    Fund flows              Lipper                              not available free
    ======================  ==================================  =========================

    The gotcha worth stating in an interview: IBES summary files are *dated*, and using
    the current consensus for a past date is a look-ahead error that makes any
    estimate-revision factor look far better than it is.
    """

    app_key: str | None = None
    _session: Any = field(default=None, repr=False)

    #: Canonical item name -> the TR.* field the LSEG Data Library exposes it under,
    #: with the underlying Worldscope item in the comment. Kept as data rather than
    #: buried in each method so the mapping is reviewable in one place - and so the
    #: vocabulary map in docs/ can be generated from it rather than transcribed.
    FIELD_MAP: dict[str, str] = field(default_factory=lambda: {
        "BOOK_EQUITY": "TR.F.TotShHoldEq",          # WC03501
        "NET_INCOME": "TR.F.NetIncAfterTax",        # WC01751
        "REVENUE": "TR.F.TotRevenue",               # WC01001
        "TOTAL_ASSETS": "TR.F.TotAssets",           # WC02999
        "TOTAL_DEBT": "TR.F.TotDebt",               # WC03255
        "GROSS_PROFIT": "TR.F.GrossProfIndPropTot",  # WC01100
        "OPERATING_CASHFLOW": "TR.F.NetCashFlowOp",  # WC04860
        "CAPEX": "TR.F.CAPEXTot",                   # WC04601
        "DIVIDENDS_PAID": "TR.F.DivPaidTot",        # WC04551
        "SHARES_OUTSTANDING": "TR.F.ComShrOutsTot",  # WC05301
        "FREE_FLOAT": "TR.FreeFloatPct",
    })

    @property
    def name(self) -> str:
        return "lseg-data"

    def available(self) -> bool:
        """True when the library is installed and an app key is present."""
        try:
            import lseg.data  # noqa: F401
        except ImportError:
            return False
        return bool(self.app_key)

    def _open(self) -> Any:
        """Open a session, or explain precisely what is missing.

        Real code behind an import guard rather than commented-out call shapes. It runs
        the moment the library and a key are present, and until then it fails with the
        specific reason instead of quietly returning nothing.
        """
        if self._session is not None:
            return self._session
        try:
            import lseg.data as ld
        except ImportError as exc:
            raise ProviderUnavailableError(
                "the LSEG Data Library is not installed. `uv add lseg-data`, then "
                "supply an app key from a Workspace or Eikon desktop session."
            ) from exc
        if not self.app_key:
            raise ProviderUnavailableError(
                "no LSEG app key. Set MINIFTSE_LSEG_APP_KEY or pass app_key=."
            )
        ld.open_session(app_key=self.app_key)
        self._session = ld
        return ld

    def get_prices(self, listing_ids: list[str] | None, start: dt.date, end: dt.date
                   ) -> pd.DataFrame:
        """Daily closes from Datastream, keyed on RIC."""
        if listing_ids is None:
            raise ProviderUnavailableError(
                "supply RICs explicitly; universe enumeration goes through the "
                "index constituent service, not the history endpoint"
            )
        ld = self._open()
        raw = ld.get_history(
            universe=listing_ids,
            fields=["TR.PriceClose", "TR.PriceOpen", "TR.PriceHigh", "TR.PriceLow",
                    "TR.Volume", "TR.PriceCloseCurrency"],
            start=start.isoformat(), end=end.isoformat(), interval="daily",
        )
        return self._normalise_prices(raw)

    @staticmethod
    def _normalise_prices(raw: pd.DataFrame) -> pd.DataFrame:
        """Reshape an LSEG history frame into `PRICE_SCHEMA`.

        Separated from the call so it is unit-testable against a recorded fixture
        without a licence — which is the only part of a vendor adapter that ever
        actually breaks.
        """
        frame = raw.reset_index()
        frame.columns = [str(c).strip().lower().replace(" ", "_") for c in frame.columns]
        rename = {
            "instrument": "listing_id", "price_close": "close", "price_open": "open",
            "price_high": "high", "price_low": "low", "volume": "volume",
            "price_close_currency": "currency",
        }
        frame = frame.rename(columns={k: v for k, v in rename.items()
                                      if k in frame.columns})
        if "date" in frame.columns:
            frame["date"] = pd.to_datetime(frame["date"], utc=True).dt.date
        frame["security_id"] = frame.get("listing_id")
        frame["is_suspended"] = False
        wanted = ["security_id", "listing_id", "date", "open", "high", "low", "close",
                  "volume", "currency", "is_suspended"]
        return frame[[c for c in wanted if c in frame.columns]]

    def get_fundamentals(self, security_ids: list[str] | None, items: list[str],
                         as_of: dt.date, max_staleness_days: int = 550) -> pd.DataFrame:
        """Worldscope fundamentals as they stood on `as_of`.

        `SDate` plus `Period="FY0"` is what makes this point-in-time. Omit them and the
        API returns the *restated* figure, which is the classic look-ahead: a value
        factor built on it uses book equity nobody could have known.
        """
        if security_ids is None:
            raise ProviderUnavailableError("supply an explicit universe")
        ld = self._open()
        unknown = [i for i in items if i not in self.FIELD_MAP]
        if unknown:
            raise ProviderUnavailableError(f"no LSEG field mapped for {unknown}")

        raw = ld.get_data(
            universe=security_ids,
            fields=[self.FIELD_MAP[i] for i in items] + ["TR.F.PeriodEndDate",
                                                         "TR.F.OriginalAnnounceDate"],
            parameters={"Period": "FY0", "SDate": as_of.isoformat(), "Scale": 0},
        )
        return self._normalise_fundamentals(raw, items, as_of)

    def _normalise_fundamentals(self, raw: pd.DataFrame, items: list[str],
                                as_of: dt.date) -> pd.DataFrame:
        inverse = {self.FIELD_MAP[i]: i for i in items}
        frame = raw.reset_index()
        frame.columns = [str(c).strip() for c in frame.columns]
        id_col = next((c for c in frame.columns if c.lower() == "instrument"),
                      frame.columns[0])

        rows: list[dict[str, Any]] = []
        for row in frame.itertuples(index=False):
            record = row._asdict()
            period_end = record.get("Period End Date")
            filed = record.get("Original Announcement Date") or period_end
            for field_name, item in inverse.items():
                value = record.get(field_name)
                if value is None or (isinstance(value, float) and pd.isna(value)):
                    continue
                rows.append({
                    "security_id": record.get(id_col), "item": item,
                    "period_end": pd.Timestamp(period_end).date() if period_end
                    else None,
                    "filed_date": pd.Timestamp(filed).date() if filed else as_of,
                    "value": float(value), "currency": record.get("Currency", "USD"),
                })
        out = pd.DataFrame(rows)
        # Enforce the contract locally rather than trusting the vendor's SDate handling.
        # A defensive filter costs nothing and catches a configuration mistake that is
        # otherwise invisible until someone audits a factor two years later.
        return out[out["filed_date"] <= as_of] if not out.empty else out

    def get_estimates(self, security_ids: list[str], as_of: dt.date) -> pd.DataFrame:
        """IBES consensus as it stood on `as_of`.

        The `SDate` parameter is the whole point. IBES summary files are dated, and
        using today's consensus for a past date makes an estimate-revision factor look
        far better than it is — the classic way that signal gets faked.
        """
        ld = self._open()
        raw = ld.get_data(
            universe=security_ids,
            fields=["TR.EPSMean", "TR.EPSNumberOfEstimates", "TR.EPSMeanEstDate",
                    "TR.EPSMean(Period=FY1)", "TR.EPSMean(Period=FY2)"],
            parameters={"SDate": as_of.isoformat(), "Period": "FY1"},
        )
        frame = raw.reset_index()
        frame["as_of"] = as_of
        return frame

    def get_index_constituents(self, index_ric: str, as_of: dt.date) -> pd.DataFrame:
        """FTSE Russell constituents and weights, as at a date.

        The endpoint that makes real reconciliation possible: comparing a rebuilt index
        against the published constituent list is the only way to find out whether the
        rules were implemented as written.
        """
        ld = self._open()
        return ld.get_data(
            universe=[f"0#{index_ric.lstrip('0#')}"],
            fields=["TR.IndexConstituentRIC", "TR.IndexConstituentWeightPercent",
                    "TR.IndexConstituentName"],
            parameters={"SDate": as_of.isoformat()},
        )

    def get_shares(self, security_ids: list[str] | None, as_of: dt.date) -> pd.DataFrame:
        frame = self.get_fundamentals(security_ids, ["SHARES_OUTSTANDING"], as_of)
        if frame.empty:
            raise ProviderUnavailableError("no share counts returned")
        free_float = self.get_fundamentals(security_ids, ["FREE_FLOAT"], as_of)
        floats = (free_float.set_index("security_id")["value"] / 100.0
                  if not free_float.empty else pd.Series(dtype=float))
        return pd.DataFrame({
            "security_id": frame["security_id"],
            "effective_date": frame["period_end"],
            "knowledge_date": frame["filed_date"],
            "shares_outstanding": frame["value"],
            "free_float_factor": frame["security_id"].map(floats).fillna(1.0),
            "foreign_ownership_limit": 1.0,
        })

    def get_shares_history(self, security_ids: list[str] | None, start: dt.date,
                           end: dt.date) -> pd.DataFrame:
        raise ProviderUnavailableError(
            "share-count history needs a Datastream time-series request per security "
            "(WC05301 with a date range) rather than a point-in-time snapshot"
        )

    def get_corp_actions(self, security_ids: list[str] | None, start: dt.date,
                         end: dt.date) -> pd.DataFrame:
        ld = self._open()
        raw = ld.get_data(
            universe=security_ids or [],
            fields=["TR.CAAdjustmentType", "TR.CAAdjustmentFactor",
                    "TR.CAExDate", "TR.CAPayDate", "TR.CAGrossDivAmount"],
            parameters={"SDate": start.isoformat(), "EDate": end.isoformat()},
        )
        return raw.reset_index()

    def get_fx(self, base: str, quotes: list[str], start: dt.date, end: dt.date
               ) -> pd.DataFrame:
        ld = self._open()
        raw = ld.get_history(
            universe=[f"{q}{base}=R" for q in quotes if q != base],
            fields=["TR.PriceClose"], start=start.isoformat(), end=end.isoformat(),
        )
        frame = raw.reset_index()
        frame["base"] = base
        return frame

    def get_deposit_rates(self, currencies: list[str], start: dt.date, end: dt.date
                          ) -> pd.DataFrame:
        ld = self._open()
        return ld.get_history(
            universe=[f"{c}1MD=" for c in currencies], fields=["TR.PriceClose"],
            start=start.isoformat(), end=end.isoformat(),
        ).reset_index()

    def get_classifications(self, security_ids: list[str] | None, as_of: dt.date
                            ) -> pd.DataFrame:
        """ICB directly, which is the point — this is FTSE Russell's own scheme."""
        ld = self._open()
        raw = ld.get_data(
            universe=security_ids or [],
            fields=["TR.ICBIndustryCode", "TR.ICBSupersectorCode", "TR.ICBSectorCode"],
            parameters={"SDate": as_of.isoformat()},
        )
        frame = raw.reset_index()
        frame["effective_date"] = as_of
        frame["knowledge_date"] = as_of
        return frame

    def get_issuers(self) -> pd.DataFrame:
        raise ProviderUnavailableError(
            "issuer master comes from PermID entity records, not the data library. "
            "Use PermIdProvider."
        )

    def get_securities(self) -> pd.DataFrame:
        raise ProviderUnavailableError("enumerate via get_index_constituents")

    def get_listings(self) -> pd.DataFrame:
        raise ProviderUnavailableError("enumerate via get_index_constituents")

    def get_identifier_map(self) -> pd.DataFrame:
        ld = self._open()
        return ld.get_data(
            universe=[], fields=["TR.ISIN", "TR.SEDOL", "TR.CUSIP", "TR.RIC",
                                 "TR.OrganizationID"],
        ).reset_index()


@dataclass
class PermIdProvider:
    """LSEG PermID - a free, open, permanent entity identifier service.

    Worth using for real: it is the one LSEG-specific piece of infrastructure available
    without a licence, and permanence across renames and redomiciles is exactly the
    property a security master needs.
    """

    api_key: str | None = None
    BASE = "https://api.permid.org/match"

    @property
    def name(self) -> str:
        return "permid"

    def match_organisations(self, names: list[str], countries: list[str] | None = None
                            ) -> pd.DataFrame:
        """Resolve company names to PermIDs via the record-matching endpoint."""
        if not self.api_key:
            raise ProviderUnavailableError(
                "PermID needs a free API key from developers.lseg.com"
            )
        countries = countries or ["" for _ in names]
        payload = "\n".join(
            ["LocalID,Standard,Name,Country"]
            + [f"{i},Organization,{n},{c}" for i, (n, c) in enumerate(zip(names, countries,
                strict=False))]
        )
        resp = requests.post(
            f"{self.BASE}/file",
            headers={"x-ag-access-token": self.api_key, "Content-Type": "text/plain",
                     "x-openmatch-numberOfMatchesPerRecord": "1"},
            data=payload.encode(), timeout=60,
        )
        resp.raise_for_status()
        return pd.DataFrame(resp.json().get("outputContentResponse", []))


# --------------------------------------------------------------------------------------


@dataclass
class CompositeProvider:
    """Route each Protocol method to whichever backend serves it best.

    This is how a real platform is wired: prices from one vendor, fundamentals from
    another, reference data from a third, with a documented precedence order and a
    cross-vendor reconciliation job watching the seams. `quality.checks` contains the
    cross-source comparison that goes with it.
    """

    prices: Any
    fundamentals: Any
    reference: Any
    corp_actions: Any | None = None
    fx: Any | None = None

    @property
    def name(self) -> str:
        return (
            f"composite(prices={getattr(self.prices, 'name', '?')},"
            f"fundamentals={getattr(self.fundamentals, 'name', '?')},"
            f"reference={getattr(self.reference, 'name', '?')})"
        )

    #: Which backend serves each Protocol method. Explicit rather than implicit so the
    #: routing can be inspected - by `provider_capability_matrix`, and by whoever is
    #: debugging why a number came from the vendor they did not expect.
    ROUTING: dict[str, str] = field(default_factory=lambda: {
        "get_prices": "prices",
        "get_corp_actions": "corp_actions_or_prices",
        "get_fundamentals": "fundamentals",
        "get_shares": "reference",
        "get_shares_history": "reference",
        "get_fx": "fx_or_reference",
        "get_deposit_rates": "fx_or_reference",
        "get_classifications": "reference",
        "get_issuers": "reference",
        "get_securities": "reference",
        "get_listings": "reference",
        "get_identifier_map": "reference",
    })

    def route(self, method: str) -> Any:
        """The backend that would actually serve `method`."""
        target = self.ROUTING.get(method, "reference")
        if target == "corp_actions_or_prices":
            return self.corp_actions or self.prices
        if target == "fx_or_reference":
            return self.fx or self.reference
        return getattr(self, target)

    def get_prices(self, listing_ids: list[str] | None, start: dt.date, end: dt.date
                   ) -> pd.DataFrame:
        return self.prices.get_prices(listing_ids, start, end)

    def get_fundamentals(self, security_ids: list[str] | None, items: list[str],
                         as_of: dt.date, max_staleness_days: int = 550) -> pd.DataFrame:
        return self.fundamentals.get_fundamentals(
            security_ids, items, as_of, max_staleness_days)

    def get_corp_actions(self, security_ids: list[str] | None, start: dt.date,
                         end: dt.date) -> pd.DataFrame:
        source = self.corp_actions or self.prices
        return source.get_corp_actions(security_ids, start, end)

    def get_shares(self, security_ids: list[str] | None, as_of: dt.date) -> pd.DataFrame:
        return self.reference.get_shares(security_ids, as_of)

    def get_shares_history(self, security_ids: list[str] | None, start: dt.date,
                           end: dt.date) -> pd.DataFrame:
        return self.reference.get_shares_history(security_ids, start, end)

    def get_fx(self, base: str, quotes: list[str], start: dt.date, end: dt.date
               ) -> pd.DataFrame:
        source = self.fx or self.reference
        return source.get_fx(base, quotes, start, end)

    def get_deposit_rates(self, currencies: list[str], start: dt.date, end: dt.date
                          ) -> pd.DataFrame:
        source = self.fx or self.reference
        return source.get_deposit_rates(currencies, start, end)

    def get_classifications(self, security_ids: list[str] | None, as_of: dt.date
                            ) -> pd.DataFrame:
        return self.reference.get_classifications(security_ids, as_of)

    def get_issuers(self) -> pd.DataFrame:
        return self.reference.get_issuers()

    def get_securities(self) -> pd.DataFrame:
        return self.reference.get_securities()

    def get_listings(self) -> pd.DataFrame:
        return self.reference.get_listings()

    def get_identifier_map(self) -> pd.DataFrame:
        return self.reference.get_identifier_map()


@dataclass
class PermIdEnrichment:
    """Match a security master's issuers to PermIDs and report the failure modes.

    The match rate is the interesting output, not the matches. Name matching against a
    reference database fails in patterns — legal-form suffixes, transliteration,
    holding companies whose registered name differs from the trading name — and knowing
    *which* pattern is failing is what tells you whether to fix the input, the matcher,
    or accept the gap and route it to manual review.
    """

    provider: PermIdProvider
    min_confidence: float = 0.7

    def enrich(self, master: Any, limit: int | None = None) -> dict[str, Any]:
        """Attach PermIDs to issuers in place, returning a match report."""
        issuers = list(master.issuers.values())[:limit]
        if not issuers:
            return {"attempted": 0, "matched": 0, "match_rate": 0.0, "failures": {}}

        names = [i.name for i in issuers]
        countries = [str(i.nationality) for i in issuers]
        matches = self.provider.match_organisations(names, countries)

        matched = 0
        failures: dict[str, int] = {}
        for issuer, (_, row) in zip(issuers, matches.iterrows(), strict=False):
            perm_id = row.get("Match OpenPermID") or row.get("match_openpermid")
            score = float(row.get("Match Score", row.get("match_score", 0)) or 0)
            if perm_id and score >= self.min_confidence * 100:
                master.issuers[issuer.issuer_id] = replace_issuer(issuer, str(perm_id))
                matched += 1
            else:
                reason = self._classify_failure(issuer.name, score)
                failures[reason] = failures.get(reason, 0) + 1

        return {
            "attempted": len(issuers), "matched": matched,
            "match_rate": matched / len(issuers),
            "failures": failures,
            "note": (
                "A match rate below about 85% on real company names usually means the "
                "input names carry legal-form suffixes the matcher is not stripping, "
                "not that the entities are missing from PermID."
            ),
        }

    @staticmethod
    def _classify_failure(name: str, score: float) -> str:
        if score == 0:
            return "no candidate returned"
        if score < 50:
            return "weak match, likely a different entity"
        suffixes = (" plc", " ltd", " inc", " ag", " sa", " nv", " spa", " ab")
        if any(name.lower().endswith(s) for s in suffixes):
            return "borderline; name carries a legal-form suffix"
        return "borderline below confidence threshold"


def replace_issuer(issuer: Any, perm_id: str) -> Any:
    from dataclasses import replace as dc_replace

    return dc_replace(issuer, perm_id=perm_id)


@dataclass
class FredProvider:
    """Short rates and FX from the St. Louis Fed's FRED service.

    Free, no key required for the CSV endpoint, and the only free source of the deposit
    rates needed to synthesise covered-interest-parity forwards for the hedged index.
    Without it the free stack has a genuine hole: neither Yahoo nor EDGAR publishes an
    interest rate, so a currency hedge cannot be priced at all.
    """

    cache_dir: Path = Path("data/raw/fred")
    BASE = "https://fred.stlouisfed.org/graph/fredgraph.csv"

    #: Currency -> FRED series for a short money-market rate. Not perfectly comparable
    #: instruments across currencies, which matters: a CIP forward built from mismatched
    #: tenors carries a basis that looks like hedge error.
    RATE_SERIES: dict[str, str] = field(default_factory=lambda: {
        "USD": "DGS3MO", "EUR": "IR3TIB01EZM156N", "GBP": "IR3TIB01GBM156N",
        "JPY": "IR3TIB01JPM156N", "CHF": "IR3TIB01CHM156N", "CAD": "IR3TIB01CAM156N",
        "AUD": "IR3TIB01AUM156N", "SEK": "IR3TIB01SEM156N", "KRW": "IR3TIB01KRM156N",
        # HKD has no OECD three-month series on FRED. The Hong Kong dollar is pegged to
        # the US dollar under the Linked Exchange Rate System, so HIBOR tracks the USD
        # curve closely and the USD series is the standard proxy. Not free: the peg is
        # a policy choice, and HIBOR has decoupled from USD rates during liquidity
        # squeezes - so a hedged index carrying material HKD exposure should be priced
        # off quoted HKD forwards rather than this.
        "HKD": "DGS3MO",
    })

    PROXIED_RATES: frozenset[str] = frozenset({"HKD"})
    """Currencies served by another currency's series. Declared so the substitution is
    visible in a report rather than buried in a mapping."""

    FX_SERIES: dict[str, tuple[str, bool]] = field(default_factory=lambda: {
        # series id, and whether it is quoted as USD-per-unit (False means inverted)
        "GBP": ("DEXUSUK", True), "EUR": ("DEXUSEU", True), "AUD": ("DEXUSAL", True),
        "JPY": ("DEXJPUS", False), "CHF": ("DEXSZUS", False), "CAD": ("DEXCAUS", False),
        "SEK": ("DEXSDUS", False), "HKD": ("DEXHKUS", False), "KRW": ("DEXKOUS", False),
    })

    @property
    def name(self) -> str:
        return "fred"

    def _series(self, series_id: str) -> pd.DataFrame:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        dest = self.cache_dir / f"{series_id}.csv"
        if not dest.exists():
            resp = requests.get(self.BASE, params={"id": series_id},
                                headers={"User-Agent": USER_AGENT}, timeout=60)
            resp.raise_for_status()
            dest.write_bytes(resp.content)
        frame = pd.read_csv(dest)
        frame.columns = ["date", "value"]
        frame["date"] = pd.to_datetime(frame["date"]).dt.date
        # FRED marks missing observations with a literal '.', which reads as a string
        # and silently poisons the column dtype if not handled.
        frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
        return frame.dropna(subset=["value"])

    def get_deposit_rates(self, currencies: list[str], start: dt.date, end: dt.date
                          ) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        for ccy in currencies:
            series_id = self.RATE_SERIES.get(ccy)
            if series_id is None:
                raise ProviderUnavailableError(f"no FRED rate series mapped for {ccy}")
            frame = self._series(series_id)
            frame = frame[(frame["date"] >= start) & (frame["date"] <= end)]
            frames.append(pd.DataFrame({
                "date": frame["date"], "currency": ccy,
                "deposit_rate": frame["value"] / 100.0,  # FRED quotes percent
                "is_proxied": ccy in self.PROXIED_RATES,
            }))
        if not frames:
            raise ProviderUnavailableError("no deposit rates retrieved")
        return pd.concat(frames, ignore_index=True)

    def get_fx(self, base: str, quotes: list[str], start: dt.date, end: dt.date
               ) -> pd.DataFrame:
        if base != "USD":
            raise ProviderUnavailableError(
                "FRED quotes everything against USD; cross rates must be triangulated"
            )
        frames: list[pd.DataFrame] = []
        for quote in quotes:
            if quote == "USD":
                continue
            mapping = self.FX_SERIES.get(quote)
            if mapping is None:
                raise ProviderUnavailableError(f"no FRED FX series for {quote}")
            series_id, usd_per_unit = mapping
            frame = self._series(series_id)
            frame = frame[(frame["date"] >= start) & (frame["date"] <= end)]
            # Normalise to the repo convention: base per one unit of quote.
            rate = frame["value"] if usd_per_unit else 1.0 / frame["value"]
            frames.append(pd.DataFrame({
                "date": frame["date"], "base": base, "quote": quote, "rate": rate,
            }))
        if not frames:
            raise ProviderUnavailableError("no FX series retrieved")
        return pd.concat(frames, ignore_index=True)


def build_free_composite(
    price_source: Any | None = None,
    fundamental_source: Any | None = None,
    reference_source: Any | None = None,
    fx_source: Any | None = None,
) -> CompositeProvider:
    """The realistic free-data stack: prices from Yahoo, fundamentals from EDGAR.

    Exactly the shape a real platform has — prices from one vendor, fundamentals from
    another, reference from a third — and the reason `CompositeProvider` exists rather
    than a single monolithic adapter. Neither source alone satisfies the Protocol;
    together they very nearly do, and the gaps that remain (free float, corporate action
    detail) are precisely what the commercial products are sold for.
    """
    prices = price_source or YFinanceProvider()
    fundamentals = fundamental_source or EdgarProvider()
    reference = reference_source or fundamentals
    fx = fx_source or FredProvider()
    return CompositeProvider(prices=prices, fundamentals=fundamentals,
                             reference=reference, corp_actions=prices, fx=fx)


def provider_capability_matrix(providers: dict[str, Any]) -> pd.DataFrame:
    """Which provider can actually serve which Protocol method.

    Generated by introspection rather than maintained by hand, so it cannot drift from
    the code. This is the table to have in front of you when deciding what to compose
    with what — and the honest answer to "why not just use free data": the gaps are
    free float, corporate action detail, and analyst estimates, every time.
    """
    methods = [
        "get_prices", "get_corp_actions", "get_shares", "get_shares_history",
        "get_fundamentals", "get_fx", "get_deposit_rates", "get_classifications",
        "get_issuers", "get_securities", "get_listings", "get_identifier_map",
    ]
    rows: list[dict[str, Any]] = []
    for label, provider in providers.items():
        row: dict[str, Any] = {"provider": label}
        for method in methods:
            row[method] = _supports(provider, method)
        rows.append(row)
    return pd.DataFrame(rows).set_index("provider")


def _supports(provider: Any, method: str) -> str:
    """Whether a provider can genuinely serve a call.

    A composite delegates, so inspecting its wrapper says nothing: the wrapper always
    looks supported because all it does is forward. The first version of this matrix
    reported the free composite as supporting `get_deposit_rates` when neither Yahoo nor
    EDGAR publishes an interest rate — a capability table that overstates coverage is
    worse than none, because it is consulted precisely when choosing what to trust.
    """
    fn = getattr(provider, method, None)
    if fn is None:
        return "absent"

    if isinstance(provider, CompositeProvider):
        target = provider.route(method)
        return "routed" if _supports(target, method) == "yes" else "unsupported"

    try:
        import inspect

        source = inspect.getsource(fn)
    except (OSError, TypeError):
        return "yes"
    # A body that does nothing but raise is a legitimate implementation - it states the
    # limit rather than returning an empty frame - but it is not coverage.
    body = source.split('"""')[-1] if '"""' in source else source
    return "unsupported" if "raise ProviderUnavailableError" in body \
        and "return " not in body else "yes"


def default_provider(n_securities: int = 500, seed: int = 20260809) -> MarketDataProvider:
    """The reference universe. Everything in this repo runs against this by default."""
    from miniftse.data.synthetic import SyntheticConfig, SyntheticUniverse

    return SyntheticUniverse(  # type: ignore[return-value]
        SyntheticConfig(n_securities=n_securities, seed=seed)
    )
