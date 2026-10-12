"""
Unit tests for the APIx FastAPI server endpoints.

Uses httpx.AsyncClient with FastAPI's TestClient pattern.
Tests run against an in-memory SQLite database (async) to avoid
requiring a running PostgreSQL instance.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import StaticPool
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from apix.agents.server import app
from apix.models.database import get_db
from apix.models.db_models import Base, FareRecord, IndexRecord
from apix.models.schemas import IndexFrequency


# ── Test Database Setup ──────────────────────────────────────────

# In-memory async SQLite for tests (no PostgreSQL required)
_test_engine = create_async_engine(
    "sqlite+aiosqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)

_test_session_factory = async_sessionmaker(
    bind=_test_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def _override_get_db():
    """Override FastAPI's get_db dependency with test database."""
    async with _test_session_factory() as session:
        try:
            yield session
        finally:
            await session.close()


# Wire the override into the FastAPI app
app.dependency_overrides[get_db] = _override_get_db


@pytest.fixture(autouse=True)
async def setup_test_db():
    """Create tables before each test, drop after."""
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def seed_index_data():
    """Seed the test database with sample index records."""
    async with _test_session_factory() as session:
        records = [
            IndexRecord(
                observation_date=date(2026, 1, 10),
                frequency=IndexFrequency.DAILY,
                route="ALL",
                index_value=Decimal("100.55"),
                base_period="2026-01",
                passenger_weight_version="v1",
                sample_size=10,
            ),
            IndexRecord(
                observation_date=date(2026, 1, 15),
                frequency=IndexFrequency.DAILY,
                route="ALL",
                index_value=Decimal("99.45"),
                base_period="2026-01",
                passenger_weight_version="v1",
                sample_size=8,
            ),
            IndexRecord(
                observation_date=date(2026, 2, 5),
                frequency=IndexFrequency.DAILY,
                route="ALL",
                index_value=Decimal("111.60"),
                base_period="2026-01",
                passenger_weight_version="v1",
                sample_size=12,
            ),
            IndexRecord(
                observation_date=date(2026, 1, 15),
                frequency=IndexFrequency.WEEKLY,
                route="ALL",
                index_value=Decimal("100.00"),
                base_period="2026-01",
                passenger_weight_version="v1",
                sample_size=2,
            ),
            IndexRecord(
                observation_date=date(2026, 1, 10),
                frequency=IndexFrequency.DAILY,
                route="DEL-BOM",
                index_value=Decimal("102.30"),
                base_period="2026-01",
                passenger_weight_version="v1",
                sample_size=5,
            ),
        ]
        session.add_all(records)
        await session.commit()


@pytest.fixture
async def seed_fare_data():
    """Seed the test database with sample fare records for elasticity."""
    async with _test_session_factory() as session:
        fares = [
            FareRecord(
                source="indigo",
                origin="DEL",
                destination="BOM",
                route="DEL-BOM",
                travel_date=date(2026, 1, 10),
                lead_time_days=1,
                lead_time_bucket="T+1",
                airline="IndiGo",
                flight_number="6E-204",
                index_fare=Decimal("5500.00"),
                base_fare=Decimal("3850.00"),
                taxes=Decimal("1650.00"),
                udf=Decimal("0"),
                convenience_fee=Decimal("0"),
                is_anomaly=False,
                scraped_at=datetime.now(timezone.utc),
            ),
            FareRecord(
                source="indigo",
                origin="DEL",
                destination="BOM",
                route="DEL-BOM",
                travel_date=date(2026, 1, 11),
                lead_time_days=1,
                lead_time_bucket="T+1",
                airline="IndiGo",
                flight_number="6E-205",
                index_fare=Decimal("5800.00"),
                base_fare=Decimal("4060.00"),
                taxes=Decimal("1740.00"),
                udf=Decimal("0"),
                convenience_fee=Decimal("0"),
                is_anomaly=False,
                scraped_at=datetime.now(timezone.utc),
            ),
            FareRecord(
                source="indigo",
                origin="DEL",
                destination="BOM",
                route="DEL-BOM",
                travel_date=date(2026, 1, 10),
                lead_time_days=7,
                lead_time_bucket="T+7",
                airline="IndiGo",
                flight_number="6E-204",
                index_fare=Decimal("4200.00"),
                base_fare=Decimal("2940.00"),
                taxes=Decimal("1260.00"),
                udf=Decimal("0"),
                convenience_fee=Decimal("0"),
                is_anomaly=False,
                scraped_at=datetime.now(timezone.utc),
            ),
            # Anomaly — should be excluded from elasticity
            FareRecord(
                source="indigo",
                origin="DEL",
                destination="BOM",
                route="DEL-BOM",
                travel_date=date(2026, 1, 10),
                lead_time_days=1,
                lead_time_bucket="T+1",
                airline="IndiGo",
                flight_number="6E-206",
                index_fare=Decimal("99999.00"),
                base_fare=Decimal("70000.00"),
                taxes=Decimal("29999.00"),
                udf=Decimal("0"),
                convenience_fee=Decimal("0"),
                is_anomaly=True,
                scraped_at=datetime.now(timezone.utc),
            ),
        ]
        session.add_all(fares)
        await session.commit()


# ── Tests ────────────────────────────────────────────────────────

