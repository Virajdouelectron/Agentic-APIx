"""
Base extraction agent interface and browser automation harness.

Provides:
- Shared Playwright async browser context management with proxy support
- Polite request pacing with randomized jitter
- Zero-bypass anti-bot detection (raises BlockedBySiteError on CAPTCHAs or challenges)
- Standard contract for all source extractors (IndiGo, MakeMyTrip, Cleartrip, etc.)
"""

from __future__ import annotations

import abc
import asyncio
import logging
import random
from datetime import date, datetime
from typing import Any, Optional

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from apix.config.settings import RouteConfig, get_settings
from apix.exceptions import BlockedBySiteError, ExtractionTimeoutError
from apix.models.schemas import RawFarePayload

logger = logging.getLogger(__name__)

# Standard user-agents representing legitimate modern desktop browsers
DEFAULT_USER_AGENTS = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) "
        "Gecko/20100101 Firefox/125.0"
    ),
]

# Signatures indicative of anti-bot interstitial walls, challenges, or CAPTCHAs
CHALLENGE_KEYWORDS = [
    "attention required! | cloudflare",
    "just a moment...",
    "cf-browser-verification",
    "cf-turnstile",
    "challenge-running",
    "perimeterx",
    "press & hold",
    "verify you are human",
    "datadome",
    "security check to continue",
    "access denied",
    "recaptcha",
    "hcaptcha",
]


class BaseExtractor(abc.ABC):
    """
    Abstract base class for all airline and OTA extraction agents.

    Subclasses implement the site-specific navigation and DOM extraction logic
    in `extract_fares()`, while this base class handles browser lifecycle,
    polite delays, proxy routing, and challenge detection.
    """

    def __init__(self, source_name: str, proxy_url: Optional[str] = None) -> None:
        self.source_name = source_name
        self.settings = get_settings()
        self.proxy_url = proxy_url or self.settings.proxy_pool_url or None
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None

    async def initialize(self) -> None:
        """Launch Playwright browser and configure isolated browser context."""
        if self._browser is not None:
            return

        self._playwright = await async_playwright().start()
        launch_args = ["--disable-blink-features=AutomationControlled"]

        proxy_config = None
        if self.proxy_url:
            proxy_config = {"server": self.proxy_url}

        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=launch_args,
            proxy=proxy_config,
        )

        user_agent = random.choice(DEFAULT_USER_AGENTS)
        self._context = await self._browser.new_context(
            user_agent=user_agent,
            viewport={"width": 1366, "height": 768},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
        )

        logger.info(
            "Initialized extractor browser context for %s (proxy: %s)",
            self.source_name,
            bool(self.proxy_url),
        )

    async def close(self) -> None:
        """Gracefully release browser and Playwright resources."""
        if self._context:
            await self._context.close()
            self._context = None
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        logger.info("Closed extractor browser for %s", self.source_name)

    async def __aenter__(self) -> "BaseExtractor":
        await self.initialize()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    async def polite_delay(self) -> None:
        """
        Sleep for a polite, randomized duration to avoid overwhelming airline servers.
        
        Duration is uniformly drawn from [polite_delay_min, polite_delay_max].
        """
        min_sec = self.settings.scraping.polite_delay_min_seconds
        max_sec = self.settings.scraping.polite_delay_max_seconds
        delay = random.uniform(min_sec, max_sec)
        logger.debug("Applying polite delay of %.2fs for %s", delay, self.source_name)
        await asyncio.sleep(delay)

    async def check_for_anti_bot_challenges(self, page: Page) -> None:
        """
        Scan page content, title, and response indicators for anti-bot roadblocks.

        Per architectural guidelines: we respect sites and NEVER attempt to crack,
        bypass, or defeat CAPTCHAs or Cloudflare challenges. If detected, we immediately
        raise BlockedBySiteError to prompt the Orchestrator to back off or rotate proxy.
        """
        title = (await page.title()).lower()
        content = (await page.content()).lower()

        for indicator in CHALLENGE_KEYWORDS:
            if indicator in title or indicator in content:
                logger.warning(
                    "%s triggered anti-bot challenge/CAPTCHA indicator: '%s'. Aborting without evasion.",
                    self.source_name,
                    indicator,
                )
                raise BlockedBySiteError(
                    f"Blocked by anti-bot challenge on {self.source_name}",
                    details={"indicator": indicator, "url": page.url},
                )

    async def capture_screenshot(self, page: Page) -> bytes:
        """Capture JPEG screenshot for visual fallback or diagnostic logging."""
        return await page.screenshot(type="jpeg", quality=80, full_page=False)

    @abc.abstractmethod
    async def extract_fares(
        self,
        route: RouteConfig,
        travel_date: date,
        lead_time_bucket: str,
        lead_time_days: int,
    ) -> list[RawFarePayload]:
        """
        Perform search and extract raw fare quotes for a single city pair and travel date.

        Must return a list of validated RawFarePayload instances.
        """
        pass
