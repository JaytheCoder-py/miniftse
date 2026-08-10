# Multi-stage: the build stage carries the toolchain, the runtime stage does not.
# A smaller image is not the main reason - a runtime without a compiler and without
# build tooling is a smaller thing to have to reason about when it runs unattended.

FROM python:3.12-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /build

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

WORKDIR /app
COPY --from=builder --chown=miniftse:miniftse /build/.venv /app/.venv
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
