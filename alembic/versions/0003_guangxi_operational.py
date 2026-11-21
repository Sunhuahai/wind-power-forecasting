"""广西真实风电场业务结构：采用独立 UTC 时序表，不修改竞赛实验表。

生产目标为 PostgreSQL + TimescaleDB。此迁移只创建 PostgreSQL 表，
hypertable 激活须按 docs/部署与工程/guangxi-timescale-activation.sql 单独执行。
既有 hourly/normalized 实验不受影响。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def _utc(name: str, **kwargs):
    return sa.Column(name, sa.DateTime(timezone=True), **kwargs)


def _json(name: str):
    return sa.Column(name, JSONB, nullable=False,
                     server_default=sa.text("'{}'::jsonb"))


def upgrade():
    op.create_table(
        "farm_capacity_history",
        sa.Column("farm_id", sa.Integer, sa.ForeignKey("wind_farms.id"), nullable=False),
        _utc("effective_at", nullable=False),
        _utc("effective_to"),
        sa.Column("capacity_mw", sa.Float, nullable=False),
        sa.Column("source", sa.String(128), nullable=False),
        _utc("recorded_at", nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("farm_id", "effective_at"),
        sa.CheckConstraint("capacity_mw > 0", name="ck_gx_capacity_positive"),
        sa.CheckConstraint("effective_to IS NULL OR effective_to > effective_at",
                           name="ck_gx_capacity_interval"),
    )
    op.create_table(
        "operational_power",
        sa.Column("farm_id", sa.Integer, sa.ForeignKey("wind_farms.id"), nullable=False),
        _utc("valid_at", nullable=False),
        sa.Column("source", sa.String(128), nullable=False),
        _utc("received_at", nullable=False),
        sa.Column("actual_mw", sa.Float),
        sa.Column("available_mw", sa.Float),
        sa.Column("theoretical_mw", sa.Float),
        sa.Column("curtailed", sa.Boolean, nullable=False, server_default=sa.text("false")),
        _json("quality"),
        sa.PrimaryKeyConstraint("farm_id", "valid_at", "source", "received_at"),
        sa.CheckConstraint("actual_mw IS NULL OR actual_mw >= 0",
                           name="ck_gx_power_actual"),
        sa.CheckConstraint("available_mw IS NULL OR available_mw >= 0",
                           name="ck_gx_power_available"),
        sa.CheckConstraint("theoretical_mw IS NULL OR theoretical_mw >= 0",
                           name="ck_gx_power_theoretical"),
    )
    op.create_index("ix_gx_power_asof", "operational_power",
                    ["farm_id", "valid_at", "received_at"])
    op.create_table(
        "operational_nwp",
        sa.Column("farm_id", sa.Integer, sa.ForeignKey("wind_farms.id"), nullable=False),
        _utc("valid_at", nullable=False),
        sa.Column("source", sa.String(128), nullable=False),
        _utc("issued_at", nullable=False),
        _utc("received_at", nullable=False),
        sa.Column("u100", sa.Float),
        sa.Column("v100", sa.Float),
        sa.Column("u10", sa.Float),
        sa.Column("v10", sa.Float),
        sa.Column("temperature_k", sa.Float),
        sa.Column("pressure_pa", sa.Float),
        _json("extra"),
        sa.PrimaryKeyConstraint("farm_id", "valid_at", "source",
                                "issued_at", "received_at"),
        sa.CheckConstraint("valid_at > issued_at", name="ck_gx_nwp_future"),
    )
    op.create_index("ix_gx_nwp_asof", "operational_nwp",
                    ["farm_id", "valid_at", "issued_at", "received_at"])
    op.create_table(
        "operational_events",
        sa.Column("event_id", sa.String(64), primary_key=True),
        sa.Column("farm_id", sa.Integer, sa.ForeignKey("wind_farms.id"), nullable=False),
        _utc("start_at", nullable=False),
        _utc("end_at", nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("approval_status", sa.String(32), nullable=False,
                  server_default=sa.text("'pending'")),
        sa.Column("evidence_uri", sa.String(1024)),
        _utc("recorded_at", nullable=False, server_default=sa.text("now()")),
        _json("metadata"),
        sa.CheckConstraint("end_at > start_at", name="ck_gx_event_interval"),
        sa.CheckConstraint("approval_status IN ('pending','approved','rejected')",
                           name="ck_gx_event_approval"),
    )
    op.create_index("ix_gx_events_farm_time", "operational_events",
                    ["farm_id", "start_at", "end_at"])
    op.create_table(
        "forecast_issuances",
        sa.Column("issuance_id", sa.String(64), primary_key=True),
        sa.Column("farm_id", sa.Integer, sa.ForeignKey("wind_farms.id"), nullable=False),
        sa.Column("product", sa.String(32), nullable=False),
        sa.Column("series_id", sa.String(128), nullable=False),
        sa.Column("model_version", sa.String(128), nullable=False),
        _utc("issued_at", nullable=False),
        _utc("cutoff_at", nullable=False),
        _utc("created_at", nullable=False, server_default=sa.text("now()")),
        sa.Column("stage", sa.String(32), nullable=False,
                  server_default=sa.text("'generated'")),
        _json("input_snapshot"),
        sa.UniqueConstraint("farm_id", "product", "series_id", "issued_at",
                            name="uq_gx_forecast_issue_series"),
        sa.CheckConstraint("cutoff_at <= issued_at", name="ck_gx_forecast_cutoff"),
        sa.CheckConstraint("product IN ('day_ahead','ultra_short','medium')",
                           name="ck_gx_forecast_product"),
    )
    op.create_index("ix_gx_issuances_lookup", "forecast_issuances",
                    ["farm_id", "product", "series_id", "issued_at"])
    op.create_table(
        "forecast_values",
        sa.Column("issuance_id", sa.String(64),
                  sa.ForeignKey("forecast_issuances.issuance_id"), nullable=False),
        _utc("valid_at", nullable=False),
        sa.Column("lead_minutes", sa.Integer, nullable=False),
        sa.Column("predicted_mw", sa.Float, nullable=False),
        sa.Column("p10_mw", sa.Float),
        sa.Column("p90_mw", sa.Float),
        sa.PrimaryKeyConstraint("issuance_id", "valid_at"),
        sa.CheckConstraint("lead_minutes > 0", name="ck_gx_forecast_lead"),
        sa.CheckConstraint("predicted_mw >= 0", name="ck_gx_forecast_power"),
        sa.CheckConstraint("p10_mw IS NULL OR p10_mw >= 0",
                           name="ck_gx_forecast_p10"),
        sa.CheckConstraint("p90_mw IS NULL OR p90_mw >= 0",
                           name="ck_gx_forecast_p90"),
        sa.CheckConstraint("p10_mw IS NULL OR p90_mw IS NULL OR p10_mw <= p90_mw",
                           name="ck_gx_forecast_quantile_order"),
    )
    op.create_index("ix_gx_forecast_valid", "forecast_values",
                    ["valid_at", "issuance_id"])
    op.create_table(
        "dispatch_submissions",
        sa.Column("submission_id", sa.String(64), primary_key=True),
        sa.Column("issuance_id", sa.String(64),
                  sa.ForeignKey("forecast_issuances.issuance_id"), nullable=False),
        _utc("submitted_at", nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("payload_uri", sa.String(1024), nullable=False),
        sa.Column("endpoint", sa.String(256)),
        _json("metadata"),
        sa.CheckConstraint("length(payload_sha256) = 64",
                           name="ck_gx_submission_hash"),
    )
    op.create_index("ix_gx_submissions_issue", "dispatch_submissions",
                    ["issuance_id", "submitted_at"])
    op.create_table(
        "dispatch_receipts",
        sa.Column("receipt_id", sa.String(64), primary_key=True),
        sa.Column("submission_id", sa.String(64),
                  sa.ForeignKey("dispatch_submissions.submission_id"), nullable=False),
        _utc("received_at", nullable=False),
        sa.Column("status", sa.String(64), nullable=False),
        _json("response"),
    )
    op.create_index("ix_gx_receipts_submission", "dispatch_receipts",
                    ["submission_id", "received_at"])
    op.create_table(
        "gx_policy_versions",
        sa.Column("code", sa.String(64), primary_key=True),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("source_url", sa.String(1024), nullable=False),
        _json("parameters"),
        _utc("recorded_at", nullable=False, server_default=sa.text("now()")),
    )
    policy_table = sa.table("gx_policy_versions",
        sa.column("code", sa.String), sa.column("status", sa.String),
        sa.column("source_url", sa.String), sa.column("parameters", JSONB))
    op.bulk_insert(policy_table, [
        dict(code="GX_WIND_PUBLIC_2025", status="published_rule",
             source_url="https://nfj.nea.gov.cn/xwzx/tzgg/202502/P020250214585645425665.pdf",
             parameters={"day_ahead_threshold": 0.60, "ultra_short_threshold": 0.65,
                         "a3_guangxi": 2, "a4_guangxi": 2,
                         "accuracy_denominator_floor": 0.2,
                         "ultra_short_mode": "mean_then_daily_rmse"}),
        dict(code="GX_WIND_DRAFT_202608",
             status="consultation_draft_not_confirmed_effective",
             source_url="https://nfj.nea.gov.cn/xwzx/tzgg/202608/P020260824351987511048.pdf",
             parameters={"day_ahead_threshold": 0.65, "ultra_short_threshold": 0.75,
                         "ultra_short_mode": "weighted_per_target_rmse_then_daily_mean",
                         "draft_hour_weights": [4, 3, 2, 1],
                         "within_hour_equal_weights": "assumption_pending_confirmation"}),
    ])
    op.create_table(
        "gx_daily_evaluations",
        sa.Column("evaluation_id", sa.String(64), primary_key=True),
        sa.Column("farm_id", sa.Integer, sa.ForeignKey("wind_farms.id"), nullable=False),
        sa.Column("target_date", sa.Date, nullable=False),
        sa.Column("product", sa.String(32), nullable=False),
        sa.Column("policy_code", sa.String(64),
                  sa.ForeignKey("gx_policy_versions.code"), nullable=False),
        sa.Column("model_version", sa.String(128)),
        sa.Column("series_id", sa.String(128)),
        sa.Column("issuance_id", sa.String(64),
                  sa.ForeignKey("forecast_issuances.issuance_id")),
        _utc("evaluated_at", nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("scored_targets", sa.Integer, nullable=False),
        sa.Column("accuracy", sa.Float),
        sa.Column("mae_mw", sa.Float),
        sa.Column("rmse_mw", sa.Float),
        _json("detail"),
        sa.CheckConstraint("scored_targets BETWEEN 0 AND 96",
                           name="ck_gx_score_target_count"),
    )
    op.create_index("ix_gx_daily_scores", "gx_daily_evaluations",
                    ["farm_id", "target_date", "policy_code", "product"])


def downgrade():
    bind = op.get_bind()
    exists = bind.execute(sa.text(
        "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname='timescaledb')"
    )).scalar()
    if exists:
        active = bind.execute(sa.text(
            "SELECT hypertable_name FROM timescaledb_information.hypertables "
            "WHERE hypertable_name IN "
            "('operational_power','operational_nwp','forecast_values')"
        )).scalars().all()
        if active:
            raise RuntimeError("Active Timescale hypertables: restore an audited backup instead")
    for table in (
        "gx_daily_evaluations", "gx_policy_versions", "dispatch_receipts",
        "dispatch_submissions", "forecast_values", "forecast_issuances",
        "operational_events", "operational_nwp", "operational_power",
        "farm_capacity_history",
    ):
        op.drop_table(table)
