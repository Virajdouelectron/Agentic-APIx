"""
Orchestrator Agent for the APIx domestic airfare pipeline.

Coordinates the daily extraction workflow:
- Reads routes and advance-purchase windows dynamically from YAML config
- Builds the job matrix (route x window x source) and computes departure dates
- Dispatches extraction jobs concurrently with bounded parallelism (asyncio.Semaphore)
- Recovers from anti-bot blocks using exponential backoff, jitter, and proxy rotation
- Isolates individual job failures so a single failing route never aborts the pipeline run
- Produces auditable run summaries (planned, succeeded, retried, failed)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
import logging
import random
import uuid
from typing import Any, Callable, Dict, List, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from apix.agents.extraction.base import BaseExtractor
from apix.agents.extraction.indigo import IndiGoExtractor
from apix.agents.extraction.spicejet import SpiceJetExtractor
from apix.config.settings import RouteConfig, Settings, WindowConfig, get_settings
from apix.exceptions import BlockedBySiteError
from apix.models.schemas import RawFarePayload, RunSummary


logger = logging.getLogger(__name__)


class JobStatus(str, Enum):
    """Execution state of an individual extraction job."""
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


class ProxyPool:
    """
    Manages IP / proxy rotation for extraction requests.

    Supports rotating through a static list of proxies, or generating session-tagged
    endpoints from a rotating proxy gateway (e.g. proxy.provider.com:8080#session-xyz).
    """

    def __init__(self, base_proxy_url: Optional[str] = None, proxies: Optional[List[str]] = None) -> None:
        self.base_proxy_url = base_proxy_url
        self.proxies = list(proxies) if proxies else []
        self._index = 0

    def get_proxy(self, attempt: int = 1) -> Optional[str]:
        """
        Return the next proxy endpoint.
        
        If a static list exists, cycles through it sequentially.
        If a single gateway URL is configured, attaches a session nonce so providers
        assign a fresh exit IP.
        """
        if self.proxies:
            proxy = self.proxies[self._index % len(self.proxies)]
            self._index += 1
            return proxy

        if self.base_proxy_url:
            session_id = f"{uuid.uuid4().hex[:8]}-att{attempt}"
            separator = "&" if "?" in self.base_proxy_url else "?"
            return f"{self.base_proxy_url}{separator}session={session_id}"

        return None


@dataclass
class ExtractionJob:
    """
    State container for a single route-window-source scraping task.
    """
    job_id: str
    route: RouteConfig
    window: WindowConfig
    travel_date: date
    source: str
    status: JobStatus = JobStatus.PENDING
    attempts: int = 0
    proxy_used: Optional[str] = None
    error_message: Optional[str] = None
    payloads: List[RawFarePayload] = field(default_factory=list)

    @property
    def route_str(self) -> str:
        return f"{self.route.origin}-{self.route.destination}"


class Orchestrator:
    """
    System controller orchestrating parallel scraping, retries, and run metrics.
    """

    def __init__(
        self,
        settings: Optional[Settings] = None,
        proxy_pool: Optional[ProxyPool] = None,
        extractor_factories: Optional[Dict[str, Callable[[Optional[str]], BaseExtractor]]] = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.proxy_pool = proxy_pool or ProxyPool(base_proxy_url=self.settings.proxy_pool_url)

        # Default source extractors registered in the pipeline
        self.extractor_factories = extractor_factories or {
            "indigo": lambda proxy: self._get_extractor_for_source("indigo", proxy),
            "spicejet": lambda proxy: self._get_extractor_for_source("spicejet", proxy),
        }

    @staticmethod
    def _get_extractor_for_source(source: str, proxy: Optional[str] = None) -> BaseExtractor:
        """Factory creating extractor instance for a configured source."""
        if source == "indigo":
            return IndiGoExtractor(proxy_url=proxy)
        elif source == "spicejet":
            return SpiceJetExtractor(proxy_url=proxy)
        raise ValueError(f"Unknown extractor source: '{source}'")

    def build_job_matrix(self, reference_date: Optional[date] = None) -> List[ExtractionJob]:

        """
        Construct all discrete extraction jobs:
        City Pairs (DGCA routes) x Lead-time Windows (T+1..T+45) x Registered Sources
        """
        ref_date = reference_date or datetime.now(timezone.utc).date()
        jobs: List[ExtractionJob] = []

        for route in self.settings.routes:
            for window in self.settings.windows:
                travel_date = ref_date + timedelta(days=window.days)
                for source in self.extractor_factories:
                    job_id = f"{source}_{route.origin}-{route.destination}_{window.label}_{travel_date.isoformat()}"
                    jobs.append(
                        ExtractionJob(
                            job_id=job_id,
                            route=route,
                            window=window,
                            travel_date=travel_date,
                            source=source,
                        )
                    )

        logger.info(
            "Built job matrix: %d total jobs across %d routes, %d windows, and %d sources",
            len(jobs),
            len(self.settings.routes),
            len(self.settings.windows),
            len(self.extractor_factories),
        )
        return jobs

    def calculate_backoff(self, attempt: int) -> float:
        """
        Compute exponential backoff delay with uniform jitter.
        Formula: base * 2^(attempt - 1) + uniform(0, jitter_max)
        """
        base = self.settings.scraping.backoff_base_seconds
        jitter_max = self.settings.scraping.backoff_jitter_max_seconds
        delay = (base * (2 ** (attempt - 1))) + random.uniform(0.0, jitter_max)
        return max(0.1, delay)

    async def execute_job(
        self,
        job: ExtractionJob,
        semaphore: asyncio.Semaphore,
    ) -> ExtractionJob:
        """
        Execute an extraction job with concurrency control, retries, and proxy rotation.
        
        Guarantees:
        - If an extractor is blocked or encounters a transient error, backs off with jitter,
          rotates proxy, and retries up to max_retries.
        - Exhausted failures are cleanly recorded as FAILED without raising an unhandled exception.
        """
        max_retries = self.settings.scraping.max_retries

        async with semaphore:
            while job.attempts < max_retries:
                job.attempts += 1
                job.status = JobStatus.RUNNING
                job.proxy_used = self.proxy_pool.get_proxy(attempt=job.attempts)

                logger.debug(
                    "Starting job %s (attempt %d/%d) via %s",
                    job.job_id,
                    job.attempts,
                    max_retries,
                    job.source,
                )

                try:
                    factory = self.extractor_factories.get(job.source)
                    if not factory:
                        raise ValueError(f"No extractor factory registered for source '{job.source}'")

                    extractor = factory(job.proxy_used)
                    async with extractor:
                        payloads = await extractor.extract_fares(
                            route=job.route,
                            travel_date=job.travel_date,
                            lead_time_bucket=job.window.label,
                            lead_time_days=job.window.days,
                        )

                    job.payloads = payloads
                    job.status = JobStatus.SUCCESS
                    job.error_message = None
                    logger.info(
                        "Job %s succeeded: %d quotes extracted",
                        job.job_id,
                        len(payloads),
                    )
                    return job

                except BlockedBySiteError as exc:
                    job.status = JobStatus.BLOCKED
                    job.error_message = str(exc)
                    if job.attempts < max_retries:
                        backoff_delay = self.calculate_backoff(job.attempts)
                        logger.warning(
                            "Job %s blocked by %s on attempt %d/%d. Rotating proxy and backing off %.2fs",
                            job.job_id,
                            job.source,
                            job.attempts,
                            max_retries,
                            backoff_delay,
                        )
                        await asyncio.sleep(backoff_delay)
                    else:
                        logger.error(
                            "Job %s failed permanently: maximum attempts (%d) reached after block: %s",
                            job.job_id,
                            max_retries,
                            exc,
                        )

                except Exception as exc:
                    job.error_message = str(exc)
                    if job.attempts < max_retries:
                        backoff_delay = self.calculate_backoff(job.attempts)
                        logger.warning(
                            "Job %s encountered error (%s) on attempt %d/%d. Retrying in %.2fs",
                            job.job_id,
                            exc,
                            job.attempts,
                            max_retries,
                            backoff_delay,
                        )
                        await asyncio.sleep(backoff_delay)
                    else:
                        logger.error(
                            "Job %s permanently failed after %d attempts: %s",
                            job.job_id,
                            max_retries,
                            exc,
                        )

            # If loop exited without return, retries were exhausted
            job.status = JobStatus.FAILED
            return job

    async def run_pipeline(
        self,
        reference_date: Optional[date] = None,
    ) -> tuple[RunSummary, List[RawFarePayload]]:
        """
        Execute a complete pipeline run:
        1. Generates jobs from config.
        2. Dispatches in parallel bounded by concurrency_limit.
        3. Aggregates results and builds RunSummary.
        """
        run_id = f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        started_at = datetime.now(timezone.utc)

        jobs = self.build_job_matrix(reference_date=reference_date)
        concurrency = self.settings.scraping.concurrency_limit
        semaphore = asyncio.Semaphore(concurrency)

        logger.info(
            "Starting pipeline run %s with %d jobs (concurrency limit: %d)",
            run_id,
            len(jobs),
            concurrency,
        )

        tasks = [self.execute_job(job, semaphore) for job in jobs]
        completed_jobs: List[ExtractionJob] = await asyncio.gather(*tasks)

        completed_at = datetime.now(timezone.utc)
        duration = (completed_at - started_at).total_seconds()

        # Compute metric tallies
        succeeded_jobs = sum(1 for j in completed_jobs if j.status == JobStatus.SUCCESS)
        failed_jobs = sum(1 for j in completed_jobs if j.status == JobStatus.FAILED)
        retried_jobs = sum(1 for j in completed_jobs if j.attempts > 1)

        all_payloads: List[RawFarePayload] = []
        for j in completed_jobs:
            if j.status == JobStatus.SUCCESS:
                all_payloads.extend(j.payloads)

        summary = RunSummary(
            run_id=run_id,
            started_at=started_at,
            completed_at=completed_at,
            jobs_planned=len(jobs),
            jobs_succeeded=succeeded_jobs,
            jobs_retried=retried_jobs,
            jobs_failed=failed_jobs,
            duration_seconds=duration,
        )

        logger.info(
            "Pipeline run %s finished in %.2fs: %d planned, %d succeeded, %d retried, %d failed (%d raw payloads collected)",
            run_id,
            duration,
            summary.jobs_planned,
            summary.jobs_succeeded,
            summary.jobs_retried,
            summary.jobs_failed,
            len(all_payloads),
        )

        return summary, all_payloads

    def create_scheduler(self) -> AsyncIOScheduler:
        """
        Set up APScheduler to trigger daily extraction at 00:00 UTC.
        """
        scheduler = AsyncIOScheduler()
        scheduler.add_job(
            self.run_pipeline,
            trigger=CronTrigger(hour=0, minute=0, timezone="UTC"),
            id="daily_apix_scrape",
            name="Daily Airfare Price Index Extraction Run",
            replace_existing=True,
        )
        return scheduler
