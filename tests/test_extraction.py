"""
Unit tests for the Extraction Layer.

Tests:
- Currency text cleaning and decimal conversion
- Zero-bypass anti-bot challenge detection (raises BlockedBySiteError)
- IndiGo search URL formatting
- VLM visual fallback parsing, error handling, and repair logging
- Polite delay ranges
"""

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from apix.agents.extraction.base import BaseExtractor
from apix.agents.extraction.indigo import IndiGoExtractor, clean_currency_text
from apix.agents.extraction.spicejet import SpiceJetExtractor
from apix.agents.extraction.vlm_fallback import VLMFallbackEngine
from apix.config.settings import RouteConfig
from apix.exceptions import BlockedBySiteError, VLMFallbackError



class DummyExtractor(BaseExtractor):
    """Concrete dummy extractor for testing base behavior."""

    async def extract_fares(self, route, travel_date, lead_time_bucket, lead_time_days):
        return []


def test_clean_currency_text_helper():
    """Verify robust sanitization of various currency string formats."""
    assert clean_currency_text("₹ 4,500") == Decimal("4500.00")
    assert clean_currency_text("INR 3,250.50") == Decimal("3250.50")
    assert clean_currency_text("  6,800  ") == Decimal("6800.00")
    assert clean_currency_text("") == Decimal("0.00")
    assert clean_currency_text(None) == Decimal("0.00")


def test_indigo_search_url_formatting():
    """Verify IndiGo search URL constructor."""
    extractor = IndiGoExtractor()
    url = extractor.build_search_url("DEL", "BOM", date(2026, 10, 25))
    assert "origin=DEL" in url
    assert "destination=BOM" in url
    assert "travelDate=2026-10-25" in url


@pytest.mark.asyncio
async def test_anti_bot_detection_raises_blocked_by_site_error():
    """
    When an anti-bot challenge is detected, the extractor MUST NOT attempt
    to defeat it; it must immediately raise BlockedBySiteError.
    """
    extractor = DummyExtractor(source_name="test_portal")

    # Mock Playwright page showing Cloudflare challenge
    mock_page = MagicMock()
    mock_page.title = AsyncMock(return_value="Attention Required! | Cloudflare")
    mock_page.content = AsyncMock(return_value="<html><body>Please solve the cf-turnstile challenge</body></html>")
    mock_page.url = "https://airline.example.com/search"

    with pytest.raises(BlockedBySiteError) as exc_info:
        await extractor.check_for_anti_bot_challenges(mock_page)

    assert "Blocked by anti-bot challenge on test_portal" in str(exc_info.value)
    assert "indicator" in exc_info.value.details


@pytest.mark.asyncio
async def test_anti_bot_detection_passes_on_normal_page():
    """Normal airline results page should pass without error."""
    extractor = DummyExtractor(source_name="test_portal")

    mock_page = MagicMock()
    mock_page.title = AsyncMock(return_value="Book Cheap Flights - IndiGo")
    mock_page.content = AsyncMock(return_value="<html><body><div class='flight-results'>Flights available</div></body></html>")
    mock_page.url = "https://www.goindigo.in"

    # Should not raise
    await extractor.check_for_anti_bot_challenges(mock_page)


@pytest.mark.asyncio
async def test_vlm_fallback_missing_key_raises_error():
    """If VLM API key is missing, visual fallback raises VLMFallbackError."""
    engine = VLMFallbackEngine()
    engine.settings.vlm_api_key = ""

    with pytest.raises(VLMFallbackError) as exc_info:
        await engine.extract_fares_from_screenshot(
            screenshot_bytes=b"dummy_bytes",
            source_name="indigo",
            route_str="DEL-BOM",
        )

    assert "VLM API key not configured" in str(exc_info.value)


