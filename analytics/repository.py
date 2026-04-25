"""
analytics/repository.py

Query layer for all analytical use cases:

  - get_company_metrics()    → time series for one company
  - compare_peers()          → rank a company against its peer group
  - sector_benchmark()       → aggregate stats for a sector / period
  - cross_company()          → compare named companies side-by-side
  - trend_analysis()         → growth rates and trailing averages
  - anomaly_detection()      → flag statistical outliers
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Company, Metric, PeerGroup, Ratio

log = logging.getLogger(__name__)

# Explicit whitelist — prevents raw SQL injection via metric/ratio name parameters.
VALID_RATIO_FIELDS: frozenset[str] = frozenset({
    "gross_margin", "operating_margin", "net_margin",
    "roe", "roa", "debt_to_equity", "debt_to_assets",
    "equity_multiplier", "asset_turnover", "fcf_margin", "fcf_conversion",
})

VALID_METRIC_FIELDS: frozenset[str] = frozenset({
    "revenue", "gross_profit", "operating_income", "net_income",
    "eps_basic", "eps_diluted", "total_assets", "total_liabilities",
    "total_equity", "operating_cash_flow", "capex", "free_cash_flow",
})


# ──────────────────────────────────────────────────────────────────────────── #
# Data transfer objects                                                          #
# ──────────────────────────────────────────────────────────────────────────── #

@dataclass
class PeriodSnapshot:
    cik: str
    ticker: Optional[str]
    company_name: str
    period_label: str
    period_end: str
    form_type: str
    revenue: Optional[float] = None
    gross_profit: Optional[float] = None
    operating_income: Optional[float] = None
    net_income: Optional[float] = None
    total_assets: Optional[float] = None
    total_equity: Optional[float] = None
    operating_cash_flow: Optional[float] = None
    free_cash_flow: Optional[float] = None
    gross_margin: Optional[float] = None
    operating_margin: Optional[float] = None
    net_margin: Optional[float] = None
    roe: Optional[float] = None
    roa: Optional[float] = None
    debt_to_equity: Optional[float] = None
    fcf_margin: Optional[float] = None


@dataclass
class PeerRanking:
    cik: str
    ticker: Optional[str]
    company_name: str
    value: Optional[float]
    rank: int
    percentile: float
    peer_count: int


@dataclass
class SectorStats:
    sector: str
    period_label: str
    metric: str
    count: int
    mean: Optional[float]
    median: Optional[float]
    p25: Optional[float]
    p75: Optional[float]
    min: Optional[float]
    max: Optional[float]


# ──────────────────────────────────────────────────────────────────────────── #
# Repository                                                                     #
# ──────────────────────────────────────────────────────────────────────────── #

class AnalyticsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------ #
    # 1. Company time series                                               #
    # ------------------------------------------------------------------ #

    async def get_company_metrics(
        self,
        cik: str,
        form_type: Optional[str] = None,
        limit: int = 20,
    ) -> List[PeriodSnapshot]:
        """Return time-ordered metrics for a single company."""
        q = (
            select(Company, Metric, Ratio)
            .join(Metric, Metric.cik == Company.cik)
            .outerjoin(Ratio, Ratio.metric_id == Metric.metric_id)
            .where(Company.cik == str(int(cik)))
        )
        if form_type:
            q = q.where(Metric.form_type == form_type)

        q = q.order_by(Metric.period_end.desc()).limit(limit)
        rows = (await self.session.execute(q)).all()
        return [_to_snapshot(co, m, r) for co, m, r in rows]

    async def get_metrics_by_ticker(
        self, ticker: str, form_type: Optional[str] = None, limit: int = 20
    ) -> List[PeriodSnapshot]:
        company = await self._get_company_by_ticker(ticker)
        if not company:
            return []
        return await self.get_company_metrics(company.cik, form_type, limit)

    # ------------------------------------------------------------------ #
    # 2. Peer comparison                                                   #
    # ------------------------------------------------------------------ #

    async def compare_peers(
        self,
        cik: str,
        ratio_field: str = "net_margin",
        period_label: Optional[str] = None,
        form_type: str = "10-Q",
    ) -> List[PeerRanking]:
        """
        Rank a company against all peers in the same SIC peer group.
        ratio_field must be a name in VALID_RATIO_FIELDS.
        """
        if ratio_field not in VALID_RATIO_FIELDS:
            raise ValueError(f"Unknown ratio field: {ratio_field}")

        if period_label is None:
            period_label = await self._latest_period(cik, form_type)
        if period_label is None:
            return []

        peer_keys_result = await self.session.execute(
            select(PeerGroup.group_key).where(PeerGroup.cik == str(int(cik)))
        )
        peer_keys = [row[0] for row in peer_keys_result.all()]
        if not peer_keys:
            log.warning("No peer group found for CIK %s", cik)
            return []

        peer_ciks_result = await self.session.execute(
            select(PeerGroup.cik).where(PeerGroup.group_key.in_(peer_keys))
        )
        peer_ciks = list({row[0] for row in peer_ciks_result.all()})

        ratio_col = getattr(Ratio, ratio_field)
        q = (
            select(Company.cik, Company.ticker, Company.name, ratio_col)
            .join(Metric, Metric.cik == Company.cik)
            .join(Ratio, Ratio.metric_id == Metric.metric_id)
            .where(Company.cik.in_(peer_ciks))
            .where(Metric.period_label == period_label)
            .where(Metric.form_type == form_type)
            .where(ratio_col.isnot(None))
            .order_by(ratio_col.desc())
        )
        rows = (await self.session.execute(q)).all()

        total = len(rows)
        return [
            PeerRanking(
                cik=row[0],
                ticker=row[1],
                company_name=row[2],
                value=round(row[3], 6) if row[3] is not None else None,
                rank=idx + 1,
                percentile=round((1 - idx / total) * 100, 1) if total > 1 else 100.0,
                peer_count=total,
            )
            for idx, row in enumerate(rows)
        ]

    # ------------------------------------------------------------------ #
    # 3. Sector benchmarks                                                 #
    # ------------------------------------------------------------------ #

    async def sector_benchmark(
        self,
        sector: str,
        period_label: str,
        metric: str = "net_margin",
        form_type: str = "10-Q",
    ) -> SectorStats:
        """Aggregate stats for a sector in a given period."""
        if metric not in VALID_RATIO_FIELDS:
            raise ValueError(
                f"Unknown metric '{metric}'. Valid options: {sorted(VALID_RATIO_FIELDS)}"
            )

        # Safe: metric is validated against a whitelist before interpolation.
        sql = text(f"""
            SELECT
                COUNT(*)                            AS cnt,
                AVG(r.{metric})                     AS mean,
                PERCENTILE_CONT(0.5) WITHIN GROUP
                    (ORDER BY r.{metric})           AS median,
                PERCENTILE_CONT(0.25) WITHIN GROUP
                    (ORDER BY r.{metric})           AS p25,
                PERCENTILE_CONT(0.75) WITHIN GROUP
                    (ORDER BY r.{metric})           AS p75,
                MIN(r.{metric})                     AS min,
                MAX(r.{metric})                     AS max
            FROM ratios r
            JOIN metrics m   ON m.metric_id = r.metric_id
            JOIN companies c ON c.cik = m.cik
            WHERE c.sector = :sector
              AND m.period_label = :period_label
              AND m.form_type = :form_type
              AND r.{metric} IS NOT NULL
        """)

        row = (await self.session.execute(
            sql,
            {"sector": sector, "period_label": period_label, "form_type": form_type}
        )).one()

        return SectorStats(
            sector=sector,
            period_label=period_label,
            metric=metric,
            count=row.cnt or 0,
            mean=_r(row.mean),
            median=_r(row.median),
            p25=_r(row.p25),
            p75=_r(row.p75),
            min=_r(row.min),
            max=_r(row.max),
        )

    # ------------------------------------------------------------------ #
    # 4. Cross-company comparison                                          #
    # ------------------------------------------------------------------ #

    async def cross_company(
        self,
        tickers: List[str],
        period_label: str,
        form_type: str = "10-Q",
    ) -> List[PeriodSnapshot]:
        """Side-by-side comparison of named companies for a given period."""
        q = (
            select(Company, Metric, Ratio)
            .join(Metric, Metric.cik == Company.cik)
            .outerjoin(Ratio, Ratio.metric_id == Metric.metric_id)
            .where(Company.ticker.in_([t.upper() for t in tickers]))
            .where(Metric.period_label == period_label)
            .where(Metric.form_type == form_type)
        )
        rows = (await self.session.execute(q)).all()
        return [_to_snapshot(co, m, r) for co, m, r in rows]

    # ------------------------------------------------------------------ #
    # 5. Trend analysis                                                    #
    # ------------------------------------------------------------------ #

    async def trend_analysis(
        self,
        cik: str,
        metric: str = "revenue",
        n_periods: int = 8,
        form_type: str = "10-Q",
    ) -> List[Dict[str, Any]]:
        """Returns period-over-period growth rates and trailing averages."""
        if metric not in VALID_METRIC_FIELDS and metric not in VALID_RATIO_FIELDS:
            raise ValueError(f"Unknown metric: {metric}")

        snapshots = await self.get_company_metrics(
            cik, form_type=form_type, limit=n_periods + 1
        )
        snapshots = list(reversed(snapshots))

        results = []
        for i, s in enumerate(snapshots):
            val = getattr(s, metric, None)
            prev = getattr(snapshots[i - 1], metric, None) if i > 0 else None
            yoy_growth = None
            if i >= 4:
                val_y_ago = getattr(snapshots[i - 4], metric, None)
                if val is not None and val_y_ago and val_y_ago != 0:
                    yoy_growth = (val - val_y_ago) / abs(val_y_ago)

            qoq_growth = None
            if prev is not None and prev != 0 and val is not None:
                qoq_growth = (val - prev) / abs(prev)

            results.append({
                "period_label": s.period_label,
                "period_end": s.period_end,
                "value": val,
                "qoq_growth": round(qoq_growth, 4) if qoq_growth is not None else None,
                "yoy_growth": round(yoy_growth, 4) if yoy_growth is not None else None,
            })

        return results[1:]

    # ------------------------------------------------------------------ #
    # 6. Anomaly detection                                                 #
    # ------------------------------------------------------------------ #

    async def detect_anomalies(
        self,
        cik: str,
        metric: str = "net_margin",
        form_type: str = "10-Q",
        z_threshold: float = 2.0,
    ) -> List[Dict[str, Any]]:
        """
        Flag periods where a metric deviates more than z_threshold
        standard deviations from the company's own trailing history.
        """
        snapshots = await self.get_company_metrics(cik, form_type=form_type, limit=20)
        values = [(s.period_label, getattr(s, metric)) for s in snapshots if getattr(s, metric) is not None]

        if len(values) < 4:
            return []

        import statistics
        vals = [v for _, v in values]
        mean = statistics.mean(vals)
        stdev = statistics.stdev(vals) if len(vals) > 1 else 0

        anomalies = []
        for label, val in values:
            if stdev == 0:
                continue
            z = (val - mean) / stdev
            if abs(z) >= z_threshold:
                anomalies.append({
                    "period_label": label,
                    "value": val,
                    "z_score": round(z, 2),
                    "mean": round(mean, 4),
                    "stdev": round(stdev, 4),
                })

        return anomalies

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    async def _get_company_by_ticker(self, ticker: str) -> Optional[Company]:
        result = await self.session.execute(
            select(Company).where(Company.ticker == ticker.upper())
        )
        return result.scalar_one_or_none()

    async def _latest_period(self, cik: str, form_type: str) -> Optional[str]:
        result = await self.session.execute(
            select(Metric.period_label)
            .where(Metric.cik == str(int(cik)))
            .where(Metric.form_type == form_type)
            .order_by(Metric.period_end.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


# ──────────────────────────────────────────────────────────────────────────── #
# Helpers                                                                       #
# ──────────────────────────────────────────────────────────────────────────── #

def _to_snapshot(co: Company, m: Metric, r: Optional[Ratio]) -> PeriodSnapshot:
    return PeriodSnapshot(
        cik=co.cik,
        ticker=co.ticker,
        company_name=co.name,
        period_label=m.period_label,
        period_end=str(m.period_end),
        form_type=m.form_type,
        revenue=m.revenue,
        gross_profit=m.gross_profit,
        operating_income=m.operating_income,
        net_income=m.net_income,
        total_assets=m.total_assets,
        total_equity=m.total_equity,
        operating_cash_flow=m.operating_cash_flow,
        free_cash_flow=m.free_cash_flow,
        gross_margin=r.gross_margin if r else None,
        operating_margin=r.operating_margin if r else None,
        net_margin=r.net_margin if r else None,
        roe=r.roe if r else None,
        roa=r.roa if r else None,
        debt_to_equity=r.debt_to_equity if r else None,
        fcf_margin=r.fcf_margin if r else None,
    )


def _r(val: Any) -> Optional[float]:
    return round(float(val), 6) if val is not None else None
