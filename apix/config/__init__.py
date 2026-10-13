"""
Config package — centralized settings, route definitions, and scraping parameters.

All config flows through get_settings() which merges .env vars with YAML files.
"""

from apix.config.logging import configure_logging, get_logger
from apix.config.settings import Settings, get_settings

__all__ = ["Settings", "configure_logging", "get_logger", "get_settings"]

