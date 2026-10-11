"""
Unit tests for APIx Pydantic schemas.

Verifies:
- Raw scraper payload validation and route normalization
- Fare breakdown index_fare computation (base_fare + taxes only)
- Strict Decimal handling and rejection of negative values
- AnomalyRecord schema and AnomalyReason enum integrity
"""

from datetime import date, datetime, timezone
from decimal import Decimal
import pytest
from pydantic import ValidationError

from apix.models.schemas import (
    AnomalyReason,
    AnomalyRecord,
    CleanedFare,
    FareBreakdown,
    IndexFrequency,
    IndexPoint,
    RawFarePayload,
)


def test_fare_breakdown_index_fare_definition():
    """
    Ensure APIx fare definition:
    index_fare == base_fare + taxes
    Excludes UDF and convenience fees.
    """
    breakdown = FareBreakdown(
        base_fare=Decimal("3500.00"),
        taxes=Decimal("450.00"),
        udf=Decimal("320.00"),
        convenience_fee=Decimal("250.00"),
        total_fare=Decimal("4520.00"),
    )
    # 3500 + 450 = 3950 (UDF and convenience fee must NOT be in index fare)
    assert breakdown.index_fare == Decimal("3950.00")


def test_fare_breakdown_rejects_negative_amounts():
    """Negative fare values must raise ValidationError."""
    with pytest.raises(ValidationError):
        FareBreakdown(
            base_fare=Decimal("-100.00"),
            taxes=Decimal("50.00"),
        )


def test_raw_fare_payload_validation_and_normalization():
    """Valid raw payload must parse cleanly and uppercase route strings."""
    payload = RawFarePayload(
        source="indigo",
        route="del-bom",
        origin="DEL",
        destination="BOM",
        travel_date=date(2026, 10, 18),
        lead_time_bucket="T+7",
        lead_time_days=7,
        airline="IndiGo",
        flight_number="6E-204",
        fare_components=FareBreakdown(
            base_fare=Decimal("4200.00"),
            taxes=Decimal("550.00"),
            udf=Decimal("150.00"),
            convenience_fee=Decimal("0.00"),
        ),
        currency="INR",
        scraped_at=datetime(2026, 10, 11, 12, 0, 0),
        scraper_version="1.0.0",
    )
    assert payload.route == "DEL-BOM"
    assert payload.fare_components.index_fare == Decimal("4750.00")
    assert payload.source == "indigo"


def test_raw_fare_payload_missing_required_fields():
    """Missing mandatory fields must fail validation."""
    with pytest.raises(ValidationError):
        RawFarePayload(
            source="indigo",
            origin="DEL",
            # Missing destination, flight_number, fare_components, etc.
        )


def test_anomaly_record_structure():
    """AnomalyRecord must enforce AnomalyReason enum and capture details."""
    rec = AnomalyRecord(
        fare_identifier="6E-204_2026-10-18_T+7",
        route="DEL-BOM",
        flight_number="6E-204",
        travel_date=date(2026, 10, 18),
        lead_time_bucket="T+7",
        observed_fare=Decimal("150.00"),
        benchmark_fare=Decimal("4500.00"),
        reason_code=AnomalyReason.IMPLAUSIBLE_LOW,
        details="Fare of Rs 150 on metro trunk corridor is below minimal feasible threshold",
        flagged_at=datetime.now(timezone.utc),
    )
    assert rec.reason_code == AnomalyReason.IMPLAUSIBLE_LOW
    assert rec.observed_fare == Decimal("150.00")


def test_cleaned_fare_creation():
    """CleanedFare represents normalized data ready for econometric indexing."""
    cleaned = CleanedFare(
        source="indigo",
        origin="DEL",
        destination="BOM",
        route="DEL-BOM",
        travel_date=date(2026, 10, 18),
        lead_time_days=7,
        lead_time_bucket="T+7",
        airline="IndiGo",
        flight_number="6E-204",
        index_fare=Decimal("4750.00"),
        base_fare=Decimal("4200.00"),
        taxes=Decimal("550.00"),
        udf=Decimal("150.00"),
        convenience_fee=Decimal("0.00"),
        currency="INR",
        scraped_at=datetime.now(timezone.utc),
        is_anomaly=False,
    )
    assert cleaned.index_fare == Decimal("4750.00")
    assert not cleaned.is_anomaly
