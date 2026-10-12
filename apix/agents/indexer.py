"""
Quant & Indexing Agent for APIx.

Computes the Airfare Price Index using DGCA passenger-traffic weights,
normalizes to a configurable base period (default = 100), and calculates
lead-time elasticity curves.

Methodology
-----------
1. **Weighted Average Fare (WAF)** — a Laspeyres-style price aggregate:

       WAF(d) = Σ(w_r × mean_fare_r_d) / Σ(w_r)

   where:
   - w_r  = DGCA annual passenger-traffic weight for route r
   - mean_fare_r_d = arithmetic mean of non-anomaly index_fare values
     for route r on observation date d

   Routes with zero observations on a given date are excluded from both
   numerator and denominator, so coverage gaps don't bias the index downward.

2. **Base Period Normalization**:

       APIx(d) = (WAF(d) / WAF_base) × 100

   where WAF_base is the mean of daily WAF values in the reference
   base period (e.g., January 2026).  An APIx value of 112.4 means
   aggregate fares are 12.4% above the base period average.

3. **Weekly / Monthly Aggregation**:
   - Weekly:  Arithmetic mean of daily APIx values in the ISO week.
   - Monthly: Arithmetic mean of daily APIx values in the calendar month.

4. **Lead-Time Elasticity Curves**:
   Per (route, lead_time_bucket): mean, median, stddev, IQR, sample size.
   Reveals how advance-purchase timing affects pricing — a key metric for
   both airline yield management analysis and consumer travel policy.

References
----------
- DGCA Annual Statistical Report (passenger traffic volumes)
- Laspeyres price index methodology (standard CPI/PPI construction)
- Project decisions: index_fare = base_fare + mandatory_taxes only
  (UDF and convenience fees excluded per PROJECT.md)
"""

from __future__ import annotations

import csv
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from apix.models.schemas import (
    CleanedFare,
    ElasticityPoint,
    IndexFrequency,
    IndexPoint,
)

logger = logging.getLogger(__name__)

# ── Default paths ────────────────────────────────────────────────
_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_DEFAULT_WEIGHTS_CSV = _DATA_DIR / "dgca_weights.csv"


# ── Data Structures ──────────────────────────────────────────────

@dataclass(frozen=True)
class RouteWeight:
    """DGCA passenger-traffic weight for a single route."""
    route: str
    passengers_annual: int
    weight: float
    data_year: int
    source: str


@dataclass
class IndexResult:
    """Complete output of a single indexing run."""
    index_points: List[IndexPoint]
    elasticity_curves: List[ElasticityPoint]
    base_period: str
    base_period_waf: float
    routes_covered: int
    total_observations: int
    weight_version: str


# ── Indexer Agent ────────────────────────────────────────────────

