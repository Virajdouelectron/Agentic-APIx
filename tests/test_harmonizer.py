"""
Unit tests for the Harmonization and Anomaly Detection Agent.

Verifies:
- Standard economic index fare definition (Base Fare + Mandatory Taxes only)
- Cross-source flight deduplication logic
- Static implausibility thresholds (too low on metro corridor, too high domestic)
- Missing/zero base fare detection
- Robust statistical anomaly detection via Median Absolute Deviation (MAD) and IQR
- Non-dropping guarantee: anomalous flights are flagged and audited, never silently discarded
"""

from datetime import date, datetime, timezone
from decimal import Decimal
import pytest

from apix.agents.harmonizer import Harmonizer, HarmonizationResult
from apix.models.schemas import (
    AnomalyReason,
    CleanedFare,
    FareBreakdown,
    RawFarePayload,
)


def create_payload(
    flight_num: str,
    base: str,
    taxes: str,
    udf: str = "150.00",
    fee: str = "200.00",
    route: str = "DEL-BOM",
    source: str = "indigo",
    travel_date: date | None = None,
    bucket: str = "T+7",
) -> RawFarePayload:
    origin, dest = route.split("-")
    t_date = travel_date or date(2026, 11, 15)
    return RawFarePayload(
        source=source,
        route=route,
        origin=origin,
        destination=dest,
        travel_date=t_date,
        lead_time_bucket=bucket,
        lead_time_days=7,
        airline="IndiGo",
        flight_number=flight_num,
        fare_components=FareBreakdown(
            base_fare=Decimal(base),
            taxes=Decimal(taxes),
            udf=Decimal(udf),
            convenience_fee=Decimal(fee),
        ),
        currency="INR",
        scraped_at=datetime.now(timezone.utc),
        scraper_version="1.0.0",
    )


def test_fare_decomposition_economic_definition():
    """
    Standard definition: index_fare = base_fare + taxes.
    UDF (airport-specific fee) and convenience fees (payment/OTA markup) must be excluded.
    """
    harmonizer = Harmonizer()
    payload = create_payload(
        flight_num="6E-204",
        base="3500.00",
        taxes="450.00",
        udf="320.00",
        fee="250.00",
    )

    cleaned = harmonizer.harmonize_payload(payload)

    # 3500 + 450 = 3950
    assert cleaned.index_fare == Decimal("3950.00")
    assert cleaned.base_fare == Decimal("3500.00")
    assert cleaned.taxes == Decimal("450.00")
    assert cleaned.udf == Decimal("320.00")
    assert cleaned.convenience_fee == Decimal("250.00")
    assert not cleaned.is_anomaly


def test_cross_source_deduplication():
    """
    When the same flight is scraped from both IndiGo directly and Cleartrip OTA,
    deduplication prioritizes the direct airline quote.
    """
    harmonizer = Harmonizer()
    p1 = create_payload(flight_num="6E-501", base="4000.00", taxes="500.00", source="cleartrip")
    p2 = create_payload(flight_num="6E-501", base="4000.00", taxes="500.00", source="indigo")

    c1 = harmonizer.harmonize_payload(p1)
    c2 = harmonizer.harmonize_payload(p2)

    deduped, removed = harmonizer.deduplicate([c1, c2])

    assert len(deduped) == 1
    assert removed == 1
    assert deduped[0].source == "indigo"


def test_implausible_low_fare_flagged_not_dropped():
    """
    A fare of ₹150 on metro trunk DEL-BOM is flagged with IMPLAUSIBLE_LOW,
    but retained in the dataset for auditability.
    """
    harmonizer = Harmonizer(metro_min_fare=Decimal("1000.00"))
    payload = create_payload(flight_num="6E-101", base="100.00", taxes="50.00")

    result = harmonizer.process_batch([payload])

    # Record must NOT be dropped
    assert len(result.cleaned_fares) == 1
    fare = result.cleaned_fares[0]
    assert fare.is_anomaly is True
    assert AnomalyReason.IMPLAUSIBLE_LOW in fare.anomaly_reasons

    # Anomaly audit record must be generated
    assert len(result.anomalies) == 1
    assert result.anomalies[0].reason_code == AnomalyReason.IMPLAUSIBLE_LOW
    assert result.anomalies[0].observed_fare == Decimal("150.00")


