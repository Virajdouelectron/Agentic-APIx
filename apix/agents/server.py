"""
FastAPI application serving the Airfare Price Index (APIx) API.

Endpoints:
    GET /health              — System health check with database connectivity
    GET /index               — Query APIx time series (daily/weekly/monthly)
    GET /index/routes/{route} — Route-specific index values
    GET /elasticity          — Lead-time elasticity curves
    GET /docs                — Auto-generated OpenAPI documentation (Swagger)
    GET /redoc               — ReDoc alternative documentation

All query endpoints target < 500ms response time (R5.4) via:
- Async SQLAlchemy sessions (non-blocking IO)
- Database-level indexes on (frequency, observation_date, route)
- Pagination defaults to prevent unbounded result sets
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from apix import __version__
from apix.models.database import close_db, get_db, init_db
from apix.models.db_models import IndexRecord, FareRecord
from apix.models.schemas import (
    ElasticityPoint,
    HealthResponse,
    IndexFrequency,
    IndexPoint,
)

logger = logging.getLogger(__name__)


# ── Lifespan ─────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage async resource lifecycle: DB init on startup, cleanup on shutdown."""
    logger.info("APIx server starting — initializing database tables")
    await init_db()
    yield
    logger.info("APIx server shutting down — disposing database connections")
    await close_db()


# ── App ──────────────────────────────────────────────────────────

app = FastAPI(
    title="APIx — Airfare Price Index",
    description=(
        "A DGCA-weighted economic price index for Indian domestic airfares. "
        "Serves daily, weekly, and monthly index values alongside lead-time "
        "elasticity curves for institutional consumers (NSO, RBI)."
    ),
    version=__version__,
    lifespan=lifespan,
)

# Allow dashboard and other frontends to consume the API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Response Models ──────────────────────────────────────────────

class IndexResponse(BaseModel):
    """Paginated list of index observations."""
    count: int = Field(description="Number of records returned")
    base_period: str = Field(description="Reference base period for normalization")
    data: List[IndexPoint] = Field(description="Index observation points")


class ElasticityResponse(BaseModel):
    """Lead-time elasticity curves grouped by route and bucket."""
    count: int = Field(description="Number of elasticity points returned")
    data: List[ElasticityPoint] = Field(description="Elasticity data points")


class ErrorResponse(BaseModel):
    """Standard error response envelope."""
    detail: str


# ── Health Endpoint ──────────────────────────────────────────────

@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["System"],
    summary="Health check with database connectivity",
)
async def health_check(db: AsyncSession = Depends(get_db)) -> HealthResponse:
    """
    Returns system health status including database connectivity.
    Used by load balancers and monitoring systems.
    """
    db_connected = False
    db_details = {}

    try:
        result = await db.execute(text("SELECT 1"))
        row = result.scalar()
        db_connected = row == 1
        db_details["ping"] = "ok"
    except Exception as exc:
        db_details["ping"] = "failed"
        db_details["error"] = str(exc)
        logger.warning("Health check DB ping failed: %s", exc)

    return HealthResponse(
        status="ok" if db_connected else "degraded",
        timestamp=datetime.now(timezone.utc),
        version=__version__,
        database_connected=db_connected,
        details=db_details,
    )


# ── Index Endpoints ──────────────────────────────────────────────

@app.get(
    "/index",
    response_model=IndexResponse,
    tags=["Index"],
    summary="Query APIx time series",
    responses={400: {"model": ErrorResponse}},
)
async def get_index(
    freq: IndexFrequency = Query(
        IndexFrequency.DAILY,
        description="Aggregation frequency: daily, weekly, or monthly",
    ),
    from_date: Optional[date] = Query(
        None,
        alias="from",
        description="Start date (inclusive), format: YYYY-MM-DD",
    ),
    to_date: Optional[date] = Query(
        None,
        alias="to",
        description="End date (inclusive), format: YYYY-MM-DD",
    ),
    limit: int = Query(
        365,
        ge=1,
        le=3650,
        description="Maximum number of records to return",
    ),
    db: AsyncSession = Depends(get_db),
) -> IndexResponse:
    """
    Retrieve APIx index values filtered by frequency and date range.

    - **freq**: `daily`, `weekly`, or `monthly`
    - **from / to**: Optional date range filter
    - **limit**: Max records (default 365, max 3650)

    Returns index values normalized to base period = 100.
    """
    stmt = (
        select(IndexRecord)
        .where(IndexRecord.frequency == freq)
        .order_by(IndexRecord.observation_date.desc())
        .limit(limit)
    )

    if from_date:
        stmt = stmt.where(IndexRecord.observation_date >= from_date)
    if to_date:
        stmt = stmt.where(IndexRecord.observation_date <= to_date)

    result = await db.execute(stmt)
    records = result.scalars().all()

    data = [
        IndexPoint(
            observation_date=r.observation_date,
            frequency=r.frequency,
            route=r.route,
            index_value=float(r.index_value),
            base_period=r.base_period,
            passenger_weight_version=r.passenger_weight_version,
            sample_size=r.sample_size,
        )
        for r in records
    ]

    # Determine base_period from first record or default
    bp = data[0].base_period if data else "N/A"

    return IndexResponse(count=len(data), base_period=bp, data=data)


