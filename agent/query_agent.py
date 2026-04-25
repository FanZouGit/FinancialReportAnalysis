"""
agent/query_agent.py

Phase 6: LLM query agent using PydanticAI.

The agent exposes the analytics repository as typed tools that the LLM
can call to answer natural language financial questions.

Example queries:
  "Compare Apple and Microsoft's margins for Q3 2023"
  "Which software companies had the highest ROE last quarter?"
  "Show me NVDA's revenue trend over the past 2 years"
  "Are there any S&P 500 companies with unusual net margin in Q2 2024?"
  "What did Meta say about AI investment in their most recent 10-Q?"
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from pydantic import BaseModel
from pydantic_ai import Agent, RunContext
from sqlalchemy.ext.asyncio import AsyncSession

from analytics.repository import AnalyticsRepository, PeriodSnapshot
from agent.semantic_search import SemanticSearch
from config.settings import settings

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────── #
# Dependency injection                                                           #
# ──────────────────────────────────────────────────────────────────────────── #

@dataclass
class AgentDeps:
    session: AsyncSession
    repo: AnalyticsRepository
    search: SemanticSearch


# ──────────────────────────────────────────────────────────────────────────── #
# Typed result schemas                                                           #
# ──────────────────────────────────────────────────────────────────────────── #

class FinancialAnswer(BaseModel):
    """The agent's final structured answer."""
    summary: str
    data_points: List[Dict[str, Any]]
    caveats: Optional[str] = None
    sources: List[str] = []   # period labels / tickers referenced


# ──────────────────────────────────────────────────────────────────────────── #
# Agent definition                                                               #
# ──────────────────────────────────────────────────────────────────────────── #

SYSTEM_PROMPT = """
You are a financial analyst assistant with access to SEC EDGAR filing data
for thousands of publicly traded companies.

You have access to tools that query:
- Company financial metrics (revenue, net income, margins, etc.)
- Peer comparison and rankings within an industry
- Sector-level benchmark statistics
- Cross-company comparisons
- Revenue and margin trend analysis
- Anomaly detection for unusual financial metrics
- Semantic search over MD&A text from 10-K and 10-Q filings

When answering questions:
1. Call the appropriate tool(s) first — do not guess values from memory
2. Use multiple tools if the question requires cross-referencing
3. Express monetary values in millions (divide by 1,000,000) or billions
4. Express margins and ratios as percentages where appropriate
5. Flag any data gaps or NULL values explicitly
6. Be precise about which period the data covers
7. Always cite the source ticker(s) and period label(s) in your answer
"""

# Agent is built lazily so importing this module doesn't require API keys at
# import time (needed for tests and cold imports without .env).
_financial_agent: Optional[Agent] = None


def _get_agent() -> Agent:
    global _financial_agent
    if _financial_agent is None:
        if not settings.anthropic_api_key and not settings.openai_api_key:
            raise RuntimeError(
                "No LLM API key configured. Set ANTHROPIC_API_KEY or OPENAI_API_KEY in .env"
            )
        model = (
            "anthropic:claude-sonnet-4-6" if settings.anthropic_api_key
            else "openai:gpt-4o"
        )
        agent: Agent = Agent(
            model,
            deps_type=AgentDeps,
            result_type=FinancialAnswer,
            system_prompt=SYSTEM_PROMPT,
        )
        _register_tools(agent)
        _financial_agent = agent
    return _financial_agent


# ──────────────────────────────────────────────────────────────────────────── #
# Tools — registered lazily via _register_tools()                               #
# ──────────────────────────────────────────────────────────────────────────── #

