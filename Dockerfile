# Multi-stage: the build stage carries the toolchain, the runtime stage does not.
# A smaller image is not the main reason - a runtime without a compiler and without
# build tooling is a smaller thing to have to reason about when it runs unattended.

FROM python:3.12-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Same path the runtime stage copies the venv to (/app), not /build. `uv sync` writes
# each console script (`uvicorn`, `miniftse`) with a shebang that is the venv's own
# absolute interpreter path at creation time - `/build/.venv/bin/python` if built here
# under a /build workdir. A later `COPY --from=builder /build/.venv /app/.venv` copies
# the files but cannot rewrite that shebang, so every script would exec a python that
# only ever existed in the discarded builder stage ("exec .../bin/uvicorn: no such
# file or directory" - the runtime error for a shebang pointing nowhere). Building at
# the same absolute path the venv is later copied to is the fix; not a relocatable
# venv or a `python -m` workaround for every entry point.
WORKDIR /app

# Dependencies first, in their own layer. They change far less often than the source,
# so an ordinary code change reuses this layer instead of resolving the whole tree.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY src/ ./src/
COPY README.md ./
RUN uv sync --frozen --no-dev


FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="miniftse" \
      org.opencontainers.image.description="Rules-based equity index platform" \
      org.opencontainers.image.source="https://github.com/example/miniftse"

# Fixed so container runs are reproducible. Python randomises string hashing by
# default, which this repo has already been bitten by once.
ENV PYTHONHASHSEED=0 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

# Non-root. A production index job has no reason to run as root, and the container
# only ever reads market data and writes parquet.
RUN useradd --create-home --uid 1000 miniftse

# WORKDIR creates /app before USER switches below, so it is root-owned by default; the
# COPY --chown lines further down set ownership on what they copy in, not on /app
# itself. Without this, `build-index`'s `artefacts/manifests/` write (miniftse, not
# root) fails with a permission error the moment it tries to create a fresh directory
# under /app - found while smoke-testing this stage for Task 14, pre-existing and not
# specific to the desk stage below.
WORKDIR /app
RUN chown miniftse:miniftse /app

COPY --from=builder --chown=miniftse:miniftse /app/.venv /app/.venv
COPY --chown=miniftse:miniftse src/ /app/src/
COPY --chown=miniftse:miniftse ground_rules/ /app/ground_rules/
COPY --chown=miniftse:miniftse tests/golden/ /app/tests/golden/

USER miniftse

# Verifies the package imports and the CLI resolves. A container that starts but
# cannot import its own package fails at 6am rather than at build time.
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import miniftse.cli"]

ENTRYPOINT []
CMD ["miniftse", "build-index"]


# The ops desk. Built from `runtime` rather than from scratch - same venv, same
# non-root user, same reasoning about a slim, compiler-free image. This is the last
# stage in the file, so a plain `docker build .` (what a Hugging Face Docker Space
# runs; there is no way to hand it a `--target`) builds the desk, not the index
# builder above. CI's own `docker build` (`.github/workflows/ci.yml`, `build` job)
# passes `--target runtime` explicitly so it keeps smoke-testing the CLI image, not
# this one.
FROM runtime AS desk

# desk/data/ is the precomputed snapshot (`make desk-data`, `miniftse desk-snapshot`)
# - committed to the repo like artefacts/ (Task 14), so this is a plain COPY, not a
# build step. No secrets live here: every file is a published index figure or a
# validation-suite fixture, and there is no writable volume - the desk only ever
# reads this directory.
COPY --chown=miniftse:miniftse desk/data/ /app/desk/data/

# `/ask`'s live methodology assistant rebuilds its retrieval index from these two
# directories at every startup (`desk/state.py::_build_assistant` - the same corpus
# `desk/snapshot.py` read to score `evals.json` at build time, not a second corpus).
# `runtime` above only ever needed `ground_rules/` (already copied); `memos/` is
# desk-only, so it is copied here rather than promoted into the shared stage.
COPY --chown=miniftse:miniftse memos/ /app/memos/

# 7860 is the Hugging Face Spaces convention for a Docker Space's exposed port.
EXPOSE 7860

# Hits the app's own /healthz rather than re-checking the import - that already ran
# once at process start; what matters now is whether the ASGI server is answering.
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:7860/healthz', timeout=5)"]

# Factory mode, not a module-level `app` - `desk/app.py`'s own module docstring
# records why the module-level app the plan sketched was deliberately removed in
# Task 3; every entry point, uvicorn included, calls `create_app()` itself.
CMD ["uvicorn", "miniftse.desk.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "7860"]
