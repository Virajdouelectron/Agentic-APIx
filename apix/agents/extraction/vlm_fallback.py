"""
Vision-Language Model (VLM) visual fallback engine.

When CSS or XPath selectors break due to portal redesigns:
1. Captures a screenshot of the search results page.
2. Sends the image to a multimodal VLM (GPT-4o, Gemini 1.5, etc.) with structured prompts.
3. Parses returned JSON to extract flight numbers and fare components (Base Fare, Taxes, UDF, Convenience Fee).
4. Logs the repair event and appends metadata to data/repaired_selectors.json for human review.
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

import httpx

from apix.config.settings import get_settings
from apix.exceptions import VLMFallbackError
from apix.models.schemas import FareBreakdown

logger = logging.getLogger(__name__)

# Default system instruction for multimodal vision extraction
DEFAULT_VLM_PROMPT = """
You are an expert financial data extraction agent analyzing an Indian airline or travel booking search results screenshot.
Your task is to identify flight options and extract their numerical fare components in Indian Rupees (INR).

For each flight visible in the image, extract:
- "flight_number": e.g., "6E-204" or "AI-805"
- "departure_time": e.g., "06:00"
- "base_fare": numerical amount of the airline base fare (e.g., 3850)
- "taxes": numerical amount of government taxes and fees (e.g., 420)
- "udf": User Development Fee if explicitly broken out, else 0
- "convenience_fee": Booking or convenience charge if visible, else 0

Output strictly a valid JSON object matching this schema:
{
  "flights": [
    {
      "flight_number": "string",
      "departure_time": "string",
      "base_fare": number,
      "taxes": number,
      "udf": number,
      "convenience_fee": number
    }
  ]
}
Do not include markdown codeblocks or conversational text outside the JSON.
"""


class VLMFallbackEngine:
    """
    Multimodal visual fallback agent for resilient self-healing extraction.
    """

    def __init__(self, repair_log_path: Optional[Path] = None) -> None:
        self.settings = get_settings()
        self.repair_log_path = repair_log_path or (
            Path(__file__).resolve().parent.parent.parent / "data" / "repaired_selectors.json"
        )

    async def extract_fares_from_screenshot(
        self,
        screenshot_bytes: bytes,
        source_name: str,
        route_str: str,
        custom_prompt: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """
        Send page screenshot to VLM and extract structured flight fare components.
        """
        api_key = self.settings.vlm_api_key
        api_url = self.settings.vlm_api_url
        model = self.settings.vlm_model

        if not api_key:
            logger.warning(
                "VLM API key is not configured in environment. Visual fallback cannot contact remote model."
            )
            raise VLMFallbackError(
                "VLM API key not configured. Visual fallback unavailable.",
                details={"source": source_name, "route": route_str},
            )

        prompt = custom_prompt or DEFAULT_VLM_PROMPT
        base64_img = base64.b64encode(screenshot_bytes).decode("utf-8")
        data_url = f"data:image/jpeg;base64,{base64_img}"

        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": data_url, "detail": "high"},
                        },
                    ],
                }
            ],
            "max_tokens": 1500,
            "temperature": 0.0,
        }

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        logger.info(
            "Invoking VLM visual fallback for %s on route %s using model %s",
            source_name,
            route_str,
            model,
        )

        try:
            async with httpx.AsyncClient(timeout=45.0) as client:
                response = await client.post(api_url, json=payload, headers=headers)
                response.raise_for_status()
                result = response.json()

            raw_text = (
                result.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
                .strip()
            )

            # Strip markdown codeblocks if model wraps JSON
            if raw_text.startswith("```json"):
                raw_text = raw_text[len("```json") :].strip()
            if raw_text.startswith("```"):
                raw_text = raw_text[len("```") :].strip()
            if raw_text.endswith("```"):
                raw_text = raw_text[:-3].strip()

            parsed = json.loads(raw_text)
            flights_data = parsed.get("flights", [])

            # Record repair audit log
            self._log_repair_event(
                source_name=source_name,
                route_str=route_str,
                flight_count=len(flights_data),
            )

            return flights_data

        except httpx.HTTPError as exc:
            logger.error("VLM API HTTP request failed: %s", exc)
            raise VLMFallbackError(
                f"VLM HTTP error: {exc}", details={"source": source_name, "route": route_str}
            ) from exc
        except (json.JSONDecodeError, KeyError, IndexError) as exc:
            logger.error("Failed to parse VLM visual fallback response: %s", exc)
            raise VLMFallbackError(
                f"Invalid VLM response format: {exc}",
                details={"source": source_name, "route": route_str},
            ) from exc

    def _log_repair_event(
        self, source_name: str, route_str: str, flight_count: int
    ) -> None:
        """
        Record selector failure and VLM repair event for operator review.
        """
        self.repair_log_path.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": source_name,
            "route": route_str,
            "flights_extracted": flight_count,
            "needs_human_selector_review": True,
            "note": "DOM selector failed; VLM visual fallback was invoked to extract fares.",
        }

        try:
            events = []
            if self.repair_log_path.exists():
                with open(self.repair_log_path, "r", encoding="utf-8") as f:
                    try:
                        events = json.load(f)
                    except json.JSONDecodeError:
                        events = []

            events.append(event)
            with open(self.repair_log_path, "w", encoding="utf-8") as f:
                json.dump(events, f, indent=2)

            logger.warning(
                "Logged selector repair event for %s (%s). Flagged for human review in %s",
                source_name,
                route_str,
                self.repair_log_path.name,
            )
        except Exception as exc:
            logger.error("Could not write selector repair log: %s", exc)
