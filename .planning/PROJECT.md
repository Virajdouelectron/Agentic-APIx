# PROJECT: Airfare Price Index (APIx)

## Vision

A production-grade, multi-agent data pipeline that collects domestic Indian airfare data, harmonizes it into a consistent economic indicator, and serves it to institutions like the National Statistical Office (NSO) and Reserve Bank of India (RBI). APIx is intended as a transparent, auditable, and reproducible price index — not a scraping project, but an economic measurement tool that happens to need web data.

## Problem Statement

India lacks a real-time, standardized airfare price index. Published fare data is fragmented across airline portals and OTAs, each with different fare breakdowns, UX flows, and anti-bot measures. Government bodies that need airfare inflation signals (for CPI components, transport policy, or monetary analysis) currently rely on delayed DGCA reports or ad-hoc surveys. APIx fills this gap with daily automated collection and a rigorous, documented indexing methodology.

## Architecture: Five Cooperating Agents

| Agent | Responsibility | Key Tech |
|-------|---------------|----------|
| **Orchestrator** | Daily scheduler, job dispatch, retry/failover, run summaries | asyncio, APScheduler, YAML config |
| **Extraction** | Per-source browser automation, CSS/XPath selectors with VLM fallback, respectful scraping | Playwright (async), VLM integration |
| **Harmonization & Anomaly** | Validation, fare decomposition, dedup, anomaly flagging | Pydantic v2, pandas |
| **Quant & Indexing** | DGCA-weighted index computation, lead-time elasticity curves | pandas, NumPy |
| **API Serving & Dashboard** | REST API, data persistence, interactive dashboard | FastAPI, SQLAlchemy 2.x, Alembic, PostgreSQL, Plotly/Streamlit |

### Data Flow

```
DGCA city-pair list + advance-purchase windows (config)
        │
        ▼
  Orchestrator ──dispatch──▶ Extraction Agents (parallel, rate-limited)
        │                         │
        │                    raw JSON payloads
        │                         │
        ▼                         ▼
  Run summary              Harmonization Agent
                                  │
                           cleaned, validated fares
                                  │
                                  ▼
                          Quant & Indexing Agent
                                  │
                         APIx (daily/weekly/monthly)
                         + elasticity curves
                                  │
                                  ▼
                        API Serving & Dashboard
                         (FastAPI + PostgreSQL + Plotly)
```

## Key Design Decisions

1. **Fare Definition**: Base fare + mandatory taxes (government-imposed), excluding optional add-ons (seat selection, meals, baggage), convenience fees charged by OTAs, and User Development Fees (UDF — airport-specific, not reflective of airline pricing). This keeps the index comparable across sources. Documented and configurable.

2. **Self-Healing Extractors**: CSS/XPath selectors live in YAML config, not hardcoded. When a selector fails, the extractor takes a screenshot and asks a Vision-Language Model (e.g., GPT-4V, Gemini) to locate fare components. The repaired data is used, cached, and flagged for human review. This buys time when sites change layouts — which they do constantly.

3. **Respectful Scraping**: Rate limits, polite delays, robots.txt compliance. CAPTCHAs and challenges are never defeated — we back off, log, and retry later. Prefer official APIs or partner feeds where available.

4. **Anomaly Flagging, Not Dropping**: Implausible fares, sudden jumps (vs. rolling MAD/IQR), sold-out routes — all flagged with reason codes, never silently dropped. Analysts can audit and override.

5. **DGCA Passenger Weights**: The index weights city pairs by actual traffic volume (DGCA data, versioned CSV). This prevents niche routes from distorting the national index. Weights are updated periodically with clear versioning.

6. **Lead-Time Elasticity**: We collect fares at T+1, T+7, T+15, T+30, T+45 days before departure. This lets us compute how prices and volatility evolve with booking lead time — a dimension most indices miss.

7. **BaseExtractor Pattern**: Each source (IndiGo, MakeMyTrip, Cleartrip, etc.) implements a thin module behind a shared interface. Adding a new source = one new file + one YAML selector config. No framework overhead.

## Tech Stack

- **Language**: Python 3.11+
- **Async**: asyncio (native)
- **Browser Automation**: Playwright (async API)
- **Validation**: Pydantic v2
- **Data Processing**: pandas, NumPy
- **Database**: PostgreSQL via SQLAlchemy 2.x, Alembic for migrations
- **API**: FastAPI with auto-generated OpenAPI docs
- **Dashboard**: Streamlit or Plotly Dash (lightweight, interactive)
- **Testing**: pytest
- **Infrastructure**: Docker + docker-compose
- **Logging**: Python `logging` with structured JSON output
- **Config**: YAML files for routes, windows, selectors; `.env` for secrets

## Project Structure

```
apix/
├── agents/
│   ├── orchestrator.py          # Daily scheduler, job dispatch, retry
│   ├── extraction/
│   │   ├── base.py              # BaseExtractor interface
│   │   ├── indigo.py            # IndiGo extractor
│   │   ├── makemytrip.py        # MakeMyTrip extractor
│   │   └── cleartrip.py         # Cleartrip extractor
│   ├── harmonizer.py            # Validation, fare splitting, anomaly detection
│   ├── indexer.py               # DGCA-weighted index, elasticity curves
│   └── server.py                # FastAPI app, endpoints
├── config/
│   ├── routes.yaml              # DGCA city-pair list
│   ├── windows.yaml             # Advance-purchase windows
│   ├── selectors/               # Per-source CSS/XPath selectors
│   └── settings.py              # Centralized settings (from env + defaults)
├── models/
│   ├── schemas.py               # Pydantic models (API + validation)
│   └── db_models.py             # SQLAlchemy ORM models
├── data/
│   └── dgca_weights.csv         # Passenger traffic weights (versioned)
├── dashboard/
│   └── app.py                   # Streamlit/Plotly dashboard
├── tests/
│   ├── test_harmonizer.py
│   ├── test_indexer.py
│   └── test_schemas.py
├── migrations/                  # Alembic migrations
├── README.md
├── docker-compose.yml
├── Dockerfile
├── pyproject.toml
├── .env.example
└── .gitignore
```

## Constraints & Risks

| Risk | Mitigation |
|------|-----------|
| Airline sites change layouts frequently | Self-healing VLM fallback + selector config, not hardcoded |
| Anti-bot detection (CAPTCHA, IP blocking) | Proxy rotation, polite delays, back-off on detection |
| DGCA weight data is delayed/incomplete | Use latest available; version the CSV; degrade gracefully |
| Fare definition varies across sources | Normalize in harmonizer with explicit mapping per source |
| PostgreSQL availability | Docker-compose manages the DB; health endpoint checks connectivity |
| VLM API availability/cost | VLM is fallback-only; primary path is config selectors |

## Assumptions

1. We have access to a proxy pool (configured via env vars) for IP rotation.
2. VLM API keys are provided via `.env` (not committed).
3. DGCA city-pair and weight data is manually maintained/updated in `data/`.
4. The initial deployment target is a single server with Docker.
5. The index base period will be set to the first full month of collected data = 100.
6. We are building for domestic routes only (India-to-India).

## Success Criteria

- Daily automated collection of fares across 50+ major city pairs, 5 lead-time windows, 3+ sources.
- Harmonized, auditable data with anomaly flags — zero silent data drops.
- A documented, reproducible index methodology that an economist can review.
- REST API serving daily/weekly/monthly index values with <500ms response time.
- Interactive dashboard showing trends, heatmaps, and elasticity curves.
- Adding a new source takes <2 hours and one new file.

## Owner

Viraj (project lead & primary developer)
