"""
Harmonization and Anomaly Detection Agent for APIx.

Responsibilities:
1. Economic Fare Standardization:
   - Evaluates fares using the standard definition:
     index_fare = base_fare + mandatory_taxes
   - Explicitly separates and excludes:
     * User Development Fees (UDF): Airport development levies tied to airport infrastructure rather than airline yield management.
     * Convenience fees / OTA fees: Transaction surcharges that distort cross-source and cross-payment comparisons.
     * Optional ancillary add-ons (meals, baggage, seat assignment).
2. Cross-Source Deduplication:
   - Identifies identical flights (origin, destination, travel_date, flight_number) captured from multiple sources.
   - Retains the most direct/authoritative quote.
3. Statistical & Rule-Based Anomaly Detection:
   - Implausible low/high bounds (e.g., ₹100 on metro route, > ₹50,000 domestic economy).
   - Missing or zero base fare components.
   - Sudden price spikes/collapses against the route's rolling median using Median Absolute Deviation (MAD)
     and Interquartile Range (IQR) robust outlier detection rules.
4. Non-Dropping Guarantee:
   - Anomalies are NEVER silently dropped. They are retained with `is_anomaly=True` and exported
     as auditable `AnomalyRecord` entries with explicit reason codes for economist review.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
import logging
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from apix.models.schemas import (
    AnomalyReason,
    AnomalyRecord,
    CleanedFare,
    RawFarePayload,
)

logger = logging.getLogger(__name__)

# Default implausibility thresholds in INR
METRO_MIN_FARE = Decimal("1000.00")      # Below ₹1,000 on metro trunk is typically a scraper misparse or error fare
REGIONAL_MIN_FARE = Decimal("600.00")    # Below ₹600 on regional routes
DOMESTIC_MAX_FARE = Decimal("50000.00")  # Above ₹50,000 for one-way domestic economy


@dataclass
class HarmonizationResult:
    """Outcome of harmonizing a batch of raw fare payloads."""
    cleaned_fares: List[CleanedFare]
    anomalies: List[AnomalyRecord]
    total_processed: int
    duplicates_removed: int

    @property
    def clean_count(self) -> int:
        return len([f for f in self.cleaned_fares if not f.is_anomaly])

    @property
    def anomaly_count(self) -> int:
        return len(self.anomalies)


class Harmonizer:
    """
    Agent responsible for data harmonization, deduplication, and anomaly auditing.
    """

    def __init__(
        self,
        metro_min_fare: Decimal = METRO_MIN_FARE,
        regional_min_fare: Decimal = REGIONAL_MIN_FARE,
        domestic_max_fare: Decimal = DOMESTIC_MAX_FARE,
        mad_threshold: float = 3.0,
        iqr_multiplier: float = 1.5,
    ) -> None:
        self.metro_min_fare = metro_min_fare
        self.regional_min_fare = regional_min_fare
        self.domestic_max_fare = domestic_max_fare
        self.mad_threshold = mad_threshold
        self.iqr_multiplier = iqr_multiplier

    def harmonize_payload(self, raw: RawFarePayload) -> CleanedFare:
        """
        Transform a RawFarePayload into a CleanedFare using standard economic definitions.
        
        Definition:
            index_fare = base_fare + taxes
        Excludes UDF and convenience fees.
        """
        # Ensure Decimal calculation
        base_fare = Decimal(str(raw.fare_components.base_fare))
        taxes = Decimal(str(raw.fare_components.taxes))
        udf = Decimal(str(raw.fare_components.udf))
        conv_fee = Decimal(str(raw.fare_components.convenience_fee))

        # Core economic price index definition
        index_fare = base_fare + taxes

        return CleanedFare(
            source=raw.source,
            origin=raw.origin.upper(),
            destination=raw.destination.upper(),
            route=f"{raw.origin.upper()}-{raw.destination.upper()}",
            travel_date=raw.travel_date,
            lead_time_days=raw.lead_time_days,
            lead_time_bucket=raw.lead_time_bucket,
            airline=raw.airline,
            flight_number=raw.flight_number.strip().upper(),
            index_fare=index_fare,
            base_fare=base_fare,
            taxes=taxes,
            udf=udf,
            convenience_fee=conv_fee,
            currency=raw.currency,
            scraped_at=raw.scraped_at,
            is_anomaly=False,
            anomaly_reasons=[],
        )

    def deduplicate(self, fares: List[CleanedFare]) -> Tuple[List[CleanedFare], int]:
        """
        Deduplicate fare observations representing the identical flight.
        
        Key: (origin, destination, travel_date, flight_number)
        If duplicates exist across sources or multiple scrapes on the same day:
        - Prefers direct airline sources (e.g. source=='indigo' for 6E flights)
        - Otherwise takes the lowest quoted index_fare
        """
        grouped: Dict[Tuple[str, str, Any, str], List[CleanedFare]] = {}
        for fare in fares:
            key = (fare.origin, fare.destination, fare.travel_date, fare.flight_number)
            grouped.setdefault(key, []).append(fare)

        deduped: List[CleanedFare] = []
        duplicates_removed = 0

        for key, items in grouped.items():
            if len(items) == 1:
                deduped.append(items[0])
            else:
                duplicates_removed += len(items) - 1
                # Sort: priority to direct airline source, then lowest price
                items.sort(
                    key=lambda x: (
                        0 if x.source.lower() in x.airline.lower() else 1,
                        x.index_fare,
                    )
                )
                chosen = items[0]
                logger.debug(
                    "Deduplicated flight %s on %s: kept quote from %s (₹%s)",
                    chosen.flight_number,
                    chosen.travel_date,
                    chosen.source,
                    chosen.index_fare,
                )
                deduped.append(chosen)

        return deduped, duplicates_removed

    def _check_rule_anomalies(
        self,
        fare: CleanedFare,
        tier: str = "metro",
    ) -> List[Tuple[AnomalyReason, str]]:
        """Check static boundary rules (zero fare, implausible low/high bounds)."""
        reasons: List[Tuple[AnomalyReason, str]] = []

        if fare.index_fare <= Decimal("0.00"):
            reasons.append(
                (AnomalyReason.SOLD_OUT, f"Fare is zero or sold-out: ₹{fare.index_fare}")
            )
            return reasons

        if fare.base_fare <= Decimal("0.00") and fare.index_fare > Decimal("0.00"):
            reasons.append(
                (
                    AnomalyReason.ZERO_BASE_FARE,
                    f"Suspicious zero base fare with total fare of ₹{fare.index_fare}",
                )
            )

        min_threshold = self.metro_min_fare if tier == "metro" else self.regional_min_fare
        if fare.index_fare < min_threshold:
            reasons.append(
                (
                    AnomalyReason.IMPLAUSIBLE_LOW,
                    f"Fare of ₹{fare.index_fare} is below {tier} corridor floor (₹{min_threshold})",
                )
            )

        if fare.index_fare > self.domestic_max_fare:
            reasons.append(
                (
                    AnomalyReason.IMPLAUSIBLE_HIGH,
                    f"Fare of ₹{fare.index_fare} exceeds domestic economy ceiling (₹{self.domestic_max_fare})",
                )
            )

        return reasons

    def detect_anomalies(
        self,
        fares: List[CleanedFare],
        route_tiers: Optional[Dict[str, str]] = None,
    ) -> Tuple[List[CleanedFare], List[AnomalyRecord]]:
        """
        Flag anomalies across rule-based bounds and robust statistical models (MAD and IQR).
        
        Guarantees:
        - NEVER drops an anomaly. All fares remain in the returned list.
        - Anomalous fares are marked `is_anomaly=True` with associated `AnomalyRecord` objects.
        """
        tiers = route_tiers or {}
        anomalies: List[AnomalyRecord] = []
        now_utc = datetime.now(timezone.utc)

        # ── Step 1: Static boundary checks ────────────────────────────
        for fare in fares:
            tier = tiers.get(fare.route, "metro")
            static_issues = self._check_rule_anomalies(fare, tier=tier)
            for reason_code, details in static_issues:
                fare.is_anomaly = True
                if reason_code not in fare.anomaly_reasons:
                    fare.anomaly_reasons.append(reason_code)
                anomalies.append(
                    AnomalyRecord(
                        fare_identifier=f"{fare.flight_number}_{fare.travel_date}_{fare.lead_time_bucket}",
                        route=fare.route,
                        flight_number=fare.flight_number,
                        travel_date=fare.travel_date,
                        lead_time_bucket=fare.lead_time_bucket,
                        observed_fare=fare.index_fare,
                        reason_code=reason_code,
                        details=details,
                        flagged_at=now_utc,
                    )
                )

        # ── Step 2: Statistical group checks (MAD & IQR) ─────────────
        # Group observations by (route, lead_time_bucket)
        buckets: Dict[Tuple[str, str], List[CleanedFare]] = {}
        for fare in fares:
            buckets.setdefault((fare.route, fare.lead_time_bucket), []).append(fare)

        for (route, bucket), bucket_fares in buckets.items():
            if len(bucket_fares) < 4:
                # Need at least 4 observations for meaningful distribution quartiles
                continue

            prices = np.array([float(f.index_fare) for f in bucket_fares])
            median_val = float(np.median(prices))

            # 1. Median Absolute Deviation (MAD)
            # MAD = median(|x - median|)
            # Normal consistency factor = 1.4826
            abs_devs = np.abs(prices - median_val)
            mad = float(np.median(abs_devs))
            scale = 1.4826 * mad if mad > 0 else 0.0

            # 2. Interquartile Range (IQR)
            q75, q25 = np.percentile(prices, [75, 25])
            iqr = q75 - q25
            lower_fence = q25 - (self.iqr_multiplier * iqr)
            upper_fence = q75 + (self.iqr_multiplier * iqr)

            for idx, fare in enumerate(bucket_fares):
                p = prices[idx]

                # Check MAD outlier
                if scale > 0.0:
                    z_mad = abs(p - median_val) / scale
                    if z_mad > self.mad_threshold:
                        fare.is_anomaly = True
                        if AnomalyReason.SUDDEN_JUMP_MAD not in fare.anomaly_reasons:
                            fare.anomaly_reasons.append(AnomalyReason.SUDDEN_JUMP_MAD)
                        anomalies.append(
                            AnomalyRecord(
                                fare_identifier=f"{fare.flight_number}_{fare.travel_date}_{fare.lead_time_bucket}",
                                route=fare.route,
                                flight_number=fare.flight_number,
                                travel_date=fare.travel_date,
                                lead_time_bucket=fare.lead_time_bucket,
                                observed_fare=fare.index_fare,
                                benchmark_fare=Decimal(f"{median_val:.2f}"),
                                reason_code=AnomalyReason.SUDDEN_JUMP_MAD,
                                details=(
                                    f"Price ₹{p:.2f} deviates from route rolling median ₹{median_val:.2f} "
                                    f"(MAD z-score={z_mad:.2f} > {self.mad_threshold})"
                                ),
                                flagged_at=now_utc,
                            )
                        )

                # Check IQR outlier
                if iqr > 0 and (p < lower_fence or p > upper_fence):
                    fare.is_anomaly = True
                    if AnomalyReason.SUDDEN_JUMP_IQR not in fare.anomaly_reasons:
                        fare.anomaly_reasons.append(AnomalyReason.SUDDEN_JUMP_IQR)
                    anomalies.append(
                        AnomalyRecord(
                            fare_identifier=f"{fare.flight_number}_{fare.travel_date}_{fare.lead_time_bucket}",
                            route=fare.route,
                            flight_number=fare.flight_number,
                            travel_date=fare.travel_date,
                            lead_time_bucket=fare.lead_time_bucket,
                            observed_fare=fare.index_fare,
                            benchmark_fare=Decimal(f"{median_val:.2f}"),
                            reason_code=AnomalyReason.SUDDEN_JUMP_IQR,
                            details=(
                                f"Price ₹{p:.2f} outside IQR bounds [₹{lower_fence:.2f}, ₹{upper_fence:.2f}]"
                            ),
                            flagged_at=now_utc,
                        )
                    )

        return fares, anomalies

    def process_batch(
        self,
        raw_payloads: List[RawFarePayload],
        route_tiers: Optional[Dict[str, str]] = None,
    ) -> HarmonizationResult:
        """
        Execute full harmonization pipeline:
        1. Parse & calculate economic index fare (base + taxes).
        2. Deduplicate duplicate flights across sources.
        3. Flag rule-based and statistical anomalies.
        """
        logger.info("Harmonizing batch of %d raw fare payloads", len(raw_payloads))
        standardized = [self.harmonize_payload(p) for p in raw_payloads]
        deduped, dupes_removed = self.deduplicate(standardized)
        cleaned_fares, anomalies = self.detect_anomalies(deduped, route_tiers=route_tiers)

        result = HarmonizationResult(
            cleaned_fares=cleaned_fares,
            anomalies=anomalies,
            total_processed=len(raw_payloads),
            duplicates_removed=dupes_removed,
        )

        logger.info(
            "Harmonization complete: %d clean fares, %d flagged anomalies, %d duplicates removed",
            result.clean_count,
            result.anomaly_count,
            result.duplicates_removed,
        )
        return result
