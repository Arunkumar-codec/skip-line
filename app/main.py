"""
app/main.py
───────────
FastAPI application entry point.
- Registers all routers (admin, org, user, public, api)
- Mounts static files
- Configures middleware (CORS, CSRF, session, trusted hosts)
- Manages application lifespan (DB init, admin seed)
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
from app.database import engine, Base
from app.middleware.logging import LoggingMiddleware
from app.middleware.rate_limit import RateLimitMiddleware

# ── Routers ───────────────────────────────────────────────────────────────────
from app.routers import (
    admin as admin_router,
    auth as auth_router,
    org as org_router,
    user as user_router,
    public as public_router,
    tokens as tokens_router,
    payments as payments_router,
    notifications as notifications_router,
    websocket as ws_router,
    employee as employee_router,
    views_admin,
    views_org,
    views_user,
    views_employee,
)
from app.services.admin_service import seed_default_admin
from app.services.rbac_service import seed_rbac_defaults


# ── Lifespan ─────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown logic."""
    logger.info("Starting Skip Line application…")

    # Ensure output directories exist
    for d in [settings.qr_code_dir, settings.receipt_dir]:
        Path(d).mkdir(parents=True, exist_ok=True)

    # Import all models so SQLAlchemy metadata is populated
    import models  # noqa: F401 — side-effect import

    # Create tables (idempotent; Alembic handles migrations in production)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Seed default admin if none exists
    await seed_default_admin()

    # Seed default RBAC permissions and roles
    await seed_rbac_defaults()

    # Start background WebSocket heartbeat
    from app.websocket.heartbeat import start_heartbeat, stop_heartbeat
    await start_heartbeat()

    logger.info("Skip Line is ready.")
    yield

    logger.info("Shutting down…")
    await stop_heartbeat()
    from app.services.notification_service import wait_for_background_tasks
    await wait_for_background_tasks(timeout=2.0)
    await engine.dispose()


# ── Application factory ───────────────────────────────────────────────────────
def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        description="Digital Queue Management & Virtual Token System",
        version="1.0.0",
        debug=settings.app_debug,
        docs_url="/api/docs" if not settings.is_production else None,
        redoc_url="/api/redoc" if not settings.is_production else None,
        lifespan=lifespan,
    )

    # ── Middleware (order matters — outermost applied last) ──────────────────
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=settings.allowed_hosts,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie="skipline_session",
        max_age=3600,
        https_only=settings.is_production,
        same_site="lax",
    )
    app.add_middleware(LoggingMiddleware)
    app.add_middleware(RateLimitMiddleware, max_requests=200, window_seconds=60)

    # ── Static files ─────────────────────────────────────────────────────────
    app.mount("/static", StaticFiles(directory="static"), name="static")
    app.mount("/qr_codes", StaticFiles(directory="qr_codes"), name="qr_codes")
    app.mount("/receipts", StaticFiles(directory="receipts"), name="receipts")

    # ── Routers ──────────────────────────────────────────────────────────────
    app.include_router(auth_router.router,          prefix="/auth",          tags=["Auth"])
    app.include_router(admin_router.router,         prefix="/admin",         tags=["Admin"])
    app.include_router(org_router.router,           prefix="/org",           tags=["Organization"])
    app.include_router(user_router.router,          prefix="/user",          tags=["User"])
    app.include_router(public_router.router,        prefix="/public",        tags=["Public"])
    app.include_router(tokens_router.router,        prefix="/tokens",        tags=["Tokens"])
    app.include_router(payments_router.router,      prefix="/payments",      tags=["Payments"])
    app.include_router(notifications_router.router, prefix="/notifications", tags=["Notifications"])
    app.include_router(employee_router.router,        prefix="/employee",      tags=["Employee Ops"])
    app.include_router(ws_router.router,            prefix="/ws",            tags=["WebSocket"])
    # HTML view routers
    app.include_router(views_admin.router,          prefix="/admin",         tags=["Admin UI"])
    app.include_router(views_org.router,            prefix="/org",           tags=["Org UI"])
    app.include_router(views_user.router,           prefix="/user",          tags=["User UI"])
    app.include_router(views_employee.router,       prefix="/employee",      tags=["Employee UI"])

    # ── Exception handlers ───────────────────────────────────────────────────
    @app.exception_handler(404)
    async def not_found(_: Request, exc: Exception):
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"detail": "Resource not found"},
        )

    @app.exception_handler(500)
    async def server_error(_: Request, exc: Exception):
        logger.exception("Unhandled server error")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal server error"},
        )

    @app.get("/health", tags=["Health"])
    async def health_check():
        return {"status": "ok", "app": settings.app_name}

    @app.get("/", response_class=HTMLResponse, tags=["Public"])
    async def landing_page(request: Request):
        from fastapi.templating import Jinja2Templates
        tmpl = Jinja2Templates(directory="templates")
        return tmpl.TemplateResponse("public/index.html", {"request": request})

    @app.get("/auth/logout", tags=["Auth"])
    async def logout_all():
        from fastapi.responses import RedirectResponse as RR
        resp = RR("/", status_code=302)
        for c in ("admin_token", "org_token", "user_token", "employee_token"):
            resp.delete_cookie(c)
        return resp

    return app


app = create_app()
