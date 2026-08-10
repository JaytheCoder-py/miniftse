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

    @property
    def name(self) -> str:
        return "lseg-data (stub)"

    def _require(self) -> None:
        raise ProviderUnavailableError(
            "LSEG Data Library is not licensed in this environment. The call shapes "
            "below are correct; supply an app key and install `lseg-data` to enable."
        )

    def get_prices(self, listing_ids: list[str] | None, start: dt.date, end: dt.date
                   ) -> pd.DataFrame:
        # import lseg.data as ld
        # ld.open_session(app_key=self.app_key)
        # return ld.get_history(
        #     universe=listing_ids,                  # RICs, e.g. ["VOD.L", "AAPL.O"]
        #     fields=["TR.PriceClose", "TR.Volume", "TR.PriceCloseCurrency"],
        #     start=start, end=end, interval="daily",
        # )
        self._require()
        raise AssertionError("unreachable")

    def get_fundamentals(self, security_ids: list[str] | None, items: list[str],
                         as_of: dt.date, max_staleness_days: int = 550) -> pd.DataFrame:
        # Worldscope items via the TR.* field layer. `Period="FY0"` plus an explicit
        # as-of is what keeps this point-in-time; omitting it returns the restated
        # figure and reintroduces look-ahead.
        # return ld.get_data(
        #     universe=security_ids,
        #     fields=["TR.F.TotShHoldEq", "TR.F.NetIncAfterTax", "TR.F.TotRevenue"],
        #     parameters={"Period": "FY0", "SDate": as_of.isoformat(), "Scale": 6},
        # )
        self._require()
        raise AssertionError("unreachable")

    def get_estimates(self, security_ids: list[str], as_of: dt.date) -> pd.DataFrame:
        # IBES consensus, as it stood on `as_of` - not as it stands today.
        # return ld.get_data(
        #     universe=security_ids,
        #     fields=["TR.EPSMean", "TR.EPSNumberOfEstimates", "TR.EPSMeanEstDate"],
        #     parameters={"SDate": as_of.isoformat(), "Period": "FY1"},
        # )
        self._require()
        raise AssertionError("unreachable")


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
            + [f"{i},Organization,{n},{c}" for i, (n, c) in enumerate(zip(names, countries))]
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


def default_provider(n_securities: int = 500, seed: int = 20260809) -> MarketDataProvider:
    """The reference universe. Everything in this repo runs against this by default."""
    from miniftse.data.synthetic import SyntheticConfig, SyntheticUniverse

    return SyntheticUniverse(  # type: ignore[return-value]
        SyntheticConfig(n_securities=n_securities, seed=seed)
    )
