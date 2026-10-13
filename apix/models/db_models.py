"""
SQLAlchemy 2.x ORM models for database persistence.

Tables:
- fare_records: Cleaned airfare observations with component breakdown
- anomaly_records: Flagged fare anomalies with categorical reason codes
- index_records: Aggregated economic price index observations
- orchestrator_runs: Run summaries and job execution logs
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from apix.models.schemas import AnomalyReason, IndexFrequency


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy 2.x declarative models."""
    pass


class FareRecord(Base):
    """
    Persisted airfare price record.

    Stores the normalized economic metric (base fare + mandatory taxes)
    alongside individual components for complete auditability.
    """
    __tablename__ = "fare_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    origin: Mapped[str] = mapped_column(String(3), index=True)
    destination: Mapped[str] = mapped_column(String(3), index=True)
    route: Mapped[str] = mapped_column(String(7), index=True)
    travel_date: Mapped[date] = mapped_column(Date, index=True)
    lead_time_days: Mapped[int] = mapped_column(Integer, index=True)
    lead_time_bucket: Mapped[str] = mapped_column(String(8), index=True)
    airline: Mapped[str] = mapped_column(String(64), index=True)
    flight_number: Mapped[str] = mapped_column(String(32))

    # Core economic metric: base_fare + taxes
    index_fare: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    base_fare: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    taxes: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    udf: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0.00"))
    convenience_fee: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0.00"))
    currency: Mapped[str] = mapped_column(String(3), default="INR")

    is_anomaly: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    scraped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow
    )

    # Relationships
    anomalies: Mapped[List["AnomalyRecordDB"]] = relationship(
        back_populates="fare_record", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_fare_records_route_travel", "route", "travel_date"),
        Index("ix_fare_records_route_lead", "route", "lead_time_bucket"),
    )


class AnomalyRecordDB(Base):
    """
    Audit log of flagged anomalies linked to fare records.
    """
    __tablename__ = "anomaly_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fare_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("fare_records.id", ondelete="SET NULL"), nullable=True
    )
    route: Mapped[str] = mapped_column(String(7), index=True)
    flight_number: Mapped[str] = mapped_column(String(32))
    travel_date: Mapped[date] = mapped_column(Date)
    lead_time_bucket: Mapped[str] = mapped_column(String(8))
    observed_fare: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    benchmark_fare: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2), nullable=True)
    reason_code: Mapped[AnomalyReason] = mapped_column(SQLEnum(AnomalyReason), index=True)
    details: Mapped[str] = mapped_column(Text)
    flagged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow
    )

    fare_record: Mapped[Optional["FareRecord"]] = relationship(back_populates="anomalies")


class IndexRecord(Base):
    """
    Computed index observations at daily, weekly, or monthly resolution.
    """
    __tablename__ = "index_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    observation_date: Mapped[date] = mapped_column(Date, index=True)
    frequency: Mapped[IndexFrequency] = mapped_column(SQLEnum(IndexFrequency), index=True)
    route: Mapped[str] = mapped_column(String(7), default="ALL", index=True)
    index_value: Mapped[Decimal] = mapped_column(Numeric(8, 2))
    base_period: Mapped[str] = mapped_column(String(16))
    passenger_weight_version: Mapped[str] = mapped_column(String(16), default="v1")
    sample_size: Mapped[int] = mapped_column(Integer, default=0)
    calculated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow
    )

    __table_args__ = (
        Index("ix_index_records_freq_date_route", "frequency", "observation_date", "route", unique=True),
    )


class OrchestratorRun(Base):
    """
    Execution run metadata and job counters for pipeline health audits.
    """
    __tablename__ = "orchestrator_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    jobs_planned: Mapped[int] = mapped_column(Integer, default=0)
    jobs_succeeded: Mapped[int] = mapped_column(Integer, default=0)
    jobs_retried: Mapped[int] = mapped_column(Integer, default=0)
    jobs_failed: Mapped[int] = mapped_column(Integer, default=0)
    duration_seconds: Mapped[Optional[float]] = mapped_column(Numeric(10, 2), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="RUNNING")
    error_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