@app.get(
    "/index/routes/{route}",
    response_model=IndexResponse,
    tags=["Index"],
    summary="Route-specific index values",
    responses={404: {"model": ErrorResponse}},
)
async def get_index_by_route(
    route: str,
    freq: IndexFrequency = Query(
        IndexFrequency.DAILY,
        description="Aggregation frequency",
    ),
    from_date: Optional[date] = Query(None, alias="from"),
    to_date: Optional[date] = Query(None, alias="to"),
    limit: int = Query(365, ge=1, le=3650),
    db: AsyncSession = Depends(get_db),
) -> IndexResponse:
    """
    Retrieve index values for a specific city-pair route.

    Route format: `{ORIGIN}-{DESTINATION}` (e.g., `DEL-BOM`).
    """
    normalized_route = route.upper().strip()

    stmt = (
        select(IndexRecord)
        .where(IndexRecord.frequency == freq)
        .where(IndexRecord.route == normalized_route)
        .order_by(IndexRecord.observation_date.desc())
        .limit(limit)
    )

    if from_date:
        stmt = stmt.where(IndexRecord.observation_date >= from_date)
    if to_date:
        stmt = stmt.where(IndexRecord.observation_date <= to_date)

    result = await db.execute(stmt)
    records = result.scalars().all()

    if not records:
        raise HTTPException(
            status_code=404,
            detail=f"No index data found for route '{normalized_route}'",
        )

    data = [
        IndexPoint(
            observation_date=r.observation_date,
            frequency=r.frequency,
            route=r.route,
            index_value=float(r.index_value),
            base_period=r.base_period,
            passenger_weight_version=r.passenger_weight_version,
            sample_size=r.sample_size,
        )
        for r in records
    ]

    bp = data[0].base_period if data else "N/A"
    return IndexResponse(count=len(data), base_period=bp, data=data)


# ── Elasticity Endpoint ──────────────────────────────────────────

@app.get(
    "/elasticity",
    response_model=ElasticityResponse,
    tags=["Elasticity"],
    summary="Lead-time elasticity curves",
)
async def get_elasticity(
    route: Optional[str] = Query(
        None,
        description="Filter by route (e.g., DEL-BOM). Omit for all routes.",
    ),
    db: AsyncSession = Depends(get_db),
) -> ElasticityResponse:
    """
    Retrieve lead-time elasticity data showing how airfare varies
    with advance-purchase timing (T+1 through T+45).

    Each point includes mean fare, median fare, standard deviation,
    IQR, and sample size per (route, lead_time_bucket).
    """
    # Elasticity is computed on-the-fly from fare_records rather than
    # stored in a dedicated table, since it's a lightweight aggregation.
    stmt = select(FareRecord).where(FareRecord.is_anomaly == False)  # noqa: E712

    if route:
        normalized_route = route.upper().strip()
        stmt = stmt.where(FareRecord.route == normalized_route)

    result = await db.execute(stmt)
    records = result.scalars().all()

    # Group by (route, lead_time_bucket) and compute stats
    from collections import defaultdict
    import numpy as np

    buckets: dict[tuple[str, str], list[float]] = defaultdict(list)
    lt_days_map: dict[tuple[str, str], int] = {}

    for r in records:
        key = (r.route, r.lead_time_bucket)
        buckets[key].append(float(r.index_fare))
        lt_days_map[key] = r.lead_time_days

    data: list[ElasticityPoint] = []
    for (rt, bucket), prices in sorted(buckets.items()):
        arr = np.array(prices)
        q75, q25 = np.percentile(arr, [75, 25]) if len(arr) >= 2 else (0.0, 0.0)

        data.append(
            ElasticityPoint(
                route=rt,
                lead_time_days=lt_days_map[(rt, bucket)],
                lead_time_bucket=bucket,
                mean_fare=round(float(np.mean(arr)), 2),
                median_fare=round(float(np.median(arr)), 2),
                std_dev=round(float(np.std(arr, ddof=1)), 2) if len(arr) > 1 else 0.0,
                iqr=round(float(q75 - q25), 2),
                sample_size=len(arr),
            )
        )

    return ElasticityResponse(count=len(data), data=data)
