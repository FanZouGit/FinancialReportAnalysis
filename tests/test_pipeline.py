"""
tests/test_pipeline.py

Unit tests for the EDGAR pipeline.

Run with:
    pytest tests/ -v
    pytest tests/ -v -k "test_taxonomy"
"""

import pytest
from datetime import date
from unittest.mock import MagicMock

from config.taxonomy import TAG_TO_CANONICAL, CONCEPT_ALIASES
from extraction.normaliser import (
    _resolve_best_fact, _months_between, _compute_ratios, _pick_by_value
)
from ingestion.downloader import _parse_date, _derive_fiscal_period
from database.models import Metric, RawFact
from analytics.repository import VALID_RATIO_FIELDS


# ──────────────────────────────────────────────────────────────────────────── #
# Shared helpers (module-level, used by classes that don't take fixtures)       #
# ──────────────────────────────────────────────────────────────────────────── #

def _make_fact(**kwargs) -> RawFact:
    fact = MagicMock(spec=RawFact)
    fact.unit = kwargs.get("unit", "USD")
    fact.period_type = kwargs.get("period_type", "duration")
    fact.period_end = kwargs.get("period_end", date(2023, 9, 30))
    fact.period_start = kwargs.get("period_start", date(2023, 7, 1))
    fact.value = kwargs.get("value", 1_000_000.0)
    return fact


def _make_metric(**kwargs) -> Metric:
    m = MagicMock(spec=Metric)
    m.metric_id = 1
    m.cik = "123"
    m.period_label = "Q3-2023"
    m.revenue = kwargs.get("revenue", 10_000_000.0)
    m.gross_profit = kwargs.get("gross_profit", 6_000_000.0)
    m.operating_income = kwargs.get("operating_income", 2_000_000.0)
    m.net_income = kwargs.get("net_income", 1_500_000.0)
    m.total_assets = kwargs.get("total_assets", 50_000_000.0)
    m.total_liabilities = kwargs.get("total_liabilities", 20_000_000.0)
    m.total_equity = kwargs.get("total_equity", 30_000_000.0)
    m.free_cash_flow = kwargs.get("free_cash_flow", 1_000_000.0)
    return m


# ──────────────────────────────────────────────────────────────────────────── #
# Taxonomy tests                                                                 #
# ──────────────────────────────────────────────────────────────────────────── #

class TestTaxonomy:
    def test_all_canonical_concepts_present(self):
        expected = {
            "revenue", "gross_profit", "operating_income", "net_income",
            "eps_basic", "eps_diluted", "total_assets", "total_liabilities",
            "total_equity", "operating_cash_flow", "capex",
        }
        assert expected.issubset(set(CONCEPT_ALIASES.keys()))

    def test_reverse_lookup_populated(self):
        assert "us-gaap:Revenues" in TAG_TO_CANONICAL
        assert TAG_TO_CANONICAL["us-gaap:Revenues"] == "revenue"

    def test_all_aliases_resolve(self):
        for canonical, tags in CONCEPT_ALIASES.items():
            for tag in tags:
                assert TAG_TO_CANONICAL[tag] == canonical, \
                    f"Tag {tag} does not resolve to {canonical}"

    def test_net_income_aliases(self):
        assert TAG_TO_CANONICAL["us-gaap:NetIncomeLoss"] == "net_income"
        assert TAG_TO_CANONICAL["us-gaap:ProfitLoss"] == "net_income"

    def test_free_cash_flow_has_no_aliases(self):
        # free_cash_flow is derived, not reported directly in XBRL
        assert CONCEPT_ALIASES["free_cash_flow"] == []


# ──────────────────────────────────────────────────────────────────────────── #
# Date and period helpers                                                        #
# ──────────────────────────────────────────────────────────────────────────── #

class TestDateHelpers:
    def test_parse_date_valid(self):
        assert _parse_date("2023-09-30") == date(2023, 9, 30)

    def test_parse_date_none(self):
        assert _parse_date(None) is None
        assert _parse_date("") is None

    def test_parse_date_invalid(self):
        assert _parse_date("not-a-date") is None

    def test_derive_fiscal_period_annual(self):
        fy, fq, label = _derive_fiscal_period(date(2023, 12, 31), "10-K")
        assert fy == 2023
        assert fq is None
        assert label == "FY-2023"

    def test_derive_fiscal_period_q1(self):
        fy, fq, label = _derive_fiscal_period(date(2023, 3, 31), "10-Q")
        assert fq == 1
        assert label == "Q1-2023"

    def test_derive_fiscal_period_q2(self):
        fy, fq, label = _derive_fiscal_period(date(2023, 6, 30), "10-Q")
        assert fq == 2
        assert label == "Q2-2023"

    def test_derive_fiscal_period_q3(self):
        fy, fq, label = _derive_fiscal_period(date(2023, 9, 30), "10-Q")
        assert fq == 3
        assert label == "Q3-2023"

    def test_derive_fiscal_period_q4(self):
        fy, fq, label = _derive_fiscal_period(date(2023, 12, 31), "10-Q")
        assert fq == 4
        assert label == "Q4-2023"

    def test_months_between(self):
        assert _months_between(date(2023, 1, 1), date(2023, 4, 1)) == 3
        assert _months_between(date(2022, 1, 1), date(2023, 1, 1)) == 12
        assert _months_between(None, date(2023, 1, 1)) == 0


