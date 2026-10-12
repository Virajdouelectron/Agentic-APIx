# ROADMAP: Airfare Price Index (APIx)

## Milestone 1: Core Pipeline (MVP)

### Phase 1: Foundation — Git, Config, Data Models
**Requirements**: R7.3, R7.5 (partial), R1.2, R2.3, R3.1
**Deliverables**:
- Git setup: `.gitignore`, feature branch, remote
- Project structure and `pyproject.toml`
- YAML config files: `routes.yaml`, `windows.yaml`, selector configs
- `settings.py` — centralized settings from env + YAML
- Pydantic schemas (`schemas.py`) for raw payloads, cleaned fares, anomalies, API responses
- SQLAlchemy models (`db_models.py`) for fares, index values, anomalies
- `.env.example`, `docker-compose.yml`
- DGCA weights CSV (sample data)
**Status**: not-started

### Phase 2: Extraction Layer
**Requirements**: R2.1–R2.9
**Deliverables**:
- `BaseExtractor` abstract interface
- Custom exceptions: `BlockedBySiteError`, `SelectorNotFoundError`, `InvalidFareError`
- IndiGo extractor (first working source)
- VLM fallback utility module
- Per-source selector YAML configs
**Status**: not-started

### Phase 3: Orchestrator
**Requirements**: R1.1–R1.6
**Deliverables**:
- `orchestrator.py` — scheduler, job dispatch, parallel execution with concurrency control
- Retry logic with exponential backoff + jitter
- Proxy rotation integration
- Run summary generation
**Status**: not-started

### Phase 4: Harmonization & Anomaly Detection
**Requirements**: R3.1–R3.5
**Deliverables**:
- `harmonizer.py` — validation, fare decomposition, dedup, anomaly flagging
- Anomaly detection rules (IQR/MAD, implausibility, missing components)
- Unit tests for outlier logic (`test_harmonizer.py`)
**Status**: not-started

### Phase 5: Quant & Indexing
**Requirements**: R4.1–R4.6
**Deliverables**:
- `indexer.py` — DGCA-weighted index, elasticity curves
- Well-documented methodology with explicit formulas
- Unit tests with hand-checkable examples (`test_indexer.py`)
**Status**: completed

### Phase 6: API Serving
**Requirements**: R5.1–R5.4
**Deliverables**:
- `server.py` — FastAPI app with all endpoints
- Pydantic response models
- Alembic migration setup
- Health endpoint
**Status**: completed


### Phase 7: Dashboard
**Requirements**: R6.1–R6.4
**Deliverables**:
- `dashboard/app.py` — interactive dashboard
- Sector heatmaps, APIx trend lines, elasticity curve charts
**Status**: not-started

### Phase 8: Docker, Tests & README
**Requirements**: R7.1–R7.5
**Deliverables**:
- `Dockerfile`, `docker-compose.yml` (finalized)
- Complete test suite
- README with full documentation
- Final review and PR preparation
**Status**: not-started
