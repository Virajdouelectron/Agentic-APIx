"""
Unit tests for the Orchestrator Agent.

Tests:
- ProxyPool static cycling and session nonce generation
- Job matrix generation from routes, windows, and sources
- Exponential backoff calculation with jitter
- Resilient retry and proxy rotation upon BlockedBySiteError
- Max retries exhaustion: permanent failure recorded without crashing the run
- Full pipeline execution and accurate RunSummary metrics
"""

import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from apix.agents.extraction.base import BaseExtractor
from apix.agents.orchestrator import (
    ExtractionJob,
    JobStatus,
    Orchestrator,
    ProxyPool,
)
from apix.config.settings import RouteConfig, ScrapingConfig, Settings, WindowConfig
from apix.exceptions import BlockedBySiteError
from apix.models.schemas import FareBreakdown, RawFarePayload


def create_mock_payload(route_str: str, travel_date: date) -> RawFarePayload:
    origin, dest = route_str.split("-")
    return RawFarePayload(
        source="mock_source",
        route=route_str,
        origin=origin,
        destination=dest,
        travel_date=travel_date,
        lead_time_bucket="T+7",
        lead_time_days=7,
        airline="Mock Air",
        flight_number="MK-101",
        fare_components=FareBreakdown(
            base_fare=Decimal("4000.00"),
            taxes=Decimal("500.00"),
            udf=Decimal("100.00"),
            convenience_fee=Decimal("150.00"),
        ),
        currency="INR",
        scraped_at=datetime.now(timezone.utc),
        scraper_version="1.0.0",
    )


