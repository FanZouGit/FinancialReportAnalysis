"""
ingestion/downloader.py

Orchestrates ingestion for one company:
  1. Fetch submissions → upsert Company record
  2. Filter 10-K / 10-Q filings not yet in the DB
  3. Fetch company_facts JSON (all XBRL in one call)
  4. Persist RawFacts for each relevant filing
  5. Build peer groups from SIC / sector
  6. Update filing status

Designed to be called from the scheduler / job queue (arq) OR
directly from the bootstrap script.
"""

import logging
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from config.taxonomy import SIC_OVERRIDES, SIC_TO_SECTOR, TAG_TO_CANONICAL
from database.models import Company, Filing, PeerGroup, RawFact
from ingestion.edgar_client import EdgarClient

log = logging.getLogger(__name__)

SUPPORTED_FORMS = {"10-K", "10-Q"}


class FilingDownloader:
    def __init__(self, session: AsyncSession, client: EdgarClient) -> None:
        self.session = session
        self.client = client

    # ------------------------------------------------------------------ #
    # Main entry point                                                      #
    # ------------------------------------------------------------------ #

    async def sync_company(self, cik: str) -> Dict[str, int]:
        """
        Full sync for one company.  Returns counts of what happened.
        """
        stats = {"new_filings": 0, "new_facts": 0, "skipped": 0}

        # Step 1: fetch submissions and upsert company
        try:
            subs = await self.client.get_submissions(cik)
        except Exception as exc:
            log.error("Failed to fetch submissions for CIK %s: %s", cik, exc)
            return stats

        company = await self._upsert_company(cik, subs)

        # Step 2: discover filings not yet in DB
        new_filings = await self._discover_new_filings(cik, subs)
        if not new_filings:
            log.info("CIK %s — no new filings found", cik)
            await self._build_peer_groups(cik, company)
            return stats

        log.info("CIK %s (%s) — %d new filings to process", cik, company.name, len(new_filings))

        # Step 3: fetch all XBRL facts in one call
        try:
            facts_payload = await self.client.get_company_facts(cik)
        except Exception as exc:
            log.error("Failed to fetch company facts for CIK %s: %s", cik, exc)
            for f in new_filings:
                f.status = "error"
                f.error_message = str(exc)
            await self.session.flush()
            return stats

        # Step 4: assign facts to filings
        new_fact_count = await self._persist_facts(new_filings, facts_payload)
        stats["new_filings"] = len(new_filings)
        stats["new_facts"] = new_fact_count

        # Step 5: build peer groups
        await self._build_peer_groups(cik, company)

        return stats

    # ------------------------------------------------------------------ #
    # Company upsert                                                        #
    # ------------------------------------------------------------------ #

    async def _upsert_company(self, cik: str, subs: Dict[str, Any]) -> Company:
        sic = str(subs.get("sic", "") or "")
        sector = SIC_OVERRIDES.get(sic) or SIC_TO_SECTOR.get(sic[:1] + "0" * (len(sic)-1))

        tickers = subs.get("tickers", [])
        exchanges = subs.get("exchanges", [])
        ticker = tickers[0] if tickers else None
        exchange = exchanges[0] if exchanges else None

        stmt = pg_insert(Company).values(
            cik=str(int(cik)),
            ticker=ticker,
            name=subs.get("name", "Unknown"),
            sic_code=sic or None,
            sic_description=subs.get("sicDescription"),
            sector=sector,
            industry_group=SIC_OVERRIDES.get(sic),
            exchange=exchange,
            state_of_inc=subs.get("stateOfIncorporation"),
        ).on_conflict_do_update(
            index_elements=["cik"],
            set_={
                "ticker": ticker,
                "name": subs.get("name", "Unknown"),
                "sic_code": sic or None,
                "sector": sector,
                "exchange": exchange,
                "updated_at": datetime.now(timezone.utc),
            },
        ).returning(Company)

        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.scalar_one()

    # ------------------------------------------------------------------ #
    # Filing discovery                                                      #
    # ------------------------------------------------------------------ #

    async def _discover_new_filings(
        self, cik: str, subs: Dict[str, Any]
    ) -> List[Filing]:
        filings_data = subs.get("filings", {})
        recent = filings_data.get("recent", {})

        forms = recent.get("form", [])
        accessions = recent.get("accessionNumber", [])
        period_ends = recent.get("reportDate", [])
        filed_dates = recent.get("filingDate", [])

        candidates = [
            {
                "accession_no": acc.replace("-", ""),
                "form_type": form,
                "period_end": period,
                "filed_date": filed,
            }
            for acc, form, period, filed in zip(accessions, forms, period_ends, filed_dates)
            if form in SUPPORTED_FORMS and period
        ]

        if not candidates:
            return []

        existing_accessions = set(
            row[0]
            for row in (
                await self.session.execute(
                    select(Filing.accession_no).where(Filing.cik == str(int(cik)))
                )
            ).all()
        )

        new_filings: List[Filing] = []
        for c in candidates:
            if c["accession_no"] in existing_accessions:
                continue

            period_date = _parse_date(c["period_end"])
            filed_date = _parse_date(c["filed_date"])
            if period_date is None:
                continue

            fy, fq, label = _derive_fiscal_period(period_date, c["form_type"])

            filing = Filing(
                cik=str(int(cik)),
                accession_no=c["accession_no"],
                form_type=c["form_type"],
                period_end=period_date,
                filed_date=filed_date or period_date,
                fiscal_year=fy,
                fiscal_quarter=fq,
                fiscal_period_label=label,
                status="downloaded",
            )
            self.session.add(filing)
            new_filings.append(filing)

        await self.session.flush()
        return new_filings

    # ------------------------------------------------------------------ #
    # Fact persistence                                                      #
    # ------------------------------------------------------------------ #

    async def _persist_facts(
        self, filings: List[Filing], facts_payload: Dict[str, Any]
    ) -> int:
        acc_to_filing: Dict[str, Filing] = {f.accession_no: f for f in filings}

        fact_rows: List[Dict[str, Any]] = []
        all_facts = facts_payload.get("facts", {})

        for taxonomy, concepts in all_facts.items():
            for concept_name, concept_data in concepts.items():
                full_tag = f"{taxonomy}:{concept_name}"
                canonical = TAG_TO_CANONICAL.get(full_tag)

                units_data = concept_data.get("units", {})
                for unit, observations in units_data.items():
                    for obs in observations:
                        acc = obs.get("accn", "").replace("-", "")
                        if acc not in acc_to_filing:
                            continue

                        filing = acc_to_filing[acc]
                        period_type = "instant" if "start" not in obs else "duration"

                        fact_rows.append({
                            "filing_id": filing.filing_id,
                            "cik": filing.cik,
                            "taxonomy": taxonomy,
                            "concept": full_tag,
                            "canonical_concept": canonical,
                            "value": obs.get("val"),
                            "unit": unit,
                            "decimals": obs.get("decimals"),
                            "period_type": period_type,
                            "period_start": _parse_date(obs.get("start")),
                            "period_end": _parse_date(obs.get("end", "")),
                        })

        if not fact_rows:
            return 0

        # asyncpg caps query parameters at 32767; RawFact has 11 columns → max 2978 rows/batch
        batch_size = 2000
        for i in range(0, len(fact_rows), batch_size):
            await self.session.execute(
                pg_insert(RawFact).values(fact_rows[i:i + batch_size]).on_conflict_do_nothing()
            )

        for f in filings:
            f.status = "extracted"

        await self.session.flush()
        log.debug("Inserted %d raw facts for %d filings", len(fact_rows), len(filings))
        return len(fact_rows)

    # ------------------------------------------------------------------ #
    # Peer groups                                                           #
    # ------------------------------------------------------------------ #

    async def _build_peer_groups(self, cik: str, company: Company) -> None:
        """Assign a company to peer groups based on SIC code and sector."""
        groups = []
        if company.sic_code:
            groups.append((f"SIC-{company.sic_code}", company.sic_description or company.sic_code))
        if company.sector:
            groups.append((f"SECTOR-{company.sector}", company.sector))
        if company.industry_group:
            groups.append((f"INDUSTRY-{company.industry_group}", company.industry_group))

        for group_key, group_label in groups:
            await self.session.execute(
                pg_insert(PeerGroup)
                .values(cik=str(int(cik)), group_key=group_key, group_label=group_label)
                .on_conflict_do_nothing()
            )
        await self.session.flush()


# ──────────────────────────────────────────────────────────────────────────── #
# Helpers                                                                       #
# ──────────────────────────────────────────────────────────────────────────── #

def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _derive_fiscal_period(
    period_end: date, form_type: str
) -> tuple[int, Optional[int], str]:
    """
    Infer fiscal year, fiscal quarter, and a human-readable label.

    Uses calendar approximation. For precise fiscal-year alignment,
    cross-reference the company's fiscal year end date from submissions.
    """
    year = period_end.year
    month = period_end.month

    if form_type == "10-K":
        return year, None, f"FY-{year}"

    if month <= 3:
        quarter = 1
    elif month <= 6:
        quarter = 2
    elif month <= 9:
        quarter = 3
    else:
        quarter = 4

    return year, quarter, f"Q{quarter}-{year}"
