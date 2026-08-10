"""DuckDB point-in-time store, and the SQL patterns that matter in this domain.

Two jobs. First, hold the universe in a columnar store so the research layer can ask
panel questions without materialising the whole thing in pandas. Second, be the
reference implementation of the query patterns a quant at an index provider writes
constantly and a pandas-only quant usually cannot:

* **as-of / bitemporal joins** - "what did we know on D about period P"
* **Type-2 slowly changing dimensions** - index membership and identifier mappings
* **gaps and islands** - continuous membership spells
* **cross-sectional ranking within date and within industry**
* **turnover between two rebalance dates as a single query**

Every query in `SQL_PATTERNS` is executable and covered by a test. They are also the
Module 4 deliverable, kept here rather than in a markdown file so they cannot rot.
"""

from __future__ import annotations

import contextlib
import datetime as dt
from pathlib import Path
from typing import Any, Final

import duckdb
import pandas as pd

from miniftse.data.synthetic import SyntheticUniverse

# --------------------------------------------------------------------------------------
# The cookbook
# --------------------------------------------------------------------------------------

SQL_PATTERNS: Final[dict[str, str]] = {}


def _pattern(name: str, sql: str) -> str:
    SQL_PATTERNS[name] = sql
    return sql


PIT_FUNDAMENTAL = _pattern(
    "pit_fundamental",
    """
    -- Most recent filing per (security, item) that was KNOWN on :as_of.
    --
    -- The whole point is the second predicate. Dropping `filed_date <= :as_of` still
    -- returns a plausible-looking answer -- it just quietly uses restatements that had
    -- not happened yet. That is the single most common look-ahead bug in equity
    -- research, and it inflates every value factor built on book equity.
    SELECT security_id, item, period_end, filed_date, value, currency
    FROM (
        SELECT *,
               ROW_NUMBER() OVER (
                   PARTITION BY security_id, item
                   ORDER BY period_end DESC, filed_date DESC
               ) AS rn
        FROM fundamentals
        WHERE filed_date <= $as_of
          AND period_end  >= $as_of - INTERVAL '550 days'
          AND item = ANY($items)
    )
    WHERE rn = 1
    """,
)

TTM_FUNDAMENTAL = _pattern(
    "ttm_fundamental",
    """
    -- Trailing twelve months of a flow item, point-in-time correct.
    --
    -- Two-stage: first collapse each period to the latest filing known on :as_of,
    -- then sum the last four such periods. Summing raw rows would double-count any
    -- period that has been restated.
    WITH latest_per_period AS (
        SELECT security_id, period_end, value,
               ROW_NUMBER() OVER (
                   PARTITION BY security_id, period_end
                   ORDER BY filed_date DESC
               ) AS rn
        FROM fundamentals
        WHERE filed_date <= $as_of AND item = $item
    ),
    ranked AS (
        SELECT security_id, period_end, value,
               ROW_NUMBER() OVER (
                   PARTITION BY security_id ORDER BY period_end DESC
               ) AS period_rank
        FROM latest_per_period
        WHERE rn = 1
    )
    SELECT security_id,
           SUM(value)        AS ttm_value,
           COUNT(*)          AS n_periods,
           MAX(period_end)   AS latest_period
    FROM ranked
    WHERE period_rank <= 4
    GROUP BY security_id
    HAVING COUNT(*) = 4
    """,
)

MOMENTUM_12_1 = _pattern(
    "momentum_12_1",
    """
    -- 12-1 momentum at every month end, in one pass.
    --
    -- The one-month skip is not decoration: including the most recent month mixes in
    -- short-term reversal, which has the opposite sign and eats the signal.
    WITH month_ends AS (
        SELECT security_id, date, close,
               ROW_NUMBER() OVER (
                   PARTITION BY security_id, date_trunc('month', date)
                   ORDER BY date DESC
               ) AS rn
        FROM prices
        WHERE NOT is_suspended
    ),
    monthly AS (
        SELECT security_id, date, close FROM month_ends WHERE rn = 1
    )
    SELECT security_id,
           date AS as_of,
           close,
           LAG(close, 1)  OVER w AS close_lag_1m,
           LAG(close, 12) OVER w AS close_lag_12m,
           LAG(close, 1)  OVER w / NULLIF(LAG(close, 12) OVER w, 0) - 1 AS mom_12_1
    FROM monthly
    WINDOW w AS (PARTITION BY security_id ORDER BY date)
    QUALIFY close_lag_12m IS NOT NULL
    """,
)

