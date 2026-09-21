"""FastAPI route composition. Oracle side effects remain in native services."""
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request
from fastapi.openapi.utils import get_openapi
from starlette.concurrency import run_in_threadpool

import server
from api_models import ApprovalInbox, Health, Session, Validation
from api_transport import dispatch

_SECURITY = {"security": [{"PrincipalBearer": []}, {"CompanySession": []}]}


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app):
        await run_in_threadpool(server.validate_startup)
        yield

    app = FastAPI(title="Oracle Patching Utility", version="0.2.0", lifespan=lifespan,
                  description="Typed overview routes. Existing patch/recovery APIs retain their controller contracts during incremental migration.",
                  docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
    router = APIRouter(prefix="/api", tags=["Controller overview"])

    @router.get("/health", response_model=Health)
    async def health(request: Request):
        return await dispatch(request, Health)

    @router.get("/session", response_model=Session, openapi_extra=_SECURITY)
    @router.get("/auth/whoami", response_model=Session, openapi_extra=_SECURITY)
    async def session(request: Request):
        return await dispatch(request, Session)

    @router.get("/validation", response_model=Validation, openapi_extra=_SECURITY)
    async def validation(request: Request):
        return await dispatch(request, Validation)

    @router.get("/approvals", response_model=ApprovalInbox, openapi_extra=_SECURITY)
    async def approvals(request: Request):
        return await dispatch(request, ApprovalInbox)

    app.include_router(router)

    def schema():
        if app.openapi_schema is None:
            result = get_openapi(title=app.title, version=app.version, description=app.description, routes=app.routes)
            result.setdefault("components", {})["securitySchemes"] = {
                "PrincipalBearer": {"type": "http", "scheme": "bearer"},
                "CompanySession": {"type": "apiKey", "in": "cookie", "name": server.company_auth.SESSION_COOKIE},
            }
            app.openapi_schema = result
        return app.openapi_schema

    app.openapi = schema

    @app.get("/api/openapi.json", include_in_schema=False)
    async def openapi(request: Request):
        # The API contract is available to authenticated readers; it carries no
        # public interactive UI or external CDN dependency in offline installs.
        return await dispatch(request, schema=app.openapi)

    @app.api_route("/{path:path}", methods=["GET", "POST"], include_in_schema=False)
    async def existing_routes(request: Request):
        return await dispatch(request)

    return app