class TestHealthEndpoint:
    """Tests for GET /health."""

    @pytest.mark.asyncio
    async def test_health_returns_200(self) -> None:
        """Health endpoint should return 200 with status and version."""
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] in ("ok", "degraded")
        assert "version" in data
        assert "database_connected" in data
        assert "timestamp" in data


class TestIndexEndpoint:
    """Tests for GET /index."""

    @pytest.mark.asyncio
    async def test_index_returns_daily_data(self, seed_index_data) -> None:
        """Should return daily index points when freq=daily."""
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/index", params={"freq": "daily"})

        assert response.status_code == 200
        data = response.json()
        assert data["count"] >= 2
        assert data["base_period"] == "2026-01"
        # All returned points should be daily frequency
        for point in data["data"]:
            assert point["frequency"] == "daily"

    @pytest.mark.asyncio
    async def test_index_filter_by_date_range(self, seed_index_data) -> None:
        """Should filter results by from/to date parameters."""
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                "/index",
                params={"freq": "daily", "from": "2026-01-01", "to": "2026-01-31"},
            )

        assert response.status_code == 200
        data = response.json()
        # Only Jan dates should appear (2 daily ALL + 1 daily DEL-BOM)
        for point in data["data"]:
            obs_date = date.fromisoformat(point["observation_date"])
            assert obs_date.month == 1

    @pytest.mark.asyncio
    async def test_index_weekly_frequency(self, seed_index_data) -> None:
        """Should return weekly index points when freq=weekly."""
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/index", params={"freq": "weekly"})

        assert response.status_code == 200
        data = response.json()
        assert data["count"] >= 1
        for point in data["data"]:
            assert point["frequency"] == "weekly"

    @pytest.mark.asyncio
    async def test_index_empty_result(self) -> None:
        """Should return empty data array when no matching records exist."""
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                "/index",
                params={"freq": "monthly", "from": "2020-01-01", "to": "2020-12-31"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 0
        assert data["data"] == []


class TestRouteIndexEndpoint:
    """Tests for GET /index/routes/{route}."""

    @pytest.mark.asyncio
    async def test_route_index_returns_data(self, seed_index_data) -> None:
        """Should return index data filtered to specific route."""
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                "/index/routes/DEL-BOM", params={"freq": "daily"}
            )

        assert response.status_code == 200
        data = response.json()
        assert data["count"] >= 1
        for point in data["data"]:
            assert point["route"] == "DEL-BOM"

    @pytest.mark.asyncio
    async def test_route_index_case_insensitive(self, seed_index_data) -> None:
        """Route parameter should be case-insensitive."""
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                "/index/routes/del-bom", params={"freq": "daily"}
            )

        assert response.status_code == 200
        data = response.json()
        assert data["count"] >= 1

    @pytest.mark.asyncio
    async def test_route_index_not_found(self, seed_index_data) -> None:
        """Should return 404 for routes with no data."""
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                "/index/routes/XYZ-ABC", params={"freq": "daily"}
            )

        assert response.status_code == 404
        assert "No index data found" in response.json()["detail"]


class TestElasticityEndpoint:
    """Tests for GET /elasticity."""

    @pytest.mark.asyncio
    async def test_elasticity_returns_data(self, seed_fare_data) -> None:
        """Should compute and return elasticity curves from fare records."""
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/elasticity")

        assert response.status_code == 200
        data = response.json()
        assert data["count"] >= 2  # T+1 and T+7 buckets
        buckets = {p["lead_time_bucket"] for p in data["data"]}
        assert "T+1" in buckets
        assert "T+7" in buckets

    @pytest.mark.asyncio
    async def test_elasticity_excludes_anomalies(self, seed_fare_data) -> None:
        """Anomalous fares should not appear in elasticity calculations."""
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/elasticity", params={"route": "DEL-BOM"})

        assert response.status_code == 200
        data = response.json()
        t1_points = [p for p in data["data"] if p["lead_time_bucket"] == "T+1"]
        assert len(t1_points) == 1
        # Mean of [5500, 5800] = 5650, NOT including the 99999 anomaly
        assert t1_points[0]["mean_fare"] == pytest.approx(5650.0, abs=0.01)
        assert t1_points[0]["sample_size"] == 2

    @pytest.mark.asyncio
    async def test_elasticity_filter_by_route(self, seed_fare_data) -> None:
        """Should filter elasticity data to specific route."""
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/elasticity", params={"route": "DEL-BOM"})

        assert response.status_code == 200
        data = response.json()
        for point in data["data"]:
            assert point["route"] == "DEL-BOM"

    @pytest.mark.asyncio
    async def test_elasticity_empty_when_no_fares(self) -> None:
        """Should return empty data when no fare records exist."""
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/elasticity")

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 0
        assert data["data"] == []


class TestOpenAPIDocs:
    """Tests for auto-generated documentation."""

    @pytest.mark.asyncio
    async def test_openapi_schema_accessible(self) -> None:
        """OpenAPI JSON schema should be accessible."""
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/openapi.json")

        assert response.status_code == 200
        schema = response.json()
        assert schema["info"]["title"] == "APIx — Airfare Price Index"
        assert "/health" in schema["paths"]
        assert "/index" in schema["paths"]
        assert "/elasticity" in schema["paths"]

    @pytest.mark.asyncio
    async def test_swagger_ui_accessible(self) -> None:
        """Swagger UI should be accessible at /docs."""
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/docs")

        assert response.status_code == 200
        assert "swagger" in response.text.lower()
