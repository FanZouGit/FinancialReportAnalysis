"""
ingestion/edgar_client.py

Async HTTP client for SEC EDGAR APIs.

Endpoints used:
  - /submissions/CIK{cik}.json          → company metadata + filing history
  - /api/xbrl/companyfacts/CIK{cik}.json → all XBRL facts for a company
  - /api/xbrl/companyconcept/...        → single concept across all filings
  - https://efts.sec.gov/LATEST/search-index?q=... → full-text filing search

SEC fair-use rules:
  - Max 10 requests/second
  - Must send User-Agent: AppName/Version email@domain.com
  - Do not cache-bust or use aggressive retry on 5xx
"""

import asyncio
import logging
from typing import Any, Dict, Optional

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from config.settings import settings

log = logging.getLogger(__name__)


class EdgarClient:
    """
    Thin async wrapper around the SEC EDGAR data APIs.

    Usage:
        async with EdgarClient() as client:
            facts = await client.get_company_facts("0000320193")   # Apple
    """

    BASE = settings.sec_base_url          # https://data.sec.gov
    EFTS = "https://efts.sec.gov"

    def __init__(self) -> None:
        self._client: Optional[httpx.AsyncClient] = None
        self._semaphore: Optional[asyncio.Semaphore] = None

    async def __aenter__(self) -> "EdgarClient":
        # Semaphore initialised here so it belongs to the running event loop.
        self._semaphore = asyncio.Semaphore(settings.sec_requests_per_second)
        self._client = httpx.AsyncClient(
            headers={
                "User-Agent": settings.sec_user_agent,
                "Accept-Encoding": "gzip, deflate",
                # Host header intentionally omitted — httpx sets it correctly
                # per-request, which matters because some calls go to www.sec.gov
                # or efts.sec.gov rather than data.sec.gov.
            },
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._client:
            await self._client.aclose()

    # ------------------------------------------------------------------ #
    # Internal helpers                                                      #
    # ------------------------------------------------------------------ #

    @retry(
        retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.TransportError)),
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        reraise=True,
    )
    async def _get(self, url: str, **kwargs: Any) -> Dict[str, Any]:
        """Rate-limited GET with retry."""
        assert self._semaphore is not None, "EdgarClient must be used as async context manager"
        async with self._semaphore:
            assert self._client is not None
            resp = await self._client.get(url, **kwargs)
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", "10"))
                log.warning("Rate limited by SEC — sleeping %ds", retry_after)
                await asyncio.sleep(retry_after)
                resp = await self._client.get(url, **kwargs)
            resp.raise_for_status()
            # Polite pause between requests
            await asyncio.sleep(1.0 / settings.sec_requests_per_second)
            return resp.json()

    @staticmethod
    def _pad_cik(cik: str) -> str:
        """EDGAR CIKs are zero-padded to 10 digits in URLs."""
        return cik.zfill(10)

    # ------------------------------------------------------------------ #
    # Public API methods                                                    #
    # ------------------------------------------------------------------ #

    async def get_submissions(self, cik: str) -> Dict[str, Any]:
        """
        Company metadata + complete filing history.

        Returns dict with keys: cik, name, sic, tickers, exchanges,
        filings.recent (last 1000 filings), filings.files (older batches)
        """
        cik_padded = self._pad_cik(cik)
        url = f"{self.BASE}/submissions/CIK{cik_padded}.json"
        log.debug("Fetching submissions for CIK %s", cik)
        return await self._get(url)

    async def get_company_facts(self, cik: str) -> Dict[str, Any]:
        """
        All XBRL facts ever reported by the company, across all filings.

        Structure: { entityName, cik, facts: { us-gaap: { Revenues: { units: { USD: [...] } } } } }
        Each leaf is a list of { accn, end, val, form, filed, ... }
        """
        cik_padded = self._pad_cik(cik)
        url = f"{self.BASE}/api/xbrl/companyfacts/CIK{cik_padded}.json"
        log.debug("Fetching company facts for CIK %s", cik)
        return await self._get(url)

    async def get_company_concept(
        self, cik: str, taxonomy: str, concept: str
    ) -> Dict[str, Any]:
        """
        Single concept across all filings.

        taxonomy: us-gaap | dei | ifrs-full
        concept:  Revenues | NetIncomeLoss | etc (no prefix)
        """
        cik_padded = self._pad_cik(cik)
        url = f"{self.BASE}/api/xbrl/companyconcept/CIK{cik_padded}/{taxonomy}/{concept}.json"
        return await self._get(url)

    async def get_all_company_tickers(self) -> Dict[str, Any]:
        """
        Full list of all SEC filers with ticker and CIK.
        Returns dict keyed by CIK string: { cik_str, ticker, title, exchange }
        Updated daily by the SEC.
        """
        url = "https://www.sec.gov/files/company_tickers_exchange.json"
        client = httpx.AsyncClient(headers={"User-Agent": settings.sec_user_agent})
        async with client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.json()

    async def get_sp500_ciks(self) -> list[str]:
        """
        Fetch current S&P 500 constituents from Wikipedia and resolve CIKs
        via the SEC ticker lookup.
        Falls back to a curated list if Wikipedia is unreachable.
        """
        # Pull ticker→CIK map from SEC
        ticker_map_raw = await self.get_all_company_tickers()
        # ticker_map_raw["data"] is a list of [cik_int, name, ticker, exchange]
        ticker_to_cik: Dict[str, str] = {}
        for row in ticker_map_raw.get("data", []):
            cik_int, _name, ticker, _exchange = row
            ticker_to_cik[ticker.upper()] = str(cik_int)

        # Try to scrape S&P 500 tickers from Wikipedia
        sp500_tickers = await self._fetch_sp500_tickers()

        ciks = []
        for ticker in sp500_tickers:
            cik = ticker_to_cik.get(ticker.upper())
            if cik:
                ciks.append(cik)
            else:
                log.warning("No CIK found for S&P 500 ticker %s", ticker)

        log.info("Resolved %d / %d S&P 500 CIKs", len(ciks), len(sp500_tickers))
        return ciks

    async def _fetch_sp500_tickers(self) -> list[str]:
        """Scrape S&P 500 tickers from Wikipedia."""
        try:
            client = httpx.AsyncClient(
                headers={"User-Agent": settings.sec_user_agent}, timeout=20.0
            )
            async with client:
                resp = await client.get(
                    "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
                )
                resp.raise_for_status()
                import re
                tickers = re.findall(r'<td><a[^>]*>([A-Z]{1,5})</a></td>', resp.text)
                if tickers:
                    return list(dict.fromkeys(tickers))  # dedupe, preserve order
        except Exception as exc:
            log.warning("Wikipedia scrape failed (%s) — using fallback ticker list", exc)

        # Fallback: representative sample of S&P 500 tickers
        return [
            "AAPL","MSFT","GOOGL","AMZN","NVDA","META","TSLA","BRK.B","JPM","JNJ",
            "UNH","XOM","V","PG","MA","HD","CVX","MRK","LLY","ABBV","PEP","KO",
            "AVGO","COST","MCD","TMO","CSCO","ACN","ABT","WMT","BAC","DHR","CRM",
            "LIN","ADBE","NEE","TXN","PM","AMGN","RTX","QCOM","HON","UNP","IBM",
            "SPGI","GS","BLK","INTU","CAT","SYK","MDLZ","ISRG","ADP","GILD","ELV",
        ]
