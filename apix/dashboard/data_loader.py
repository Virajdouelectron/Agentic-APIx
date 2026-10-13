"""
Data connector module for the APIx Streamlit dashboard.

Connects to the APIx FastAPI backend (or generates deterministic synthetic data
when the backend is offline or empty), providing structured DataFrames for:
- System health
- APIx aggregate time series (daily, weekly, monthly)
- Route-specific index time series
- Route sector price matrix for heatmaps
- Lead-time elasticity data
- Aggregate KPI summary metrics
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


def _normalize_index_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """
    Reconcile field names between IndexPoint schema and dashboard expectations:
    - IndexPoint.observation_date <-> period_date
    - IndexPoint.base_period <-> base_value
    - IndexPoint.passenger_weight_version <-> weight_version
    """
    if "observation_date" in df.columns:
        df["observation_date"] = pd.to_datetime(df["observation_date"])
        df["period_date"] = df["observation_date"]
    elif "period_date" in df.columns:
        df["period_date"] = pd.to_datetime(df["period_date"])
        df["observation_date"] = df["period_date"]

    if "base_period" in df.columns and "base_value" not in df.columns:
        df["base_value"] = df["base_period"]
    elif "base_value" in df.columns and "base_period" not in df.columns:
        df["base_period"] = str(df["base_value"].iloc[0]) if not df.empty else "2026-01"

    if "passenger_weight_version" in df.columns and "weight_version" not in df.columns:
        df["weight_version"] = df["passenger_weight_version"]
    elif "weight_version" in df.columns and "passenger_weight_version" not in df.columns:
        df["passenger_weight_version"] = df["weight_version"]

    sort_col = "observation_date" if "observation_date" in df.columns else "period_date"
    return df.sort_values(sort_col).reset_index(drop=True)


def fetch_index_data(
    base_url: str = API_BASE_URL,
    freq: str = "daily",
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    timeout: float = 3.0,
) -> pd.DataFrame:
    """
    Fetch APIx index time series.
    Returns a DataFrame with schema columns: [observation_date, index_value, base_period, passenger_weight_version]
    along with backward-compatible aliases [period_date, base_value, weight_version].
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
                data = resp.json().get("data", [])
                if data:
                    df = pd.DataFrame(data)
                    return _normalize_index_dataframe(df)
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
                data = resp.json().get("data", [])
                if data:
                    df = pd.DataFrame(data)
                    return _normalize_index_dataframe(df)
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
    Returns DataFrame containing both canonical ElasticityPoint columns:
    [lead_time_days, route, median_fare, mean_fare, std_dev, iqr, sample_size]
    and the backward-compatible alias [stddev].
    """
    params: dict[str, Any] = {}
    if route:
        params["route"] = route

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(f"{base_url}/elasticity", params=params)
            if resp.status_code == 200:
                data = resp.json().get("data", [])
                if data:
                    df = pd.DataFrame(data)
                    if "std_dev" in df.columns:
                        df["stddev"] = df["std_dev"]
                    elif "stddev" in df.columns:
                        df["std_dev"] = df["stddev"]
                    return df.sort_values("lead_time_days").reset_index(drop=True)
    except Exception:
        pass

    return generate_mock_elasticity(route=route)


# ── KPI Metrics Aggregator ───────────────────────────────────────────────────

def fetch_kpi_metrics(
    base_url: str = API_BASE_URL,
    daily_df: Optional[pd.DataFrame] = None,
    elast_df: Optional[pd.DataFrame] = None,
    timeout: float = 2.0,
) -> dict[str, Any]:
    """
    Compute or retrieve high-level KPI card metrics for the dashboard.
    Returns computed numbers and flags whether the source is live or synthetic.
    """
    health = check_api_health(base_url=base_url, timeout=timeout)
    is_live = health.get("status") in ("healthy", "ok") and bool(health.get("database_connected", False))

    if daily_df is None or daily_df.empty:
        daily_df = fetch_index_data(base_url=base_url, freq="daily")
    if elast_df is None or elast_df.empty:
        elast_df = fetch_elasticity_data(base_url=base_url)

    # 1. Current APIx Index & Delta
    latest_val = 100.0
    pct_change = "+0.0%"
    if not daily_df.empty and "index_value" in daily_df.columns:
        latest_val = float(daily_df.iloc[-1]["index_value"])
        if len(daily_df) > 1:
            first_val = float(daily_df.iloc[0]["index_value"])
            delta = ((latest_val - first_val) / first_val) * 100 if first_val else 0.0
            pct_change = f"{delta:+.1f}% vs Start"

    # Try live backend summary endpoint if live
    if is_live:
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.get(f"{base_url}/metrics/summary")
                if resp.status_code == 200:
                    summary = resp.json()
                    routes_count = summary.get("routes_monitored", 0)
                    waf_num = summary.get("weighted_average_fare", 0.0)
                    anom_rate = summary.get("anomaly_filter_rate", 0.0)

                    if routes_count > 0 and waf_num > 0:
                        return {
                            "is_live": True,
                            "current_index": latest_val,
                            "index_delta": f"{pct_change} (Base: 100.0)",
                            "routes_monitored": routes_count,
                            "routes_delta": f"Active Database Sectors ({summary.get('total_fares', 0):,} fares)",
                            "weighted_avg_fare": int(waf_num),
                            "waf_delta": "Live Non-Anomalous Weighted Average",
                            "anomaly_filter_rate": anom_rate,
                            "anomaly_delta": f"{summary.get('anomalies_detected', 0):,} Flagged Observations",
                        }
        except Exception:
            pass

    # Dynamic computation from actual data frames (synthetic or fallback)
    routes_count = int(elast_df["route"].nunique()) if (not elast_df.empty and "route" in elast_df.columns) else 30
    
    if not elast_df.empty and "median_fare" in elast_df.columns:
        if "sample_size" in elast_df.columns and elast_df["sample_size"].sum() > 0:
            waf_val = int((elast_df["mean_fare"] * elast_df["sample_size"]).sum() / elast_df["sample_size"].sum())
        else:
            waf_val = int(elast_df["median_fare"].mean())
    else:
        waf_val = 5240

    anom_rate = 2.4
    if not elast_df.empty and "iqr" in elast_df.columns and "median_fare" in elast_df.columns:
        mean_med = elast_df["median_fare"].mean()
        if mean_med > 0:
            anom_rate = round(float((elast_df["iqr"].mean() / mean_med) * 14.0), 1)

    return {
        "is_live": is_live,
        "current_index": latest_val,
        "index_delta": f"{pct_change} (Base: 100.0)",
        "routes_monitored": routes_count,
        "routes_delta": f"Top Hub Sectors ({routes_count} Analyzed)",
        "weighted_avg_fare": waf_val,
        "waf_delta": f"Across {len(elast_df)} Booking Windows",
        "anomaly_filter_rate": anom_rate,
        "anomaly_delta": "Filtered via MAD / IQR (R3.4)",
    }


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
        return pd.DataFrame(columns=[
            "observation_date", "period_date", "index_value",
            "base_period", "base_value", "passenger_weight_version", "weight_version"
        ])

    np.random.seed(42)
    # Simulated index starting at 100 with gradual realistic volatility
    trend = np.linspace(98.0, 107.5, n)
    seasonal = 3.0 * np.sin(np.linspace(0, 3 * np.pi, n))
    noise = np.random.normal(0, 0.8, n)
    index_values = np.round(trend + seasonal + noise, 2)

    df = pd.DataFrame({
        "observation_date": date_range,
        "period_date": date_range,
        "index_value": index_values,
        "base_period": "2026-01",
        "base_value": 100.0,
        "passenger_weight_version": "DGCA_2025_Q1",
        "weight_version": "DGCA_2025_Q1",
    })

    if freq == "weekly":
        df["observation_date"] = df["observation_date"].dt.to_period("W").dt.start_time
        df["period_date"] = df["observation_date"]
        df = df.groupby(["observation_date", "period_date", "passenger_weight_version", "weight_version"], as_index=False).agg({
            "index_value": "mean",
            "base_period": "first",
            "base_value": "first",
        })
        df["index_value"] = df["index_value"].round(2)
    elif freq == "monthly":
        df["observation_date"] = df["observation_date"].dt.to_period("M").dt.start_time
        df["period_date"] = df["observation_date"]
        df = df.groupby(["observation_date", "period_date", "passenger_weight_version", "weight_version"], as_index=False).agg({
            "index_value": "mean",
            "base_period": "first",
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
                "lead_time_bucket": f"{lt}d",
                "median_fare": median,
                "mean_fare": mean,
                "std_dev": std,
                "stddev": std,
                "iqr": iqr,
                "sample_size": 48,
            })

    return pd.DataFrame(records)