def test_implausible_high_fare_flagged():
    """Fare exceeding ₹50,000 for domestic economy is flagged."""
    harmonizer = Harmonizer(domestic_max_fare=Decimal("50000.00"))
    payload = create_payload(flight_num="6E-999", base="55000.00", taxes="4500.00")

    result = harmonizer.process_batch([payload])

    assert len(result.cleaned_fares) == 1
    fare = result.cleaned_fares[0]
    assert fare.is_anomaly is True
    assert AnomalyReason.IMPLAUSIBLE_HIGH in fare.anomaly_reasons
    assert result.anomalies[0].observed_fare == Decimal("59500.00")


def test_zero_base_fare_detection():
    """Suspicious zero base fare with positive taxes flagged."""
    harmonizer = Harmonizer()
    payload = create_payload(flight_num="6E-301", base="0.00", taxes="1200.00")

    result = harmonizer.process_batch([payload])

    fare = result.cleaned_fares[0]
    assert fare.is_anomaly is True
    assert AnomalyReason.ZERO_BASE_FARE in fare.anomaly_reasons


def test_sold_out_flight_detection():
    """Zero total fare flagged as SOLD_OUT."""
    harmonizer = Harmonizer()
    payload = create_payload(flight_num="6E-404", base="0.00", taxes="0.00")

    result = harmonizer.process_batch([payload])

    fare = result.cleaned_fares[0]
    assert fare.is_anomaly is True
    assert AnomalyReason.SOLD_OUT in fare.anomaly_reasons


def test_mad_and_iqr_statistical_outlier_detection():
    """
    Verify robust outlier detection using Median Absolute Deviation (MAD)
    and Interquartile Range (IQR) on a distribution of fares.
    
    9 flights clustered around ₹4,500, and 1 extreme price jump at ₹18,000.
    """
    harmonizer = Harmonizer(mad_threshold=3.0)

    base_prices = [
        "4200.00", "4300.00", "4400.00", "4450.00", "4500.00",
        "4550.00", "4600.00", "4700.00", "4800.00"
    ]
    payloads = [
        create_payload(flight_num=f"6E-{100 + i}", base=p, taxes="500.00")
        for i, p in enumerate(base_prices)
    ]

    # Add 1 massive outlier jump (Base ₹17,500 + Taxes ₹500 = ₹18,000)
    outlier_payload = create_payload(flight_num="6E-999", base="17500.00", taxes="500.00")
    payloads.append(outlier_payload)

    result = harmonizer.process_batch(payloads)

    # All 10 flights are retained in the dataset
    assert len(result.cleaned_fares) == 10

    # The 9 normal flights are clean
    clean_fares = [f for f in result.cleaned_fares if not f.is_anomaly]
    assert len(clean_fares) == 9

    # The outlier flight is flagged by MAD and IQR
    outlier = next(f for f in result.cleaned_fares if f.flight_number == "6E-999")
    assert outlier.is_anomaly is True
    assert AnomalyReason.SUDDEN_JUMP_MAD in outlier.anomaly_reasons
    assert AnomalyReason.SUDDEN_JUMP_IQR in outlier.anomaly_reasons


def test_non_dropping_audit_guarantee():
    """
    Ensure the pipeline never silently drops any flight:
    Total processed == clean_count + anomaly_count (unique fares).
    """
    harmonizer = Harmonizer()
    payloads = [
        # Normal
        create_payload(flight_num="6E-1", base="4000.00", taxes="500.00"),
        create_payload(flight_num="6E-2", base="4200.00", taxes="500.00"),
        # Implausible low
        create_payload(flight_num="6E-3", base="100.00", taxes="50.00"),
        # Zero base fare
        create_payload(flight_num="6E-4", base="0.00", taxes="800.00"),
    ]

    result = harmonizer.process_batch(payloads)

    assert result.total_processed == 4
    assert len(result.cleaned_fares) == 4
    assert result.clean_count == 2
    assert len(result.anomalies) >= 2
