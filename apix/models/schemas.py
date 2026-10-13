"""
Pydantic v2 schemas for APIx data contracts.

Defines schemas for:
- Raw scraper payloads emitted by extraction agents
- Cleaned and harmonized fare records
- Anomaly audit records with reason codes
- Economic index results and lead-time elasticity points
- API request and response contracts
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from pydantic import BaseModel, ConfigDict, Field, field_validator


class AnomalyReason(str, Enum):
    """Categorical reason codes for fare anomalies requiring audit."""
    IMPLAUSIBLE_LOW = "IMPLAUSIBLE_LOW"        # e.g., < ₹500 on metro route
    IMPLAUSIBLE_HIGH = "IMPLAUSIBLE_HIGH"      # e.g., > ₹60,000 economy domestic
    SUDDEN_JUMP_MAD = "SUDDEN_JUMP_MAD"        # Outlier vs rolling median via MAD rule
    SUDDEN_JUMP_IQR = "SUDDEN_JUMP_IQR"        # Outlier vs IQR envelope
    MISSING_COMPONENTS = "MISSING_COMPONENTS"  # Missing base fare or taxes
    SOLD_OUT = "SOLD_OUT"                      # Flight listed but zero seats/fare
    ZERO_BASE_FARE = "ZERO_BASE_FARE"          # Suspicious zero base fare


class FareBreakdown(BaseModel):
    """
    Component-wise breakdown of an airfare quote.

    All amounts are strictly Decimals to prevent floating-point inaccuracies
    in financial and economic accounting.
    """
    model_config = ConfigDict(frozen=True)

    base_fare: Decimal = Field(ge=0, description="Base airline fare before taxes")
    taxes: Decimal = Field(ge=0, default=Decimal("0"), description="Mandatory government taxes and surcharges")
    udf: Decimal = Field(ge=0, default=Decimal("0"), description="User Development Fee (airport specific)")
    convenience_fee: Decimal = Field(ge=0, default=Decimal("0"), description="Booking/convenience fee")
    total_fare: Decimal | None = Field(default=None, description="Reported total price for validation checks")

    @property
    def index_fare(self) -> Decimal:
        """
        Standard APIx economic fare definition:
        Base Fare + Mandatory Taxes (excluding UDF and OTA convenience fees).
        """
        return self.base_fare + self.taxes


class RawFarePayload(BaseModel):
    """
    Raw, timestamped JSON payload produced by an extraction agent.

    Each source yields instances of this model before harmonization.
    """
    source: str = Field(description="Extractor identifier, e.g., 'indigo', 'makemytrip'")
    route: str = Field(description="Origin-Destination IATA pair, e.g. 'DEL-BOM'")
    origin: str = Field(min_length=3, max_length=3, description="Origin IATA code")
    destination: str = Field(min_length=3, max_length=3, description="Destination IATA code")
    travel_date: date = Field(description="Scheduled flight departure date")
    lead_time_bucket: str = Field(description="Lead-time bucket, e.g. 'T+1', 'T+7', 'T+15', 'T+30', 'T+45'")
    lead_time_days: int = Field(ge=0, description="Booking lead time in days")
    airline: str = Field(description="Operating airline name or IATA code")
    flight_number: str = Field(description="Airline flight number, e.g., '6E-204'")
    fare_components: FareBreakdown = Field(description="Granular fare items")
    currency: str = Field(default="INR", description="Three-letter currency code")
    scraped_at: datetime = Field(description="UTC timestamp of extraction")
    scraper_version: str = Field(default="1.0.0", description="Version of the scraper module")
    raw_metadata: dict[str, Any] = Field(default_factory=dict, description="Diagnostic metadata")

    @field_validator("route", mode="before")
    @classmethod
    def normalize_route(cls, v: Any) -> str:
        if isinstance(v, str):
            return v.upper().strip()
        return str(v)


class AnomalyRecord(BaseModel):
    """
    Audit log item for an anomalous fare observation.
    Anomalies are flagged and retained for audit, never silently discarded.
    """
    id: str | None = None
    fare_identifier: str = Field(description="Composite key or flight reference")
    route: str = Field(description="Origin-Destination pair, e.g. 'DEL-BOM'")
    flight_number: str
    travel_date: date
    lead_time_bucket: str
    observed_fare: Decimal
    benchmark_fare: Decimal | None = None
    reason_code: AnomalyReason
    details: str
    flagged_at: datetime


class CleanedFare(BaseModel):
    """
    Harmonized, validated, and normalized fare ready for index calculation.
    """
    source: str
    origin: str
    destination: str
    route: str
    travel_date: date
    lead_time_days: int
    lead_time_bucket: str
    airline: str
    flight_number: str
    # The official APIx target metric: base fare + mandatory taxes
    index_fare: Decimal
    base_fare: Decimal
    taxes: Decimal
    udf: Decimal
    convenience_fee: Decimal
    currency: str = "INR"
    scraped_at: datetime
    is_anomaly: bool = False
    anomaly_reasons: list[AnomalyReason] = Field(default_factory=list)


class IndexFrequency(str, Enum):
    """Aggregation frequency for the APIx index."""
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class IndexPoint(BaseModel):
    """A single calculated index observation."""
    observation_date: date
    frequency: IndexFrequency
    route: str | None = Field(default=None, description="Specific route or 'ALL' for composite")
    index_value: float = Field(description="Normalized index value (base period = 100)")
    base_period: str = Field(description="Reference base period, e.g., '2026-01'")
    passenger_weight_version: str = Field(default="v1", description="DGCA traffic weight version used")
    sample_size: int = Field(default=0, description="Number of fare observations in bucket")


class ElasticityPoint(BaseModel):
    """Point along the advance-purchase lead-time price curve."""
    route: str
    lead_time_days: int
    lead_time_bucket: str
    mean_fare: float
    median_fare: float
    std_dev: float
    iqr: float
    sample_size: int


class RunSummary(BaseModel):
    """Run summary emitted by the Orchestrator agent."""
    run_id: str
    started_at: datetime
    completed_at: datetime
    jobs_planned: int
    jobs_succeeded: int
    jobs_retried: int
    jobs_failed: int
    duration_seconds: float


class HealthResponse(BaseModel):
    """System health check response."""
    status: str
    timestamp: datetime
    version: str
    database_connected: bool
    details: dict[str, Any] = Field(default_factory=dict)
