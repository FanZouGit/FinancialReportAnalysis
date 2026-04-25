# EDGAR Financial Intelligence Platform

An end-to-end AI agent pipeline for downloading, parsing, storing, and analysing
SEC EDGAR 10-K and 10-Q XBRL filings — enabling cross-company, cross-sector, and
cross-period financial analysis at scale.

---

## Table of contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Project structure](#project-structure)
4. [Technology stack](#technology-stack)
5. [Development plan](#development-plan)
   - [Phase 1 — S&P 500 ingestion](#phase-1--sp500-ingestion)
   - [Phase 2 — XBRL normalisation](#phase-2--xbrl-normalisation)
   - [Phase 3 — Metrics and ratios](#phase-3--metrics-and-ratios)
   - [Phase 4 — Peer comparison](#phase-4--peer-comparison)
   - [Phase 5 — Full universe](#phase-5--full-universe)
   - [Phase 6 — LLM query agent](#phase-6--llm-query-agent)
   - [Phase 7 — Semantic search](#phase-7--semantic-search)
6. [Quickstart](#quickstart)
7. [API reference](#api-reference)
8. [Configuration](#configuration)
9. [Testing](#testing)

---

## Overview

SEC EDGAR publishes structured XBRL financial data for every public company
filing a 10-K or 10-Q. This platform ingests that data, normalises it into a
queryable data store, and exposes it through a REST API and an LLM-powered
natural language agent.

**What you can do with it:**

- Retrieve any company's revenue, margins, cash flow, and balance sheet across
  every reported quarter and annual period
- Compare a company's profitability ratios against every peer in its SIC
  industry group, ranked by percentile
- Run sector-level benchmark queries (median net margin for Technology in Q3-2023)
- Detect statistical anomalies in a company's own financial history
- Ask natural language questions answered by an LLM agent backed by real data
- Search what companies actually said in their MD&A sections about any topic

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Data source                              │
│              SEC EDGAR — 10-K and 10-Q XBRL filings            │
└─────────────────────────────┬───────────────────────────────────┘
                              │
┌─────────────────────────────▼───────────────────────────────────┐
│                      Ingestion agent                            │
│   EDGAR full-text search · XBRL parser · deduplication         │
│   Rate limiting · Incremental sync · Filing metadata           │
└──────────┬──────────────────────────────────────────────────────┘
           │
┌──────────▼──────────────────────────────────────────────────────┐
│               Extraction & normalisation layer                  │
│   XBRL taxonomy mapping · US-GAAP normalisation                │
│   Fact validation · SIC tagging · Period alignment             │
└──────────┬──────────────────────────────────────────────────────┘
           │
┌──────────▼──────────────────────────────────────────────────────┐
│                         Data store                              │
│  ┌───────────────┐  ┌────────────────┐  ┌──────────────────┐  │
│  │ Relational DB │  │ Time-series DB │  │   Vector store   │  │
│  │  PostgreSQL   │  │  TimescaleDB   │  │    pgvector      │  │
│  │  Facts/Entities│  │ Cross-period   │  │ Semantic search  │  │
│  └───────────────┘  └────────────────┘  └──────────────────┘  │
└──────────┬──────────────────────────────────────────────────────┘
           │
┌──────────▼──────────────────────────────────────────────────────┐
│                    Analytics & query layer                      │
│   Cross-company · Cross-sector · Cross-period comparisons      │
│   Ratio analysis · Trend detection · Anomaly flagging          │
└──────────┬──────────────────────────────────────────────────────┘
           │
┌──────────▼──────────────────────────────────────────────────────┐
│                         Interfaces                              │
│        REST API · Python SDK · Dashboards · LLM agent          │
└─────────────────────────────────────────────────────────────────┘
```

---

## Project structure

```
edgar_platform/
├── config/
│   ├── settings.py          # Pydantic BaseSettings — env/config
│   └── taxonomy.py          # XBRL concept alias map, SIC→sector
│
├── ingestion/
│   ├── edgar_client.py      # Async SEC EDGAR API client
│   ├── downloader.py        # Filing sync state machine
│   └── scheduler.py         # ARQ job queue workers
│
├── extraction/
│   └── normaliser.py        # XBRL→canonical facts, metrics, ratios
│
├── database/
│   ├── models.py            # SQLAlchemy 2.0 ORM schema
│   └── engine.py            # Async engine, session factory, init_db
│
├── analytics/
│   └── repository.py        # All query patterns (peer, sector, trend)
│
├── agent/
│   ├── query_agent.py       # PydanticAI LLM agent + typed tools
│   └── semantic_search.py   # MD&A chunking, embedding, vector search
│
├── api/
│   └── main.py              # FastAPI REST application
│
├── scripts/
│   ├── bootstrap_sp500.py   # Phase 1 end-to-end pipeline runner
│   └── expand_full_universe.py  # Phase 5 full SEC filer expansion
│
└── tests/
    └── test_pipeline.py     # Unit tests for all pipeline stages
```

---

## Technology stack

| Layer | Technology | Reason |
|-------|-----------|--------|
| HTTP client | `httpx` + `tenacity` | Async, retry, SEC rate limiting |
| Job queue | `arq` + Redis | Async workers, rate-safe parallelism |
| Database | PostgreSQL + TimescaleDB | Relational + time-series in one engine |
| Vector search | `pgvector` | Semantic search without a separate service |
| ORM | SQLAlchemy 2.0 async | Type-safe, modern async support |
| Normalisation | Python + pandas | XBRL fact resolution and period alignment |
| LLM agent | PydanticAI | Type-safe tools, dependency injection, testable |
| Embeddings | `sentence-transformers` | Local, no API key required |
| API | FastAPI + Pydantic | Fast, typed, auto-documented |
| Infrastructure | Docker Compose | One-command local setup |

---

## Development plan

The platform is built in seven incremental phases. Each phase produces a
working, testable system before the next begins.

---

### Phase 1 — S&P 500 ingestion

**Goal:** prove the full pipeline end to end on a bounded, well-known dataset.

**What gets built:**

- `EdgarClient` — async HTTP wrapper around SEC EDGAR data APIs with token-bucket
  rate limiting (8 req/s), exponential backoff retry, and the mandatory
  `User-Agent` header identifying the application.
- `FilingDownloader` — per-company sync that upserts the company record, discovers
  filings not yet in the database by accession number, fetches all XBRL facts
  for the company in a single `companyfacts` API call, and persists raw facts
  mapped to their respective filing rows.
- Filing state machine: `pending → downloading → downloaded → extracting →
  extracted → computing → done | error`. Every transition is persisted so
  reruns are idempotent.
- `bootstrap_sp500.py` — resolves S&P 500 tickers to CIKs via the SEC ticker
  file, runs the full pipeline synchronously with a Rich progress display,
  and prints ready-to-use API `curl` commands on completion.

**Key design decisions:**

- Use the `companyfacts` endpoint (one call per company, all filings) rather
  than per-filing fetches. This reduces SEC API calls by 20–100× per company.
- Store raw facts exactly as reported before any normalisation, preserving
  auditability and allowing re-normalisation without re-downloading.
- Accession number deduplication means the bootstrap script is safe to re-run
  at any time.

**Completion criteria:** S&P 500 companies ingested, raw facts in database,
API returns data for `/companies/AAPL/metrics`.

---

### Phase 2 — XBRL normalisation

**Goal:** resolve the inconsistency in how companies tag financial line items
and produce a single canonical value per concept per period.

**What gets built:**

- `taxonomy.py` — maps ~30 XBRL tag variants to 10 canonical concept names
  covering the most important income statement, balance sheet, and cash flow
  items: `revenue`, `gross_profit`, `operating_income`, `net_income`,
  `eps_basic`, `eps_diluted`, `total_assets`, `total_liabilities`,
  `total_equity`, `operating_cash_flow`, `capex`.
- `_resolve_best_fact()` — selects the single best raw fact when multiple
  observations exist for the same concept and period. Resolution rules:
  prefer USD unit over others; require exact `period_end` match; for
  duration facts, pick the observation whose duration is closest to the
  expected length (3 months for 10-Q, 12 months for 10-K); for balance
  sheet items, prefer `instant` period type.
- `MetricsComputer` — iterates extracted filings, resolves each canonical
  concept, derives `free_cash_flow = operating_cash_flow − |capex|`, and
  writes a single `Metric` row per filing.

**Key design decisions:**

- NULL is used for any concept not reported in a period — never 0. This
  prevents false ratios from masking missing data.
- The concept alias table is the single source of truth. Adding a new alias
  requires only one line change in `taxonomy.py`.
- Period alignment is done at resolution time, not at ingestion time, keeping
  raw facts immutable.

**Completion criteria:** `metrics` table populated for all ingested filings;
`/companies/AAPL/metrics` returns typed revenue, net income, and margin values.

---

### Phase 3 — Metrics and ratios tables

**Goal:** pre-compute all financial ratios so analytical queries are fast reads
rather than on-the-fly calculations.

**What gets built:**

- `metrics` table — one row per company per fiscal period, with all 12 financial
  line items as nullable float columns. Unique constraint on
  `(cik, period_label, form_type)` ensures idempotent recomputation.
- `ratios` table — one-to-one with `metrics`, containing 11 pre-computed ratios:
  `gross_margin`, `operating_margin`, `net_margin`, `roe`, `roa`,
  `debt_to_equity`, `debt_to_assets`, `equity_multiplier`, `asset_turnover`,
  `fcf_margin`, `fcf_conversion`. All divisions are null-safe — a zero or null
  denominator produces NULL rather than infinity or error.
- `peer_groups` table — many-to-many mapping of companies to peer group keys
  built from SIC code, GICS sector, and industry group. Populated during
  ingestion.
- `_compute_ratios()` — pure function from a `Metric` to a `Ratio`, fully
  unit-tested with edge cases for zero denominators and null inputs.

**Key design decisions:**

- Pre-computation at write time rather than query time. A single SQL join
  is faster than recomputing ratios across thousands of rows on read.
- Separate table for ratios rather than columns on `metrics` — keeps the
  schema extensible and allows ratio recomputation without touching the
  source metrics.

**Completion criteria:** ratios table populated; all 11 ratio columns available
in the API response; `/companies/AAPL/metrics` returns margin percentages.

---

### Phase 4 — Peer comparison queries

**Goal:** enable ranking a company against its industry peers and querying
sector-level aggregate statistics.

**What gets built:**

- `compare_peers()` — given a ticker and ratio name, identifies the company's
  SIC-based peer group, fetches all peers' values for that ratio in a given
  period, returns a ranked list with percentile scores. Defaults to the
  company's most recent available period.
- `sector_benchmark()` — uses PostgreSQL `PERCENTILE_CONT` aggregate to return
  count, mean, median, 25th percentile, 75th percentile, min, and max for any
  ratio across all companies in a sector for a given period.
- `cross_company()` — side-by-side snapshot of multiple named companies for a
  given period, for direct head-to-head comparisons.
- `trend_analysis()` — computes QoQ and YoY growth rates for any metric across
  N periods, handling the case where the comparison period is missing.
- `detect_anomalies()` — flags periods where a metric deviates more than a
  configurable number of standard deviations from the company's own trailing
  history.

**Key design decisions:**

- Peer groups are stored at ingestion time, not computed at query time.
  This allows custom peer group overrides (e.g. grouping by market cap tier
  rather than SIC code) without changing the query layer.
- All analytical functions accept `form_type` as a parameter so annual (10-K)
  and quarterly (10-Q) data are never mixed inadvertently.

**Completion criteria:** `/companies/AAPL/peers?ratio=net_margin` returns
Apple ranked against all other software companies; `/sectors/Technology/benchmark`
returns quartile statistics.

---

### Phase 5 — Full universe expansion

**Goal:** scale from ~500 S&P 500 companies to all ~10,000 active SEC filers.

**What gets built:**

- `scheduler.py` — ARQ-based async job queue with three job types:
  `sync_company_job`, `compute_metrics_job`, `embed_mda_job`. Worker settings
  cap concurrency at `MAX_WORKERS` (default 4) to stay within SEC rate limits.
- `enqueue_all_filers()` — fetches the complete SEC ticker file (~10K entries),
  enqueues one `sync_company_job` per CIK, and logs progress every 500 jobs.
- `expand_full_universe.py` — interactive script that confirms intent before
  enqueueing, with instructions for starting the ARQ worker.
- `is_sp500` flag on the `Company` model allows queries to be scoped to S&P 500
  only or the full universe.

**Key design decisions:**

- Job queue rather than sequential processing. At 8 req/s with 4 workers,
  10,000 companies take approximately 2–3 hours depending on filing volume.
- Each job is idempotent — re-enqueueing a CIK that was already processed
  only fetches and stores new filings since the last run.
- Incremental sync: subsequent runs only process filings with `status = pending`.

**Completion criteria:** all active SEC filers ingested; peer comparison queries
work across the full universe; sector benchmarks include non-S&P companies.

---

### Phase 6 — LLM query agent

**Goal:** allow natural language financial questions answered by an LLM agent
that calls typed tools backed by real data — no hallucinated numbers.

**What gets built:**

- `financial_agent` — PydanticAI agent configured with either Claude or GPT-4o,
  a financial analyst system prompt, and six tools wired to the analytics
  repository.
- Six typed tools:
  - `get_company_metrics` — time series for a ticker
  - `compare_peers` — industry peer ranking
  - `sector_benchmark` — sector aggregate statistics
  - `cross_company_compare` — side-by-side multi-company comparison
  - `trend_analysis` — growth rates over N periods
  - `detect_anomalies` — statistical outlier detection
  - `search_mda` — semantic search over MD&A text (Phase 7)
- `FinancialAnswer` — Pydantic result schema with `summary`, `data_points`,
  `caveats`, and `sources` fields. The LLM cannot return a free-form string;
  every response is validated against this schema.
- `AgentDeps` — dataclass injecting the database session, analytics repository,
  and semantic search engine. Enables testing with mock dependencies.
- `/ask` API endpoint — accepts a natural language question, runs the agent,
  returns the structured answer.

**Key design decisions:**

- PydanticAI's dependency injection pattern means the agent can be tested
  with mock repositories without touching the database.
- Monetary values are converted to millions in the tool output to fit more
  data in the LLM context window. Ratios are expressed as percentages.
- The agent is model-agnostic — switch between Claude and GPT-4o by changing
  one environment variable.

**Example questions the agent can answer:**

- "Compare Apple and Microsoft's net margins for Q3 2023"
- "Which software companies had the highest ROE last quarter?"
- "Show me NVDA's revenue trend over the past two years"
- "What is the median gross margin for the Healthcare sector in FY 2023?"
- "Are there any S&P 500 companies with unusually low FCF conversion?"

**Completion criteria:** `/ask` endpoint returns structured answers for all
example questions above, with data sourced exclusively from the database.

---

### Phase 7 — Semantic search over MD&A

**Goal:** enable natural language search over what companies actually wrote in
the Management Discussion and Analysis sections of their filings.

**What gets built:**

- `MdaEmbedder` — downloads the primary filing HTML from SEC EDGAR, extracts
  the MD&A section using section header patterns (Item 7 for 10-K, Item 2 for
  10-Q), chunks the text into ~512-token segments with 64-token overlap at
  sentence boundaries, embeds each chunk, and stores the vectors in pgvector.
- `_chunk_text()` — splits text into overlapping chunks, breaking at sentence
  boundaries where possible to preserve semantic coherence.
- `_extract_mda_from_html()` — strips HTML tags and locates MD&A boundaries
  using regex patterns for SEC section headers.
- `SemanticSearch.search()` — embeds a query string and retrieves the top-K
  most similar chunks by cosine distance, with optional filters on company,
  sector, and period.
- `mda_chunks` and `chunk_embeddings` tables — store chunked text and pgvector
  embeddings respectively. An IVFFlat index on the embedding column enables
  approximate nearest-neighbour search at scale.
- `embed_mda_job` — ARQ job for background embedding of new filings.
- `search_mda` tool in the LLM agent — the agent can call semantic search as
  part of answering questions about disclosed risks, strategy, or guidance.
- `/search` API endpoint — direct semantic search without the LLM layer.

**Key design decisions:**

- Local `sentence-transformers` (all-MiniLM-L6-v2, 384 dimensions) is the
  default embedding model — no API key or cost. OpenAI embeddings can be
  swapped in by changing one environment variable.
- Chunking happens at sentence boundaries to avoid splitting mid-thought,
  which degrades retrieval quality.
- The vector index uses IVFFlat with 100 lists, suitable for up to ~1M chunks.
  Switch to HNSW for larger collections.

**Example queries:**

- "supply chain disruption risk 2023"
- "AI investment and capital allocation"
- "margin pressure from input costs"
- "forward guidance and outlook"
- "litigation and regulatory risk"

**Completion criteria:** `/search` returns ranked MD&A passages for the above
queries; the LLM agent uses `search_mda` when questions reference company
disclosures or qualitative topics.

---

## Quickstart

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Copy and configure environment
cp .env.example .env
# Edit .env — at minimum set SEC_USER_AGENT with your contact email

# 3. Start infrastructure (Postgres + TimescaleDB + Redis)
docker compose up -d

# 4. Initialise database
python -c "import asyncio; from database.engine import init_db; asyncio.run(init_db())"

# 5. Bootstrap S&P 500 (Phase 1 — runs synchronously, ~30-60 min)
python scripts/bootstrap_sp500.py

# Or test with a single company first
python scripts/bootstrap_sp500.py --ticker AAPL

# 6. Start the API
uvicorn api.main:app --reload
```

For full universe expansion (Phase 5):

```bash
# Start the job queue worker (separate terminal)
arq ingestion.scheduler.WorkerSettings

# Enqueue all ~10K SEC filers
python scripts/expand_full_universe.py
```

---

## API reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/companies/{ticker}/metrics` | Financial metrics time series |
| GET | `/companies/{ticker}/peers` | Industry peer ranking |
| GET | `/companies/{ticker}/trends` | Period-over-period growth rates |
| GET | `/companies/{ticker}/anomalies` | Statistical outlier detection |
| GET | `/sectors/{sector}/benchmark` | Sector aggregate statistics |
| POST | `/compare` | Cross-company side-by-side comparison |
| POST | `/search` | Semantic search over MD&A text |
| POST | `/ask` | Natural language LLM agent query |
| GET | `/health` | Health check |

Example requests:

```bash
# Quarterly metrics for Apple
curl "http://localhost:8000/companies/AAPL/metrics?form_type=10-Q&limit=8"

# Apple's net margin rank among software peers in Q3-2023
curl "http://localhost:8000/companies/AAPL/peers?ratio=net_margin&period_label=Q3-2023"

# Technology sector net margin benchmarks
curl "http://localhost:8000/sectors/Technology/benchmark?metric=net_margin&period_label=Q3-2023"

# Compare AAPL, MSFT, GOOGL side by side
curl -X POST "http://localhost:8000/compare" \
  -H "Content-Type: application/json" \
  -d '{"tickers": ["AAPL","MSFT","GOOGL"], "period_label": "Q3-2023"}'

# Semantic search over MD&A
curl -X POST "http://localhost:8000/search" \
  -H "Content-Type: application/json" \
  -d '{"query": "AI investment and capital allocation", "top_k": 5}'

# Natural language agent query
curl -X POST "http://localhost:8000/ask" \
  -H "Content-Type: application/json" \
  -d '{"question": "Which semiconductor companies had the best FCF margins in 2023?"}'
```

---

## Configuration

All settings are loaded from `.env` (see `.env.example`).

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql+asyncpg://...` | Async Postgres connection string |
| `REDIS_URL` | `redis://localhost:6379` | Redis for job queue |
| `SEC_USER_AGENT` | — | **Required.** `AppName/1.0 email@domain.com` |
| `SEC_REQUESTS_PER_SECOND` | `8` | Stay at or below 10 (SEC limit) |
| `OPENAI_API_KEY` | — | Required for GPT-4o agent or OpenAI embeddings |
| `ANTHROPIC_API_KEY` | — | Required for Claude agent |
| `EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | Local model; or `text-embedding-3-small` |
| `SP500_ONLY` | `true` | Set `false` for full universe |
| `MAX_WORKERS` | `4` | Parallel ingestion workers |

---

## Testing

```bash
# Run all tests
pytest tests/ -v

# Run specific test class
pytest tests/ -v -k "TestTaxonomy"
pytest tests/ -v -k "TestRatioComputation"
pytest tests/ -v -k "TestFactResolution"

# Run with coverage
pytest tests/ --cov=. --cov-report=term-missing
```

Test coverage includes: taxonomy alias resolution, date and period helpers,
XBRL fact resolution logic (unit preference, period alignment, duration
matching), ratio computation edge cases (null inputs, zero denominators),
EDGAR client URL construction, and text chunking behaviour.

---

## Build phases summary

| Phase | Description | Key files |
|-------|-------------|-----------|
| 1 | S&P 500 ingestion | `edgar_client.py`, `downloader.py`, `bootstrap_sp500.py` |
| 2 | XBRL normalisation | `taxonomy.py`, `normaliser.py` |
| 3 | Metrics and ratios | `models.py`, `normaliser.py` |
| 4 | Peer comparison | `analytics/repository.py` |
| 5 | Full universe | `scheduler.py`, `expand_full_universe.py` |
| 6 | LLM query agent | `agent/query_agent.py` |
| 7 | Semantic search | `agent/semantic_search.py` |
