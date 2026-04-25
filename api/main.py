"""
api/main.py

FastAPI application exposing:
  GET  /companies/{ticker}/metrics      → time series
  GET  /companies/{ticker}/peers        → peer ranking
  GET  /companies/{ticker}/trends       → trend analysis
  GET  /companies/{ticker}/anomalies    → anomaly detection
  GET  /sectors/{sector}/benchmark      → sector stats
  POST /compare                         → cross-company
  POST /search                          → semantic MD&A search
  POST /ask                             → LLM query agent
"""

from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from analytics.repository import AnalyticsRepository
from agent.query_agent import FinancialAnswer, ask
from agent.semantic_search import SemanticSearch
from database.engine import AsyncSessionLocal, init_db


# ──────────────────────────────────────────────────────────────────────────── #
# App lifecycle                                                                  #
# ──────────────────────────────────────────────────────────────────────────── #

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(
    title="EDGAR Financial Intelligence API",
    description="Query SEC 10-K and 10-Q financial data across companies, sectors, and time",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ──────────────────────────────────────────────────────────────────────────── #
# Dependency                                                                     #
# ──────────────────────────────────────────────────────────────────────────── #

async def get_session() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session


# ──────────────────────────────────────────────────────────────────────────── #
# Request / Response schemas                                                     #
# ──────────────────────────────────────────────────────────────────────────── #

class CompareRequest(BaseModel):
    tickers: List[str]
    period_label: str
    form_type: str = "10-Q"


class SearchRequest(BaseModel):
    query: str
    top_k: int = 10
    ticker: Optional[str] = None
    sector: Optional[str] = None
    period_label: Optional[str] = None


class AskRequest(BaseModel):
    question: str


# ──────────────────────────────────────────────────────────────────────────── #
# Routes                                                                         #
# ──────────────────────────────────────────────────────────────────────────── #

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/companies/{ticker}/metrics")
async def get_metrics(
    ticker: str,
    form_type: str = Query("10-Q", enum=["10-K", "10-Q"]),
    limit: int = Query(8, ge=1, le=40),
    session: AsyncSession = Depends(get_session),
):
    """Financial metrics time series for a company."""
    repo = AnalyticsRepository(session)
    snapshots = await repo.get_metrics_by_ticker(ticker.upper(), form_type, limit)
    if not snapshots:
        raise HTTPException(404, f"No data found for ticker {ticker}")
    return {"ticker": ticker.upper(), "count": len(snapshots), "data": snapshots}


@app.get("/companies/{ticker}/peers")
async def get_peers(
    ticker: str,
    ratio: str = Query("net_margin"),
    period_label: Optional[str] = Query(None),
    form_type: str = Query("10-Q", enum=["10-K", "10-Q"]),
    session: AsyncSession = Depends(get_session),
):
    """Rank a company against its industry peers."""
    repo = AnalyticsRepository(session)
    company = await repo._get_company_by_ticker(ticker.upper())
    if not company:
        raise HTTPException(404, f"Company not found: {ticker}")

    rankings = await repo.compare_peers(company.cik, ratio, period_label, form_type)
    return {
        "ticker": ticker.upper(),
        "ratio": ratio,
        "period": period_label or "latest",
        "rankings": rankings,
    }


@app.get("/companies/{ticker}/trends")
async def get_trends(
    ticker: str,
    metric: str = Query("revenue"),
    n_periods: int = Query(8, ge=2, le=20),
    form_type: str = Query("10-Q", enum=["10-K", "10-Q"]),
    session: AsyncSession = Depends(get_session),
):
    """Period-over-period growth rates."""
    repo = AnalyticsRepository(session)
    company = await repo._get_company_by_ticker(ticker.upper())
    if not company:
        raise HTTPException(404, f"Company not found: {ticker}")

    trends = await repo.trend_analysis(company.cik, metric, n_periods, form_type)
    return {"ticker": ticker.upper(), "metric": metric, "trends": trends}


@app.get("/companies/{ticker}/anomalies")
async def get_anomalies(
    ticker: str,
    metric: str = Query("net_margin"),
    z_threshold: float = Query(2.0, ge=1.0, le=4.0),
    form_type: str = Query("10-Q", enum=["10-K", "10-Q"]),
    session: AsyncSession = Depends(get_session),
):
    """Statistical anomalies in a company's financial history."""
    repo = AnalyticsRepository(session)
    company = await repo._get_company_by_ticker(ticker.upper())
    if not company:
        raise HTTPException(404, f"Company not found: {ticker}")

    anomalies = await repo.detect_anomalies(
        company.cik, metric, form_type, z_threshold
    )
    return {
        "ticker": ticker.upper(),
        "metric": metric,
        "z_threshold": z_threshold,
        "anomalies": anomalies,
    }


@app.get("/sectors/{sector}/benchmark")
async def get_benchmark(
    sector: str,
    metric: str = Query("net_margin"),
    period_label: str = Query(..., description="e.g. Q3-2023 or FY-2023"),
    form_type: str = Query("10-Q", enum=["10-K", "10-Q"]),
    session: AsyncSession = Depends(get_session),
):
    """Aggregate sector-level statistics."""
    repo = AnalyticsRepository(session)
    try:
        stats = await repo.sector_benchmark(sector, period_label, metric, form_type)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return stats


@app.post("/compare")
async def compare_companies(
    req: CompareRequest,
    session: AsyncSession = Depends(get_session),
):
    """Side-by-side comparison of multiple companies."""
    repo = AnalyticsRepository(session)
    snapshots = await repo.cross_company(req.tickers, req.period_label, req.form_type)
    if not snapshots:
        raise HTTPException(404, "No data found for given tickers and period")
    return {"period": req.period_label, "companies": snapshots}


@app.post("/search")
async def search_mda(
    req: SearchRequest,
    session: AsyncSession = Depends(get_session),
):
    """Semantic search over MD&A filings text."""
    searcher = SemanticSearch(session)
    cik = None
    if req.ticker:
        repo = AnalyticsRepository(session)
        company = await repo._get_company_by_ticker(req.ticker.upper())
        cik = company.cik if company else None

    results = await searcher.search(
        req.query,
        top_k=req.top_k,
        cik=cik,
        sector=req.sector,
        period_label=req.period_label,
    )
    return {"query": req.query, "count": len(results), "results": results}


@app.post("/ask", response_model=FinancialAnswer)
async def ask_agent(
    req: AskRequest,
    session: AsyncSession = Depends(get_session),
):
    """Natural language query answered by the LLM agent."""
    try:
        answer = await ask(session, req.question)
        return answer
    except Exception as exc:
        raise HTTPException(500, f"Agent error: {exc}")