CROSS_SECTIONAL_DECILE = _pattern(
    "cross_sectional_decile",
    """
    -- Decile rank of a signal, both unconditionally within date and within industry.
    --
    -- The industry-relative version is what a neutralised factor needs. Ranking
    -- globally and then wondering why the value portfolio is 40% financials is a
    -- rite of passage.
    SELECT
        s.as_of,
        s.security_id,
        c.icb_industry,
        s.signal,
        NTILE(10) OVER (PARTITION BY s.as_of ORDER BY s.signal)                  AS decile_all,
        NTILE(10) OVER (PARTITION BY s.as_of, c.icb_industry ORDER BY s.signal)  AS decile_industry,
        PERCENT_RANK() OVER (PARTITION BY s.as_of ORDER BY s.signal)             AS pctile_all
    FROM signals s
    JOIN classifications c USING (security_id)
    WHERE s.signal IS NOT NULL
    """,
)

MEMBERSHIP_AS_OF = _pattern(
    "membership_as_of",
    """
    -- Constituents on an arbitrary date, from a Type-2 dimension.
    --
    -- Half-open interval: to_date is exclusive, and NULL means still open. Getting
    -- this wrong by one day double-counts a security on every rebalance date.
    SELECT index_id, security_id, weight, from_date, to_date
    FROM index_membership
    WHERE from_date <= $as_of
      AND (to_date IS NULL OR to_date > $as_of)
    """,
)

MEMBERSHIP_SPELLS = _pattern(
    "membership_spells",
    """
    -- Gaps and islands: every continuous spell a security spent in the index.
    --
    -- The trick is the difference between a dense row number and a per-security row
    -- number over ordered dates: it is constant exactly within an unbroken run.
    WITH dated AS (
        SELECT DISTINCT index_id, security_id, as_of_date
        FROM membership_daily
    ),
    marked AS (
        SELECT *,
               ROW_NUMBER() OVER (PARTITION BY index_id, security_id ORDER BY as_of_date)
                 AS seq,
               DENSE_RANK() OVER (PARTITION BY index_id ORDER BY as_of_date)
                 AS date_rank
        FROM dated
    )
    SELECT index_id, security_id,
           MIN(as_of_date) AS spell_start,
           MAX(as_of_date) AS spell_end,
           COUNT(*)        AS days_in_spell
    FROM marked
    GROUP BY index_id, security_id, date_rank - seq
    ORDER BY security_id, spell_start
    """,
)

TURNOVER_BETWEEN_REVIEWS = _pattern(
    "turnover_between_reviews",
    """
    -- One-way turnover between two rebalance dates, in a single query.
    --
    -- FULL OUTER JOIN, not INNER: a name that entered has no prior weight and a name
    -- that left has no current weight, and both contribute their full weight to
    -- turnover. An inner join silently reports only the drift on survivors, which is
    -- the smaller and less interesting half.
    WITH before AS (
        SELECT security_id, weight FROM index_membership
        WHERE index_id = $index_id AND from_date <= $d0
          AND (to_date IS NULL OR to_date > $d0)
    ),
    after AS (
        SELECT security_id, weight FROM index_membership
        WHERE index_id = $index_id AND from_date <= $d1
          AND (to_date IS NULL OR to_date > $d1)
    )
    SELECT
        SUM(ABS(COALESCE(a.weight, 0) - COALESCE(b.weight, 0))) / 2.0 AS one_way_turnover,
        SUM(CASE WHEN b.security_id IS NULL THEN a.weight ELSE 0 END) AS additions_weight,
        SUM(CASE WHEN a.security_id IS NULL THEN b.weight ELSE 0 END) AS deletions_weight,
        COUNT(*) FILTER (WHERE b.security_id IS NULL)                 AS n_additions,
        COUNT(*) FILTER (WHERE a.security_id IS NULL)                 AS n_deletions
    FROM after a
    FULL OUTER JOIN before b USING (security_id)
    """,
)

