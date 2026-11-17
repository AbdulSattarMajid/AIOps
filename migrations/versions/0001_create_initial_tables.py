"""Create initial tables: incidents, analyses, fix_attempts, deliveries.

Revision ID: 0001
Revises:
Create Date: 2026-11-17 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # incidents
    # ------------------------------------------------------------------
    op.create_table(
        "incidents",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("signature", sa.Text(), nullable=False),
        sa.Column("service_name", sa.Text(), nullable=True),
        sa.Column("error_class", sa.Text(), nullable=True),
        sa.Column("redacted_message", sa.Text(), nullable=True),
        sa.Column("crash_file", sa.Text(), nullable=True),
        sa.Column("crash_function", sa.Text(), nullable=True),
        sa.Column("occurrences", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_incidents"),
        sa.UniqueConstraint("signature", name="uq_incidents_signature"),
    )

    # ------------------------------------------------------------------
    # analyses
    # ------------------------------------------------------------------
    op.create_table(
        "analyses",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("incident_id", sa.Integer(), nullable=False),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("signals", postgresql.JSONB(), nullable=True),
        sa.Column("classifier_on", sa.Boolean(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["incident_id"],
            ["incidents.id"],
            name="fk_analyses_incident_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_analyses"),
    )
    op.create_index("ix_analyses_incident_id", "analyses", ["incident_id"])
    op.create_index("ix_analyses_category", "analyses", ["category"])

    # ------------------------------------------------------------------
    # fix_attempts
    # ------------------------------------------------------------------
    op.create_table(
        "fix_attempts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("analysis_id", sa.Integer(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("diff_text", sa.Text(), nullable=True),
        sa.Column("validator_ok", sa.Boolean(), nullable=True),
        sa.Column("verification_status", sa.Text(), nullable=True),
        sa.Column("tests_before_passed", sa.Integer(), nullable=True),
        sa.Column("tests_before_failed", sa.Integer(), nullable=True),
        sa.Column("tests_after_passed", sa.Integer(), nullable=True),
        sa.Column("tests_after_failed", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["analysis_id"],
            ["analyses.id"],
            name="fk_fix_attempts_analysis_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_fix_attempts"),
    )
    op.create_index("ix_fix_attempts_analysis_id", "fix_attempts", ["analysis_id"])

    # ------------------------------------------------------------------
    # deliveries
    # ------------------------------------------------------------------
    op.create_table(
        "deliveries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("analysis_id", sa.Integer(), nullable=False),
        sa.Column("pr_url", sa.Text(), nullable=True),
        sa.Column("branch", sa.Text(), nullable=True),
        sa.Column("slack_sent", sa.Boolean(), nullable=True),
        sa.Column(
            "outcome",
            sa.Text(),
            nullable=False,
            server_default="open",
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "outcome IN ('open', 'merged', 'closed', 'ignored')",
            name="ck_deliveries_outcome",
        ),
        sa.ForeignKeyConstraint(
            ["analysis_id"],
            ["analyses.id"],
            name="fk_deliveries_analysis_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_deliveries"),
    )
    op.create_index("ix_deliveries_analysis_id", "deliveries", ["analysis_id"])


def downgrade() -> None:
    op.drop_index("ix_deliveries_analysis_id", table_name="deliveries")
    op.drop_table("deliveries")
    op.drop_index("ix_fix_attempts_analysis_id", table_name="fix_attempts")
    op.drop_table("fix_attempts")
    op.drop_index("ix_analyses_category", table_name="analyses")
    op.drop_index("ix_analyses_incident_id", table_name="analyses")
    op.drop_table("analyses")
    op.drop_table("incidents")
