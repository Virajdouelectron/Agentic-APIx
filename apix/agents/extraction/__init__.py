"""
Extraction Agents package.

Contains the shared BaseExtractor interface, anti-bot detection logic,
source implementations (e.g. IndiGo, SpiceJet), and VLM visual fallback repair.
"""

from apix.agents.extraction.base import BaseExtractor
from apix.agents.extraction.indigo import IndiGoExtractor
from apix.agents.extraction.spicejet import SpiceJetExtractor
from apix.agents.extraction.vlm_fallback import VLMFallbackEngine

__all__ = [
    "BaseExtractor",
    "IndiGoExtractor",
    "SpiceJetExtractor",
    "VLMFallbackEngine",
]