class Indexer:
    """
    DGCA-weighted economic price indexer for Indian domestic airfares.

    Usage:
        indexer = Indexer(base_period="2026-01")
        result = indexer.compute(cleaned_fares)
        print(result.index_points)
    """

    def __init__(
        self,
        base_period: str = "2026-01",
        weights_csv: Optional[Path] = None,
        weight_version: str = "v1",
    ) -> None:
        """
        Parameters
        ----------
        base_period : str
            Reference month for normalization in 'YYYY-MM' format.
            The index value is 100 in this period on average.
        weights_csv : Path, optional
            Path to DGCA passenger-traffic CSV. Defaults to
            apix/data/dgca_weights.csv.
        weight_version : str
            Label to tag index outputs with the weight table version.
        """
        self.base_period = base_period
        self.weight_version = weight_version
        self._weights: Dict[str, RouteWeight] = {}
        self._load_weights(weights_csv or _DEFAULT_WEIGHTS_CSV)

    def _load_weights(self, csv_path: Path) -> None:
        """
        Read DGCA passenger-traffic weights from a CSV file.

        Expected columns: origin, destination, route, passengers_annual,
        weight, data_year, source.
        """
        if not csv_path.exists():
            logger.warning("Weights CSV not found at %s — index will be unweighted", csv_path)
            return

        with open(csv_path, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                route = row["route"].strip().upper()
                self._weights[route] = RouteWeight(
                    route=route,
                    passengers_annual=int(row["passengers_annual"]),
                    weight=float(row["weight"]),
                    data_year=int(row["data_year"]),
                    source=row["source"].strip(),
                )

        logger.info(
            "Loaded %d route weights from %s (total weight: %.4f)",
            len(self._weights),
            csv_path.name,
            sum(w.weight for w in self._weights.values()),
        )

    @property
    def weights(self) -> Dict[str, RouteWeight]:
        """Read-only access to loaded route weights."""
        return dict(self._weights)

    # ── Core computation ─────────────────────────────────────────

    def _group_fares_by_date_and_route(
        self,
        fares: List[CleanedFare],
    ) -> Dict[date, Dict[str, List[Decimal]]]:
        """
        Group non-anomaly index fares by (observation_date, route).

        Returns
        -------
        {date: {route: [index_fare_1, index_fare_2, ...]}}
        """
        grouped: Dict[date, Dict[str, List[Decimal]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for fare in fares:
            if fare.is_anomaly:
                continue
            grouped[fare.travel_date][fare.route].append(fare.index_fare)
        return grouped

    def compute_daily_waf(
        self,
        fares: List[CleanedFare],
    ) -> Dict[date, float]:
        """
        Compute the Weighted Average Fare (WAF) for each observation date.

        Formula:
            WAF(d) = Σ(w_r × mean(fares_r_d)) / Σ(w_r)

        Only routes with at least one non-anomaly observation on date d
        contribute to the sum. Routes missing from the weights table are
        assigned weight 1.0 (equal-weighted fallback) so that observed
        but unweighted routes don't vanish from the index.
        """
        grouped = self._group_fares_by_date_and_route(fares)
        daily_waf: Dict[date, float] = {}

        for obs_date, route_fares in sorted(grouped.items()):
            weighted_sum = 0.0
            weight_sum = 0.0

            for route, fare_values in route_fares.items():
                if not fare_values:
                    continue

                # Arithmetic mean of all non-anomaly fares for this (date, route)
                mean_fare = float(sum(fare_values)) / len(fare_values)

                # Look up DGCA weight; fallback to 1.0 for unweighted routes
                w = self._weights[route].weight if route in self._weights else 1.0

                weighted_sum += w * mean_fare
                weight_sum += w

            if weight_sum > 0:
                daily_waf[obs_date] = weighted_sum / weight_sum

        return daily_waf

    def compute_base_period_waf(
        self,
        daily_waf: Dict[date, float],
    ) -> float:
        """
        Compute the base period WAF — the average of daily WAF values
        that fall within the base period month.

        Raises ValueError if no observations fall in the base period.
        """
        # Parse base period "YYYY-MM"
        parts = self.base_period.split("-")
        bp_year = int(parts[0])
        bp_month = int(parts[1])

        base_values = [
            v for d, v in daily_waf.items()
            if d.year == bp_year and d.month == bp_month
        ]

        if not base_values:
            raise ValueError(
                f"No observations found in base period '{self.base_period}'. "
                f"Available dates: {sorted(daily_waf.keys())[:5]}..."
            )

        bp_waf = sum(base_values) / len(base_values)
        logger.info(
            "Base period '%s' WAF: ₹%.2f (from %d daily observations)",
            self.base_period,
            bp_waf,
            len(base_values),
        )
        return bp_waf

    def compute_index_points(
        self,
        daily_waf: Dict[date, float],
        base_waf: float,
    ) -> List[IndexPoint]:
        """
        Normalize daily WAF to produce APIx index points.

        Formula:
            APIx(d) = (WAF(d) / WAF_base) × 100

        Also computes weekly and monthly aggregations as arithmetic means
        of the daily index values.
        """
        if base_waf <= 0:
            raise ValueError(f"Base period WAF must be positive, got {base_waf}")

        # ── Daily index points ───────────────────────────────────
        daily_points: List[IndexPoint] = []
        for obs_date in sorted(daily_waf):
            index_value = (daily_waf[obs_date] / base_waf) * 100.0
            daily_points.append(
                IndexPoint(
                    observation_date=obs_date,
                    frequency=IndexFrequency.DAILY,
                    route="ALL",
                    index_value=round(index_value, 2),
                    base_period=self.base_period,
                    passenger_weight_version=self.weight_version,
                )
            )

        # ── Weekly aggregation (ISO week) ────────────────────────
        weekly_buckets: Dict[Tuple[int, int], List[float]] = defaultdict(list)
        for pt in daily_points:
            iso_year, iso_week, _ = pt.observation_date.isocalendar()
            weekly_buckets[(iso_year, iso_week)].append(pt.index_value)

        weekly_points: List[IndexPoint] = []
        for (iso_year, iso_week), values in sorted(weekly_buckets.items()):
            # Use the last date in the week as the observation date
            week_dates = [
                pt.observation_date for pt in daily_points
                if pt.observation_date.isocalendar()[:2] == (iso_year, iso_week)
            ]
            weekly_points.append(
                IndexPoint(
                    observation_date=max(week_dates),
                    frequency=IndexFrequency.WEEKLY,
                    route="ALL",
                    index_value=round(sum(values) / len(values), 2),
                    base_period=self.base_period,
                    passenger_weight_version=self.weight_version,
                    sample_size=len(values),
                )
            )

        # ── Monthly aggregation ──────────────────────────────────
        monthly_buckets: Dict[Tuple[int, int], List[float]] = defaultdict(list)
        for pt in daily_points:
            monthly_buckets[(pt.observation_date.year, pt.observation_date.month)].append(
                pt.index_value
            )

        monthly_points: List[IndexPoint] = []
        for (year, month), values in sorted(monthly_buckets.items()):
            month_dates = [
                pt.observation_date for pt in daily_points
                if pt.observation_date.year == year and pt.observation_date.month == month
            ]
            monthly_points.append(
                IndexPoint(
                    observation_date=max(month_dates),
                    frequency=IndexFrequency.MONTHLY,
                    route="ALL",
                    index_value=round(sum(values) / len(values), 2),
                    base_period=self.base_period,
                    passenger_weight_version=self.weight_version,
                    sample_size=len(values),
                )
            )

        return daily_points + weekly_points + monthly_points

    # ── Lead-time elasticity ─────────────────────────────────────

    def compute_elasticity(
        self,
        fares: List[CleanedFare],
    ) -> List[ElasticityPoint]:
        """
        Compute lead-time elasticity curves per route.

        For each (route, lead_time_bucket), compute:
        - mean_fare, median_fare, std_dev, IQR, sample_size

        This captures how price changes with advance-purchase timing —
        a key signal for both airline yield analysis and travel policy.
        Only non-anomaly fares are included.
        """
        # Group by (route, lead_time_bucket)
        buckets: Dict[Tuple[str, str], List[float]] = defaultdict(list)
        lt_days_map: Dict[Tuple[str, str], int] = {}

        for fare in fares:
            if fare.is_anomaly:
                continue
            key = (fare.route, fare.lead_time_bucket)
            buckets[key].append(float(fare.index_fare))
            lt_days_map[key] = fare.lead_time_days

        elasticity_points: List[ElasticityPoint] = []

        for (route, bucket), prices in sorted(buckets.items()):
            arr = np.array(prices)
            q75, q25 = np.percentile(arr, [75, 25]) if len(arr) >= 2 else (0.0, 0.0)

            elasticity_points.append(
                ElasticityPoint(
                    route=route,
                    lead_time_days=lt_days_map[(route, bucket)],
                    lead_time_bucket=bucket,
                    mean_fare=round(float(np.mean(arr)), 2),
                    median_fare=round(float(np.median(arr)), 2),
                    std_dev=round(float(np.std(arr, ddof=1)), 2) if len(arr) > 1 else 0.0,
                    iqr=round(float(q75 - q25), 2),
                    sample_size=len(arr),
                )
            )

        logger.info(
            "Computed elasticity curves for %d (route × bucket) combinations",
            len(elasticity_points),
        )
        return elasticity_points

    # ── Public entry point ───────────────────────────────────────

    def compute(self, fares: List[CleanedFare]) -> IndexResult:
        """
        Execute the full indexing pipeline:

        1. Compute daily Weighted Average Fares (WAF) using DGCA weights.
        2. Determine the base period reference WAF.
        3. Normalize to produce APIx index at daily/weekly/monthly frequency.
        4. Compute lead-time elasticity curves per route.

        Parameters
        ----------
        fares : list[CleanedFare]
            Harmonized, deduplicated fare records from the Harmonizer agent.

        Returns
        -------
        IndexResult
            Complete indexing output with all index points, elasticity data,
            and metadata.
        """
        logger.info("Starting index computation on %d cleaned fares", len(fares))

        # Count unique routes with non-anomaly data
        active_routes = {f.route for f in fares if not f.is_anomaly}
        non_anomaly_count = sum(1 for f in fares if not f.is_anomaly)

        # Step 1: Daily WAF
        daily_waf = self.compute_daily_waf(fares)

        # Step 2: Base period reference
        base_waf = self.compute_base_period_waf(daily_waf)

        # Step 3: Index points (daily + weekly + monthly)
        index_points = self.compute_index_points(daily_waf, base_waf)

        # Step 4: Elasticity curves
        elasticity = self.compute_elasticity(fares)

        result = IndexResult(
            index_points=index_points,
            elasticity_curves=elasticity,
            base_period=self.base_period,
            base_period_waf=base_waf,
            routes_covered=len(active_routes),
            total_observations=non_anomaly_count,
            weight_version=self.weight_version,
        )

        # Summary logging
        daily_count = sum(1 for p in index_points if p.frequency == IndexFrequency.DAILY)
        weekly_count = sum(1 for p in index_points if p.frequency == IndexFrequency.WEEKLY)
        monthly_count = sum(1 for p in index_points if p.frequency == IndexFrequency.MONTHLY)

        logger.info(
            "Index computation complete: %d daily, %d weekly, %d monthly points | "
            "%d elasticity curves | base WAF ₹%.2f | %d routes covered",
            daily_count,
            weekly_count,
            monthly_count,
            len(elasticity),
            base_waf,
            len(active_routes),
        )

        return result
