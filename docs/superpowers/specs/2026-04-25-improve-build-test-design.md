# EDGAR Financial Intelligence Platform — Improve, Build & Test

**Date:** 2026-04-25
**Scope:** Make the app fully runnable and unit-testable, fix all bugs, improve code quality.

---

## Problem statement

All source files currently live flat at the project root, but every import references sub-packages
(`config.settings`, `database.engine`, etc.) that do not exist as directories. The app cannot be
imported, run, or tested in its current state. Several additional bugs exist in the flat files.

---

## Decisions

| Question | Decision |
|---|---|
| Package structure | Create proper sub-packages matching the README architecture doc |
| Implementation strategy | Restructure and fix in one pass (Approach B) |
| LLM provider | Anthropic only — `claude-sonnet-4-6`; OpenAI key optional |

---

## 1. Package structure

After migration the layout is:

```
FinancialReportAnalysis/
├── config/
│   ├── __init__.py
│   ├── settings.py          # NEW
│   └── taxonomy.py          # MOVED
├── database/
│   ├── __init__.py
│   ├── engine.py            # NEW
│   └── models.py            # MOVED
├── ingestion/
│   ├── __init__.py
│   ├── edgar_client.py      # MOVED
│   └── downloader.py        # MOVED
├── extraction/
│   ├── __init__.py
│   └── normaliser.py        # MOVED
├── analytics/
│   ├── __init__.py
│   └── repository.py        # MOVED
├── agent/
│   ├── __init__.py
│   ├── query_agent.py       # MOVED
│   └── semantic_search.py   # MOVED
├── api/
│   ├── __init__.py
│   └── main.py              # MOVED
├── scripts/
│   └── bootstrap_sp500.py   # MOVED
├── tests/
│   ├── __init__.py
│   ├── conftest.py          # NEW
│   └── test_pipeline.py     # MOVED
├── .env.example             # NEW
├── docker-compose.yml       # unchanged
├── pytest.ini               # NEW
└── requirements.txt         # unchanged (pin pydantic-ai)
```

Each package gets an empty `__init__.py`. All imports in moved files are updated to the full
package path (e.g. `from config.settings import settings`).

---

## 2. New files

### `config/settings.py`

Pydantic `BaseSettings` loaded from `.env` / environment variables.

```python
class Settings(BaseSettings):
    database_url: str                        # required
    redis_url: str = "redis://localhost:6379"
    sec_user_agent: str                      # required — SEC identity header
    sec_base_url: str = "https://data.sec.gov"
    sec_requests_per_second: int = 8
    anthropic_api_key: Optional[str] = None  # agent disabled if absent
    openai_api_key: Optional[str] = None
    embedding_model: str = "all-MiniLM-L6-v2"
    sp500_only: bool = True
    max_workers: int = 4

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
```

### `database/engine.py`

```python
engine = create_async_engine(settings.database_url, echo=False, pool_pre_ping=True)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
```

### `.env.example`

Template with all 10 variables. Required variables clearly marked. Safe defaults pre-filled.

---

## 3. Bug fixes

| # | File | Bug | Fix |
|---|---|---|---|
| 1 | `bootstrap_sp500.py:42` | `Optional` used before import (line 172) | Move `from typing import Optional` to top |
| 2 | `repository.py` `sector_benchmark()` | `metric` directly interpolated into raw SQL via `.format()` | Replace `hasattr` guard with `VALID_RATIO_FIELDS` frozenset whitelist |
| 3 | `downloader.py` | `datetime.utcnow()` deprecated in Python 3.12+ | Replace with `datetime.now(timezone.utc)` |
| 4 | `edgar_client.py` | `Host: data.sec.gov` hardcoded in shared client; breaks calls to `www.sec.gov` / `efts.sec.gov` | Remove `Host` header; httpx sets it correctly per-request |
| 5 | `query_agent.py` | Stale model `claude-sonnet-4-5` | Update to `claude-sonnet-4-6` |
| 6 | `edgar_client.py` | `_get_semaphore()` creates `asyncio.Semaphore` outside event loop | Move semaphore to per-client instance, initialised in `__aenter__` |

---

## 4. Tests

### `pytest.ini`

```ini
[pytest]
asyncio_mode = auto
testpaths = tests
```

### `tests/conftest.py`

Shared fixtures:
- `make_raw_fact(**kwargs) -> RawFact` — replaces inline `_make_fact` helper
- `make_metric(**kwargs) -> Metric` — replaces inline `_make_metric` helper

### New test coverage

| Class | Tests added |
|---|---|
| `TestTaxonomy` | `free_cash_flow` key exists with empty alias list |
| `TestFactResolution` | `_pick_by_value` returns `None` when all values are `None` |
| `TestRatioComputation` | `fcf_margin`, `fcf_conversion`, `roa`, `asset_turnover`, `equity_multiplier` |
| `TestTextChunking` | Overlap produces shared content across adjacent chunks |
| `TestSectorBenchmarkSecurity` | Invalid metric name raises `ValueError` |
| `TestEdgarClient` | `_pad_cik` edge cases (already-padded, numeric string) |
| `TestDownloader` | `_derive_fiscal_period` for Q2, Q4, and 10-K |

All tests are unit tests — no database or network required.

---

## 5. Quality improvements

**Sector benchmark whitelist**

```python
VALID_RATIO_FIELDS: frozenset[str] = frozenset({
    "gross_margin", "operating_margin", "net_margin",
    "roe", "roa", "debt_to_equity", "debt_to_assets",
    "equity_multiplier", "asset_turnover", "fcf_margin", "fcf_conversion",
})
```

**Peer group building moved into `FilingDownloader.sync_company()`**
Removes duplication between the bootstrap script and the ARQ job worker.

**`pydantic-ai` version pinned to `>=0.0.14,<0.1.0`**
The `0.0.x` series had breaking API changes; this range targets the stable `Agent`/`RunContext` API.

---

## 6. Implementation order

1. Create all package directories + empty `__init__.py` files
2. Create `config/settings.py` and `database/engine.py`
3. Move and fix each source file (imports + bugs) in dependency order:
   `taxonomy.py` → `models.py` → `edgar_client.py` → `downloader.py` → `normaliser.py` → `repository.py` → `semantic_search.py` → `query_agent.py` → `main.py` → `bootstrap_sp500.py`
4. Create `pytest.ini`, `tests/conftest.py`, update `tests/test_pipeline.py`
5. Create `.env.example`
6. Update `requirements.txt`
7. Run `pytest tests/ -v` and verify all tests pass

---

## Success criteria

- `pytest tests/ -v` passes with zero failures (no database, no network)
- `python -c "from api.main import app"` succeeds (all imports resolve)
- `docker compose up -d && python scripts/bootstrap_sp500.py --ticker AAPL` completes without error
- `uvicorn api.main:app --reload` starts and `/health` returns `{"status": "ok"}`