IDENTIFIER_AS_OF = _pattern(
    "identifier_as_of",
    """
    -- Resolve an identifier to a security AS IT STOOD on a date.
    --
    -- Tickers are recycled. Resolving one without a date bound is how a backtest ends
    -- up holding the wrong company for the first three years of its history.
    SELECT m.security_id, m.identifier_type, m.identifier_value,
           m.valid_from, m.valid_to
    FROM identifier_map m
    WHERE m.identifier_type = $id_type
      AND m.identifier_value = $id_value
      AND m.valid_from <= $as_of
      AND (m.valid_to IS NULL OR m.valid_to > $as_of)
    """,
)

STALE_PRICE_DETECTION = _pattern(
    "stale_price_detection",
    """
    -- Prices unchanged for N consecutive sessions: a stale feed, a suspension, or a
    -- genuinely untraded microcap. The quality layer needs to tell them apart, so the
    -- query reports the run length rather than a boolean.
    WITH changes AS (
        SELECT security_id, date, close,
               CASE WHEN close = LAG(close) OVER w THEN 0 ELSE 1 END AS changed
        FROM prices
        WHERE NOT is_suspended
        WINDOW w AS (PARTITION BY security_id ORDER BY date)
    ),
    runs AS (
        SELECT *, SUM(changed) OVER (
            PARTITION BY security_id ORDER BY date
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ) AS run_id
        FROM changes
    )
    SELECT security_id, MIN(date) AS run_start, MAX(date) AS run_end,
           COUNT(*) AS consecutive_sessions, ANY_VALUE(close) AS stale_price
    FROM runs
    GROUP BY security_id, run_id
    HAVING COUNT(*) >= $min_run
    ORDER BY consecutive_sessions DESC
    """,
)


# --------------------------------------------------------------------------------------
# The store
# --------------------------------------------------------------------------------------