@pytest.mark.asyncio
async def test_vlm_fallback_parses_json_and_logs_repair(tmp_path: Path):
    """
    Verify VLM visual fallback engine sends screenshot, parses JSON,
    and logs the selector repair event to file.
    """
    repair_file = tmp_path / "repaired_selectors.json"
    engine = VLMFallbackEngine(repair_log_path=repair_file)
    engine.settings.vlm_api_key = "test-key"

    fake_vlm_response = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "flights": [
                                {
                                    "flight_number": "6E-501",
                                    "departure_time": "08:15",
                                    "base_fare": 3400,
                                    "taxes": 450,
                                    "udf": 150,
                                    "convenience_fee": 200,
                                }
                            ]
                        }
                    )
                }
            }
        ]
    }

    mock_http_response = MagicMock()
    mock_http_response.status_code = 200
    mock_http_response.json.return_value = fake_vlm_response
    mock_http_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient.post", AsyncMock(return_value=mock_http_response)):
        results = await engine.extract_fares_from_screenshot(
            screenshot_bytes=b"fake_jpeg",
            source_name="indigo",
            route_str="DEL-BOM",
        )

    assert len(results) == 1
    assert results[0]["flight_number"] == "6E-501"
    assert results[0]["base_fare"] == 3400

    # Verify repair event was logged to disk
    assert repair_file.exists()
    repair_logs = json.loads(repair_file.read_text(encoding="utf-8"))
    assert len(repair_logs) == 1
    assert repair_logs[0]["source"] == "indigo"
    assert repair_logs[0]["needs_human_selector_review"] is True


@pytest.mark.asyncio
async def test_polite_delay_execution():
    """Verify polite_delay invokes sleep within configured bounds."""
    extractor = DummyExtractor(source_name="test_portal")
    extractor.settings.scraping.polite_delay_min_seconds = 0.01
    extractor.settings.scraping.polite_delay_max_seconds = 0.02

    with patch("asyncio.sleep", AsyncMock()) as mock_sleep:
        await extractor.polite_delay()
        assert mock_sleep.called
        delay_arg = mock_sleep.call_args[0][0]
        assert 0.01 <= delay_arg <= 0.02


def test_spicejet_search_url_formatting():
    """Verify SpiceJet search URL constructor."""
    extractor = SpiceJetExtractor()
    url = extractor.build_search_url("DEL", "BOM", date(2026, 10, 25))
    assert "from=DEL" in url
    assert "to=BOM" in url
    assert "departureDate=2026-10-25" in url


def test_spicejet_extractor_initialization_and_selectors():
    """Verify SpiceJet extractor loads selectors from spicejet.yaml."""
    extractor = SpiceJetExtractor()
    assert extractor.source_name == "spicejet"
    assert extractor.selector_config is not None
    assert "flight_card" in extractor.selector_config.selectors
    assert "base_fare" in extractor.selector_config.selectors
    assert "total_fare" in extractor.selector_config.selectors


def test_orchestrator_get_extractor_for_source():
    """Verify Orchestrator factory creates correct extractor subclasses."""
    from apix.agents.orchestrator import Orchestrator

    indigo = Orchestrator._get_extractor_for_source("indigo")
    assert isinstance(indigo, IndiGoExtractor)
    assert indigo.source_name == "indigo"

    spicejet = Orchestrator._get_extractor_for_source("spicejet")
    assert isinstance(spicejet, SpiceJetExtractor)
    assert spicejet.source_name == "spicejet"

    with pytest.raises(ValueError, match="Unknown extractor source"):
        Orchestrator._get_extractor_for_source("unknown_airline")


@pytest.mark.asyncio
async def test_spicejet_vlm_fallback_payload_generation():
    """Verify SpiceJet extractor converts VLM extracted flights to RawFarePayload."""
    extractor = SpiceJetExtractor()
    fake_screenshot = b"fake_screenshot"
    fake_flights = [
        {
            "flight_number": "SG-8169",
            "base_fare": 4200,
            "taxes": 650,
            "udf": 120,
            "convenience_fee": 250,
        }
    ]
    extractor.vlm_engine.extract_fares_from_screenshot = AsyncMock(return_value=fake_flights)

    route = RouteConfig(origin="DEL", destination="BOM", name="DEL-BOM", tier="metro")
    payloads = await extractor._extract_via_vlm(
        screenshot=fake_screenshot,
        route=route,
        travel_date=date(2026, 10, 25),
        lead_time_bucket="T+7",
        lead_time_days=7,
    )
    assert len(payloads) == 1
    assert payloads[0].airline == "SpiceJet"
    assert payloads[0].flight_number == "SG-8169"
    assert payloads[0].fare_components.base_fare == Decimal("4200")
    assert payloads[0].source == "spicejet"
    assert payloads[0].raw_metadata == {"vlm_fallback": True}

