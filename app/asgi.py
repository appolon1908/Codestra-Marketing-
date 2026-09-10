"""Production entrypoint with a no-spend gate around the legacy planning API."""
from __future__ import annotations

import os
from uuid import uuid4
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from .auth import authenticate
from .main import app as planning_app
from .main import capabilities as planning_capabilities
from .provider_api import router

app = FastAPI(title="Codestra Marketing", version=planning_app.version,
              openapi_url=None, docs_url=None, redoc_url=None)
app.include_router(router)


@app.get("/capabilities")
def capabilities():
    return {**planning_capabilities(), "live_advertising_enabled": False, "provider_writes": False,
            "external_delivery_enabled": False, "publication_enabled": False,
            "provider_draft_writes_enabled": os.getenv("MARKETING_DRAFT_PROVIDER_WRITES_ENABLED", "false") == "true",
            "live_activation_implemented": False, "simulation_enabled": True,
            "read_only_mode": False, "business_writes_enabled": True}


@app.get("/v1/marketing/capabilities", dependencies=[Depends(authenticate)])
def authenticated_capabilities():
    return capabilities()


@app.middleware("http")
async def no_spend_headers(request: Request, call_next):
    correlation = request.headers.get("X-Correlation-ID", "")
    if not correlation or len(correlation) > 128 or any(ord(char) < 33 or ord(char) > 126 for char in correlation):
        correlation = str(uuid4())
    path = request.url.path.rstrip("/")
    if request.method == "POST" and path.startswith("/v1/marketing/campaigns/") and path.rsplit("/", 1)[-1] in {"activate", "resume"}:
        response = JSONResponse(status_code=423, content={"detail": "live_advertising_activation_not_authorized"})
    elif path.startswith("/v1/marketing/providers/"):
        response = JSONResponse(status_code=403, content={"detail": "use_tenant_bound_provider_boundary"})
    else:
        response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Correlation-ID"] = correlation
    return response


# Existing URLs remain served by the original planning application.
app.mount("/", planning_app)
