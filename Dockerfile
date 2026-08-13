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
# stage in the file, so a plain `docker build .` (what Cloud Build runs for `gcloud run
# deploy --source .`; there is no way to hand it a `--target`, and the same was true of
# the Hugging Face Space this deployment used to target) builds the desk, not the index
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

# 7860 by convention (it is what a Hugging Face Docker Space expects, and this image
# stays portable to one). Cloud Run injects its own `PORT` and routes to 8080 unless
# told otherwise, so its deploy passes `--port 7860` rather than this image growing a
# shell-form CMD just to expand `$PORT` - see `desk/README.md`.
EXPOSE 7860

# Hits the app's own /healthz rather than re-checking the import - that already ran
# once at process start; what matters now is whether the ASGI server is answering.
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:7860/healthz', timeout=5)"]

# Factory mode, not a module-level `app` - `desk/app.py`'s own module docstring
# records why the module-level app the plan sketched was deliberately removed in
# Task 3; every entry point, uvicorn included, calls `create_app()` itself.
#
# --proxy-headers --forwarded-allow-ips=*: a managed platform's router sits in front of
# this container, so every request uvicorn sees would otherwise arrive from that
# router's own address - and `desk/limits.py`'s per-IP rate limiter (`request.client.
# host`) would bucket every visitor together under it instead of limiting per visitor.
# These flags tell uvicorn to trust the router's `X-Forwarded-For` and substitute it
# for `request.client`.
#
# Trusting *every* hop (`*`) is NOT safe here, contrary to what this comment used to
# assert (DECISIONS.md D-017). The old claim - that no public ingress can set that header
# and have uvicorn believe it - holds only for a router that *replaces* `X-Forwarded-For`.
# Routers generally append, and Google Cloud appends the caller's address to whatever the
# caller already sent, while uvicorn's `_TrustedHosts.get_trusted_client_address` returns
# the *leftmost* entry once `always_trust` is set - the caller-supplied one. Measured on
# uvicorn 0.52.1: 65 requests under one forged header gave 60 served then 5 x 429, and 65
# rotating the forged header gave zero 429s. The desk is read-only, so the exposure is
# spend rather than data, and `--max-instances` on the deployment is what bounds it.
# `desk/README.md` carries the probe to run against the live service, plus the fix -
# trust the specific peer address instead of `*`, so uvicorn walks the header from the
# right and returns the first untrusted entry.
#
# Running locally (`make desk-serve`) needs neither flag: there is no proxy in front, so
# `request.client.host` is already the real peer address.
CMD ["uvicorn", "miniftse.desk.app:create_app", "--factory", "--host", "0.0.0.0", \
     "--port", "7860", "--proxy-headers", "--forwarded-allow-ips=*"]
