"""
Centralized settings for the APIx pipeline.

Loads environment variables via pydantic-settings, then reads YAML config files
for routes, windows, and selectors. One import gives you everything:

    from apix.config.settings import get_settings
    settings = get_settings()
    print(settings.routes)        # list of RouteConfig
    print(settings.windows)       # list of WindowConfig
    print(settings.concurrency_limit)  # int, from env or default
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# ── Resolve paths relative to this file ──────────────────────────
_CONFIG_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _CONFIG_DIR.parent.parent


# ── Typed config models (not DB models — just structured config) ─
from pydantic import BaseModel


class RouteConfig(BaseModel):
    """A single city-pair route from the DGCA list."""
    origin: str = Field(description="IATA code for origin airport")
    destination: str = Field(description="IATA code for destination airport")
    name: str = Field(description="Human-readable route label")
    tier: str = Field(description="'metro' or 'regional' — affects anomaly thresholds")


class WindowConfig(BaseModel):
    """An advance-purchase lead-time window."""
    label: str = Field(description="Display label like 'T+7'")
    days: int = Field(ge=1, description="Days before departure to search")


class ScrapingConfig(BaseModel):
    """Thresholds that govern extraction behaviour."""
    concurrency_limit: int = 5
    max_retries: int = 3
    backoff_base_seconds: float = 2.0
    backoff_jitter_max_seconds: float = 1.5
    polite_delay_min_seconds: float = 1.0
    polite_delay_max_seconds: float = 3.0
    page_timeout_seconds: int = 30
    blocked_cooldown_seconds: int = 60


class SelectorEntry(BaseModel):
    """A single CSS/XPath selector for a fare component or flight field."""
    type: str = Field(description="'css' or 'xpath'")
    value: str = Field(description="The selector string")
    description: str = ""
    fallback_enabled: bool = False


class SourceSelectors(BaseModel):
    """All selectors for one extraction source."""
    source: str
    base_url: str
    selectors: dict[str, SelectorEntry]
    vlm_prompts: dict[str, str] = {}


# ── Main settings class ─────────────────────────────────────────
class Settings(BaseSettings):
    """
    App-wide settings merged from .env and YAML config files.

    Environment variables override defaults. YAML files (routes, windows,
    selectors) are loaded once at startup and cached.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Database ─────────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://apix:changeme@localhost:5432/apix"

    # ── Proxy ────────────────────────────────────────────────────
    proxy_pool_url: str = ""

    # ── VLM (fallback for broken selectors) ──────────────────────
    vlm_api_key: str = ""
    vlm_api_url: str = "https://api.openai.com/v1/chat/completions"
    vlm_model: str = "gpt-4o"

    # ── Application ──────────────────────────────────────────────
    log_level: str = "INFO"
    concurrency_limit: int = 5
    max_retries: int = 3
    backoff_base_seconds: float = 2.0
    polite_delay_min: float = 1.0
    polite_delay_max: float = 3.0

    # ── FastAPI ──────────────────────────────────────────────────
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # ── Loaded from YAML (not env vars) ──────────────────────────
    # These are populated by _load_yaml_configs() after init.
    routes: list[RouteConfig] = []
    windows: list[WindowConfig] = []
    scraping: ScrapingConfig = ScrapingConfig()
    source_selectors: dict[str, SourceSelectors] = {}

    def model_post_init(self, __context: Any) -> None:
        """Load YAML configs after env vars are resolved."""
        self._load_yaml_configs()

    def _load_yaml_configs(self) -> None:
        """Read routes, windows, and selector YAML files into typed models."""
        # Routes
        if not self.routes:
            routes_path = _CONFIG_DIR / "routes.yaml"
            if routes_path.exists():
                with open(routes_path) as f:
                    data = yaml.safe_load(f)
                self.routes = [RouteConfig(**r) for r in data.get("routes", [])]

        # Windows + scraping thresholds
        if not self.windows:
            windows_path = _CONFIG_DIR / "windows.yaml"
            if windows_path.exists():
                with open(windows_path) as f:
                    data = yaml.safe_load(f)
                self.windows = [WindowConfig(**w) for w in data.get("windows", [])]
                if "scraping" in data and not hasattr(self, "_scraping_custom_set"):
                    self.scraping = ScrapingConfig(**data["scraping"])

        # Per-source selectors (one YAML per source in selectors/ dir)
        selectors_dir = _CONFIG_DIR / "selectors"
        if selectors_dir.is_dir():
            for yaml_file in selectors_dir.glob("*.yaml"):
                with open(yaml_file) as f:
                    data = yaml.safe_load(f)
                if data and "source" in data:
                    raw_selectors = data.get("selectors", {})
                    parsed = {
                        name: SelectorEntry(**entry)
                        for name, entry in raw_selectors.items()
                    }
                    source = SourceSelectors(
                        source=data["source"],
                        base_url=data.get("base_url", ""),
                        selectors=parsed,
                        vlm_prompts=data.get("vlm_prompts", {}),
                    )
                    self.source_selectors[data["source"]] = source


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Return the global settings singleton.

    Cached so YAML files are only read once. Call get_settings.cache_clear()
    if you need to reload (e.g., in tests).
    """
    return Settings()
