"""
Data connector module for the APIx Streamlit dashboard.

Connects to the APIx FastAPI backend (or generates deterministic synthetic data
when the backend is offline or empty), providing structured DataFrames for:
- System health
- APIx aggregate time series (daily, weekly, monthly)
- Route-specific index time series
- Route sector price matrix for heatmaps
- Lead-time elasticity data
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
import httpx

API_BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8000").rstrip("/")


def check_api_health(base_url: str = API_BASE_URL, timeout: float = 2.0) -> dict[str, Any]:
    """Check backend health via GET /health."""
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(f"{base_url}/health")
            if resp.status_code == 200:
                return resp.json()
            return {"status": "degraded", "http_status": resp.status_code}
    except Exception as exc:
        return {"status": "unreachable", "error": str(exc)}


def fetch_index_data(
    base_url: str = API_BASE_URL,
    freq: str = "daily",
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    timeout: float = 3.0,
) -> pd.DataFrame:
    """
    Fetch APIx index time series.
    Returns a DataFrame with columns: [period_date, index_value, base_value, weight_version].
    """
    params: dict[str, Any] = {"freq": freq}
    if from_date:
        params["from"] = from_date.isoformat()
    if to_date:
        params["to"] = to_date.isoformat()

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(f"{base_url}/index", params=params)
            if resp.status_code == 200:
                items = resp.json().get("items", [])
                if items:
                    df = pd.DataFrame(items)
                    df["period_date"] = pd.to_datetime(df["period_date"])
                    return df.sort_values("period_date").reset_index(drop=True)
    except Exception:
        pass

    # Fallback: Generate realistic demo time series
    return generate_mock_index_timeseries(freq=freq, from_date=from_date, to_date=to_date)


def fetch_route_index(
    origin: str,
    destination: str,
    base_url: str = API_BASE_URL,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    timeout: float = 3.0,
) -> pd.DataFrame:
    """Fetch route-specific index time series."""
    params: dict[str, Any] = {}
    if from_date:
        params["from"] = from_date.isoformat()
    if to_date:
        params["to"] = to_date.isoformat()

    try:
        with httpx.Client(timeout=timeout) as client:
            url = f"{base_url}/index/routes/{origin.upper()}-{destination.upper()}"
            resp = client.get(url, params=params)
            if resp.status_code == 200:
                items = resp.json().get("items", [])
                if items:
                    df = pd.DataFrame(items)
                    df["period_date"] = pd.to_datetime(df["period_date"])
                    return df.sort_values("period_date").reset_index(drop=True)
    except Exception:
        pass

    return generate_mock_route_index(origin=origin, destination=destination)


def fetch_elasticity_data(
    base_url: str = API_BASE_URL,
    route: Optional[str] = None,
    timeout: float = 3.0,
) -> pd.DataFrame:
    """
    Fetch elasticity curves.
    Returns DataFrame: [lead_time_days, route, median_fare, mean_fare, stddev, iqr, sample_size].
    """
    params: dict[str, Any] = {}
    if route:
        params["route"] = route

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(f"{base_url}/elasticity", params=params)
            if resp.status_code == 200:
                items = resp.json().get("items", [])
                if items:
                    return pd.DataFrame(items).sort_values("lead_time_days").reset_index(drop=True)
    except Exception:
        pass

    return generate_mock_elasticity(route=route)


# ── Deterministic Mock Generators ─────────────────────────────────────────────

def generate_mock_index_timeseries(
    freq: str = "daily",
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    days: int = 45,
) -> pd.DataFrame:
    """Generate realistic APIx price index time series for demonstration."""
    end = to_date or date.today()
    start = from_date or (end - timedelta(days=days))

    date_range = pd.date_range(start=start, end=end, freq="D")
    n = len(date_range)
    if n == 0:
        return pd.DataFrame(columns=["period_date", "index_value", "base_value", "weight_version"])

    np.random.seed(42)
    # Simulated index starting at 100 with gradual realistic volatility
    trend = np.linspace(98.0, 107.5, n)
    seasonal = 3.0 * np.sin(np.linspace(0, 3 * np.pi, n))
    noise = np.random.normal(0, 0.8, n)
    index_values = np.round(trend + seasonal + noise, 2)

    df = pd.DataFrame({
        "period_date": date_range,
        "index_value": index_values,
        "base_value": 100.0,
        "weight_version": "DGCA_2025_Q1",
    })

    if freq == "weekly":
        df["period_date"] = df["period_date"].dt.to_period("W").dt.start_time
        df = df.groupby(["period_date", "weight_version"], as_index=False).agg({
            "index_value": "mean",
            "base_value": "first",
        })
        df["index_value"] = df["index_value"].round(2)
    elif freq == "monthly":
        df["period_date"] = df["period_date"].dt.to_period("M").dt.start_time
        df = df.groupby(["period_date", "weight_version"], as_index=False).agg({
            "index_value": "mean",
            "base_value": "first",
        })
        df["index_value"] = df["index_value"].round(2)

    return df


def generate_mock_route_index(origin: str = "DEL", destination: str = "BOM") -> pd.DataFrame:
    """Generate route specific index series."""
    df = generate_mock_index_timeseries(days=30)
    df["route"] = f"{origin.upper()}-{destination.upper()}"
    return df


def generate_mock_sector_matrix() -> pd.DataFrame:
    """
    Generate Origin x Destination matrix of weighted average fares
    across top 6 Indian metro hubs (DEL, BOM, BLR, MAA, CCU, HYD).
    """
    airports = ["DEL", "BOM", "BLR", "HYD", "CCU", "MAA"]
    base_fares = {
        ("DEL", "BOM"): 5420,
        ("DEL", "BLR"): 6250,
        ("DEL", "HYD"): 4980,
        ("DEL", "CCU"): 5110,
        ("DEL", "MAA"): 5890,
        ("BOM", "BLR"): 3850,
        ("BOM", "HYD"): 3490,
        ("BOM", "CCU"): 6300,
        ("BOM", "MAA"): 4120,
        ("BLR", "HYD"): 2890,
        ("BLR", "CCU"): 6720,
        ("BLR", "MAA"): 2450,
        ("HYD", "CCU"): 5340,
        ("HYD", "MAA"): 3120,
        ("CCU", "MAA"): 5620,
    }

    matrix = pd.DataFrame(index=airports, columns=airports, dtype=float)
    np.random.seed(101)

    for orig in airports:
        for dest in airports:
            if orig == dest:
                matrix.loc[orig, dest] = np.nan
            else:
                pair = tuple(sorted([orig, dest]))
                base = base_fares.get(pair, 4500)  # type: ignore[arg-type]
                fluctuation = int(np.random.uniform(-250, 350))
                matrix.loc[orig, dest] = base + fluctuation

    return matrix


def generate_mock_elasticity(route: Optional[str] = None) -> pd.DataFrame:
    """Generate price lead-time elasticity curve data."""
    lead_times = [1, 3, 7, 14, 21, 30]
    routes = [route] if route else ["DEL-BOM", "BLR-DEL", "BOM-BLR", "DEL-CCU"]

    records: list[dict[str, Any]] = []
    base_fares = {"DEL-BOM": 5500, "BLR-DEL": 6200, "BOM-BLR": 3900, "DEL-CCU": 5200}

    for r in routes:
        base = base_fares.get(r, 4800)
        for lt in lead_times:
            # Elasticity multiplier: prices surge as lead-time decreases (hyperbolic / exponential curve)
            multiplier = 1.0 + (35.0 / (lt + 5.0)) * 0.28
            median = round(base * multiplier, 2)
            mean = round(median * 1.02, 2)
            std = round(median * 0.12, 2)
            iqr = round(median * 0.18, 2)
            records.append({
                "route": r,
                "lead_time_days": lt,
                "median_fare": median,
                "mean_fare": mean,
                "stddev": std,
                "iqr": iqr,
                "sample_size": 48,
            })

    return pd.DataFrame(records)
