"""
Unit and integration tests for Phase 7 Dashboard components.

Tests:
- API health check handling
- Data loader functions (index, route index, elasticity, sector matrix)
- Graceful mock generation when backend is offline
- Real backend response contract parsing (reading 'data' key and reconciling fields)
- KPI metrics dynamic computation and live vs synthetic flagging
- Dev dependencies verification in pyproject.toml
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pandas as pd
import pytest

from apix.dashboard.data_loader import (
    check_api_health,
    fetch_elasticity_data,
    fetch_index_data,
    fetch_kpi_metrics,
    fetch_route_index,
    generate_mock_elasticity,
    generate_mock_index_timeseries,
    generate_mock_sector_matrix,
)


def test_api_health_check_handles_unreachable():
    """Verify health checker returns unreachable dictionary when host is invalid."""
    result = check_api_health(base_url="http://127.0.0.1:59999", timeout=0.5)
    assert result["status"] == "unreachable"
    assert "error" in result


def test_mock_index_timeseries_daily_weekly_monthly():
    """Verify mock time series generates expected frequencies and columns."""
    daily = generate_mock_index_timeseries(freq="daily", days=10)
    assert not daily.empty
    assert "observation_date" in daily.columns
    assert "period_date" in daily.columns
    assert "index_value" in daily.columns
    assert "base_period" in daily.columns
    assert "base_value" in daily.columns
    assert len(daily) > 5

    weekly = generate_mock_index_timeseries(freq="weekly", days=30)
    assert not weekly.empty
    assert len(weekly) < len(generate_mock_index_timeseries(freq="daily", days=30))

    monthly = generate_mock_index_timeseries(freq="monthly", days=60)
    assert not monthly.empty


def test_sector_matrix_dimensions_and_null_diagonal():
    """Verify sector matrix is 6x6 with null diagonal (no intra-city flights)."""
    matrix = generate_mock_sector_matrix()
    assert isinstance(matrix, pd.DataFrame)
    assert matrix.shape == (6, 6)

    # Diagonal must be NaN (e.g., DEL-DEL, BOM-BOM)
    for airport in matrix.index:
        assert pd.isna(matrix.loc[airport, airport])

    # Off-diagonal should be numeric positive fares
    assert matrix.loc["DEL", "BOM"] > 1000
    assert matrix.loc["BLR", "DEL"] > 1000


def test_elasticity_data_structure():
    """Verify elasticity curves generate all lead-time intervals."""
    df = generate_mock_elasticity()
    assert not df.empty
    assert "lead_time_days" in df.columns
    assert "median_fare" in df.columns
    assert "std_dev" in df.columns
    assert "stddev" in df.columns
    assert "iqr" in df.columns
    assert set([1, 3, 7, 14, 21, 30]).issubset(set(df["lead_time_days"]))

    # Test single route filter
    df_single = generate_mock_elasticity(route="DEL-BOM")
    assert all(df_single["route"] == "DEL-BOM")


def test_fetch_functions_fallback_when_server_offline():
    """Verify fetch functions gracefully fall back to mock data if backend unavailable."""
    bad_url = "http://127.0.0.1:59999"

    idx_df = fetch_index_data(base_url=bad_url)
    assert not idx_df.empty
    assert "index_value" in idx_df.columns
    assert "observation_date" in idx_df.columns

    route_df = fetch_route_index("DEL", "BOM", base_url=bad_url)
    assert not route_df.empty

    elast_df = fetch_elasticity_data(base_url=bad_url)
    assert not elast_df.empty


def test_fetch_index_data_real_backend_schema_parsing(monkeypatch):
    """
    Test that fetch_index_data() correctly reads from the 'data' key of
    FastAPI's IndexResponse schema and reconciles field names.
    """
    mock_payload = {
        "count": 2,
        "base_period": "2026-01",
        "data": [
            {
                "observation_date": "2026-10-01",
                "frequency": "daily",
                "route": "ALL",
                "index_value": 104.2,
                "base_period": "2026-01",
                "passenger_weight_version": "v1",
                "sample_size": 250,
            },
            {
                "observation_date": "2026-10-02",
                "frequency": "daily",
                "route": "ALL",
                "index_value": 105.1,
                "base_period": "2026-01",
                "passenger_weight_version": "v1",
                "sample_size": 260,
            },
        ],
    }

    mock_resp = httpx.Response(200, json=mock_payload, request=httpx.Request("GET", "http://mock-api/index"))
    monkeypatch.setattr(httpx.Client, "get", lambda *args, **kwargs: mock_resp)

    df = fetch_index_data(base_url="http://mock-api")
    assert not df.empty
    assert len(df) == 2
    # Verify canonical schema fields
    assert "observation_date" in df.columns
    assert "index_value" in df.columns
    assert "base_period" in df.columns
    assert "passenger_weight_version" in df.columns
    # Verify reconciled backward-compatible aliases
    assert "period_date" in df.columns
    assert "base_value" in df.columns
    assert "weight_version" in df.columns

    assert df.iloc[0]["index_value"] == 104.2
    assert df.iloc[1]["index_value"] == 105.1


def test_fetch_elasticity_data_real_backend_schema_parsing(monkeypatch):
    """
    Test that fetch_elasticity_data() correctly reads from the 'data' key of
    FastAPI's ElasticityResponse schema and reconciles std_dev / stddev.
    """
    mock_payload = {
        "count": 1,
        "data": [
            {
                "route": "DEL-BOM",
                "lead_time_days": 7,
                "lead_time_bucket": "7d",
                "mean_fare": 5200.0,
                "median_fare": 5100.0,
                "std_dev": 350.0,
                "iqr": 450.0,
                "sample_size": 50,
            }
        ],
    }

    mock_resp = httpx.Response(200, json=mock_payload, request=httpx.Request("GET", "http://mock-api/elasticity"))
    monkeypatch.setattr(httpx.Client, "get", lambda *args, **kwargs: mock_resp)

    df = fetch_elasticity_data(base_url="http://mock-api")
    assert not df.empty
    assert len(df) == 1
    # Check canonical schema name and alias
    assert "std_dev" in df.columns
    assert "stddev" in df.columns
    assert df.iloc[0]["std_dev"] == 350.0
    assert df.iloc[0]["stddev"] == 350.0
    assert df.iloc[0]["median_fare"] == 5100.0
    assert df.iloc[0]["route"] == "DEL-BOM"


def test_fetch_kpi_metrics_synthetic_and_computation():
    """Verify fetch_kpi_metrics calculates values dynamically and flags synthetic mode."""
    metrics = fetch_kpi_metrics(base_url="http://127.0.0.1:59999")
    assert isinstance(metrics, dict)
    assert "current_index" in metrics
    assert "routes_monitored" in metrics
    assert "weighted_avg_fare" in metrics
    assert "anomaly_filter_rate" in metrics
    assert "is_live" in metrics
    assert metrics["is_live"] is False
    assert metrics["routes_monitored"] > 0
    assert metrics["weighted_avg_fare"] > 0


def test_aiosqlite_in_dev_optional_dependencies():
    """
    Verify that aiosqlite is explicitly included in [project.optional-dependencies].dev
    so that `pip install -e '.[dev]'` installs test dependencies correctly.
    """
    pyproject_path = Path(__file__).resolve().parent.parent / "pyproject.toml"
    with open(pyproject_path, "rb") as f:
        config = tomllib.load(f)

    dev_deps = config.get("project", {}).get("optional-dependencies", {}).get("dev", [])
    has_aiosqlite = any("aiosqlite" in dep for dep in dev_deps)
    assert has_aiosqlite, f"aiosqlite missing from dev dependencies: {dev_deps}"
