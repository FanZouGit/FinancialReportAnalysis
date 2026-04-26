"""
database/models.py

Full SQLAlchemy 2.0 ORM schema.

Tables
------
companies           — master entity registry (CIK, ticker, SIC, sector)
filings             — one row per 10-K / 10-Q filing
raw_facts           — every XBRL fact exactly as reported
metrics             — pre-computed, period-aligned financial line items
ratios              — pre-computed financial ratios
peer_groups         — maps each company to its peer set
mda_chunks          — MD&A text segments for semantic search
embeddings          — vector embeddings for mda_chunks
"""

from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Date, DateTime, Float,
    ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ──────────────────────────────────────────────────────────────────────────── #
# Companies                                                                     #
# ──────────────────────────────────────────────────────────────────────────── #

class Company(Base):
    __tablename__ = "companies"

    cik: Mapped[str] = mapped_column(String(10), primary_key=True)
    ticker: Mapped[Optional[str]] = mapped_column(String(10), index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    sic_code: Mapped[Optional[str]] = mapped_column(String(4))
    sic_description: Mapped[Optional[str]] = mapped_column(String(255))
    sector: Mapped[Optional[str]] = mapped_column(String(100))
    industry_group: Mapped[Optional[str]] = mapped_column(String(100))
    exchange: Mapped[Optional[str]] = mapped_column(String(20))
    state_of_inc: Mapped[Optional[str]] = mapped_column(String(4))
    is_sp500: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    filings: Mapped[list["Filing"]] = relationship(back_populates="company")
    metrics: Mapped[list["Metric"]] = relationship(back_populates="company")

    __table_args__ = (
        Index("ix_companies_sector", "sector"),
        Index("ix_companies_sic", "sic_code"),
        Index("ix_companies_sp500", "is_sp500"),
    )


# ──────────────────────────────────────────────────────────────────────────── #
# Filings                                                                       #
# ──────────────────────────────────────────────────────────────────────────── #

class Filing(Base):
    __tablename__ = "filings"

    filing_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    cik: Mapped[str] = mapped_column(ForeignKey("companies.cik"), nullable=False)
    accession_no: Mapped[str] = mapped_column(String(25), nullable=False)
    form_type: Mapped[str] = mapped_column(String(10), nullable=False)   # 10-K or 10-Q
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    filed_date: Mapped[date] = mapped_column(Date, nullable=False)
    fiscal_year: Mapped[Optional[int]] = mapped_column(Integer)
    fiscal_quarter: Mapped[Optional[int]] = mapped_column(Integer)       # 1-4; None for 10-K
    fiscal_period_label: Mapped[Optional[str]] = mapped_column(String(10))  # e.g. "Q3-2023"

    # Processing state machine
    # pending → downloading → downloaded → extracting → extracted → computing → done | error
    status: Mapped[str] = mapped_column(String(20), default="pending")
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    raw_json_path: Mapped[Optional[str]] = mapped_column(String(500))    # local cache path
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    company: Mapped["Company"] = relationship(back_populates="filings")
    raw_facts: Mapped[list["RawFact"]] = relationship(back_populates="filing")
    metrics: Mapped[list["Metric"]] = relationship(back_populates="filing")
    mda_chunks: Mapped[list["MdaChunk"]] = relationship(back_populates="filing")

    __table_args__ = (
        UniqueConstraint("cik", "accession_no", name="uq_filing_accession"),
        Index("ix_filings_cik_period", "cik", "period_end"),
        Index("ix_filings_status", "status"),
        Index("ix_filings_form_type", "form_type"),
        Index("ix_filings_fiscal_label", "fiscal_period_label"),
    )


# ──────────────────────────────────────────────────────────────────────────── #
# Raw XBRL facts — exactly as reported                                          #
# ──────────────────────────────────────────────────────────────────────────── #

class RawFact(Base):
    __tablename__ = "raw_facts"

    fact_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    filing_id: Mapped[int] = mapped_column(ForeignKey("filings.filing_id"), nullable=False)
    cik: Mapped[str] = mapped_column(String(10), nullable=False)

    # XBRL concept exactly as reported
    taxonomy: Mapped[str] = mapped_column(String(20), default="us-gaap")  # or dei, ifrs-full
    concept: Mapped[str] = mapped_column(String(255), nullable=False)       # e.g. us-gaap:Revenues
    canonical_concept: Mapped[Optional[str]] = mapped_column(String(100))   # normalised name

    # Value
    value: Mapped[Optional[float]] = mapped_column(Float)
    unit: Mapped[Optional[str]] = mapped_column(String(20))                 # USD, shares, pure
    decimals: Mapped[Optional[int]] = mapped_column(Integer)

    # Period
    period_type: Mapped[str] = mapped_column(String(10))    # instant or duration
    period_start: Mapped[Optional[date]] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)

    filing: Mapped["Filing"] = relationship(back_populates="raw_facts")

    __table_args__ = (
        Index("ix_raw_facts_filing", "filing_id"),
        Index("ix_raw_facts_cik_concept", "cik", "canonical_concept"),
        Index("ix_raw_facts_period_end", "period_end"),
    )


