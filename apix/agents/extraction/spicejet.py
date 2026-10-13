"""
SpiceJet airline extraction agent.

Implements BaseExtractor for SpiceJet (SG) domestic flights across India.
Uses externalized selectors from config/selectors/spicejet.yaml.
Falls back to VLM visual extraction when portal markup shifts unexpectedly.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, List, Optional

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from apix.agents.extraction.base import BaseExtractor
from apix.agents.extraction.indigo import clean_currency_text
from apix.agents.extraction.vlm_fallback import VLMFallbackEngine
from apix.config.settings import RouteConfig, SelectorEntry
from apix.exceptions import ExtractionTimeoutError
from apix.models.schemas import FareBreakdown, RawFarePayload

logger = logging.getLogger(__name__)


class SpiceJetExtractor(BaseExtractor):
    """
    Extraction agent specialized in scraping SpiceJet domestic airfares.
    """

    def __init__(
        self,
        proxy_url: Optional[str] = None,
        vlm_engine: Optional[VLMFallbackEngine] = None,
    ) -> None:
        super().__init__(source_name="spicejet", proxy_url=proxy_url)
        self.vlm_engine = vlm_engine or VLMFallbackEngine()
        self.selector_config = self.settings.source_selectors.get("spicejet")

    def _get_selector(self, key: str) -> Optional[SelectorEntry]:
        """Safely fetch selector entry from loaded config."""
        if self.selector_config and key in self.selector_config.selectors:
            return self.selector_config.selectors[key]
        return None

    def build_search_url(self, origin: str, destination: str, travel_date: date) -> str:
        """
        Construct SpiceJet domestic flight search URL.
        """
        date_str = travel_date.strftime("%Y-%m-%d")
        return (
            f"https://www.spicejet.com/search"
            f"?from={origin}&to={destination}&tripType=1&departureDate={date_str}"
        )

    async def extract_fares(
        self,
        route: RouteConfig,
        travel_date: date,
        lead_time_bucket: str,
        lead_time_days: int,
    ) -> list[RawFarePayload]:
        """
        Execute full extraction flow for a SpiceJet route query:
        1. Navigate to search results with polite delay.
        2. Detect anti-bot challenges (raise BlockedBySiteError if detected).
        3. Attempt DOM selector-based extraction.
        4. If selectors yield zero results, fallback to VLM visual extraction.
        5. Return validated RawFarePayload models.
        """
        if not self._context:
            await self.initialize()

        page = await self._context.new_page()
        search_url = self.build_search_url(route.origin, route.destination, travel_date)
        route_str = f"{route.origin}-{route.destination}"

        try:
            logger.info("Extracting SpiceJet fares for %s departing on %s", route_str, travel_date)
            await self.polite_delay()

            timeout_ms = self.settings.scraping.page_timeout_seconds * 1000
            try:
                await page.goto(search_url, timeout=timeout_ms, wait_until="domcontentloaded")
            except PlaywrightTimeoutError as exc:
                raise ExtractionTimeoutError(
                    f"Navigation timed out after {timeout_ms}ms on {search_url}",
                    details={"route": route_str, "date": str(travel_date)},
                ) from exc

            # Anti-bot detection checkpoint
            await self.check_for_anti_bot_challenges(page)

            # Wait briefly for dynamic fare cards to mount
            flight_card_selector = self._get_selector("flight_card")
            card_css = flight_card_selector.value if flight_card_selector else "div[data-testid='flight-card']"

            has_cards = False
            try:
                await page.wait_for_selector(card_css, timeout=8000)
                has_cards = True
            except PlaywrightTimeoutError:
                logger.warning(
                    "SpiceJet flight cards selector '%s' stopped matching or timed out on %s. Trying visual fallback.",
                    card_css,
                    route_str,
                )

            payloads: list[RawFarePayload] = []

            # ── Primary Path: DOM Extraction ──────────────────────────
            if has_cards:
                payloads = await self._extract_via_dom(
                    page=page,
                    route=route,
                    travel_date=travel_date,
                    lead_time_bucket=lead_time_bucket,
                    lead_time_days=lead_time_days,
                )

            # ── Self-Healing Fallback: VLM Visual Extraction ──────────
            if not payloads:
                logger.info(
                    "Primary DOM extraction yielded 0 results for %s. Initiating VLM visual fallback.",
                    route_str,
                )
                screenshot = await self.capture_screenshot(page)
                payloads = await self._extract_via_vlm(
                    screenshot=screenshot,
                    route=route,
                    travel_date=travel_date,
                    lead_time_bucket=lead_time_bucket,
                    lead_time_days=lead_time_days,
                )

            logger.info("Successfully extracted %d SpiceJet fares for %s", len(payloads), route_str)
            return payloads

        finally:
            await page.close()

    async def _extract_via_dom(
        self,
        page: Page,
        route: RouteConfig,
        travel_date: date,
        lead_time_bucket: str,
        lead_time_days: int,
    ) -> list[RawFarePayload]:
        """Parse flight result elements directly from DOM using configured selectors."""
        flight_card_sel = self._get_selector("flight_card")
        card_css = flight_card_sel.value if flight_card_sel else "div[data-testid='flight-card']"
        cards = await page.query_selector_all(card_css)

        fn_sel = self._get_selector("flight_number")
        base_sel = self._get_selector("base_fare")
        tax_sel = self._get_selector("taxes")
        udf_sel = self._get_selector("udf")
        fee_sel = self._get_selector("convenience_fee")
        total_sel = self._get_selector("total_fare")

        payloads: list[RawFarePayload] = []
        now_utc = datetime.now(timezone.utc)

        for card in cards:
            flight_num_el = await card.query_selector(fn_sel.value if fn_sel else "[data-testid='flight-number']")
            flight_number = (await flight_num_el.inner_text()).strip() if flight_num_el else "SG-UNKNOWN"

            base_el = await card.query_selector(base_sel.value if base_sel else "[data-testid='base-fare-price']")
            tax_el = await card.query_selector(tax_sel.value if tax_sel else "[data-testid='tax-price']")
            udf_el = await card.query_selector(udf_sel.value if udf_sel else "[data-testid='udf-price']")
            fee_el = await card.query_selector(fee_sel.value if fee_sel else "[data-testid='convenience-price']")
            total_el = await card.query_selector(total_sel.value if total_sel else "[data-testid='total-price']")

            base_val = clean_currency_text(await base_el.inner_text() if base_el else None)
            tax_val = clean_currency_text(await tax_el.inner_text() if tax_el else None)
            udf_val = clean_currency_text(await udf_el.inner_text() if udf_el else None)
            fee_val = clean_currency_text(await fee_el.inner_text() if fee_el else None)
            total_val = clean_currency_text(await total_el.inner_text() if total_el else None)

            # If base fare is not explicitly split from total, estimate based on industry standard
            if base_val == Decimal("0.00") and total_val > Decimal("0.00"):
                base_val = total_val * Decimal("0.85")
                tax_val = total_val * Decimal("0.15")

            breakdown = FareBreakdown(
                base_fare=base_val,
                taxes=tax_val,
                udf=udf_val,
                convenience_fee=fee_val,
                total_fare=total_val if total_val > Decimal("0.00") else None,
            )

            payloads.append(
                RawFarePayload(
                    source="spicejet",
                    route=f"{route.origin}-{route.destination}",
                    origin=route.origin,
                    destination=route.destination,
                    travel_date=travel_date,
                    lead_time_bucket=lead_time_bucket,
                    lead_time_days=lead_time_days,
                    airline="SpiceJet",
                    flight_number=flight_number,
                    fare_components=breakdown,
                    currency="INR",
                    scraped_at=now_utc,
                    scraper_version="1.0.0",
                )
            )

        return payloads

    async def _extract_via_vlm(
        self,
        screenshot: bytes,
        route: RouteConfig,
        travel_date: date,
        lead_time_bucket: str,
        lead_time_days: int,
    ) -> list[RawFarePayload]:
        """Convert page screenshot into structured RawFarePayloads via VLM."""
        route_str = f"{route.origin}-{route.destination}"
        custom_prompt = (
            self.selector_config.vlm_prompts.get("fare_extraction")
            if self.selector_config
            else None
        )

        vlm_flights = await self.vlm_engine.extract_fares_from_screenshot(
            screenshot_bytes=screenshot,
            source_name="spicejet",
            route_str=route_str,
            custom_prompt=custom_prompt,
        )

        payloads: list[RawFarePayload] = []
        now_utc = datetime.now(timezone.utc)

        for item in vlm_flights:
            flight_num = item.get("flight_number") or "SG-VLM"
            base_fare = Decimal(str(item.get("base_fare", 0)))
            taxes = Decimal(str(item.get("taxes", 0)))
            udf = Decimal(str(item.get("udf", 0)))
            convenience_fee = Decimal(str(item.get("convenience_fee", 0)))

            breakdown = FareBreakdown(
                base_fare=base_fare,
                taxes=taxes,
                udf=udf,
                convenience_fee=convenience_fee,
            )

            payloads.append(
                RawFarePayload(
                    source="spicejet",
                    route=route_str,
                    origin=route.origin,
                    destination=route.destination,
                    travel_date=travel_date,
                    lead_time_bucket=lead_time_bucket,
                    lead_time_days=lead_time_days,
                    airline="SpiceJet",
                    flight_number=flight_num,
                    fare_components=breakdown,
                    currency="INR",
                    scraped_at=now_utc,
                    scraper_version="1.0.0-vlm",
                    raw_metadata={"vlm_fallback": True},
                )
            )

        return payloads
