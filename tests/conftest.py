from datetime import date
from unittest.mock import MagicMock

import pytest

from database.models import Metric, RawFact


@pytest.fixture
def make_raw_fact():
    def _factory(**kwargs) -> RawFact:
        fact = MagicMock(spec=RawFact)
        fact.unit = kwargs.get("unit", "USD")
        fact.period_type = kwargs.get("period_type", "duration")
        fact.period_end = kwargs.get("period_end", date(2023, 9, 30))
        fact.period_start = kwargs.get("period_start", date(2023, 7, 1))
        fact.value = kwargs.get("value", 1_000_000.0)
        return fact
    return _factory


@pytest.fixture
def make_metric():
    def _factory(**kwargs) -> Metric:
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
    return _factory