# ──────────────────────────────────────────────────────────────────────────── #
# Metrics — normalised, period-aligned financial line items                     #
# ──────────────────────────────────────────────────────────────────────────── #

class Metric(Base):
    """
    One row per company per fiscal period.
    All monetary values in USD (no scaling — raw reported values).
    NULL means the concept was not reported for that period.
    """
    __tablename__ = "metrics"

    metric_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    cik: Mapped[str] = mapped_column(ForeignKey("companies.cik"), nullable=False)
    filing_id: Mapped[int] = mapped_column(ForeignKey("filings.filing_id"), nullable=False)
    period_label: Mapped[str] = mapped_column(String(10), nullable=False)   # Q3-2023 / FY-2023
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    form_type: Mapped[str] = mapped_column(String(10), nullable=False)

    # Income statement
    revenue: Mapped[Optional[float]] = mapped_column(Float)
    gross_profit: Mapped[Optional[float]] = mapped_column(Float)
    operating_income: Mapped[Optional[float]] = mapped_column(Float)
    net_income: Mapped[Optional[float]] = mapped_column(Float)
    eps_basic: Mapped[Optional[float]] = mapped_column(Float)
    eps_diluted: Mapped[Optional[float]] = mapped_column(Float)

    # Balance sheet (point-in-time at period_end)
    total_assets: Mapped[Optional[float]] = mapped_column(Float)
    total_liabilities: Mapped[Optional[float]] = mapped_column(Float)
    total_equity: Mapped[Optional[float]] = mapped_column(Float)

    # Cash flow
    operating_cash_flow: Mapped[Optional[float]] = mapped_column(Float)
    capex: Mapped[Optional[float]] = mapped_column(Float)
    free_cash_flow: Mapped[Optional[float]] = mapped_column(Float)   # derived

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    company: Mapped["Company"] = relationship(back_populates="metrics")
    filing: Mapped["Filing"] = relationship(back_populates="metrics")
    ratio: Mapped[Optional["Ratio"]] = relationship(back_populates="metric", uselist=False)

    __table_args__ = (
        UniqueConstraint("cik", "period_label", "form_type", name="uq_metric_period"),
        Index("ix_metrics_cik_period", "cik", "period_end"),
        Index("ix_metrics_period_label", "period_label"),
    )


# ──────────────────────────────────────────────────────────────────────────── #
# Ratios — pre-computed financial ratios                                        #
# ──────────────────────────────────────────────────────────────────────────── #

