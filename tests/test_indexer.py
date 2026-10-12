"""
Unit tests for the Quant & Indexing Agent.

Tests use hand-checkable examples so a human reviewer can verify the
expected values with a calculator. The test data uses 3 routes on 2 dates
in the base period and 1 date outside it to validate:

1. DGCA-weighted average fare (WAF) computation
2. Base period normalization (APIx = 100 in base period)
3. Weekly and monthly aggregation
4. Lead-time elasticity curve computation
5. Edge cases: missing routes, anomalies excluded, single observations
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from apix.agents.indexer import Indexer, IndexResult, RouteWeight
from apix.models.schemas import (
    CleanedFare,
    ElasticityPoint,
    IndexFrequency,
    IndexPoint,
)


# ── Test Fixtures ────────────────────────────────────────────────

def _make_fare(
    route: str,
    travel_date: date,
    index_fare: Decimal,
    lead_time_days: int = 7,
    lead_time_bucket: str = "T+7",
    is_anomaly: bool = False,
    source: str = "indigo",
) -> CleanedFare:
    """Factory for creating test CleanedFare objects."""
    origin, destination = route.split("-")
    return CleanedFare(
        source=source,
        origin=origin,
        destination=destination,
        route=route,
        travel_date=travel_date,
        lead_time_days=lead_time_days,
        lead_time_bucket=lead_time_bucket,
        airline="IndiGo",
        flight_number=f"6E-{hash(route) % 900 + 100}",
        index_fare=index_fare,
        base_fare=index_fare * Decimal("0.7"),
        taxes=index_fare * Decimal("0.3"),
        udf=Decimal("0"),
        convenience_fee=Decimal("0"),
        scraped_at=datetime.now(timezone.utc),
        is_anomaly=is_anomaly,
    )


def _make_indexer_with_known_weights() -> Indexer:
    """
    Create an Indexer with exactly 3 routes and known weights.

    Route weights (sum = 1.0 for easy mental arithmetic):
        DEL-BOM: 0.50
        DEL-BLR: 0.30
        BOM-BLR: 0.20
    """
    indexer = Indexer(base_period="2026-01", weights_csv=None)
    # Override weights manually (skip CSV loading — we gave a bogus path)
    indexer._weights = {
        "DEL-BOM": RouteWeight("DEL-BOM", 5000000, 0.50, 2025, "TEST"),
        "DEL-BLR": RouteWeight("DEL-BLR", 3000000, 0.30, 2025, "TEST"),
        "BOM-BLR": RouteWeight("BOM-BLR", 2000000, 0.20, 2025, "TEST"),
    }
    return indexer


# ── Hand-Checkable Scenario ──────────────────────────────────────
#
# Scenario: 3 routes, 3 dates (2 in base period Jan 2026, 1 in Feb)
#
# Date 2026-01-10:
#   DEL-BOM: fares [5000, 6000] → mean = 5500
#   DEL-BLR: fares [4000]       → mean = 4000
#   BOM-BLR: fares [3000]       → mean = 3000
#
#   WAF = (0.50 * 5500 + 0.30 * 4000 + 0.20 * 3000) / (0.50 + 0.30 + 0.20)
#       = (2750 + 1200 + 600) / 1.0
#       = 4550.00
#
# Date 2026-01-15:
#   DEL-BOM: fares [5200]       → mean = 5200
#   DEL-BLR: fares [4200]       → mean = 4200
#   BOM-BLR: fares [3200]       → mean = 3200
#
#   WAF = (0.50 * 5200 + 0.30 * 4200 + 0.20 * 3200) / 1.0
#       = (2600 + 1260 + 640) / 1.0
#       = 4500.00
#
# Base period WAF = mean(4550, 4500) = 4525.00
#
# APIx on 2026-01-10 = (4550 / 4525) * 100 = 100.55
# APIx on 2026-01-15 = (4500 / 4525) * 100 = 99.45
#
# Date 2026-02-05 (outside base period):
#   DEL-BOM: fares [6000]       → mean = 6000
#   DEL-BLR: fares [4500]       → mean = 4500
#   BOM-BLR: fares [3500]       → mean = 3500
#
#   WAF = (0.50 * 6000 + 0.30 * 4500 + 0.20 * 3500) / 1.0
#       = (3000 + 1350 + 700) / 1.0
#       = 5050.00
#
#   APIx = (5050 / 4525) * 100 = 111.60

def _build_scenario_fares() -> list[CleanedFare]:
    """Build the hand-checkable 3-route, 3-date fare set described above."""
    d1 = date(2026, 1, 10)
    d2 = date(2026, 1, 15)
    d3 = date(2026, 2, 5)

    return [
        # Date 1: Jan 10
        _make_fare("DEL-BOM", d1, Decimal("5000")),
        _make_fare("DEL-BOM", d1, Decimal("6000")),
        _make_fare("DEL-BLR", d1, Decimal("4000")),
        _make_fare("BOM-BLR", d1, Decimal("3000")),
        # Date 2: Jan 15
        _make_fare("DEL-BOM", d2, Decimal("5200")),
        _make_fare("DEL-BLR", d2, Decimal("4200")),
        _make_fare("BOM-BLR", d2, Decimal("3200")),
        # Date 3: Feb 5
        _make_fare("DEL-BOM", d3, Decimal("6000")),
        _make_fare("DEL-BLR", d3, Decimal("4500")),
        _make_fare("BOM-BLR", d3, Decimal("3500")),
    ]


# ── Tests ────────────────────────────────────────────────────────

class TestDailyWAF:
    """Tests for the weighted average fare computation."""

    def test_waf_three_routes_hand_checkable(self) -> None:
        """Verify WAF matches the hand-calculated values in the scenario."""
        indexer = _make_indexer_with_known_weights()
        fares = _build_scenario_fares()
        daily_waf = indexer.compute_daily_waf(fares)

        assert date(2026, 1, 10) in daily_waf
        assert date(2026, 1, 15) in daily_waf
        assert date(2026, 2, 5) in daily_waf

        # WAF(Jan 10) = 4550.00
        assert daily_waf[date(2026, 1, 10)] == pytest.approx(4550.0, abs=0.01)
        # WAF(Jan 15) = 4500.00
        assert daily_waf[date(2026, 1, 15)] == pytest.approx(4500.0, abs=0.01)
        # WAF(Feb 5) = 5050.00
        assert daily_waf[date(2026, 2, 5)] == pytest.approx(5050.0, abs=0.01)

    def test_anomalies_excluded_from_waf(self) -> None:
        """Anomalous fares should NOT contribute to the weighted average."""
        indexer = _make_indexer_with_known_weights()
        fares = [
            _make_fare("DEL-BOM", date(2026, 1, 10), Decimal("5000")),
            _make_fare("DEL-BOM", date(2026, 1, 10), Decimal("99999"), is_anomaly=True),
            _make_fare("DEL-BLR", date(2026, 1, 10), Decimal("4000")),
            _make_fare("BOM-BLR", date(2026, 1, 10), Decimal("3000")),
        ]
        daily_waf = indexer.compute_daily_waf(fares)

        # Only the 5000 fare for DEL-BOM should count, not the 99999 anomaly
        # WAF = (0.50 * 5000 + 0.30 * 4000 + 0.20 * 3000) / 1.0
        #     = (2500 + 1200 + 600) / 1.0 = 4300
        assert daily_waf[date(2026, 1, 10)] == pytest.approx(4300.0, abs=0.01)

    def test_missing_route_uses_fallback_weight(self) -> None:
        """Routes not in the DGCA table get weight=1.0 fallback."""
        indexer = _make_indexer_with_known_weights()
        fares = [
            _make_fare("DEL-BOM", date(2026, 1, 10), Decimal("5000")),
            # Unknown route — should get weight=1.0
            _make_fare("DEL-GOI", date(2026, 1, 10), Decimal("3000")),
        ]
        daily_waf = indexer.compute_daily_waf(fares)

        # WAF = (0.50 * 5000 + 1.0 * 3000) / (0.50 + 1.0)
        #     = (2500 + 3000) / 1.50 = 3666.67
        assert daily_waf[date(2026, 1, 10)] == pytest.approx(3666.67, abs=0.01)

    def test_empty_fares_returns_empty(self) -> None:
        """No fares → no WAF values."""
        indexer = _make_indexer_with_known_weights()
        daily_waf = indexer.compute_daily_waf([])
        assert daily_waf == {}


class TestBasePeriod:
    """Tests for base period WAF computation."""

    def test_base_period_waf_hand_checkable(self) -> None:
        """Base period WAF = mean of Jan 10 and Jan 15 daily WAFs."""
        indexer = _make_indexer_with_known_weights()
        fares = _build_scenario_fares()
        daily_waf = indexer.compute_daily_waf(fares)
        bp_waf = indexer.compute_base_period_waf(daily_waf)

        # mean(4550, 4500) = 4525.00
        assert bp_waf == pytest.approx(4525.0, abs=0.01)

    def test_base_period_no_data_raises(self) -> None:
        """Should raise ValueError if no observations in the base period."""
        indexer = Indexer(base_period="2020-01", weights_csv=None)
        with pytest.raises(ValueError, match="No observations found"):
            indexer.compute_base_period_waf({date(2026, 1, 10): 5000.0})


class TestIndexPoints:
    """Tests for APIx index normalization and aggregation."""

    def test_daily_index_hand_checkable(self) -> None:
        """Verify daily APIx values match the hand-calculated scenario."""
        indexer = _make_indexer_with_known_weights()
        fares = _build_scenario_fares()
        daily_waf = indexer.compute_daily_waf(fares)
        bp_waf = indexer.compute_base_period_waf(daily_waf)
        points = indexer.compute_index_points(daily_waf, bp_waf)

        daily_points = [p for p in points if p.frequency == IndexFrequency.DAILY]
        daily_by_date = {p.observation_date: p.index_value for p in daily_points}

        # APIx(Jan 10) = (4550 / 4525) * 100 = 100.55
        assert daily_by_date[date(2026, 1, 10)] == pytest.approx(100.55, abs=0.01)
        # APIx(Jan 15) = (4500 / 4525) * 100 = 99.45
        assert daily_by_date[date(2026, 1, 15)] == pytest.approx(99.45, abs=0.01)
        # APIx(Feb 5) = (5050 / 4525) * 100 = 111.60
        assert daily_by_date[date(2026, 2, 5)] == pytest.approx(111.60, abs=0.01)

    def test_weekly_aggregation(self) -> None:
        """Weekly index should be the mean of daily values in the same ISO week."""
        indexer = _make_indexer_with_known_weights()
        fares = _build_scenario_fares()
        daily_waf = indexer.compute_daily_waf(fares)
        bp_waf = indexer.compute_base_period_waf(daily_waf)
        points = indexer.compute_index_points(daily_waf, bp_waf)

        weekly_points = [p for p in points if p.frequency == IndexFrequency.WEEKLY]
        assert len(weekly_points) >= 1
        for wp in weekly_points:
            assert wp.route == "ALL"
            assert wp.base_period == "2026-01"

    def test_monthly_aggregation(self) -> None:
        """Monthly index should be the mean of daily values in the calendar month."""
        indexer = _make_indexer_with_known_weights()
        fares = _build_scenario_fares()
        daily_waf = indexer.compute_daily_waf(fares)
        bp_waf = indexer.compute_base_period_waf(daily_waf)
        points = indexer.compute_index_points(daily_waf, bp_waf)

        monthly_points = [p for p in points if p.frequency == IndexFrequency.MONTHLY]
        # Should have at least 2 monthly points (Jan + Feb)
        assert len(monthly_points) >= 2

        jan_point = next(
            p for p in monthly_points
            if p.observation_date.month == 1 and p.observation_date.year == 2026
        )
        # Monthly APIx for Jan = mean(100.55, 99.45) = 100.00
        assert jan_point.index_value == pytest.approx(100.0, abs=0.01)

    def test_base_period_monthly_equals_100(self) -> None:
        """The base period month average should be exactly 100 by construction."""
        indexer = _make_indexer_with_known_weights()
        fares = _build_scenario_fares()
        daily_waf = indexer.compute_daily_waf(fares)
        bp_waf = indexer.compute_base_period_waf(daily_waf)
        points = indexer.compute_index_points(daily_waf, bp_waf)

        monthly_points = [p for p in points if p.frequency == IndexFrequency.MONTHLY]
        jan_point = next(
            p for p in monthly_points
            if p.observation_date.month == 1 and p.observation_date.year == 2026
        )
        assert jan_point.index_value == pytest.approx(100.0, abs=0.01)

    def test_zero_base_waf_raises(self) -> None:
        """Should raise ValueError when base WAF is zero."""
        indexer = _make_indexer_with_known_weights()
        with pytest.raises(ValueError, match="positive"):
            indexer.compute_index_points({date(2026, 1, 10): 5000.0}, 0.0)


class TestElasticity:
    """Tests for lead-time elasticity curve computation."""

    def test_elasticity_basic(self) -> None:
        """Should compute mean, median, stddev, IQR for each (route, bucket)."""
        indexer = _make_indexer_with_known_weights()
        fares = [
            _make_fare("DEL-BOM", date(2026, 1, 10), Decimal("5000"), lead_time_days=1, lead_time_bucket="T+1"),
            _make_fare("DEL-BOM", date(2026, 1, 11), Decimal("5500"), lead_time_days=1, lead_time_bucket="T+1"),
            _make_fare("DEL-BOM", date(2026, 1, 10), Decimal("4000"), lead_time_days=7, lead_time_bucket="T+7"),
            _make_fare("DEL-BOM", date(2026, 1, 11), Decimal("4200"), lead_time_days=7, lead_time_bucket="T+7"),
        ]
        elasticity = indexer.compute_elasticity(fares)

        assert len(elasticity) == 2  # T+1 and T+7 for DEL-BOM

        t1_point = next(p for p in elasticity if p.lead_time_bucket == "T+1")
        assert t1_point.route == "DEL-BOM"
        assert t1_point.mean_fare == pytest.approx(5250.0, abs=0.01)
        assert t1_point.sample_size == 2

        t7_point = next(p for p in elasticity if p.lead_time_bucket == "T+7")
        assert t7_point.mean_fare == pytest.approx(4100.0, abs=0.01)
        assert t7_point.sample_size == 2

    def test_elasticity_excludes_anomalies(self) -> None:
        """Anomalous fares should not appear in elasticity curves."""
        indexer = _make_indexer_with_known_weights()
        fares = [
            _make_fare("DEL-BOM", date(2026, 1, 10), Decimal("5000"), lead_time_days=1, lead_time_bucket="T+1"),
            _make_fare("DEL-BOM", date(2026, 1, 10), Decimal("99999"), lead_time_days=1, lead_time_bucket="T+1", is_anomaly=True),
        ]
        elasticity = indexer.compute_elasticity(fares)

        assert len(elasticity) == 1
        assert elasticity[0].mean_fare == pytest.approx(5000.0)
        assert elasticity[0].sample_size == 1

    def test_elasticity_single_observation(self) -> None:
        """Single fare in a bucket should have zero stddev and IQR."""
        indexer = _make_indexer_with_known_weights()
        fares = [
            _make_fare("DEL-BOM", date(2026, 1, 10), Decimal("5000"), lead_time_days=30, lead_time_bucket="T+30"),
        ]
        elasticity = indexer.compute_elasticity(fares)

        assert len(elasticity) == 1
        pt = elasticity[0]
        assert pt.std_dev == 0.0
        assert pt.sample_size == 1

    def test_elasticity_empty_fares(self) -> None:
        """No fares → no elasticity points."""
        indexer = _make_indexer_with_known_weights()
        elasticity = indexer.compute_elasticity([])
        assert elasticity == []


class TestFullPipeline:
    """End-to-end tests for Indexer.compute()."""

    def test_compute_returns_complete_result(self) -> None:
        """Full pipeline should produce daily/weekly/monthly points and elasticity."""
        indexer = _make_indexer_with_known_weights()
        fares = _build_scenario_fares()
        result = indexer.compute(fares)

        assert isinstance(result, IndexResult)
        assert result.base_period == "2026-01"
        assert result.base_period_waf == pytest.approx(4525.0, abs=0.01)
        assert result.routes_covered == 3
        assert result.total_observations == 10

        # Should have all three frequency types
        frequencies = {p.frequency for p in result.index_points}
        assert IndexFrequency.DAILY in frequencies
        assert IndexFrequency.WEEKLY in frequencies
        assert IndexFrequency.MONTHLY in frequencies

        # Should have elasticity curves
        assert len(result.elasticity_curves) > 0

    def test_compute_with_only_anomalies_raises(self) -> None:
        """If all fares are anomalies, base period has no data → raises."""
        indexer = _make_indexer_with_known_weights()
        fares = [
            _make_fare("DEL-BOM", date(2026, 1, 10), Decimal("5000"), is_anomaly=True),
        ]
        with pytest.raises(ValueError, match="No observations found"):
            indexer.compute(fares)

    def test_weight_version_propagated(self) -> None:
        """The weight_version should appear on all index points."""
        indexer = Indexer(base_period="2026-01", weights_csv=None, weight_version="v2-test")
        indexer._weights = {
            "DEL-BOM": RouteWeight("DEL-BOM", 5000000, 0.50, 2025, "TEST"),
        }
        fares = [
            _make_fare("DEL-BOM", date(2026, 1, 10), Decimal("5000")),
        ]
        result = indexer.compute(fares)

        for pt in result.index_points:
            assert pt.passenger_weight_version == "v2-test"
        assert result.weight_version == "v2-test"