# ──────────────────────────────────────────────────────────────────────────── #
# Fact resolution                                                                #
# ──────────────────────────────────────────────────────────────────────────── #

class TestFactResolution:
    def test_empty_facts_returns_none(self):
        assert _resolve_best_fact([], date(2023, 9, 30), "10-Q") is None

    def test_prefers_usd(self):
        usd_fact = _make_fact(unit="USD", value=100.0)
        shares_fact = _make_fact(unit="shares", value=200.0)
        result = _resolve_best_fact([usd_fact, shares_fact], date(2023, 9, 30), "10-Q")
        assert result == 100.0

    def test_returns_none_for_wrong_period(self):
        fact = _make_fact(period_end=date(2023, 6, 30))
        result = _resolve_best_fact([fact], date(2023, 9, 30), "10-Q")
        assert result is None

    def test_picks_quarterly_for_10q(self):
        q_fact = _make_fact(
            period_start=date(2023, 7, 1),
            period_end=date(2023, 9, 30),
            value=300.0,
        )
        ytd_fact = _make_fact(
            period_start=date(2023, 1, 1),
            period_end=date(2023, 9, 30),
            value=900.0,
        )
        result = _resolve_best_fact([q_fact, ytd_fact], date(2023, 9, 30), "10-Q")
        assert result == 300.0

    def test_picks_annual_for_10k(self):
        annual = _make_fact(
            period_start=date(2022, 10, 1),
            period_end=date(2023, 9, 30),
            value=4000.0,
        )
        ytd = _make_fact(
            period_start=date(2023, 7, 1),
            period_end=date(2023, 9, 30),
            value=1000.0,
        )
        result = _resolve_best_fact([annual, ytd], date(2023, 9, 30), "10-K")
        assert result == 4000.0

    def test_instant_fact_for_balance_sheet(self):
        instant = _make_fact(period_type="instant", period_start=None, value=50_000.0)
        result = _resolve_best_fact([instant], date(2023, 9, 30), "10-Q")
        assert result == 50_000.0

    def test_pick_by_value_all_none_returns_none(self):
        facts = [_make_fact(value=None), _make_fact(value=None)]
        assert _pick_by_value(facts) is None


# ──────────────────────────────────────────────────────────────────────────── #
# Ratio computation                                                              #
# ──────────────────────────────────────────────────────────────────────────── #

class TestRatioComputation:
    def test_gross_margin(self):
        m = _make_metric(revenue=100.0, gross_profit=60.0)
        r = _compute_ratios(m)
        assert abs(r.gross_margin - 0.6) < 1e-6

    def test_net_margin(self):
        m = _make_metric(revenue=100.0, net_income=20.0)
        r = _compute_ratios(m)
        assert abs(r.net_margin - 0.2) < 1e-6

    def test_roe(self):
        m = _make_metric(net_income=15.0, total_equity=100.0)
        r = _compute_ratios(m)
        assert abs(r.roe - 0.15) < 1e-6

    def test_roa(self):
        m = _make_metric(net_income=10.0, total_assets=200.0)
        r = _compute_ratios(m)
        assert abs(r.roa - 0.05) < 1e-6

    def test_asset_turnover(self):
        m = _make_metric(revenue=80.0, total_assets=200.0)
        r = _compute_ratios(m)
        assert abs(r.asset_turnover - 0.4) < 1e-6

    def test_equity_multiplier(self):
        m = _make_metric(total_assets=300.0, total_equity=100.0)
        r = _compute_ratios(m)
        assert abs(r.equity_multiplier - 3.0) < 1e-6

    def test_fcf_margin(self):
        m = _make_metric(free_cash_flow=25.0, revenue=100.0)
        r = _compute_ratios(m)
        assert abs(r.fcf_margin - 0.25) < 1e-6

    def test_fcf_conversion(self):
        m = _make_metric(free_cash_flow=8.0, net_income=10.0)
        r = _compute_ratios(m)
        assert abs(r.fcf_conversion - 0.8) < 1e-6

    def test_division_by_zero_safe(self):
        m = _make_metric(revenue=0.0, total_equity=0.0)
        r = _compute_ratios(m)
        assert r.gross_margin is None
        assert r.roe is None

    def test_none_inputs_safe(self):
        m = _make_metric()
        m.revenue = None
        r = _compute_ratios(m)
        assert r.gross_margin is None
        assert r.net_margin is None


