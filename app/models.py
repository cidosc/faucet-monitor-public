"""SQLAlchemy 2.0 ORM models.

Design notes
------------
* Status/type columns are VARCHAR (validated by Python enums), not native PG ENUMs.
  Rationale: this project will keep adding chains, statuses and requirement kinds.
  ``ALTER TYPE ... ADD VALUE`` migrations are painful and non-transactional, while a
  VARCHAR needs no migration at all. The invariants that genuinely need DB-level
  enforcement here are the UNIQUE constraints that guarantee idempotency:
      transactions (chain_id, tx_hash)
      claims.idempotency_key
      notifications.dedupe_key
  Those ARE enforced in the database, because they are what prevents duplicate
  claims and duplicate alerts after a crash or restart.
* ``wallets`` deliberately has NO private-key column and never will.
* Nullable == "unknown". We never invent values to fill a gap.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

# JSONB on PostgreSQL, plain JSON elsewhere (keeps unit tests on SQLite possible)
JsonType = JSON().with_variant(JSONB, "postgresql")


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JsonType, list[Any]: JsonType}


def _ts(nullable: bool = True, default_now: bool = False) -> Mapped[datetime]:
    kwargs: dict[str, Any] = {"nullable": nullable}
    if default_now:
        kwargs["server_default"] = func.now()
    return mapped_column(DateTime(timezone=True), **kwargs)  # type: ignore[return-value]


# ────────────────────────────────────────────────────────────────────────────
# 1. chains
# ────────────────────────────────────────────────────────────────────────────
class Chain(Base):
    __tablename__ = "chains"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    chain_type: Mapped[str] = mapped_column(String(16), nullable=False, default="evm",
                                            server_default=sa_text("'evm'"))
    chain_id: Mapped[int | None] = mapped_column(BigInteger)  # EVM numeric chainId
    native_symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    # We store the ENV VAR NAME, never the URL itself.
    rpc_url_env: Mapped[str] = mapped_column(String(64), nullable=False)
    explorer_url: Mapped[str | None] = mapped_column(Text)
    is_testnet: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True,
                                             server_default=sa_text("true"))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True,
                                          server_default=sa_text("true"))
    created_at: Mapped[datetime] = _ts(nullable=False, default_now=True)

    projects: Mapped[list[Project]] = relationship(back_populates="chain")


# ────────────────────────────────────────────────────────────────────────────
# 2. projects
# ────────────────────────────────────────────────────────────────────────────
class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    chain_id: Mapped[int | None] = mapped_column(ForeignKey("chains.id", ondelete="SET NULL"))
    network: Mapped[str | None] = mapped_column(String(64))

    project_url: Mapped[str | None] = mapped_column(Text)
    docs_url: Mapped[str | None] = mapped_column(Text)
    faucet_url: Mapped[str | None] = mapped_column(Text)
    github_url: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)

    source_plugin: Mapped[str] = mapped_column(String(32), nullable=False)
    discovered_at: Mapped[datetime] = _ts(nullable=False, default_now=True)
    last_checked_at: Mapped[datetime | None] = _ts()

    status: Mapped[str] = mapped_column(String(24), nullable=False, default="UNKNOWN",
                                        server_default=sa_text("'UNKNOWN'"))

    # UNKNOWN until a human reads the project's Terms/Docs/FAQ.
    automation_allowed: Mapped[str] = mapped_column(String(16), nullable=False, default="UNKNOWN",
                                                    server_default=sa_text("'UNKNOWN'"))
    automation_evidence: Mapped[str | None] = mapped_column(Text)  # quoted source + URL
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = _ts(nullable=False, default_now=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    chain: Mapped[Chain | None] = relationship(back_populates="projects")
    faucets: Mapped[list[Faucet]] = relationship(back_populates="project", cascade="all, delete-orphan")
    requirements: Mapped[list[Requirement]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


# ────────────────────────────────────────────────────────────────────────────
# 3. faucets
# ────────────────────────────────────────────────────────────────────────────
class Faucet(Base):
    __tablename__ = "faucets"
    __table_args__ = (UniqueConstraint("project_id", "url", name="uq_faucet_project_url"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[str] = mapped_column(String(24), nullable=False, default="UNKNOWN",
                                        server_default=sa_text("'UNKNOWN'"))
    previous_status: Mapped[str | None] = mapped_column(String(24))
    last_checked_at: Mapped[datetime | None] = _ts()
    last_available_at: Mapped[datetime | None] = _ts()

    cooldown_seconds: Mapped[int | None] = mapped_column(Integer)  # NULL = unknown
    # Widened to 78,18 (max uint256 is ~1.16e77) because spam tokens routinely report
    # absurd supplies; 38,18 overflowed on real incoming transfers.
    claim_amount: Mapped[Decimal | None] = mapped_column(Numeric(78, 18))
    amount_unit: Mapped[str | None] = mapped_column(String(32))

    requires_wallet_address: Mapped[bool | None] = mapped_column(Boolean)
    requires_login: Mapped[bool | None] = mapped_column(Boolean)
    login_provider: Mapped[str | None] = mapped_column(String(32))
    requires_captcha: Mapped[bool | None] = mapped_column(Boolean)
    captcha_type: Mapped[str | None] = mapped_column(String(32))
    cloudflare_challenge: Mapped[bool | None] = mapped_column(Boolean)
    requires_social: Mapped[bool | None] = mapped_column(Boolean)

    # Both of these require explicit human confirmation; the automated scanner may
    # never set them to True.
    is_official_source: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False,
                                                     server_default=sa_text("false"))
    is_free_resource: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False,
                                                   server_default=sa_text("false"))

    automation_allowed: Mapped[str] = mapped_column(String(16), nullable=False, default="UNKNOWN",
                                                    server_default=sa_text("'UNKNOWN'"))
    # Explicit allowlist flag for the auto-claim gate (separate from automation_allowed).
    auto_claim_allowlisted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False,
                                                         server_default=sa_text("false"))

    content_hash: Mapped[str | None] = mapped_column(String(64))
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0,
                                                      server_default=sa_text("0"))
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = _ts(nullable=False, default_now=True)

    project: Mapped[Project] = relationship(back_populates="faucets")
    checks: Mapped[list[FaucetCheck]] = relationship(
        back_populates="faucet", cascade="all, delete-orphan"
    )


# ────────────────────────────────────────────────────────────────────────────
# 4. faucet_checks  (evidence trail for every probe)
# ────────────────────────────────────────────────────────────────────────────
class FaucetCheck(Base):
    __tablename__ = "faucet_checks"
    __table_args__ = (Index("ix_faucet_checks_faucet_time", "faucet_id", "checked_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    faucet_id: Mapped[int] = mapped_column(
        ForeignKey("faucets.id", ondelete="CASCADE"), nullable=False
    )
    checked_at: Mapped[datetime] = _ts(nullable=False, default_now=True)

    http_status: Mapped[int | None] = mapped_column(Integer)
    final_url: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    content_changed: Mapped[bool | None] = mapped_column(Boolean)

    detected_status: Mapped[str] = mapped_column(String(24), nullable=False)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(3, 2))
    evidence: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    faucet: Mapped[Faucet] = relationship(back_populates="checks")


# ────────────────────────────────────────────────────────────────────────────
# 5. wallets  (NO private keys, ever)
# ────────────────────────────────────────────────────────────────────────────
class Wallet(Base):
    __tablename__ = "wallets"
    __table_args__ = (UniqueConstraint("address", "chain_id", name="uq_wallet_address_chain"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    address: Mapped[str] = mapped_column(String(128), nullable=False)
    chain_id: Mapped[int | None] = mapped_column(ForeignKey("chains.id", ondelete="SET NULL"))
    is_test_wallet: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True,
                                                 server_default=sa_text("true"))
    auto_faucet: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False,
                                              server_default=sa_text("false"))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True,
                                          server_default=sa_text("true"))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts(nullable=False, default_now=True)


# ────────────────────────────────────────────────────────────────────────────
# 6. requirements
# ────────────────────────────────────────────────────────────────────────────
class Requirement(Base):
    __tablename__ = "requirements"
    __table_args__ = (UniqueConstraint("project_id", "code", name="uq_requirement_project_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    req_type: Mapped[str] = mapped_column(String(40), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict,
                                                    server_default=sa_text("'{}'"))
    verifiable: Mapped[str] = mapped_column(String(16), nullable=False, default="UNKNOWN",
                                            server_default=sa_text("'UNKNOWN'"))
    is_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True,
                                              server_default=sa_text("true"))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0,
                                            server_default=sa_text("0"))

    project: Mapped[Project] = relationship(back_populates="requirements")


# ────────────────────────────────────────────────────────────────────────────
# 7. eligibility_results
# ────────────────────────────────────────────────────────────────────────────
class EligibilityResult(Base):
    __tablename__ = "eligibility_results"
    __table_args__ = (
        Index("ix_elig_wallet_req_time", "wallet_id", "requirement_id", "checked_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    wallet_id: Mapped[int] = mapped_column(
        ForeignKey("wallets.id", ondelete="CASCADE"), nullable=False
    )
    requirement_id: Mapped[int] = mapped_column(
        ForeignKey("requirements.id", ondelete="CASCADE"), nullable=False
    )
    checked_at: Mapped[datetime] = _ts(nullable=False, default_now=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # PASS|FAIL|UNKNOWN
    observed_value: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text)


# ────────────────────────────────────────────────────────────────────────────
# 8. eligibility_summaries (rollup for fast dashboard rendering)
# ────────────────────────────────────────────────────────────────────────────
class EligibilitySummary(Base):
    __tablename__ = "eligibility_summaries"
    __table_args__ = (UniqueConstraint("wallet_id", "project_id", name="uq_eligsum_wallet_project"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    wallet_id: Mapped[int] = mapped_column(
        ForeignKey("wallets.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    pass_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fail_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unknown_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    checked_at: Mapped[datetime] = _ts(nullable=False, default_now=True)


# ────────────────────────────────────────────────────────────────────────────
# 9. tasks
# ────────────────────────────────────────────────────────────────────────────
class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    wallet_id: Mapped[int | None] = mapped_column(ForeignKey("wallets.id", ondelete="SET NULL"))
    requirement_id: Mapped[int | None] = mapped_column(
        ForeignKey("requirements.id", ondelete="SET NULL")
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    task_type: Mapped[str] = mapped_column(String(24), nullable=False)  # AUTO_ALLOWED|MANUAL_REQUIRED
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING",
                                        server_default=sa_text("'PENDING'"))
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0,
                                          server_default=sa_text("0"))
    due_at: Mapped[datetime | None] = _ts()
    discovered_at: Mapped[datetime] = _ts(nullable=False, default_now=True)
    completed_at: Mapped[datetime | None] = _ts()
    source_url: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)


# ────────────────────────────────────────────────────────────────────────────
# 10. transactions
# ────────────────────────────────────────────────────────────────────────────
class Transaction(Base):
    __tablename__ = "transactions"
    # This UNIQUE is a core idempotency guarantee.
    __table_args__ = (UniqueConstraint("chain_id", "tx_hash", name="uq_tx_chain_hash"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chain_id: Mapped[int] = mapped_column(ForeignKey("chains.id"), nullable=False)
    wallet_id: Mapped[int | None] = mapped_column(ForeignKey("wallets.id", ondelete="SET NULL"))
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))
    tx_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    direction: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING",
                                        server_default=sa_text("'PENDING'"))
    block_number: Mapped[int | None] = mapped_column(BigInteger)
    native_value: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    occurred_at: Mapped[datetime | None] = _ts()
    raw: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    created_at: Mapped[datetime] = _ts(nullable=False, default_now=True)


# ────────────────────────────────────────────────────────────────────────────
# 11. costs
# ────────────────────────────────────────────────────────────────────────────
class Cost(Base):
    __tablename__ = "costs"
    __table_args__ = (Index("ix_costs_project_time", "project_id", "occurred_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    transaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE")
    )
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))
    wallet_id: Mapped[int | None] = mapped_column(ForeignKey("wallets.id", ondelete="SET NULL"))
    chain_id: Mapped[int] = mapped_column(ForeignKey("chains.id"), nullable=False)

    gas_used: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    gas_price_wei: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    native_symbol: Mapped[str | None] = mapped_column(String(16))
    native_price_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    price_source: Mapped[str | None] = mapped_column(String(24))
    estimated_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    occurred_at: Mapped[datetime] = _ts(nullable=False, default_now=True)


# ────────────────────────────────────────────────────────────────────────────
# 12. claims
# ────────────────────────────────────────────────────────────────────────────
class Claim(Base):
    __tablename__ = "claims"
    __table_args__ = (Index("ix_claims_faucet_wallet_time", "faucet_id", "wallet_id", "attempted_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    faucet_id: Mapped[int] = mapped_column(
        ForeignKey("faucets.id", ondelete="CASCADE"), nullable=False
    )
    wallet_id: Mapped[int] = mapped_column(
        ForeignKey("wallets.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))
    chain_id: Mapped[int | None] = mapped_column(ForeignKey("chains.id", ondelete="SET NULL"))

    mode: Mapped[str] = mapped_column(String(8), nullable=False)  # AUTO|MANUAL
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    tx_hash: Mapped[str | None] = mapped_column(String(128))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(78, 18))
    amount_unit: Mapped[str | None] = mapped_column(String(32))

    attempted_at: Mapped[datetime] = _ts(nullable=False, default_now=True)
    completed_at: Mapped[datetime | None] = _ts()
    next_available_at: Mapped[datetime | None] = _ts()
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1,
                                               server_default=sa_text("1"))
    error: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)

    # Core idempotency guarantee against duplicate claims.
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)


# ────────────────────────────────────────────────────────────────────────────
# 13. notifications
# ────────────────────────────────────────────────────────────────────────────
class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_status_time", "status", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    # Core dedupe guarantee against duplicate alerts.
    dedupe_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    channel: Mapped[str] = mapped_column(String(24), nullable=False)

    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))
    faucet_id: Mapped[int | None] = mapped_column(ForeignKey("faucets.id", ondelete="SET NULL"))
    wallet_id: Mapped[int | None] = mapped_column(ForeignKey("wallets.id", ondelete="SET NULL"))

    payload: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict,
                                                    server_default=sa_text("'{}'"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING",
                                        server_default=sa_text("'PENDING'"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0,
                                          server_default=sa_text("0"))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts(nullable=False, default_now=True)
    sent_at: Mapped[datetime | None] = _ts()


# ────────────────────────────────────────────────────────────────────────────
# 14. scan_logs
# ────────────────────────────────────────────────────────────────────────────
class ScanLog(Base):
    __tablename__ = "scan_logs"
    __table_args__ = (Index("ix_scan_logs_job_time", "job_name", "started_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    job_name: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = _ts(nullable=False, default_now=True)
    finished_at: Mapped[datetime | None] = _ts()
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="RUNNING",
                                        server_default=sa_text("'RUNNING'"))
    items_found: Mapped[int] = mapped_column(Integer, nullable=False, default=0,
                                             server_default=sa_text("0"))
    items_updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0,
                                               server_default=sa_text("0"))
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0,
                                             server_default=sa_text("0"))
    error: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    triggered_by: Mapped[str] = mapped_column(String(24), nullable=False, default="scheduler",
                                              server_default=sa_text("'scheduler'"))


# ────────────────────────────────────────────────────────────────────────────
# 15. claim_targets
#
# Describes something that CAN be claimed: an airdrop claim contract or a faucet.
#
# This table contains NO key material and grants NO authority. It is a description used
# by the readiness checker to (a) determine eligibility and (b) *simulate* the claim via
# eth_call. Simulation is read-only: no transaction is ever signed or broadcast from
# this code path.
#
# `human_verified` defaults to False and gates any future execution: an unverified
# target may be monitored and simulated, never executed.
# ────────────────────────────────────────────────────────────────────────────
class ClaimTarget(Base):
    __tablename__ = "claim_targets"
    __table_args__ = (UniqueConstraint("project_id", "label", name="uq_claim_target_project_label"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    chain_id: Mapped[int] = mapped_column(ForeignKey("chains.id"), nullable=False)
    label: Mapped[str] = mapped_column(String(128), nullable=False)

    # CONTRACT_CLAIM | FAUCET_HTTP | GASLESS_RELAYER | MANUAL_ONLY
    # MANUAL_ONLY is the default: we describe it, we never act on it.
    kind: Mapped[str] = mapped_column(String(24), nullable=False, default="MANUAL_ONLY",
                                      server_default=sa_text("'MANUAL_ONLY'"))

    claim_url: Mapped[str | None] = mapped_column(Text)          # human-facing page
    contract_address: Mapped[str | None] = mapped_column(String(64))

    # e.g. "claim(address,uint256,bytes32[])". The 4-byte selector is derived, not stored.
    claim_signature: Mapped[str | None] = mapped_column(String(255))
    # Read-only existence check, e.g. "isClaimed(address)" or "claimed(uint256)".
    claimed_signature: Mapped[str | None] = mapped_column(String(255))
    # Which value feeds the claimed-signature's argument: 'address' or 'index'.
    claimed_arg: Mapped[str | None] = mapped_column(String(16))

    # Merkle-proof retrieval. Often the only place eligibility data is published.
    proof_source_url: Mapped[str | None] = mapped_column(Text)   # may contain {address}
    proof_json_path: Mapped[str | None] = mapped_column(String(255))  # dotted path in the JSON
    amount_field: Mapped[str | None] = mapped_column(String(64))
    proof_field: Mapped[str | None] = mapped_column(String(64))

    window_start: Mapped[datetime | None] = _ts()
    window_end: Mapped[datetime | None] = _ts()

    # Only a human who has read the terms may set this true, and only with evidence.
    human_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False,
                                                 server_default=sa_text("false"))
    evidence: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True,
                                          server_default=sa_text("true"))
    created_at: Mapped[datetime] = _ts(nullable=False, default_now=True)

    project: Mapped[Project] = relationship()
    chain: Mapped[Chain] = relationship()


# ────────────────────────────────────────────────────────────────────────────
# 16. claim_readiness
#
# One row per (wallet, claim_target) check. Records what was OBSERVED, never an action.
# Everything here is derived from read-only RPC calls and public HTTP endpoints.
# ────────────────────────────────────────────────────────────────────────────
class ClaimReadiness(Base):
    __tablename__ = "claim_readiness"
    __table_args__ = (
        Index("ix_claim_readiness_wallet_target", "wallet_id", "claim_target_id", "checked_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    wallet_id: Mapped[int] = mapped_column(
        ForeignKey("wallets.id", ondelete="CASCADE"), nullable=False
    )
    claim_target_id: Mapped[int] = mapped_column(
        ForeignKey("claim_targets.id", ondelete="CASCADE"), nullable=False
    )
    checked_at: Mapped[datetime] = _ts(nullable=False, default_now=True)

    # READY | ALREADY_CLAIMED | NOT_ELIGIBLE | WINDOW_CLOSED | WINDOW_NOT_OPEN |
    # UNSIMULATED | UNKNOWN
    status: Mapped[str] = mapped_column(String(24), nullable=False)

    window_open: Mapped[bool | None] = mapped_column(Boolean)
    already_claimed: Mapped[bool | None] = mapped_column(Boolean)
    simulated_ok: Mapped[bool | None] = mapped_column(Boolean)
    revert_reason: Mapped[str | None] = mapped_column(Text)

    gas_estimate: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    gas_price_wei: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    native_symbol: Mapped[str | None] = mapped_column(String(16))
    estimated_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))

    # The exact call we would make. Stored so a human can inspect/execute it themselves.
    calldata: Mapped[str | None] = mapped_column(Text)
    to_address: Mapped[str | None] = mapped_column(String(64))

    evidence: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict,
                                                     server_default=sa_text("'{}'"))
    error: Mapped[str | None] = mapped_column(Text)


# ────────────────────────────────────────────────────────────────────────────
# 17. incoming_transfers
#
# Tokens and native currency that arrived in a monitored wallet. This is how an airdrop
# is usually discovered: value appears that you never asked for.
#
# `unique_id` is UNIQUE per chain, which makes re-scanning idempotent — the same transfer
# can never be recorded or notified twice, no matter how often the scan window overlaps.
# ────────────────────────────────────────────────────────────────────────────
class IncomingTransfer(Base):
    __tablename__ = "incoming_transfers"
    __table_args__ = (
        UniqueConstraint("chain_id", "unique_id", name="uq_incoming_chain_unique"),
        Index("ix_incoming_wallet_time", "wallet_id", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chain_id: Mapped[int] = mapped_column(ForeignKey("chains.id"), nullable=False)
    wallet_id: Mapped[int] = mapped_column(
        ForeignKey("wallets.id", ondelete="CASCADE"), nullable=False
    )

    # Stable identity of this transfer within the chain:
    #   alchemy  -> the provider's uniqueId
    #   log scan -> "<tx_hash>:<log_index>"
    unique_id: Mapped[str] = mapped_column(String(160), nullable=False)

    tx_hash: Mapped[str | None] = mapped_column(String(128))
    block_number: Mapped[int | None] = mapped_column(BigInteger)
    occurred_at: Mapped[datetime | None] = _ts()

    category: Mapped[str] = mapped_column(String(16), nullable=False, default="erc20",
                                          server_default=sa_text("'erc20'"))
    contract_address: Mapped[str | None] = mapped_column(String(64))  # NULL = native
    asset_symbol: Mapped[str | None] = mapped_column(String(64))
    asset_name: Mapped[str | None] = mapped_column(String(128))

    raw_value: Mapped[Decimal | None] = mapped_column(Numeric(78, 18))
    decimals: Mapped[int | None] = mapped_column(Integer)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(78, 18))
    from_address: Mapped[str | None] = mapped_column(String(128))
    to_address: Mapped[str | None] = mapped_column(String(128))

    # Whether this arrival has been surfaced to the user.
    notified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False,
                                           server_default=sa_text("false"))
    raw: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict,
                                                server_default=sa_text("'{}'"))
    detected_at: Mapped[datetime] = _ts(nullable=False, default_now=True)


ALL_TABLES = [
    "chains", "projects", "faucets", "faucet_checks", "wallets", "requirements",
    "eligibility_results", "eligibility_summaries", "tasks", "transactions",
    "costs", "claims", "notifications", "scan_logs",
    "claim_targets", "claim_readiness", "incoming_transfers",
]

