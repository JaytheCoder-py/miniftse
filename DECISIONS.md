# Decision log

Every judgement call, with the reasoning and the alternative that was rejected.
An index is a chain of these. In an interview you will be asked *why*, not *what*.

Format:

```
## D-NNN — <short title>
**Date:** YYYY-MM-DD · **Module:** M<n> · **Status:** accepted | superseded by D-NNN

**Context.** What forced a choice.
**Decision.** What we do.
**Alternatives rejected.** What else was on the table and why it lost.
**Consequences.** What this now commits us to, including the bad parts.
```

---

## D-001 — src-layout package, `uv` for environment management
**Date:** 2026-08-09 · **Module:** M0 (setup) · **Status:** accepted

**Context.** The repo needs to be installable and importable identically from a clean
clone, CI, and a notebook. Notebook-relative imports are the usual failure mode.

**Decision.** `src/` layout, `uv` for locking, package installed editable.

**Alternatives rejected.** Flat layout (imports resolve against the working directory,
so tests can pass locally and fail in CI); `poetry` (fine, but slower and `uv` is
becoming the default in quant shops); conda (heavyweight, poor lockfile story).

**Consequences.** Nothing is importable until `uv sync`. `uv.lock` is committed and
is the source of truth for reproducibility — a run manifest (M10) will reference it.

---

## D-002 — `mypy --strict` from day one
**Date:** 2026-08-09 · **Module:** M0 (setup) · **Status:** accepted

**Context.** Index maths is full of quantities that are all `float` to the compiler and
all different to a human: a price, a weight, a divisor, an adjustment factor, a
free-float fraction. Confusing two of them is the classic production bug.

**Decision.** Strict typing enforced from the first commit rather than retrofitted in M10.

**Alternatives rejected.** Add typing later — retrofitting `--strict` to an untyped
codebase is a multi-day slog and usually gets abandoned.

**Consequences.** Slower to write. Every public function needs annotations. This is the
point: it forces the domain model to be explicit.