def _register_tools(agent: Agent) -> None:
    """Attach all tool functions to the given agent instance."""

    @agent.tool
    async def get_company_metrics(
        ctx: RunContext[AgentDeps],
        ticker: str,
        form_type: str = "10-Q",
        limit: int = 8,
    ) -> List[Dict[str, Any]]:
        """
        Get financial metrics time series for a company.

        Args:
            ticker:    Stock ticker symbol (e.g. AAPL, MSFT, NVDA)
            form_type: "10-Q" for quarterly, "10-K" for annual
            limit:     Number of periods to return (most recent first)
        """
        snapshots = await ctx.deps.repo.get_metrics_by_ticker(ticker, form_type, limit)
        return [_snapshot_to_dict(s) for s in snapshots]

    @agent.tool
    async def compare_peers(
        ctx: RunContext[AgentDeps],
        ticker: str,
        ratio: str = "net_margin",
        period_label: Optional[str] = None,
        form_type: str = "10-Q",
    ) -> List[Dict[str, Any]]:
        """
        Rank a company against its industry peers on a given ratio.

        Args:
            ticker:       Stock ticker symbol
            ratio:        One of: gross_margin, operating_margin, net_margin,
                          roe, roa, debt_to_equity, fcf_margin, asset_turnover
            period_label: e.g. "Q3-2023" or "FY-2023". Defaults to latest.
            form_type:    "10-Q" or "10-K"
        """
        company = await ctx.deps.repo._get_company_by_ticker(ticker)
        if not company:
            return [{"error": f"Company not found for ticker {ticker}"}]
        rankings = await ctx.deps.repo.compare_peers(
            company.cik, ratio, period_label, form_type
        )
        return [
            {
                "rank": r.rank,
                "ticker": r.ticker,
                "company": r.company_name,
                "value": r.value,
                "percentile": r.percentile,
                "peer_count": r.peer_count,
            }
            for r in rankings
        ]

    @agent.tool
    async def sector_benchmark(
        ctx: RunContext[AgentDeps],
        sector: str,
        metric: str,
        period_label: str,
        form_type: str = "10-Q",
    ) -> Dict[str, Any]:
        """
        Get aggregate statistics for a sector.

        Args:
            sector:       Sector name (e.g. "Technology", "Financials", "Healthcare")
            metric:       Ratio or metric name
            period_label: e.g. "Q3-2023"
            form_type:    "10-Q" or "10-K"
        """
        stats = await ctx.deps.repo.sector_benchmark(sector, period_label, metric, form_type)
        return {
            "sector": stats.sector,
            "period": stats.period_label,
            "metric": stats.metric,
            "count": stats.count,
            "mean": stats.mean,
            "median": stats.median,
            "p25_to_p75": f"{stats.p25} – {stats.p75}",
            "min": stats.min,
            "max": stats.max,
        }

    @agent.tool
    async def cross_company_compare(
        ctx: RunContext[AgentDeps],
        tickers: List[str],
        period_label: str,
        form_type: str = "10-Q",
    ) -> List[Dict[str, Any]]:
        """
        Side-by-side financial comparison of multiple companies.

        Args:
            tickers:      List of ticker symbols, e.g. ["AAPL", "MSFT", "GOOGL"]
            period_label: Fiscal period, e.g. "Q3-2023"
            form_type:    "10-Q" or "10-K"
        """
        snapshots = await ctx.deps.repo.cross_company(tickers, period_label, form_type)
        return [_snapshot_to_dict(s) for s in snapshots]

    @agent.tool
    async def trend_analysis(
        ctx: RunContext[AgentDeps],
        ticker: str,
        metric: str = "revenue",
        n_periods: int = 8,
        form_type: str = "10-Q",
    ) -> List[Dict[str, Any]]:
        """
        Get period-over-period growth rates for a company metric.

        Args:
            ticker:    Stock ticker symbol
            metric:    Metric name: revenue, net_income, operating_income,
                       total_assets, operating_cash_flow, free_cash_flow
            n_periods: Number of periods
            form_type: "10-Q" or "10-K"
        """
        company = await ctx.deps.repo._get_company_by_ticker(ticker)
        if not company:
            return [{"error": f"Company not found for ticker {ticker}"}]
        return await ctx.deps.repo.trend_analysis(company.cik, metric, n_periods, form_type)

    @agent.tool
    async def detect_anomalies(
        ctx: RunContext[AgentDeps],
        ticker: str,
        metric: str = "net_margin",
        z_threshold: float = 2.0,
        form_type: str = "10-Q",
    ) -> List[Dict[str, Any]]:
        """
        Identify periods where a metric is statistically unusual.

        Args:
            ticker:      Stock ticker
            metric:      The metric to analyse
            z_threshold: Standard deviations from mean to flag (default 2.0)
            form_type:   "10-Q" or "10-K"
        """
        company = await ctx.deps.repo._get_company_by_ticker(ticker)
        if not company:
            return [{"error": f"Company not found for ticker {ticker}"}]
        return await ctx.deps.repo.detect_anomalies(
            company.cik, metric, form_type, z_threshold
        )

    @agent.tool
    async def search_mda(
        ctx: RunContext[AgentDeps],
        query: str,
        top_k: int = 5,
        ticker: Optional[str] = None,
        sector: Optional[str] = None,
        period_label: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Semantic search over MD&A text from 10-K and 10-Q filings.

        Args:
            query:        Natural language search query
            top_k:        Number of results (default 5)
            ticker:       Optionally filter to one company
            sector:       Optionally filter to one sector
            period_label: Optionally filter to one period
        """
        cik = None
        if ticker:
            company = await ctx.deps.repo._get_company_by_ticker(ticker)
            cik = company.cik if company else None

        results = await ctx.deps.search.search(
            query, top_k=top_k, cik=cik, sector=sector, period_label=period_label
        )
        for r in results:
            if r.get("text") and len(r["text"]) > 600:
                r["text"] = r["text"][:600] + "…"
        return results


# ──────────────────────────────────────────────────────────────────────────── #
# Public runner                                                                  #
# ──────────────────────────────────────────────────────────────────────────── #

async def ask(session: AsyncSession, question: str) -> FinancialAnswer:
    """
    Ask the financial agent a natural language question.

    Usage:
        async with get_db() as session:
            answer = await ask(session, "What is Apple's revenue trend?")
            print(answer.summary)
    """
    deps = AgentDeps(
        session=session,
        repo=AnalyticsRepository(session),
        search=SemanticSearch(session),
    )
    result = await _get_agent().run(question, deps=deps)
    return result.data


# ──────────────────────────────────────────────────────────────────────────── #
# Helpers                                                                       #
# ──────────────────────────────────────────────────────────────────────────── #

def _snapshot_to_dict(s: PeriodSnapshot) -> Dict[str, Any]:
    return {
        "ticker": s.ticker,
        "company": s.company_name,
        "period": s.period_label,
        "period_end": s.period_end,
        "form_type": s.form_type,
        # Format monetary values in millions
        "revenue_m": _to_m(s.revenue),
        "gross_profit_m": _to_m(s.gross_profit),
        "operating_income_m": _to_m(s.operating_income),
        "net_income_m": _to_m(s.net_income),
        "total_assets_m": _to_m(s.total_assets),
        "total_equity_m": _to_m(s.total_equity),
        "operating_cash_flow_m": _to_m(s.operating_cash_flow),
        "free_cash_flow_m": _to_m(s.free_cash_flow),
        # Ratios as percentages
        "gross_margin_pct": _to_pct(s.gross_margin),
        "operating_margin_pct": _to_pct(s.operating_margin),
        "net_margin_pct": _to_pct(s.net_margin),
        "roe_pct": _to_pct(s.roe),
        "roa_pct": _to_pct(s.roa),
        "debt_to_equity": s.debt_to_equity,
        "fcf_margin_pct": _to_pct(s.fcf_margin),
    }


def _to_m(val: Optional[float]) -> Optional[float]:
    return round(val / 1_000_000, 2) if val is not None else None


def _to_pct(val: Optional[float]) -> Optional[float]:
    return round(val * 100, 2) if val is not None else None