class MockExtractor(BaseExtractor):
    """Configurable mock extractor for orchestrator test scenarios."""

    def __init__(
        self,
        source_name: str = "mock_source",
        proxy_url: str | None = None,
        fail_count: int = 0,
        fail_with_block: bool = True,
    ) -> None:
        super().__init__(source_name=source_name, proxy_url=proxy_url)
        self.fail_count = fail_count
        self.fail_with_block = fail_with_block
        self.call_count = 0
        self.proxies_seen: list[str | None] = []

    async def initialize(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def extract_fares(self, route, travel_date, lead_time_bucket, lead_time_days):
        self.call_count += 1
        self.proxies_seen.append(self.proxy_url)

        if self.call_count <= self.fail_count:
            if self.fail_with_block:
                raise BlockedBySiteError(f"Simulated anti-bot block on call {self.call_count}")
            raise RuntimeError(f"Simulated unexpected network failure on call {self.call_count}")

        return [create_mock_payload(f"{route.origin}-{route.destination}", travel_date)]


def test_proxy_pool_rotation_cycling():
    """Verify static proxy list cycling."""
    pool = ProxyPool(proxies=["http://p1:8080", "http://p2:8080"])
    assert pool.get_proxy() == "http://p1:8080"
    assert pool.get_proxy() == "http://p2:8080"
    assert pool.get_proxy() == "http://p1:8080"


def test_proxy_pool_gateway_session_nonce():
    """Verify rotating gateway attaches unique session tokens per attempt."""
    pool = ProxyPool(base_proxy_url="http://gate.proxy.com:8000")
    p1 = pool.get_proxy(attempt=1)
    p2 = pool.get_proxy(attempt=2)

    assert p1 is not None and "gate.proxy.com:8000?session=" in p1
    assert p2 is not None and "att2" in p2
    assert p1 != p2


def test_build_job_matrix():
    """Verify cartesian product of routes x windows x registered sources."""
    settings = Settings(
        routes=[
            RouteConfig(origin="DEL", destination="BOM", name="DEL-BOM", tier="metro"),
            RouteConfig(origin="BLR", destination="DEL", name="BLR-DEL", tier="metro"),
        ],
        windows=[
            WindowConfig(label="T+1", days=1),
            WindowConfig(label="T+7", days=7),
            WindowConfig(label="T+15", days=15),
        ],
    )
    orchestrator = Orchestrator(
        settings=settings,
        extractor_factories={"indigo": lambda p: MockExtractor(proxy_url=p)},
    )

    ref_date = date(2026, 11, 1)
    jobs = orchestrator.build_job_matrix(reference_date=ref_date)

    # 2 routes x 3 windows x 1 source = 6 jobs
    assert len(jobs) == 6
    del_bom_t7 = next(j for j in jobs if j.route.origin == "DEL" and j.window.label == "T+7")
    assert del_bom_t7.travel_date == date(2026, 11, 8)
    assert del_bom_t7.status == JobStatus.PENDING


def test_calculate_backoff_increases_with_attempts():
    """Verify backoff increases exponentially with jitter within configured bounds."""
    settings = Settings(
        scraping=ScrapingConfig(
            backoff_base_seconds=1.0,
            backoff_jitter_max_seconds=0.2,
        )
    )
    orchestrator = Orchestrator(settings=settings)

    d1 = orchestrator.calculate_backoff(attempt=1)
    d2 = orchestrator.calculate_backoff(attempt=2)
    d3 = orchestrator.calculate_backoff(attempt=3)

    # Base: 1.0 * 2^0 = 1.0 (+0.0..0.2) -> 1.0..1.2
    assert 1.0 <= d1 <= 1.2
    # Base: 1.0 * 2^1 = 2.0 (+0.0..0.2) -> 2.0..2.2
    assert 2.0 <= d2 <= 2.2
    # Base: 1.0 * 2^2 = 4.0 (+0.0..0.2) -> 4.0..4.2
    assert 4.0 <= d3 <= 4.2


@pytest.mark.asyncio
async def test_execute_job_retries_on_block_with_proxy_rotation():
    """
    Simulate a job that gets blocked on attempt 1, but succeeds on attempt 2
    after proxy rotation.
    """
    settings = Settings(
        scraping=ScrapingConfig(max_retries=3, backoff_base_seconds=0.01, backoff_jitter_max_seconds=0.01)
    )
    pool = ProxyPool(proxies=["http://p1:8080", "http://p2:8080", "http://p3:8080"])
    
    mock_extractor = MockExtractor(fail_count=1, fail_with_block=True)
    orchestrator = Orchestrator(
        settings=settings,
        proxy_pool=pool,
        extractor_factories={"test": lambda p: mock_extractor},
    )

    job = ExtractionJob(
        job_id="test_job_1",
        route=RouteConfig(origin="DEL", destination="BOM", name="DEL-BOM", tier="metro"),
        window=WindowConfig(label="T+1", days=1),
        travel_date=date(2026, 11, 2),
        source="test",
    )

    semaphore = asyncio.Semaphore(1)
    with patch("asyncio.sleep", AsyncMock()):
        result = await orchestrator.execute_job(job, semaphore)

    assert result.status == JobStatus.SUCCESS
    assert result.attempts == 2
    assert len(result.payloads) == 1
    assert result.error_message is None


@pytest.mark.asyncio
async def test_execute_job_exhaustion_marks_failed_without_aborting():
    """
    When an extraction job is repeatedly blocked past max_retries, it must
    record as FAILED cleanly without raising an unhandled exception.
    """
    settings = Settings(
        scraping=ScrapingConfig(max_retries=2, backoff_base_seconds=0.01, backoff_jitter_max_seconds=0.01)
    )
    mock_extractor = MockExtractor(fail_count=10, fail_with_block=True)
    orchestrator = Orchestrator(
        settings=settings,
        extractor_factories={"test": lambda p: mock_extractor},
    )

    job = ExtractionJob(
        job_id="failing_job",
        route=RouteConfig(origin="DEL", destination="CCU", name="DEL-CCU", tier="metro"),
        window=WindowConfig(label="T+7", days=7),
        travel_date=date(2026, 11, 8),
        source="test",
    )

    semaphore = asyncio.Semaphore(1)
    with patch("asyncio.sleep", AsyncMock()):
        result = await orchestrator.execute_job(job, semaphore)

    assert result.status == JobStatus.FAILED
    assert result.attempts == 2
    assert "Simulated anti-bot block" in (result.error_message or "")
    assert len(result.payloads) == 0


@pytest.mark.asyncio
async def test_run_pipeline_summary_and_fault_tolerance():
    """
    Run full pipeline where one job succeeds immediately, one succeeds after
    retry, and one fails permanently. Verify RunSummary counters.
    """
    settings = Settings(
        routes=[
            RouteConfig(origin="DEL", destination="BOM", name="DEL-BOM", tier="metro"),
            RouteConfig(origin="DEL", destination="BLR", name="DEL-BLR", tier="metro"),
            RouteConfig(origin="DEL", destination="CCU", name="DEL-CCU", tier="metro"),
        ],
        windows=[
            WindowConfig(label="T+1", days=1),
        ],
        scraping=ScrapingConfig(max_retries=2, backoff_base_seconds=0.01, backoff_jitter_max_seconds=0.01),
    )

    call_counts = {}

    def factory_for_route(proxy_url):
        class DynamicExtractor(BaseExtractor):
            async def initialize(self): pass
            async def close(self): pass
            async def extract_fares(self, route, travel_date, lead_time_bucket, lead_time_days):
                r_key = route.destination
                call_counts[r_key] = call_counts.get(r_key, 0) + 1
                cnt = call_counts[r_key]

                if r_key == "BOM":
                    return [create_mock_payload(f"{route.origin}-{route.destination}", travel_date)]
                elif r_key == "BLR":
                    if cnt == 1:
                        raise BlockedBySiteError("BLR blocked once")
                    return [create_mock_payload(f"{route.origin}-{route.destination}", travel_date)]
                else:  # CCU
                    raise BlockedBySiteError("CCU always blocked")

        return DynamicExtractor(source_name="dynamic", proxy_url=proxy_url)

    orchestrator = Orchestrator(
        settings=settings,
        extractor_factories={"test": factory_for_route},
    )

    with patch("asyncio.sleep", AsyncMock()):
        summary, payloads = await orchestrator.run_pipeline(reference_date=date(2026, 11, 1))

    # 3 jobs planned: 1 immediate success, 1 retried success, 1 failed
    assert summary.jobs_planned == 3
    assert summary.jobs_succeeded == 2
    assert summary.jobs_retried == 2  # BLR retried once, CCU retried once
    assert summary.jobs_failed == 1
    assert len(payloads) == 2  # DEL-BOM + DEL-BLR
