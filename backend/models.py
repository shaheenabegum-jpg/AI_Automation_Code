"""
SQLAlchemy ORM models — PostgreSQL.
Columns match the Pet_LandingPage.xlsx schema exactly.
"""
import uuid
from datetime import datetime
from typing import Optional
from sqlalchemy import (
    String, Text, Integer, DateTime, ForeignKey,
    Enum as SAEnum, ARRAY, JSON
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from database import Base
import enum


class ValidationStatus(str, enum.Enum):
    pending = "pending"
    valid = "valid"
    invalid = "invalid"


class ExecutionStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    passed = "passed"
    failed = "failed"
    error = "error"


# ── Test Cases ──────────────────────────────────────────────────────────────────
class TestCase(Base):
    __tablename__ = "test_cases"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Direct mapping from Excel columns
    test_script_num: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    module: Mapped[str] = mapped_column(String(200), nullable=False)
    test_case_name: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    raw_steps: Mapped[Optional[str]] = mapped_column(Text)            # original cell
    expected_results: Mapped[Optional[str]] = mapped_column(Text)
    parsed_json: Mapped[dict] = mapped_column(JSON, nullable=False)   # normalized
    excel_source: Mapped[Optional[str]] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    scripts: Mapped[list["GeneratedScript"]] = relationship(back_populates="test_case")


# ── Generated Scripts ────────────────────────────────────────────────────────────
class GeneratedScript(Base):
    __tablename__ = "generated_scripts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    test_case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("test_cases.id"), nullable=False
    )
    typescript_code: Mapped[str] = mapped_column(Text, nullable=False)
    file_path: Mapped[Optional[str]] = mapped_column(String(500))     # path in framework repo
    framework_version: Mapped[Optional[str]] = mapped_column(String(50))
    github_commit: Mapped[Optional[str]] = mapped_column(String(40))
    github_branch: Mapped[Optional[str]] = mapped_column(String(200))  # branch where script was committed
    validation_status: Mapped[ValidationStatus] = mapped_column(
        SAEnum(ValidationStatus), default=ValidationStatus.pending
    )
    validation_errors: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    test_case: Mapped["TestCase"] = relationship(back_populates="scripts")
    runs: Mapped[list["ExecutionRun"]] = relationship(back_populates="script")
    prompts: Mapped[list["UserPrompt"]] = relationship(back_populates="script")


# ── Execution Runs ───────────────────────────────────────────────────────────────
class ExecutionRun(Base):
    __tablename__ = "execution_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    script_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("generated_scripts.id"), nullable=False
    )
    # Execution parameters (all dropdown values)
    environment: Mapped[str] = mapped_column(String(20), nullable=False)     # dev/sit/uat
    browser: Mapped[str] = mapped_column(String(20), nullable=False)          # chromium/firefox/webkit
    device: Mapped[str] = mapped_column(String(80), nullable=False)           # Desktop Chrome / iPhone 13 …
    execution_mode: Mapped[str] = mapped_column(String(10), nullable=False)   # headless/headed
    browser_version: Mapped[str] = mapped_column(String(30), default="stable")
    tags: Mapped[Optional[list]] = mapped_column(ARRAY(String))               # regression/smoke …

    status: Mapped[ExecutionStatus] = mapped_column(
        SAEnum(ExecutionStatus), default=ExecutionStatus.queued
    )
    start_time: Mapped[Optional[datetime]] = mapped_column(DateTime)
    end_time: Mapped[Optional[datetime]] = mapped_column(DateTime)
    logs: Mapped[Optional[str]] = mapped_column(Text)
    allure_report_path: Mapped[Optional[str]] = mapped_column(String(500))
    exit_code: Mapped[Optional[int]] = mapped_column(Integer)

    script: Mapped["GeneratedScript"] = relationship(back_populates="runs")


# ── Prompt Audit ─────────────────────────────────────────────────────────────────
class UserPrompt(Base):
    __tablename__ = "user_prompts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    script_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("generated_scripts.id"), nullable=False
    )
    prompt_text: Mapped[str] = mapped_column(Text, nullable=False)
    framework_context_hash: Mapped[Optional[str]] = mapped_column(String(64))
    model_used: Mapped[str] = mapped_column(String(50), default="claude-opus-4-6")
    input_tokens: Mapped[Optional[int]] = mapped_column(Integer)
    output_tokens: Mapped[Optional[int]] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    script: Mapped["GeneratedScript"] = relationship(back_populates="prompts")
