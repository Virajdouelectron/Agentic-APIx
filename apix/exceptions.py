"""
Domain-specific exceptions for the APIx pipeline.

Raised across extraction, harmonization, and indexing modules to signal
meaningful operational and business events without swallowing stack traces.
"""

from __future__ import annotations

from typing import Any, Optional


class APIxError(Exception):
    """Base exception for all APIx pipeline errors."""

    def __init__(self, message: str, details: Optional[dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def __str__(self) -> str:
        if self.details:
            return f"{self.message} | details={self.details}"
        return self.message


class BlockedBySiteError(APIxError):
    """
    Raised when an airline portal or OTA triggers anti-bot measures,
    such as CAPTCHA, Cloudflare challenge, or HTTP 403 / 429 block.
    
    Instructs the Orchestrator to back off, rotate IP proxy, or re-route.
    """
    pass


class SelectorNotFoundError(APIxError):
    """
    Raised when a configured CSS/XPath selector fails to match any DOM node.
    
    Serves as the trigger for the Vision-Language Model (VLM) visual fallback.
    """
    pass


class InvalidFareError(APIxError):
    """
    Raised when extracted fare payload contains fundamentally invalid values
    (e.g., negative amounts, empty components, unparseable currency).
    """
    pass


class ExtractionTimeoutError(APIxError):
    """
    Raised when page navigation, selector wait, or network idle exceeds deadline.
    """
    pass


class VLMFallbackError(APIxError):
    """
    Raised when the Vision-Language Model fails to locate fare components,
    returns invalid JSON, or times out during visual fallback.
    """
    pass


class ConfigError(APIxError):
    """Raised when configuration files or environment settings are invalid."""
    pass
