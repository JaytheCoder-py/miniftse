"""FastAPI wiring for the ops desk: lifespan, static/template mounting, health, and the
error handlers that keep a visitor from ever seeing a raw traceback.

No index mathematics, no validation logic and no retrieval logic lives here - every
number on every page is produced by a library call reached through `state.py` (and,
from Task 4 on, `services.py`). This module's job is HTTP, HTML and process wiring.

**Deviation from the plan's sketch.** The plan shows a module-level `app =
create_app()` for uvicorn to import directly. `desk/data/` is not committed to the
repository, so a module-level call would raise `FileNotFoundError` at import time
everywhere the module is merely imported - test collection, CI, a stray `python -c
"import miniftse.desk.app"`. Only `create_app` is exported; every entry point calls it
itself, and uvicorn is run in factory mode: `uvicorn miniftse.desk.app:create_app
--factory` (see the Makefile's `desk-serve` target).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from miniftse.desk.state import DeskState, load_desk_state

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"


def create_app(data_dir: Path = Path("desk/data")) -> FastAPI:
    """Build the ops desk application.

    Nothing is loaded from `data_dir` until the returned app's `lifespan` runs - the
    call itself is cheap, so constructing an app in a test with a `tmp_path` snapshot
    (or a `tmp_path` with no snapshot at all, to exercise the missing-snapshot path)
    costs nothing until a `TestClient` actually starts it.
    """
    data_dir = Path(data_dir)
    templates = Jinja2Templates(directory=TEMPLATES_DIR)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        desk = load_desk_state(data_dir)
        app.state.desk = desk
        # Task 13 replaces this with a real per-IP token bucket for the POST routes.
        app.state.limiter = None
        # Every template extends base.html, and base.html's footer (git sha, build
        # date) and nav need the snapshot without every route handler threading it
        # through by hand - a Jinja global, set once `desk` is known, does that.
        templates.env.globals["desk"] = desk
        yield

    app = FastAPI(title="miniftse ops desk", lifespan=lifespan)
    app.state.templates = templates
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse(url="/day")

    @app.get("/healthz")
    async def healthz(request: Request) -> dict[str, Any]:
        """Liveness plus proof of *which* snapshot is being served - the git sha the
        deployed container was built from, not just that the process answers."""
        desk: DeskState = request.app.state.desk
        return {
            "status": "ok",
            "snapshot_git_sha": desk.manifest["git_sha"],
            "loaded_at": desk.loaded_at.isoformat(),
        }

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> Response:
        """Covers 404 (no route matched) and every explicit `HTTPException` a route
        raises (the closed-set validation in later tasks: bad `date`, bad `fault_id`,
        an out-of-range `seed`) - one template, rendered inside the site layout rather
        than Starlette's bare default page."""
        return templates.TemplateResponse(
            request,
            "partials/error.html",
            {"status_code": exc.status_code, "detail": str(exc.detail)},
            status_code=exc.status_code,
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> Response:
        """Anything not already an `HTTPException`. A visitor gets the same styled
        error card as a 404, not a traceback. Starlette's `ServerErrorMiddleware` sends
        this response and then re-raises the original exception on its way out, so it
        still reaches the process's logs (and, in a test with default settings, the
        test itself) - only what the visitor sees changes."""
        return templates.TemplateResponse(
            request,
            "partials/error.html",
            {"status_code": 500, "detail": "Something went wrong on our end."},
            status_code=500,
        )

    return app
