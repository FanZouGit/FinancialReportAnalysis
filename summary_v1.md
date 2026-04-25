# EDGAR Financial Intelligence Platform — v1 Summary

## What was done

### Structure

Established proper sub-package layout matching the README architecture doc. All source files
were flat at the project root and could not be imported. Moved every file into its correct
package and deleted the originals.

```
FinancialReportAnalysis/
├── config/               settings.py, taxonomy.py
├── database/             engine.py, models.py
├── ingestion/            edgar_client.py, downloader.py
├── extraction/           normaliser.py
├── analytics/            repository.py
├── agent/                query_agent.py, semantic_search.py
├── api/                  main.py
├── scripts/              bootstrap_sp500.py
├── tests/                conftest.py, test_pipeline.py
├── .env.example
├── pytest.ini
└── requirements.txt
```

---

### New files created

| File | Purpose |
|------|---------|
| `config/settings.py` | Pydantic `BaseSettings` loaded from `.env`. Defines all 10 config variables with safe defaults. |
| `database/engine.py` | Async SQLAlchemy engine, `AsyncSessionLocal` session factory, and `init_db()` (creates tables + enables pgvector extension). |
| `.env.example` | Annotated config template. Copy to `.env` and fill in the two required variables. |
| `pytest.ini` | Sets `asyncio_mode = auto` and `testpaths = tests`. |
| `tests/conftest.py` | Shared `make_raw_fact` and `make_metric` fixtures used across test classes. |

---

### 6 bugs fixed

**Bug 1 — `bootstrap_sp500.py`: `Optional` used before import**
`Optional[str]` appeared in the function signature on line 42, but `from typing import Optional`
was at line 172. Moved the import to the top of the file.

**Bug 2 — `analytics/repository.py`: SQL injection in `sector_benchmark()`**
The `metric` parameter was directly interpolated into a raw SQL string via `.format(metric=metric)`.
An `hasattr` check existed but was not sufficient. Fixed by introducing a `VALID_RATIO_FIELDS`
frozenset whitelist of the 11 known ratio column names. Any input not in the set raises
`ValueError` before it reaches the SQL string.

```python
VALID_RATIO_FIELDS: frozenset[str] = frozenset({
    "gross_margin", "operating_margin", "net_margin",
    "roe", "roa", "debt_to_equity", "debt_to_assets",
    "equity_multiplier", "asset_turnover", "fcf_margin", "fcf_conversion",
})
```

**Bug 3 — `ingestion/downloader.py`: deprecated `datetime.utcnow()`**
`datetime.utcnow()` is deprecated in Python 3.12+. Replaced with `datetime.now(timezone.utc)`.

**Bug 4 — `ingestion/edgar_client.py`: hardcoded `Host: data.sec.gov` header**
The shared `httpx.AsyncClient` had `"Host": "data.sec.gov"` in its default headers. Some
methods call `www.sec.gov` and `efts.sec.gov`, which received the wrong `Host` header.
Removed the header entirely — httpx sets it correctly per-request.

**Bug 5 — `agent/query_agent.py`: stale model name**
`"anthropic:claude-sonnet-4-5"` updated to `"anthropic:claude-sonnet-4-6"`.

**Bug 6 — `ingestion/edgar_client.py`: semaphore created outside the event loop**
The module-level `_get_semaphore()` created an `asyncio.Semaphore` on first call, which
could happen before the event loop was running. Moved the semaphore to a per-client instance
variable, initialised in `__aenter__` (inside the running event loop).

---

### Bonus bug found by tests

`us-gaap:LiabilitiesAndStockholdersEquity` appeared as a fallback alias in both
`total_liabilities` and `total_equity` in `config/taxonomy.py`. The reverse lookup dict
`TAG_TO_CANONICAL` can only map one tag to one canonical name, so the second assignment
silently won and `total_liabilities` lost its fallback. The tag represents a balance sheet
total, not either value in isolation, so it was removed from both alias lists.

---

### Quality improvements

**`_build_peer_groups` consolidated into `FilingDownloader`**
Previously lived as a standalone async function in `scripts/bootstrap_sp500.py`. The ARQ
job queue worker (Phase 5) would have needed to duplicate it. Moved into
`FilingDownloader._build_peer_groups()` and called at the end of `sync_company()`, so both
the bootstrap script and the job worker use the same code path.

**`agent/query_agent.py` — lazy agent initialisation**
The `Agent(...)` was constructed at module level, forcing pydantic-ai to validate the API
key at import time. This broke cold imports without a `.env` file (e.g. during tests or CI).
Refactored to a `_get_agent()` factory that builds the agent on first call to `ask()`.

**`requirements.txt` updates**
- `pydantic-ai==0.0.*` → `pydantic-ai>=0.0.14,<0.1.0` (pins to the stable `Agent`/`RunContext` API)
- `pydantic-settings==2.*` added (required for `BaseSettings` in pydantic v2 — was missing)

---

### Tests: 43/43 passing

No database or network connection required.

New test coverage added:

| Class | Tests added |
|---|---|
| `TestTaxonomy` | `free_cash_flow` has no XBRL aliases (derived, not reported) |
| `TestDateHelpers` | Q2 and Q4 fiscal period derivation |
| `TestFactResolution` | `_pick_by_value` returns `None` when all values are `None` |
| `TestRatioComputation` | `roa`, `asset_turnover`, `equity_multiplier`, `fcf_margin`, `fcf_conversion` |
| `TestTextChunking` | Overlap produces shared content across adjacent chunks |
| `TestEdgarClient` | `_pad_cik` with already-padded input and single-digit CIK |
| `TestSectorBenchmarkSecurity` | Whitelist rejects unknown/malicious metric names |

---

## Quickstart

```bash
# 1. Copy and configure environment
cp .env.example .env
# Edit .env — set SEC_USER_AGENT and ANTHROPIC_API_KEY

# 2. Install dependencies
pip install -r requirements.txt

# 3. Start infrastructure
docker compose up -d

# 4. Run tests (no DB needed)
pytest tests/ -v

# 5. Bootstrap a single company
python scripts/bootstrap_sp500.py --ticker AAPL

# 6. Start the API
uvicorn api.main:app --reload

# 7. Verify
curl http://localhost:8000/health
curl "http://localhost:8000/companies/AAPL/metrics?form_type=10-Q&limit=4"
```
