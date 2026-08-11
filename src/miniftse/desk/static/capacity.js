// miniftse ops desk - Capacity tab: the fund-size slider.
//
// This is the ONE place in the ops desk where index-adjacent arithmetic runs outside
// the library, and it is deliberate, not an oversight: `docs/superpowers/plans/
// 2026-08-11-capacity-viz.md`, "## Task 11: capacity.js" ports
// `weighting.schemes.capacity_constrained_weights` / `weighted_average_days_to_trade`
// (src/miniftse/weighting/schemes.py:184-253) to JavaScript for exactly this reason -
// a visitor dragging a slider needs the days-to-trade capacity trim recomputed on
// every input event, and there is no way to serve that live from the server without a
// network round trip per pixel of drag. `capacityConstrainedWeights` and
// `weightedAverageDaysToTrade` below are a direct, line-for-line port of those two
// functions - not a reinvention - so if the two ever disagree, this file is the one
// that is wrong.
//
// Everything else on `/index` (stat tiles, the level-history chart, the constituents
// table, the six-scheme trade-off table, the risk/attribution one-pagers) is rendered
// straight from a `desk/snapshot.py` payload with no arithmetic in between; see
// `app.py`'s `/index` route docstring.

/**
 * Trim weights an investable fund of `fundSize` could not build within
 * `opts.maxDaysToTrade` days at `opts.participation` of ADV, redistributing the
 * trimmed weight pro-rata across names with headroom. Direct port of
 * `capacity_constrained_weights`.
 *
 * @param {{security_id: string, weight: number, adv: number}[]} constituents
 * @param {number} fundSize
 * @param {{maxDaysToTrade?: number, participation?: number}} [opts]
 * @returns {{weights: Record<string, number>, trimmed: string[]}}
 */
function capacityConstrainedWeights(constituents, fundSize, opts = {}) {
  const { maxDaysToTrade = 5.0, participation = 0.20 } = opts;

  const base = {};
  const adv = {};
  for (const c of constituents) {
    base[c.security_id] = c.weight;
    adv[c.security_id] = c.adv;
  }

  if (fundSize <= 0) {
    return { weights: { ...base }, trimmed: [] };
  }

  const ceilings = {};
  for (const id in base) {
    ceilings[id] = adv[id] > 0
      ? (adv[id] * participation * maxDaysToTrade) / fundSize
      : 0.0;
  }

  const w = { ...base };
  const frozen = new Set();
  for (let iter = 0; iter < 100; iter++) {
    const breaching = Object.keys(w).filter((k) => !frozen.has(k) && w[k] > ceilings[k]);
    if (breaching.length === 0) break;
    for (const k of breaching) {
      w[k] = ceilings[k];
      frozen.add(k);
    }
    const residual = 1.0 - [...frozen].reduce((s, k) => s + w[k], 0);
    const free = Object.keys(w).filter((k) => !frozen.has(k));
    const freeMass = free.reduce((s, k) => s + w[k], 0);
    if (freeMass <= 0 || residual <= 0) break;
    for (const k of free) {
      w[k] *= residual / freeMass;
    }
  }

  const total = Object.values(w).reduce((s, v) => s + v, 0);
  const normalised = {};
  for (const k in w) normalised[k] = total > 0 ? w[k] / total : 0;
  return { weights: normalised, trimmed: [...frozen] };
}

/**
 * The single weighted-average days-to-trade figure for a weight set. Direct port of
 * `weighted_average_days_to_trade`. Zero-ADV or zero-weight names are excluded from
 * both the numerator and the denominator, matching the Python `finite` filter.
 *
 * @param {Record<string, number>} weights
 * @param {{security_id: string, adv: number}[]} constituents
 * @param {number} fundSize
 * @param {number} [participation]
 * @returns {number} days, or Infinity if nothing in the set has finite days-to-trade.
 */
function weightedAverageDaysToTrade(weights, constituents, fundSize, participation = 0.20) {
  let numerator = 0;
  let denominator = 0;
  for (const c of constituents) {
    const w = weights[c.security_id] ?? 0;
    if (c.adv > 0 && w > 0) {
      const dtt = (fundSize * w) / (c.adv * participation);
      numerator += w * dtt;
      denominator += w;
    }
  }
  return denominator > 0 ? numerator / denominator : Infinity;
}

/**
 * `fmtMoney`/`fundSizeFromSlider`: the same log-scale slider mapping the capacity-viz
 * plan's Task 12 (`render_capacity.js`) describes - fund sizes from $10m to $10bn,
 * because capacity questions span orders of magnitude and a linear slider would waste
 * nearly all of its range below $1bn.
 *
 * The exponent step here is `7 + sliderValue`, matching the plan's formula exactly;
 * the slider's own `max` (in `index_tab.html`, `<input ... max="3">`) is what bounds
 * it to $10bn, not this function - the plan's sample markup paired this formula with
 * `max="9"` in its slider element, which the comment above the formula says reaches
 * "$10bn" but the arithmetic actually reaches 10**16 ($10 quadrillion), a mismatch
 * between that comment and that markup discovered while browser-testing this port.
 * `max="3"` is the value that makes `10 ** (7 + sliderValue)` actually span $10m..$10bn,
 * as intended.
 */
function fundSizeFromSlider(sliderValue) {
  return 10 ** (7 + Number(sliderValue));
}

function fmtMoney(value) {
  if (value >= 1e9) return `$${(value / 1e9).toFixed(1)}bn`;
  if (value >= 1e6) return `$${(value / 1e6).toFixed(0)}m`;
  return `$${value.toFixed(0)}`;
}

/**
 * Wires up the Capacity tab's fund-size slider: reads the constituents/capacity_params
 * `/index` embedded in `#capacity-constituents-data` (a
 * `<script type="application/json">` tag `app.py`'s `/index` route wrote via
 * `_embed_json`), and on every slider `input` event recomputes and writes the two live
 * table cells `#capacity-trimmed` and `#capacity-avg-days`. A no-op if the page has no
 * slider (e.g. this script loaded on a different page).
 */
function initCapacitySlider() {
  const slider = document.getElementById("fund-size-slider");
  const dataEl = document.getElementById("capacity-constituents-data");
  if (!slider || !dataEl) return;

  const { constituents, capacity_params: capacityParams } = JSON.parse(dataEl.textContent);
  const label = document.getElementById("fund-size-label");
  const trimmedEl = document.getElementById("capacity-trimmed");
  const daysEl = document.getElementById("capacity-avg-days");

  function update() {
    const fundSize = fundSizeFromSlider(slider.value);
    if (label) label.textContent = fmtMoney(fundSize);

    const { weights, trimmed } = capacityConstrainedWeights(constituents, fundSize, {
      participation: capacityParams.participation,
      maxDaysToTrade: capacityParams.max_days_to_trade,
    });
    if (trimmedEl) trimmedEl.textContent = String(trimmed.length);

    const avgDays = weightedAverageDaysToTrade(
      weights, constituents, fundSize, capacityParams.participation
    );
    if (daysEl) daysEl.textContent = Number.isFinite(avgDays) ? avgDays.toFixed(1) : "∞";
  }

  slider.addEventListener("input", update);
  update();
}

document.addEventListener("DOMContentLoaded", initCapacitySlider);
