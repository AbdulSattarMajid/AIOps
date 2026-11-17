"""SQLAlchemy 2.x declarative models for the AIOps PostgreSQL history store.

Four tables:
  incidents   – one row per unique crash signature
  analyses    – classifier + agent result per incident analysis run
  fix_attempts – each LLM patch attempt inside an analysis
  deliveries  – GitHub PR / Slack delivery record per analysis
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# incidents
# ---------------------------------------------------------------------------

class Incident(Base):
    __tablename__ = "incidents"
    __table_args__ = (
        UniqueConstraint("signature", name="uq_incidents_signature"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signature: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    service_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_class: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    redacted_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    crash_file: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    crash_function: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    occurrences: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    first_seen: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_seen: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    analyses: Mapped[list[Analysis]] = relationship(
        "Analysis", back_populates="incident", cascade="all, delete-orphan"
    )


# ---------------------------------------------------------------------------
# analyses
# ---------------------------------------------------------------------------

class Analysis(Base):
    __tablename__ = "analyses"
    __table_args__ = (
        Index("ix_analyses_category", "category"),
        Index("ix_analyses_incident_id", "incident_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("incidents.id", ondelete="CASCADE"),
        nullable=False,
    )
    category: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    confidence: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    signals: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    classifier_on: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    incident: Mapped[Incident] = relationship("Incident", back_populates="analyses")
    fix_attempts: Mapped[list[FixAttempt]] = relationship(
        "FixAttempt", back_populates="analysis", cascade="all, delete-orphan"
    )
    deliveries: Mapped[list[Delivery]] = relationship(
        "Delivery", back_populates="analysis", cascade="all, delete-orphan"
    )


# ---------------------------------------------------------------------------
# fix_attempts
# ---------------------------------------------------------------------------

class FixAttempt(Base):
    __tablename__ = "fix_attempts"
    __table_args__ = (
        Index("ix_fix_attempts_analysis_id", "analysis_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    analysis_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("analyses.id", ondelete="CASCADE"),
        nullable=False,
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    diff_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    validator_ok: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    verification_status: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tests_before_passed: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    tests_before_failed: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    tests_after_passed: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    tests_after_failed: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    analysis: Mapped[Analysis] = relationship("Analysis", back_populates="fix_attempts")


# ---------------------------------------------------------------------------
# deliveries
# ---------------------------------------------------------------------------

_OUTCOME_CHECK = CheckConstraint(
    "outcome IN ('open', 'merged', 'closed', 'ignored')",
    name="ck_deliveries_outcome",
)


class Delivery(Base):
    __tablename__ = "deliveries"
    __table_args__ = (
        _OUTCOME_CHECK,
        Index("ix_deliveries_analysis_id", "analysis_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    analysis_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("analyses.id", ondelete="CASCADE"),
        nullable=False,
    )
    pr_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    branch: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    slack_sent: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    outcome: Mapped[str] = mapped_column(Text, nullable=False, default="open")
    decided_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    analysis: Mapped[Analysis] = relationship("Analysis", back_populates="deliveries")
