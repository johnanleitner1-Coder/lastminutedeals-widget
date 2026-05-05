"""
FastAPI application — AI booking widget for tour operators.

Standalone service: independent from the main LMDH API server.
Serves both web widget and WhatsApp channels.
"""

import os
import time
from collections import defaultdict
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path

from app.config import OPERATORS, WIDGET_BASE_URL
from app.routers import health, chat, checkout, webhook, whatsapp, connect

APP_DIR = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(APP_DIR / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: initialize SQLite database
    from app.services.database import init_db
    init_db()
    print("[STARTUP] SQLite database initialized")

    # Startup: pre-load product catalogs for all operators
    from app.services.availability import get_products_with_catalog
    for op_id, op in OPERATORS.items():
        try:
            products = get_products_with_catalog(op)
            cataloged = sum(1 for p in products if p.get("has_catalog_entry"))
            print(f"[STARTUP] {op.display_name}: {len(products)} OCTO products, {cataloged} with catalog content")
        except Exception as e:
            print(f"[STARTUP] Failed to load products for {op_id}: {e}")
    yield


app = FastAPI(
    title="Tour Booking Widget",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS — restrict to operator domains in production
allowed_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",")]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials="*" not in allowed_origins,  # credentials + wildcard is invalid
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


# ── Rate limiting ────────────────────────────────────────────────────────────
_rate_limit_store: dict[str, list[float]] = defaultdict(list)
_RATE_LIMIT_CHAT = 30       # messages per window
_RATE_LIMIT_SESSION = 5     # new sessions per window
_RATE_LIMIT_WINDOW = 300    # 5 minute window


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    if request.url.path in ("/api/chat", "/api/session", "/api/checkout"):
        client_ip = request.client.host if request.client else "unknown"
        key = f"{client_ip}:{request.url.path}"
        now = time.time()

        # Clean old entries for this key
        active = [t for t in _rate_limit_store[key] if now - t < _RATE_LIMIT_WINDOW]

        limit = _RATE_LIMIT_SESSION if request.url.path == "/api/session" else _RATE_LIMIT_CHAT
        if len(active) >= limit:
            _rate_limit_store[key] = active
            return JSONResponse(
                {"error": "Too many requests. Please wait a moment."},
                status_code=429,
            )
        active.append(now)
        _rate_limit_store[key] = active

        # Periodic cleanup: remove stale keys every ~100 requests
        if len(_rate_limit_store) > 100:
            stale_keys = [k for k, v in _rate_limit_store.items() if not v or now - v[-1] > _RATE_LIMIT_WINDOW]
            for k in stale_keys:
                del _rate_limit_store[k]

    return await call_next(request)


# Mount routers
app.include_router(health.router)
app.include_router(chat.router)
app.include_router(checkout.router)
app.include_router(webhook.router)
app.include_router(whatsapp.router)
app.include_router(connect.router)

# Static files (widget.js)
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")


# ── Widget JS endpoint ───────────────────────────────────────────────────────

@app.get("/widget.js")
async def serve_widget_js(request: Request, op: str = ""):
    """Serve the embeddable widget script."""
    path = APP_DIR / "static" / "widget.js"
    if not path.exists():
        return JSONResponse({"error": "Widget not built"}, status_code=404)

    content = path.read_text(encoding="utf-8")
    # Inject config
    content = content.replace("__WIDGET_API_URL__", WIDGET_BASE_URL)
    content = content.replace("__OPERATOR_ID__", op)

    from fastapi.responses import Response
    return Response(content=content, media_type="application/javascript")


# ── Booking confirmation page (mobile Stripe redirect) ───────────────────────

@app.get("/booking/confirmed")
async def booking_confirmed(request: Request, session_id: str = "", token: str = ""):
    return TEMPLATES.TemplateResponse("confirmed.html", {
        "request": request,
        "session_id": session_id,
        "token": token,
    })


@app.get("/booking/cancelled")
async def booking_cancelled(request: Request):
    return TEMPLATES.TemplateResponse("confirmed.html", {
        "request": request,
        "session_id": "",
        "cancelled": True,
    })


# ── Demo page ─────────────────────────────────────────────────────────────────

@app.get("/demo")
async def demo(request: Request):
    return TEMPLATES.TemplateResponse("demo.html", {"request": request})


# ── Privacy policy ────────────────────────────────────────────────────────────

@app.get("/privacy")
async def privacy(request: Request):
    return TEMPLATES.TemplateResponse("privacy.html", {"request": request})


# ── Dashboard (imported separately to keep this file clean) ──────────────────

try:
    from app.routers.dashboard import router as dashboard_router
    app.include_router(dashboard_router)
except ImportError:
    pass
