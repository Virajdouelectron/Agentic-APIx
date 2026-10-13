# APIx — Indian Domestic Airfare Price Index

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.104+-green.svg)](https://fastapi.tiangolo.com)
[![Streamlit](https://img.shields.io/badge/Streamlit-1.29+-red.svg)](https://streamlit.io)
[![Playwright](https://img.shields.io/badge/Playwright-Async-orange.svg)](https://playwright.dev/python/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**APIx** is a multi-agent data collection, validation, and economic indexing pipeline that continuously tracks, harmonizes, and computes price indexes for Indian domestic commercial aviation.

Operating on a Laspeyres-style weighted formula normalized by official Directorate General of Civil Aviation (DGCA) city-pair passenger traffic shares, APIx computes national composite indexes, route-level benchmarks, and lead-time elasticity curves while filtering pricing anomalies and bot blocks.

---

## Architecture Overview

APIx is architected as 5 cooperating agents executing in decoupled stages:

```mermaid
graph TD
    A[Orchestrator Agent] -->|Matrix of Routes x Windows| B[Extraction Agent]
    B -->|Playwright Headless Chrome| C[Airline / OTA Pages]
    B -.->|CSS Selector Drift Fallback| D[Vision-Language Model VLM]
    B -->|Raw Fare Payloads| E[Harmonizer Agent]
    E -->|IQR / MAD Anomaly Detection & Normalization| F[(PostgreSQL Database)]
    F --> G[Quant & Indexing Agent]
    G -->|DGCA-Weighted WAF & Elasticity Curves| F
    F --> H[FastAPI Serving Backend]
    F --> I[Streamlit Interactive Dashboard]
```

### The 5 Cooperating Agents

1. **Orchestrator Agent** (`apix/agents/orchestrator.py`):
   - Generates the daily scraping job matrix across 30 top routes and 6 lead-time windows (1, 3, 7, 14, 21, 30 days).
   - Manages an IP rotation proxy pool with gateway session nonces.
   - Fault-tolerant exponential backoff with jitter and per-route polite delays.
2. **Extraction Agent** (`apix/agents/extraction/`):
   - Asynchronous Playwright browser automation with anti-bot evasion techniques.
   - Dual-path parser: Fast CSS/XPath selector extraction across IndiGo and SpiceJet, with automated VLM (GPT-4o/Gemini) visual self-healing fallback when DOM changes.

3. **Harmonizer Agent** (`apix/agents/harmonizer.py`):
   - Strict Pydantic v2 validation enforcing economic fare definitions (Base Fare + Mandatory Fuel/Airport Fees; optional OTA markups excluded).
   - Cross-source deduplication preserving cheapest fares per flight code.
   - Robust non-dropping anomaly detection using Median Absolute Deviation (MAD) and Interquartile Range (IQR). Flagged anomalies are audited, not discarded.
4. **Quant & Indexing Agent** (`apix/agents/indexer.py`):
   - DGCA passenger-weighted average fare (WAF) aggregation.
   - Base-period Laspeyres normalization (Base Period = 100.0).
   - Daily, weekly, and monthly index time series.
   - Booking horizon lead-time elasticity modeling.
5. **Serving & Dashboard Agents** (`apix/agents/server.py` & `apix/dashboard/app.py`):
   - High-performance asynchronous FastAPI REST backend (<500ms response time).
   - Streamlit glassmorphic dark-theme executive dashboard featuring sector heatmaps, index trends, and elasticity curves.

---

## Quickstart (Local Development)

### Prerequisites
- Python 3.11+
- [uv](https://github.com/astral-sh/uv) or `pip`
- Docker & Docker Compose (optional, for containerized run)

### 1. Clone & Set Up Environment

```bash
git clone https://github.com/Virajdouelectron/Agentic-APIx.git
cd Agentic-APIx

# Create virtual environment and install dependencies
uv venv
source .venv/bin/activate   # On Windows: .venv\Scripts\activate
uv pip install -e ".[dev]"

# Install Playwright browser binaries
playwright install chromium
```

### 2. Configure Environment Variables

Copy `.env.example` to `.env` and adjust settings as needed:

```bash
cp .env.example .env
```

```ini
DATABASE_URL=postgresql+asyncpg://apix:changeme@localhost:5432/apix
PROXY_POOL_URL=http://user:pass@proxy.example.com:8080
VLM_API_KEY=your-vlm-api-key-here
LOG_LEVEL=INFO
API_HOST=0.0.0.0
API_PORT=8000
```

### 3. Run Database Migrations

Apply Alembic migrations to set up PostgreSQL schema:

```bash
alembic upgrade head
```

### 4. Run the Full Test Suite

```bash
pytest -v
```

All 65 unit and integration tests run in-memory without requiring external services.

---

## Running with Docker Compose (R7.1)

To bring up the entire stack (PostgreSQL, FastAPI, and Streamlit Dashboard) with a single command:

```bash
docker compose up --build
```

### Available Services

| Service | Port | Description |
|---------|------|-------------|
| **FastAPI Backend** | `http://localhost:8000` | REST API with interactive Swagger docs at `/docs` |
| **Streamlit Dashboard** | `http://localhost:8501` | Interactive visual dashboard with heatmaps & trend lines |
| **PostgreSQL Database** | `localhost:5432` | Relational data store |

---

## API Endpoints (R5.2)

The FastAPI server provides the following endpoints:

| Method | Endpoint | Query Parameters | Description |
|--------|----------|------------------|-------------|
| `GET` | `/health` | — | System health check and database connectivity verification |
| `GET` | `/index` | `freq` (daily/weekly/monthly), `from`, `to` | National composite airfare price index time series |
| `GET` | `/index/routes/{origin}-{destination}` | `from`, `to` | Route-specific index series (e.g. `/index/routes/DEL-BOM`) |
| `GET` | `/elasticity` | `route` (optional) | Lead-time price elasticity curves (1d, 3d, 7d, 14d, 21d, 30d) |

OpenAPI documentation is available at `http://localhost:8000/docs` and `http://localhost:8000/redoc`.

---

## Index Methodology (R4.1–R4.6)

### 1. Fare Definition
For index purity, APIx defines the **Index Fare** strictly as:
$$\text{Index Fare} = \text{Base Fare} + \text{Mandatory Fuel Surcharge (YQ)} + \text{Passenger Service / Airport Fees}$$
Optional ancillaries (seat selection, excess baggage, convenience fees, meals) are excluded to ensure true apples-to-apples economic comparison across airlines and OTAs.

### 2. Weighted Average Fare (WAF)
The daily Weighted Average Fare is computed using fixed DGCA passenger traffic volume weights:
$$\text{WAF}_t = \sum_{r \in \text{Routes}} w_r \cdot \bar{P}_{r, t}$$
where $w_r$ is route $r$'s share of total domestic traffic ($\sum w_r = 1.0$), and $\bar{P}_{r, t}$ is the median non-anomalous fare on route $r$ for travel date $t$.

### 3. Laspeyres-Style Normalization
Index points are expressed relative to a base period (January 2026 = 100.0):
$$\text{APIx}_t = \left( \frac{\text{WAF}_t}{\text{WAF}_{\text{base}}} \right) \times 100.0$$

### 4. Lead-Time Elasticity
For each booking window $L \in \{1, 3, 7, 14, 21, 30\}$, APIx aggregates fare distributions (median, mean, standard deviation, interquartile range) to capture dynamic yield management price surges.

---

## Adding a New Scraper / Source (R7.5)

To add a new airline or OTA source (e.g., Air India Express):

1. **Create Extractor Class**:
   Create `apix/agents/extraction/airindia_express.py` subclassing `BaseExtractor`:

   ```python
   from apix.agents.extraction.base import BaseExtractor
   from apix.models.schemas import RawFarePayload
   from datetime import date
   from typing import List

   class AirIndiaExpressExtractor(BaseExtractor):
       source_name = "airindia_express"

       async def search_fares(self, origin: str, destination: str, travel_date: date) -> List[RawFarePayload]:
           page = await self.new_page()
           url = f"https://www.airindiaexpress.com/search?from={origin}&to={destination}&date={travel_date.isoformat()}"
           await self.navigate_safely(page, url)
           # Extract fares using self.selectors or self.vlm_fallback
           ...
           return fares
   ```

2. **Add Selectors**:
   Define CSS / XPath selectors in `apix/config/selectors/airindia_express.yaml`.

3. **Register in Orchestrator**:
   Import and register the extractor inside `apix/agents/orchestrator.py` in the `_get_extractor_for_source` factory.

---

## Known Limitations & Production Notes

- **Headless Browser Fingerprinting**: Aggressive anti-bot systems (Cloudflare Turnstile, Kasada, Akamai) may occasionally challenge headless sessions. Production deployments should route through residential proxy pools with TLS fingerprint impersonation.
- **VLM API Rate Limits**: VLM fallback provides self-healing resilience when DOM selectors break, but consumes multimodal LLM tokens. It is calibrated as a degraded-mode safety net rather than the primary extraction path.
- **Base Period Recalibration**: Under DGCA schedule revisions (typically bi-annual Summer/Winter schedules), route traffic weights should be updated by versioning the weights configuration matrix (`DGCA_2025_Q1` ➔ `DGCA_2025_Q2`).

---

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.