class PitStore:
    """A DuckDB database over the universe tables, with as-of query helpers."""

    def __init__(self, path: Path | str | None = None) -> None:
        """`path=None` gives an in-memory database, which is what tests use."""
        self.path = str(path) if path is not None else ":memory:"
        self.con = duckdb.connect(self.path)
        self.con.execute("SET TimeZone='UTC'")

    # ---------------------------------------------------------------- loading

    def load_universe(self, universe: SyntheticUniverse) -> None:
        """Register every table from a generated universe."""
        tables = {
            "prices": universe._generated["prices"],
            "shares": universe._generated["shares"],
            "corp_actions": universe._generated["corp_actions"],
            "fundamentals": universe._fundamentals,
            "fx": universe._fx,
            "securities": universe.get_securities(),
            "listings": universe.get_listings(),
            "classifications": universe.get_classifications(None, universe.config.end),
        }
        for name, df in tables.items():
            self.register(name, df)
        self._build_identifier_map(universe)

    def register(self, name: str, df: pd.DataFrame) -> None:
        """Replace a table with the contents of a DataFrame."""
        self.con.register(f"_tmp_{name}", df)
        self.con.execute(f"CREATE OR REPLACE TABLE {name} AS SELECT * FROM _tmp_{name}")
        self.con.unregister(f"_tmp_{name}")

    def _build_identifier_map(self, universe: SyntheticUniverse) -> None:
        """Unpivot the wide identifier frame into the long, bitemporal shape a real
        master uses: one row per (security, identifier type, validity interval)."""
        wide = universe.get_identifier_map()
        frames = []
        for id_type in ("isin", "sedol", "ticker"):
            part = wide[["security_id", "valid_from", "valid_to"]].copy()
            part["identifier_type"] = id_type
            part["identifier_value"] = wide[id_type]
            frames.append(part)
        table = pd.concat(frames, ignore_index=True)
        # `valid_to` is NULL for every currently-open mapping, and an all-None column
        # reaches DuckDB as INTEGER. Every as-of query then fails on
        # "Cannot compare INTEGER and DATE" - and it fails precisely on the open
        # intervals, which are the normal case.
        for column in ("valid_from", "valid_to"):
            table[column] = pd.to_datetime(table[column], errors="coerce")
        self.register("identifier_map", table)

    def index_tables(self) -> None:
        """Indexes on the columns every query filters on. DuckDB is columnar and often
        fine without them, but the join keys still benefit and the habit is correct."""
        for stmt in (
            "CREATE INDEX IF NOT EXISTS ix_px ON prices(security_id, date)",
            "CREATE INDEX IF NOT EXISTS ix_fund ON fundamentals(security_id, item, filed_date)",
            "CREATE INDEX IF NOT EXISTS ix_sh ON shares(security_id, knowledge_date)",
            "CREATE INDEX IF NOT EXISTS ix_ca ON corp_actions(security_id, ex_date)",
        ):
            # Index already present, or unsupported on a view. Neither is a problem:
            # DuckDB is columnar and performs well without these.
            with contextlib.suppress(duckdb.Error):
                self.con.execute(stmt)

    # ---------------------------------------------------------------- queries

    def sql(self, query: str, **params: Any) -> pd.DataFrame:
        return self.con.execute(query, params).df() if params else self.con.execute(query).df()

    def pit_fundamentals(self, as_of: dt.date, items: list[str]) -> pd.DataFrame:
        return self.sql(PIT_FUNDAMENTAL, as_of=as_of, items=items)

    def ttm(self, as_of: dt.date, item: str) -> pd.DataFrame:
        return self.sql(TTM_FUNDAMENTAL, as_of=as_of, item=item)

    def naive_fundamentals(self, as_of: dt.date, items: list[str]) -> pd.DataFrame:
        """The *wrong* query, kept deliberately.

        Identical to `pit_fundamentals` but without the `filed_date <= as_of` guard.
        It exists so the test suite can assert that the two disagree on a known
        restatement: a look-ahead guard nobody has ever seen fail is not evidence of
        anything.
        """
        return self.sql(
            """
            SELECT security_id, item, period_end, filed_date, value, currency
            FROM (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY security_id, item
                    ORDER BY period_end DESC, filed_date DESC) AS rn
                FROM fundamentals
                WHERE period_end <= $as_of AND item = ANY($items)
            ) WHERE rn = 1
            """,
            as_of=as_of,
            items=items,
        )

    def momentum(self) -> pd.DataFrame:
        return self.sql(MOMENTUM_12_1)

    def membership_as_of(self, as_of: dt.date) -> pd.DataFrame:
        return self.sql(MEMBERSHIP_AS_OF, as_of=as_of)

    def membership_spells(self) -> pd.DataFrame:
        return self.sql(MEMBERSHIP_SPELLS)

    def turnover(self, index_id: str, d0: dt.date, d1: dt.date) -> pd.DataFrame:
        return self.sql(TURNOVER_BETWEEN_REVIEWS, index_id=index_id, d0=d0, d1=d1)

    def resolve_identifier(self, id_type: str, value: str, as_of: dt.date) -> pd.DataFrame:
        return self.sql(IDENTIFIER_AS_OF, id_type=id_type, id_value=value, as_of=as_of)

    def stale_prices(self, min_run: int = 3) -> pd.DataFrame:
        return self.sql(STALE_PRICE_DETECTION, min_run=min_run)

    def prices_wide(self, start: dt.date, end: dt.date, field: str = "close") -> pd.DataFrame:
        """Pivot to a date x security matrix. Only at the point of use - the store keeps
        everything long, because the panel is genuinely ragged."""
        long = self.sql(
            f"SELECT date, security_id, {field} AS v FROM prices "  # noqa: S608 - field is ours
            "WHERE date BETWEEN $start AND $end AND NOT is_suspended",
            start=start,
            end=end,
        )
        return long.pivot(index="date", columns="security_id", values="v").sort_index()

    def table_names(self) -> list[str]:
        return [r[0] for r in self.con.execute("SHOW TABLES").fetchall()]

    def row_counts(self) -> dict[str, int]:
        return {
            t: int(self.con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])  # type: ignore[index]  # noqa: E501,S608
            for t in self.table_names()
        }

    def close(self) -> None:
        self.con.close()

    def __enter__(self) -> PitStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def write_sql_cookbook(dest: Path) -> Path:
    """Emit SQL_PATTERNS as a markdown cookbook.

    Generated rather than hand-maintained so the document and the tested queries cannot
    drift apart - the usual fate of a SQL_PATTERNS.md written once and never re-run.
    """
    lines = [
        "# SQL patterns for index research",
        "",
        "Generated from `miniftse.data.store.SQL_PATTERNS`. Every query here is executed",
        "by the test suite against the reference universe, so this file cannot go stale.",
        "",
    ]
    for name, sql in SQL_PATTERNS.items():
        lines += [f"## `{name}`", "", "```sql", sql.strip(), "```", ""]
    dest.write_text("\n".join(lines), encoding="utf-8")
    return dest
