from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.errors import GatewayError
from app.mcp_server import mcp
from app.oauth import router as oauth_router
from app.security import MCPBearerAuthMiddleware
from app.upload_api import router as upload_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with mcp.session_manager.run():
        yield


settings = get_settings()
app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    lifespan=lifespan,
)


@app.exception_handler(GatewayError)
async def gateway_error_handler(request: Request, exc: GatewayError):
    return JSONResponse(
        {
            "error": exc.code,
            "message": exc.message,
            "details": exc.details,
        },
        status_code=exc.status_code,
    )


@app.get("/")
def root():
    return {
        "service": settings.app_name,
        "status": "ok",
        "mcp_url": settings.mcp_resource,
        "canonical_storage": "GitHub",
        "persistent_disk_required": False,
    }


@app.get("/health")
def health():
    return {"status": "ok"}


app.include_router(oauth_router)
app.include_router(upload_router)
app.mount("/mcp", mcp.streamable_http_app())
app.add_middleware(MCPBearerAuthMiddleware)
