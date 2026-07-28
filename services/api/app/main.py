"""Create the read-only FastAPI application and its HTTP routes."""

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Literal

import asyncpg
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import Settings
from .database import Database, DatabaseUnavailableError
from .models import (
    FindingDetail,
    FindingPage,
    HealthResponse,
    OpsSummary,
    PackageHistory,
    StatsResponse,
    TraceSummary,
)
from .repository import PostgresReadRepository, ReadRepository


def create_app(
    repository: ReadRepository | None = None, settings: Settings | None = None
) -> FastAPI:
    """Create the API with production or injected read dependencies.

    Args:
        repository: Optional repository override used by tests and local callers.
        settings: Optional validated settings override.

    Returns:
        A configured FastAPI application.

    """
    settings = settings or Settings.from_env()
    if repository is None:
        database = Database(settings.database_url)
        repo: ReadRepository = PostgresReadRepository(
            database,
            settings.grafana_base_url,
            settings.source_mode,
        )
    else:
        database = None
        repo = repository

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if database is not None:
            await database.connect()
        yield
        if database is not None:
            await database.close()

    app = FastAPI(title=settings.api_title, version="0.1.0", lifespan=lifespan)
    app.state.repository = repo
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["GET"],
        allow_headers=["Accept", "Content-Type"],
    )

    @app.exception_handler(asyncpg.PostgresError)
    @app.exception_handler(OSError)
    @app.exception_handler(DatabaseUnavailableError)
    async def database_unavailable(_: Request, __: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": "database unavailable"},
        )

    def get_repository(request: Request) -> ReadRepository:
        return request.app.state.repository

    @app.get("/api/health", response_model=HealthResponse, tags=["operations"])
    async def health(
        response: Response,
        store: Annotated[ReadRepository, Depends(get_repository)],
    ) -> HealthResponse:
        ready = await store.ready()
        if not ready:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthResponse(
            status="healthy" if ready else "degraded",
            database="ready" if ready else "unavailable",
        )

    @app.get("/api/findings", response_model=FindingPage, tags=["findings"])
    async def findings(
        store: Annotated[ReadRepository, Depends(get_repository)],
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 25,
        package: Annotated[str | None, Query(max_length=200)] = None,
        change_type: Annotated[str | None, Query(max_length=100)] = None,
        processing_priority: Annotated[str | None, Query(max_length=50)] = None,
        min_confidence: Annotated[float | None, Query(ge=0, le=1)] = None,
        disposition: Annotated[str | None, Query(max_length=50)] = None,
        published_from: datetime | None = None,
        published_to: datetime | None = None,
        sort_by: Literal["event_time"] = "event_time",
        sort_direction: Literal["asc", "desc"] = "desc",
        publishable: bool = True,
        include_insufficient: bool = False,
    ) -> FindingPage:
        return FindingPage.model_validate(
            await store.list_findings(
                page=page,
                page_size=page_size,
                package=package,
                change_type=change_type,
                processing_priority=processing_priority,
                min_confidence=min_confidence,
                disposition=disposition,
                published_from=published_from,
                published_to=published_to,
                sort_by=sort_by,
                sort_direction=sort_direction,
                publishable=publishable,
                include_insufficient=include_insufficient,
            )
        )

    @app.get("/api/findings/{finding_id}", response_model=FindingDetail, tags=["findings"])
    async def finding_detail(
        finding_id: str,
        store: Annotated[ReadRepository, Depends(get_repository)],
    ) -> FindingDetail:
        value = await store.get_finding(finding_id)
        if value is None:
            raise HTTPException(status_code=404, detail="finding not found")
        return FindingDetail.model_validate(value)

    @app.get(
        "/api/packages/{normalized_name}",
        response_model=PackageHistory,
        tags=["packages"],
    )
    async def package_history(
        normalized_name: str,
        store: Annotated[ReadRepository, Depends(get_repository)],
    ) -> PackageHistory:
        normalized_name = re.sub(r"[-_.]+", "-", normalized_name).lower()
        value = await store.get_package(normalized_name)
        if value is None:
            raise HTTPException(status_code=404, detail="package not found")
        return PackageHistory.model_validate(value)

    @app.get("/api/stats", response_model=StatsResponse, tags=["operations"])
    async def stats(
        store: Annotated[ReadRepository, Depends(get_repository)],
    ) -> StatsResponse:
        return StatsResponse.model_validate(await store.get_stats())

    @app.get("/api/ops/summary", response_model=OpsSummary, tags=["operations"])
    async def ops_summary(
        store: Annotated[ReadRepository, Depends(get_repository)],
    ) -> OpsSummary:
        return OpsSummary.model_validate(await store.get_ops_summary())

    @app.get(
        "/api/findings/{finding_id}/trace-summary",
        response_model=TraceSummary,
        tags=["findings"],
    )
    async def trace_summary(
        finding_id: str,
        store: Annotated[ReadRepository, Depends(get_repository)],
    ) -> TraceSummary:
        value = await store.get_trace_summary(finding_id)
        if value is None:
            raise HTTPException(status_code=404, detail="finding not found")
        return TraceSummary.model_validate(value)

    return app


app = create_app()
