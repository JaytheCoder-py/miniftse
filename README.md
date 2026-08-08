# miniftse

A rules-based global equity index engine: security master, corporate actions,
divisor-based calculation, factor variants, a risk model, and the production
scaffolding an index provider actually needs (validation gates, golden-master
regression tests, run manifests).

Built module by module against a 12-week training plan for index research & design.

## Status

| Module | Area | State |
|---|---|---|
| M1 | Security master, corporate actions | in progress |
| M2 | Index mathematics (divisor, PR/GTR/NTR, capping) | not started |
| M3 | FTSE Russell methodology → own Ground Rules | not started |
| M5 | Cross-sectional regression, Fama-MacBeth | not started |
| M6 | Factor index construction | not started |
| M8 | Constrained optimisation | not started |
| M10 | Production engineering | not started |
| M13 | AI-enabled research workflows | not started |
| M15 | Governance, regulation, client communication | not started |

## Setup

```bash
uv sync
uv run pytest
```

## Layout

```
src/miniftse/
  secmaster/    identifiers, issuer/security/listing hierarchy, PIT mappings
  corpactions/  event model, adjustment factors, divisor impact
  universe/     eligibility screens
  weighting/    float, capping, factor tilts
  calc/         divisor, chaining, PR/GTR/NTR
  review/       periodic reconstitution
  risk/         covariance and factor risk model
  optim/        constrained optimisation
  attrib/       performance attribution
  quality/      validation and reconciliation
  agents/       LLM tooling
  research/     cross-sectional regression toolkit

ground_rules/   the published methodology document
memos/          one-page write-ups, one per module
communications/ consultation paper, client responses
tests/
```

- `notebooks/` is exploration only and is never the source of truth.
- `DECISIONS.md` records every judgement call and the alternative rejected.