class Ratio(Base):
    __tablename__ = "ratios"

    ratio_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    metric_id: Mapped[int] = mapped_column(
        ForeignKey("metrics.metric_id"), nullable=False, unique=True
    )
    cik: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    period_label: Mapped[str] = mapped_column(String(10), nullable=False)

    # Profitability
    gross_margin: Mapped[Optional[float]] = mapped_column(Float)       # gross_profit / revenue
    operating_margin: Mapped[Optional[float]] = mapped_column(Float)   # operating_income / revenue
    net_margin: Mapped[Optional[float]] = mapped_column(Float)         # net_income / revenue
    roe: Mapped[Optional[float]] = mapped_column(Float)                # net_income / equity
    roa: Mapped[Optional[float]] = mapped_column(Float)                # net_income / assets

    # Leverage
    debt_to_equity: Mapped[Optional[float]] = mapped_column(Float)
    debt_to_assets: Mapped[Optional[float]] = mapped_column(Float)
    equity_multiplier: Mapped[Optional[float]] = mapped_column(Float)  # assets / equity

    # Efficiency
    asset_turnover: Mapped[Optional[float]] = mapped_column(Float)     # revenue / assets
    fcf_margin: Mapped[Optional[float]] = mapped_column(Float)         # fcf / revenue
    fcf_conversion: Mapped[Optional[float]] = mapped_column(Float)     # fcf / net_income

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    metric: Mapped["Metric"] = relationship(back_populates="ratio")

    __table_args__ = (
        Index("ix_ratios_cik_period", "cik", "period_label"),
    )


# ──────────────────────────────────────────────────────────────────────────── #
# Peer groups                                                                   #
# ──────────────────────────────────────────────────────────────────────────── #

class PeerGroup(Base):
    """
    Many-to-many: each company belongs to one or more peer groups.
    group_key is typically the SIC code or GICS sector + size bucket.
    """
    __tablename__ = "peer_groups"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    cik: Mapped[str] = mapped_column(ForeignKey("companies.cik"), nullable=False)
    group_key: Mapped[str] = mapped_column(String(50), nullable=False)  # e.g. "SIC-7372"
    group_label: Mapped[str] = mapped_column(String(100))               # e.g. "Software"

    __table_args__ = (
        UniqueConstraint("cik", "group_key", name="uq_peer_group"),
        Index("ix_peer_groups_key", "group_key"),
    )


# ──────────────────────────────────────────────────────────────────────────── #
# MD&A text and embeddings — Phase 7                                            #
# ──────────────────────────────────────────────────────────────────────────── #

class MdaChunk(Base):
    """
    Chunked MD&A section text for semantic search.
    Each chunk is ~512 tokens, with 64-token overlap.
    """
    __tablename__ = "mda_chunks"

    chunk_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    filing_id: Mapped[int] = mapped_column(ForeignKey("filings.filing_id"), nullable=False)
    cik: Mapped[str] = mapped_column(String(10), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    char_start: Mapped[int] = mapped_column(Integer)
    char_end: Mapped[int] = mapped_column(Integer)
    token_count: Mapped[Optional[int]] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    filing: Mapped["Filing"] = relationship(back_populates="mda_chunks")
    embedding: Mapped[Optional["ChunkEmbedding"]] = relationship(
        back_populates="chunk", uselist=False
    )

    __table_args__ = (
        UniqueConstraint("filing_id", "chunk_index", name="uq_mda_chunk"),
        Index("ix_mda_chunks_cik", "cik"),
    )


class ChunkEmbedding(Base):
    """
    Stores the vector embedding for each MD&A chunk.
    Using pgvector extension — vector(384) for all-MiniLM-L6-v2,
    or vector(1536) for OpenAI text-embedding-3-small.
    """
    __tablename__ = "chunk_embeddings"

    embedding_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chunk_id: Mapped[int] = mapped_column(
        ForeignKey("mda_chunks.chunk_id"), nullable=False, unique=True
    )
    model_name: Mapped[str] = mapped_column(String(100))
    # Actual vector stored via pgvector Column (not mapped_column — needs raw Column)
    # See database/repositories.py for raw SQL insert with vector type

    chunk: Mapped["MdaChunk"] = relationship(back_populates="embedding")
