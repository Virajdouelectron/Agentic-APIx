"""
APIx Agents package.

Houses the 5 cooperating agents:
1. Orchestrator: Scheduler, job dispatcher, proxy rotator, retry manager
2. Extraction: BaseExtractor, source-specific scrapers, VLM self-healing fallback
3. Harmonizer: Pydantic validator, fare decomposition, deduplication, anomaly detection
4. Indexer: DGCA-weighted economic price indexer and lead-time elasticity modeling
5. Server: FastAPI application and serving endpoints
"""
