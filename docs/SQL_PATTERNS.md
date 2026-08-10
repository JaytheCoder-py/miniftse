# SQL patterns for index research

Generated from `miniftse.data.store.SQL_PATTERNS`. Every query here is executed
by the test suite against the reference universe, so this file cannot go stale.

## `pit_fundamental`

```sql
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
```

## `ttm_fundamental`

```sql
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
```

## `momentum_12_1`

```sql
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
```

## `cross_sectional_decile`

```sql
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
```

## `membership_as_of`

```sql
-- Constituents on an arbitrary date, from a Type-2 dimension.
    --
    -- Half-open interval: to_date is exclusive, and NULL means still open. Getting
    -- this wrong by one day double-counts a security on every rebalance date.
    SELECT index_id, security_id, weight, from_date, to_date
    FROM index_membership
    WHERE from_date <= $as_of
      AND (to_date IS NULL OR to_date > $as_of)
```

## `membership_spells`

```sql
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
```

## `turnover_between_reviews`

```sql
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
```

## `identifier_as_of`

```sql
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
```

## `stale_price_detection`

```sql
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
```
