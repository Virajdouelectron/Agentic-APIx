"""Initial schema — fare_records, anomaly_records, index_records, orchestrator_runs

Revision ID: 0001
Revises: None
Create Date: 2026-10-12
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── fare_records ─────────────────────────────────────────────
    op.create_table(
        "fare_records",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source", sa.String(32), nullable=False, index=True),
        sa.Column("origin", sa.String(3), nullable=False, index=True),
        sa.Column("destination", sa.String(3), nullable=False, index=True),
        sa.Column("route", sa.String(7), nullable=False, index=True),
        sa.Column("travel_date", sa.Date(), nullable=False, index=True),
        sa.Column("lead_time_days", sa.Integer(), nullable=False, index=True),
        sa.Column("lead_time_bucket", sa.String(8), nullable=False, index=True),
        sa.Column("airline", sa.String(64), nullable=False, index=True),
        sa.Column("flight_number", sa.String(32), nullable=False),
        sa.Column("index_fare", sa.Numeric(10, 2), nullable=False),
        sa.Column("base_fare", sa.Numeric(10, 2), nullable=False),
        sa.Column("taxes", sa.Numeric(10, 2), nullable=False),
        sa.Column("udf", sa.Numeric(10, 2), server_default="0.00"),
        sa.Column("convenience_fee", sa.Numeric(10, 2), server_default="0.00"),
        sa.Column("currency", sa.String(3), server_default="INR"),
        sa.Column("is_anomaly", sa.Boolean(), server_default="false", index=True),
        sa.Column("scraped_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_index(
        "ix_fare_records_route_travel", "fare_records", ["route", "travel_date"]
    )
    op.create_index(
        "ix_fare_records_route_lead", "fare_records", ["route", "lead_time_bucket"]
    )

    # ── anomaly_records ──────────────────────────────────────────
    op.create_table(
        "anomaly_records",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "fare_id",
            sa.Integer(),
            sa.ForeignKey("fare_records.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("route", sa.String(7), nullable=False, index=True),
        sa.Column("flight_number", sa.String(32), nullable=False),
        sa.Column("travel_date", sa.Date(), nullable=False),
        sa.Column("lead_time_bucket", sa.String(8), nullable=False),
        sa.Column("observed_fare", sa.Numeric(10, 2), nullable=False),
        sa.Column("benchmark_fare", sa.Numeric(10, 2), nullable=True),
        sa.Column(
            "reason_code",
            sa.Enum(
                "IMPLAUSIBLE_LOW",
                "IMPLAUSIBLE_HIGH",
                "SUDDEN_JUMP_MAD",
                "SUDDEN_JUMP_IQR",
                "MISSING_COMPONENTS",
                "SOLD_OUT",
                "ZERO_BASE_FARE",
                name="anomalyreason",
            ),
            nullable=False,
            index=True,
        ),
        sa.Column("details", sa.Text(), nullable=False),
        sa.Column("flagged_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # ── index_records ────────────────────────────────────────────
    op.create_table(
        "index_records",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("observation_date", sa.Date(), nullable=False, index=True),
        sa.Column(
            "frequency",
            sa.Enum("daily", "weekly", "monthly", name="indexfrequency"),
            nullable=False,
            index=True,
        ),
        sa.Column("route", sa.String(7), server_default="ALL", index=True),
        sa.Column("index_value", sa.Numeric(8, 2), nullable=False),
        sa.Column("base_period", sa.String(16), nullable=False),
        sa.Column("passenger_weight_version", sa.String(16), server_default="v1"),
        sa.Column("sample_size", sa.Integer(), server_default="0"),
        sa.Column("calculated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_index(
        "ix_index_records_freq_date_route",
        "index_records",
        ["frequency", "observation_date", "route"],
        unique=True,
    )

    # ── orchestrator_runs ────────────────────────────────────────
    op.create_table(
        "orchestrator_runs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("jobs_planned", sa.Integer(), server_default="0"),
        sa.Column("jobs_succeeded", sa.Integer(), server_default="0"),
        sa.Column("jobs_retried", sa.Integer(), server_default="0"),
        sa.Column("jobs_failed", sa.Integer(), server_default="0"),
        sa.Column("duration_seconds", sa.Numeric(10, 2), nullable=True),
        sa.Column("status", sa.String(24), server_default="RUNNING"),
        sa.Column("error_summary", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("orchestrator_runs")
    op.drop_table("index_records")
    op.drop_table("anomaly_records")
    op.drop_table("fare_records")
    op.execute("DROP TYPE IF EXISTS anomalyreason")
    op.execute("DROP TYPE IF EXISTS indexfrequency")
