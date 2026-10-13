"""
Unit and integration tests for Phase 7 Dashboard components.

Tests:
- API health check handling
- Data loader functions (index, route index, elasticity, sector matrix)
- Graceful mock generation when backend is offline
- Structure and columns of generated DataFrames for Plotly rendering
"""

from __future__ import annotations

import pandas as pd
import pytest

from apix.dashboard.data_loader import (
    check_api_health,
    fetch_elasticity_data,
    fetch_index_data,
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
    assert "period_date" in daily.columns
    assert "index_value" in daily.columns
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

    route_df = fetch_route_index("DEL", "BOM", base_url=bad_url)
    assert not route_df.empty

    elast_df = fetch_elasticity_data(base_url=bad_url)
    assert not elast_df.empty
