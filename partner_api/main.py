"""FastAPI wiring: the /v1 router, error format, request IDs and structured logs.

Two ways to run it:
  1. Standalone on Cloud Run (recommended): partner_api.server:app
  2. Inside the existing consumer FastAPI app: mount_partner_api(existing_app)
"""
from __future__ import annotations

import json
import logging
import secrets
import time
from contextlib import asynccontextmanager
from datetime import date

from fastapi import APIRouter, Depends, FastAPI, File, Form, Query, Request, UploadFile
from fastapi.exception_handlers import (http_exception_handler,
                                        request_validation_exception_handler)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import Settings, load_settings
from .engine import Engine, GeminiEngine, SandboxEngine
from .errors import ApiError
from .models import MenuItemIn
from .service import Ctx, PartnerService
from .store import MongoStore, Store

PREFIX = "/v1"
log = logging.getLogger("partner_api")


def build_router(service: PartnerService) -> APIRouter:
    router = APIRouter(prefix=PREFIX)

    async def ctx_dep(request: Request) -> Ctx:
        ctx = await service.authenticate(request.headers.get("authorization"))
        request.state.partner_id = ctx.partner_id
        request.state.key_mode = ctx.mode
        return ctx

    @router.post("/menu-items/analyze", summary="Nutrition for one structured menu item (cached)")
    async def analyze_menu_item(item: MenuItemIn, ctx: Ctx = Depends(ctx_dep)):
        service.require_scope(ctx, "nutrition:read")
        await service.rate_limit(ctx)
        return await service.analyze_menu_item(ctx, item)

    @router.post("/photos/analyze", summary="Nutrition estimate from a food photo (photo not stored)")
    async def analyze_photo(image: UploadFile = File(...), hint: str | None = Form(default=None),
                            ctx: Ctx = Depends(ctx_dep)):
        service.require_scope(ctx, "photos:analyze")
        await service.rate_limit(ctx)
        data = await image.read(service.settings.max_photo_bytes + 1)
        return await service.analyze_photo(ctx, data, image.content_type, hint)

    @router.get("/usage", summary="Metered usage between two UTC dates, inclusive (defaults to this month)")
    async def usage(from_: date | None = Query(default=None, alias="from"),
                    to: date | None = Query(default=None), ctx: Ctx = Depends(ctx_dep)):
        service.require_scope(ctx, "usage:read")
        return await service.usage(ctx, from_, to)

    return router


def _install_handlers(app: FastAPI) -> None:
    """Partner-format errors on /v1 only; other paths keep FastAPI's default behaviour."""

    def ours(request: Request) -> bool:
        return request.url.path.startswith(PREFIX)

    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError):
        return JSONResponse(exc.body(), status_code=exc.status, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        if not ours(request):
            return await request_validation_exception_handler(request, exc)
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", []) if p not in ("body",))
        msg = f"{where}: {first.get('msg', 'invalid input')}" if where else first.get("msg", "invalid input")
        return JSONResponse(ApiError(422, "invalid_item", msg).body(), status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        if not ours(request):
            return await http_exception_handler(request, exc)
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return JSONResponse(ApiError(exc.status_code, code, str(exc.detail)).body(), status_code=exc.status_code)

    @app.middleware("http")
    async def request_log(request: Request, call_next):
        if not ours(request):
            return await call_next(request)
        rid = request.headers.get("x-request-id") or "req_" + secrets.token_hex(8)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            log.exception("unhandled error")
            response = JSONResponse(ApiError(500, "internal", "Internal error. Quote the request id to support.").body(),
                                    status_code=500)
        response.headers["X-Request-Id"] = rid
        # One JSON line per request; Cloud Logging parses it. Never logs bodies or images.
        print(json.dumps({
            "severity": "ERROR" if response.status_code >= 500 else "INFO",
            "message": "partner_api_request", "request_id": rid, "method": request.method,
            "path": request.url.path, "status": response.status_code,
            "latency_ms": round((time.perf_counter() - start) * 1000, 1),
            "partner_id": getattr(request.state, "partner_id", None),
            "mode": getattr(request.state, "key_mode", None),
        }), flush=True)
        return response


def _default_parts(settings: Settings, store: Store | None, live_engine: Engine | None):
    store = store or MongoStore(settings.mongo_uri, settings.mongo_db)
    if live_engine is None and settings.gemini_api_key:
        live_engine = GeminiEngine(settings.gemini_api_key, settings.gemini_model, settings.ai_timeout_s)
    return store, live_engine


def mount_partner_api(app: FastAPI, settings: Settings | None = None, store: Store | None = None,
                      live_engine: Engine | None = None) -> PartnerService:
    """Add /v1 to an existing FastAPI app (e.g. the consumer backend). Call ensure_indexes at startup."""
    settings = settings or load_settings()
    store, live_engine = _default_parts(settings, store, live_engine)
    service = PartnerService(store, settings, live_engine, SandboxEngine())
    app.include_router(build_router(service))
    _install_handlers(app)
    return service


def create_app(settings: Settings | None = None, store: Store | None = None,
               live_engine: Engine | None = None) -> FastAPI:
    settings = settings or load_settings()
    store, live_engine = _default_parts(settings, store, live_engine)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await store.ensure_indexes()
        yield

    prod = settings.is_production
    app = FastAPI(
        title="Dietary Insight Partner API",
        version="1.0.0",
        lifespan=lifespan,
        docs_url=None if prod else f"{PREFIX}/docs",
        redoc_url=None,
        openapi_url=None if prod else f"{PREFIX}/openapi.json",
    )
    mount_partner_api(app, settings, store, live_engine)

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"ok": True}

    return app