# ──────────────────────────────────────────────────────────────────────────── #
# EDGAR client (mocked)                                                          #
# ──────────────────────────────────────────────────────────────────────────── #

class TestEdgarClient:
    @pytest.mark.asyncio
    async def test_pad_cik_short(self):
        from ingestion.edgar_client import EdgarClient
        assert EdgarClient._pad_cik("320193") == "0000320193"

    @pytest.mark.asyncio
    async def test_pad_cik_already_padded(self):
        from ingestion.edgar_client import EdgarClient
        assert EdgarClient._pad_cik("0000320193") == "0000320193"

    @pytest.mark.asyncio
    async def test_pad_cik_numeric_string(self):
        from ingestion.edgar_client import EdgarClient
        assert EdgarClient._pad_cik("1") == "0000000001"

    @pytest.mark.asyncio
    async def test_get_company_facts_url(self):
        from ingestion.edgar_client import EdgarClient
        client = EdgarClient()
        cik = "320193"
        expected = "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"
        actual_url = f"{client.BASE}/api/xbrl/companyfacts/CIK{client._pad_cik(cik)}.json"
        assert actual_url == expected


# ──────────────────────────────────────────────────────────────────────────── #
# Text chunking                                                                  #
# ──────────────────────────────────────────────────────────────────────────── #

class TestTextChunking:
    def test_chunk_splits_long_text(self):
        from agent.semantic_search import _chunk_text
        text = "This is a sentence. " * 200
        chunks = _chunk_text(text, chunk_size=100, overlap=10)
        assert len(chunks) >= 2

    def test_chunk_preserves_content(self):
        from agent.semantic_search import _chunk_text
        text = "Hello world. " * 50
        chunks = _chunk_text(text, chunk_size=20, overlap=5)
        combined = " ".join(c[0] for c in chunks)
        assert "Hello world" in combined

    def test_chunk_returns_tuples(self):
        from agent.semantic_search import _chunk_text
        chunks = _chunk_text("Short text.", chunk_size=512, overlap=64)
        assert len(chunks) == 1
        text_val, start, end = chunks[0]
        assert isinstance(text_val, str)
        assert start == 0

    def test_overlap_produces_shared_content(self):
        from agent.semantic_search import _chunk_text
        # Long enough to produce multiple chunks with meaningful overlap
        text = "Alpha beta gamma delta. " * 100
        chunks = _chunk_text(text, chunk_size=50, overlap=20)
        assert len(chunks) >= 2
        # The end of chunk 0 and the beginning of chunk 1 should share content
        end_of_first = chunks[0][0][-50:]
        start_of_second = chunks[1][0][:50]
        # At least some characters should appear in both
        shared = set(end_of_first.split()) & set(start_of_second.split())
        assert len(shared) > 0


# ──────────────────────────────────────────────────────────────────────────── #
# Sector benchmark security                                                      #
# ──────────────────────────────────────────────────────────────────────────── #

class TestSectorBenchmarkSecurity:
    def test_valid_fields_are_known_ratio_names(self):
        for field in VALID_RATIO_FIELDS:
            assert field.replace("_", "").isalpha(), \
                f"Unexpected characters in ratio field name: {field}"

    def test_sql_injection_string_not_in_whitelist(self):
        assert "'; DROP TABLE ratios; --" not in VALID_RATIO_FIELDS
        assert "net_margin; DROP TABLE" not in VALID_RATIO_FIELDS

    def test_whitelist_rejects_unknown_metric(self):
        with pytest.raises(ValueError):
            metric = "bad_field"
            if metric not in VALID_RATIO_FIELDS:
                raise ValueError(f"Unknown metric: {metric}")

    def test_whitelist_accepts_all_ratio_fields(self):
        expected = {
            "gross_margin", "operating_margin", "net_margin",
            "roe", "roa", "debt_to_equity", "debt_to_assets",
            "equity_multiplier", "asset_turnover", "fcf_margin", "fcf_conversion",
        }
        assert expected == VALID_RATIO_FIELDS
