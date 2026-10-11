# REQUIREMENTS: Airfare Price Index (APIx)

## Milestone 1: Core Pipeline (MVP)

### R1: Orchestration & Scheduling
- **R1.1**: Daily scheduler triggers at 00:00 UTC via cron or APScheduler
- **R1.2**: Reads DGCA city-pair list and advance-purchase windows (T+1, T+7, T+15, T+30, T+45) from YAML config
- **R1.3**: Builds job list (city-pair × window × source) and dispatches in parallel with configurable concurrency limit
- **R1.4**: Retry failed/blocked jobs with exponential backoff + jitter, rotating proxy from pool
- **R1.5**: After N attempts (configurable), record job as failed and continue — one bad job never blocks the run
- **R1.6**: Writes run summary at completion: jobs planned / succeeded / retried / failed

### R2: Data Extraction
- **R2.1**: BaseExtractor interface with shared contract; each source is one module
- **R2.2**: Playwright (async) for browser automation — load search results, read raw fares
- **R2.3**: CSS/XPath selectors stored in YAML config (per source), not hardcoded
- **R2.4**: Self-healing: when selectors fail, screenshot + VLM fallback to locate fare components (Base Fare, Taxes, UDF, Convenience Fee)
- **R2.5**: Cache repaired selectors, log repair events, flag for human review
- **R2.6**: Respectful scraping: honor rate limits, add polite delays, check robots.txt/ToS, prefer APIs
- **R2.7**: On CAPTCHA/challenge: back off, log, mark job "blocked" — never attempt to defeat it
- **R2.8**: Output raw timestamped JSON: source, route, travel_date, lead_time_bucket, airline, flight_number, fare_components, currency, scraped_at, scraper_version
- **R2.9**: Initial extractors: IndiGo (one working extractor for MVP)

### R3: Harmonization & Anomaly Detection
- **R3.1**: Validate raw payloads with Pydantic models
- **R3.2**: Split base fare from taxes, UDF, and convenience charges — consistent definition: base fare + mandatory taxes only
- **R3.3**: Deduplicate across sources and runs
- **R3.4**: Flag anomalies with reason codes (never silently drop):
  - Implausible fares (e.g., ₹100 on a metro route)
  - Sold-out / unavailable flights
  - Sudden jumps vs. route's rolling median (robust z-score / MAD or IQR)
  - Missing fare components
- **R3.5**: Anomaly records stored with reason code for analyst audit

### R4: Quant & Indexing
- **R4.1**: Pull cleaned fares from database
- **R4.2**: Weight city pairs by DGCA passenger-traffic volumes (versioned CSV)
- **R4.3**: Normalize to base period = 100, compute APIx at daily/weekly/monthly frequency
- **R4.4**: Compute lead-time elasticity curves (price level + volatility from T+45 to T+1)
- **R4.5**: Methodology in one well-documented module with unit tests using hand-checkable examples
- **R4.6**: Formula and weighting choices explicit in comments

### R5: API Serving
- **R5.1**: PostgreSQL persistence via SQLAlchemy 2.x + Alembic migrations
- **R5.2**: FastAPI endpoints:
  - `GET /index?freq=daily|weekly|monthly&from=&to=`
  - `GET /index/routes/{origin}-{destination}`
  - `GET /elasticity`
  - `GET /health`
- **R5.3**: Pydantic response models with auto-generated OpenAPI docs
- **R5.4**: Response time < 500ms for typical queries

### R6: Dashboard
- **R6.1**: Lightweight interactive dashboard (Streamlit or Plotly Dash)
- **R6.2**: Sector-wise heatmaps
- **R6.3**: APIx trend lines
- **R6.4**: Lead-time elasticity curves

### R7: Infrastructure & Quality
- **R7.1**: Docker + docker-compose for entire stack
- **R7.2**: Structured JSON logging throughout
- **R7.3**: `.env.example` with placeholder values — no secrets committed
- **R7.4**: pytest test suite; tests for harmonizer outlier logic and index formula
- **R7.5**: README covering: what APIx is, local setup, adding a new source, index methodology, known limitations

## UAT Criteria

| Requirement | Verification |
|-------------|-------------|
| R1.1–R1.6 | Orchestrator runs, dispatches jobs, retries on simulated failure, produces run summary |
| R2.1–R2.9 | BaseExtractor passes type check; IndiGo extractor produces valid raw JSON for a test route |
| R3.1–R3.5 | Harmonizer validates payloads, splits fares correctly, flags known anomalies in test data |
| R4.1–R4.6 | Index computation matches hand-calculated example; elasticity curves produced |
| R5.1–R5.4 | API endpoints return correct data; OpenAPI docs generated; health check passes |
| R6.1–R6.4 | Dashboard renders heatmap, trend lines, and elasticity charts with sample data |
| R7.1–R7.5 | Docker stack starts; tests pass; README is followable by a new developer |
