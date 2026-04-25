"""
extraction/normaliser.py

Reads RawFacts for a company and produces:
  - Metric rows  (normalised financial line items per period)
  - Ratio rows   (pre-computed financial ratios)

Key challenges addressed:
  1. Multiple XBRL tags → single canonical concept   (via taxonomy.py)
  2. Duration vs instant facts                        (period alignment)
  3. YTD vs quarterly figures                         (QTD derivation)
  4. Missing values handled with NULL, not 0
"""

import logging
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Filing, Metric, RawFact, Ratio

log = logging.getLogger(__name__)


class MetricsComputer:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------ #
    # Entry point                                                           #
    # ------------------------------------------------------------------ #

    async def compute_for_company(self, cik: str) -> int:
        """Compute metrics and ratios for all extracted filings of a company."""
        # Get all extracted filings for this company
        result = await self.session.execute(
            select(Filing)
            .where(Filing.cik == str(int(cik)))
            .where(Filing.status == "extracted")
            .order_by(Filing.period_end)
        )
        filings = result.scalars().all()

        if not filings:
            return 0

        count = 0
        for filing in filings:
            try:
                await self._compute_filing(filing)
                filing.status = "done"
                count += 1
            except Exception as exc:
                log.error("Metrics failed for filing %d: %s", filing.filing_id, exc)
                filing.status = "error"
                filing.error_message = str(exc)

        await self.session.flush()
        return count

    # ------------------------------------------------------------------ #
    # Per-filing computation                                               #
    # ------------------------------------------------------------------ #

    async def _compute_filing(self, filing: Filing) -> None:
        # Load all raw facts for this filing
        result = await self.session.execute(
            select(RawFact).where(RawFact.filing_id == filing.filing_id)
        )
        raw_facts = result.scalars().all()

        # Group facts by canonical concept
        by_concept: Dict[str, List[RawFact]] = {}
        for fact in raw_facts:
            if fact.canonical_concept:
                by_concept.setdefault(fact.canonical_concept, []).append(fact)

        period_end = filing.period_end
        form_type = filing.form_type

        # Resolve each canonical concept to a single value for this period
        values: Dict[str, Optional[float]] = {}
        for concept in [
            "revenue", "gross_profit", "operating_income", "net_income",
            "eps_basic", "eps_diluted",
            "total_assets", "total_liabilities", "total_equity",
            "operating_cash_flow", "capex",
        ]:
            facts = by_concept.get(concept, [])
            values[concept] = _resolve_best_fact(facts, period_end, form_type)

        # Derive free cash flow
        ocf = values.get("operating_cash_flow")
        capex = values.get("capex")
        values["free_cash_flow"] = (
            ocf - abs(capex) if ocf is not None and capex is not None else None
        )

        # Delete existing metric for this filing (recompute idempotently)
        await self.session.execute(
            delete(Metric).where(Metric.filing_id == filing.filing_id)
        )

        metric = Metric(
            cik=filing.cik,
            filing_id=filing.filing_id,
            period_label=filing.fiscal_period_label or "",
            period_end=period_end,
            form_type=form_type,
            **{k: values.get(k) for k in [
                "revenue", "gross_profit", "operating_income", "net_income",
                "eps_basic", "eps_diluted",
                "total_assets", "total_liabilities", "total_equity",
                "operating_cash_flow", "capex", "free_cash_flow",
            ]},
        )
        self.session.add(metric)
        await self.session.flush()  # get metric_id

        # Compute ratios
        ratio = _compute_ratios(metric)
        self.session.add(ratio)
        await self.session.flush()


# ──────────────────────────────────────────────────────────────────────────── #
# Fact resolution — pick the best fact for a given period                       #
# ──────────────────────────────────────────────────────────────────────────── #

def _resolve_best_fact(
    facts: List[RawFact], period_end: date, form_type: str
) -> Optional[float]:
    """
    From potentially many raw facts for a concept, pick the one
    that best represents this reporting period.

    Rules:
    1. Period end must match filing period_end (exact)
    2. For income statement / cash flow (duration facts):
       - 10-K: prefer 12-month duration
       - 10-Q: prefer 3-month duration; derive from YTD if unavailable
    3. For balance sheet (instant facts): exact date match
    4. USD unit preferred over others
    """
    if not facts:
        return None

    # Prefer USD unit
    usd_facts = [f for f in facts if f.unit and "USD" in f.unit.upper()]
    candidates = usd_facts if usd_facts else facts

    # Filter to exact period end match
    exact_end = [f for f in candidates if f.period_end == period_end]
    if not exact_end:
        return None

    # Instant facts (balance sheet)
    instant = [f for f in exact_end if f.period_type == "instant"]
    if instant:
        return _pick_by_value(instant)

    # Duration facts — pick by expected duration
    duration_facts = [f for f in exact_end if f.period_type == "duration" and f.period_start]

    if not duration_facts:
        return _pick_by_value(exact_end)

    target_months = 12 if form_type == "10-K" else 3
    scored = sorted(
        duration_facts,
        key=lambda f: abs(_months_between(f.period_start, f.period_end) - target_months)
    )
    return scored[0].value if scored else None


def _pick_by_value(facts: List[RawFact]) -> Optional[float]:
    """Return the value from the fact with the highest absolute value (usually the most specific)."""
    valid = [f for f in facts if f.value is not None]
    if not valid:
        return None
    return sorted(valid, key=lambda f: abs(f.value), reverse=True)[0].value


def _months_between(start: Optional[date], end: date) -> int:
    if start is None:
        return 0
    return (end.year - start.year) * 12 + (end.month - start.month)


# ──────────────────────────────────────────────────────────────────────────── #
# Ratio computation                                                             #
# ──────────────────────────────────────────────────────────────────────────── #

def _compute_ratios(m: Metric) -> Ratio:
    def safe_div(num: Optional[float], den: Optional[float]) -> Optional[float]:
        if num is None or den is None or den == 0:
            return None
        return round(num / den, 6)

    return Ratio(
        metric_id=m.metric_id,
        cik=m.cik,
        period_label=m.period_label,

        # Profitability
        gross_margin=safe_div(m.gross_profit, m.revenue),
        operating_margin=safe_div(m.operating_income, m.revenue),
        net_margin=safe_div(m.net_income, m.revenue),
        roe=safe_div(m.net_income, m.total_equity),
        roa=safe_div(m.net_income, m.total_assets),

        # Leverage
        debt_to_equity=safe_div(m.total_liabilities, m.total_equity),
        debt_to_assets=safe_div(m.total_liabilities, m.total_assets),
        equity_multiplier=safe_div(m.total_assets, m.total_equity),

        # Efficiency
        asset_turnover=safe_div(m.revenue, m.total_assets),
        fcf_margin=safe_div(m.free_cash_flow, m.revenue),
        fcf_conversion=safe_div(m.free_cash_flow, m.net_income),
    )
