"""
agent/semantic_search.py

Phase 7: Semantic search over MD&A text.

Pipeline:
  1. Extract MD&A section from filing (SEC inline XBRL / HTML)
  2. Chunk into overlapping segments (~512 tokens)
  3. Embed with sentence-transformers or OpenAI
  4. Store in pgvector
  5. Query by cosine similarity

MD&A is found in:
  10-K: Item 7 — "Management's Discussion and Analysis of Financial Condition..."
  10-Q: Item 2 — "Management's Discussion and Analysis..."
"""

import logging
import re
from typing import List, Optional, Tuple

import httpx
import numpy as np
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import settings
from database.models import ChunkEmbedding, Filing, MdaChunk

log = logging.getLogger(__name__)

CHUNK_SIZE = 512          # tokens (approximate)
CHUNK_OVERLAP = 64        # tokens overlap between chunks
AVG_CHARS_PER_TOKEN = 4   # rough estimate


# ──────────────────────────────────────────────────────────────────────────── #
# Embedding model (lazy singleton)                                              #
# ──────────────────────────────────────────────────────────────────────────── #

_embedding_model = None

def _get_embedding_model():
    global _embedding_model
    if _embedding_model is None:
        from sentence_transformers import SentenceTransformer
        _embedding_model = SentenceTransformer(settings.embedding_model)
    return _embedding_model


def _embed_texts(texts: List[str]) -> List[List[float]]:
    """Embed a list of strings. Returns list of float vectors."""
    if settings.embedding_model.startswith("text-embedding"):
        # OpenAI embeddings
        from openai import OpenAI
        client = OpenAI(api_key=settings.openai_api_key)
        resp = client.embeddings.create(input=texts, model=settings.embedding_model)
        return [item.embedding for item in resp.data]
    else:
        # Local sentence-transformers
        model = _get_embedding_model()
        vectors = model.encode(texts, batch_size=32, show_progress_bar=False)
        return [v.tolist() for v in vectors]


# ──────────────────────────────────────────────────────────────────────────── #
# MDA Embedder                                                                  #
# ──────────────────────────────────────────────────────────────────────────── #

class MdaEmbedder:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def embed_filing(self, filing_id: int) -> int:
        """Download, chunk, and embed the MD&A section of a filing."""
        from sqlalchemy import select
        result = await self.session.execute(
            select(Filing).where(Filing.filing_id == filing_id)
        )
        filing = result.scalar_one_or_none()
        if not filing:
            log.error("Filing %d not found", filing_id)
            return 0

        # Fetch MD&A text from SEC
        mda_text = await self._fetch_mda(filing)
        if not mda_text:
            log.warning("No MD&A found for filing %d", filing_id)
            return 0

        # Chunk
        chunks = _chunk_text(mda_text, chunk_size=CHUNK_SIZE, overlap=CHUNK_OVERLAP)
        log.info("Filing %d: %d MD&A chunks", filing_id, len(chunks))

        # Embed
        embeddings = _embed_texts([c[0] for c in chunks])

        # Persist
        count = 0
        for idx, ((text_content, char_start, char_end), embedding) in enumerate(
            zip(chunks, embeddings)
        ):
            chunk = MdaChunk(
                filing_id=filing_id,
                cik=filing.cik,
                chunk_index=idx,
                text=text_content,
                char_start=char_start,
                char_end=char_end,
                token_count=len(text_content) // AVG_CHARS_PER_TOKEN,
            )
            self.session.add(chunk)
            await self.session.flush()

            # Insert embedding with pgvector raw SQL
            vector_str = "[" + ",".join(str(v) for v in embedding) + "]"
            await self.session.execute(
                text("""
                    INSERT INTO chunk_embeddings (chunk_id, model_name, embedding)
                    VALUES (:chunk_id, :model_name, :embedding::vector)
                    ON CONFLICT (chunk_id) DO UPDATE
                    SET embedding = EXCLUDED.embedding
                """),
                {
                    "chunk_id": chunk.chunk_id,
                    "model_name": settings.embedding_model,
                    "embedding": vector_str,
                },
            )
            count += 1

        await self.session.flush()
        return count

    async def _fetch_mda(self, filing: Filing) -> Optional[str]:
        """
        Download the filing's primary document from SEC EDGAR and extract
        the MD&A section.
        """
        accession_formatted = (
            filing.accession_no[:10] + "-" +
            filing.accession_no[10:12] + "-" +
            filing.accession_no[12:]
        )
        cik_padded = filing.cik.zfill(10)
        index_url = (
            f"https://www.sec.gov/Archives/edgar/data/{filing.cik}/"
            f"{filing.accession_no}/index.json"
        )

        try:
            async with httpx.AsyncClient(
                headers={"User-Agent": settings.sec_user_agent}, timeout=30.0
            ) as client:
                resp = await client.get(index_url)
                resp.raise_for_status()
                index_data = resp.json()

            # Find the primary HTML/HTM document
            primary_doc = _find_primary_doc(index_data)
            if not primary_doc:
                return None

            doc_url = (
                f"https://www.sec.gov/Archives/edgar/data/{filing.cik}/"
                f"{filing.accession_no}/{primary_doc}"
            )
            async with httpx.AsyncClient(
                headers={"User-Agent": settings.sec_user_agent}, timeout=60.0
            ) as client:
                resp = await client.get(doc_url)
                resp.raise_for_status()
                html = resp.text

            return _extract_mda_from_html(html, filing.form_type)

        except Exception as exc:
            log.error("Failed to fetch MD&A for filing %d: %s", filing.filing_id, exc)
            return None


# ──────────────────────────────────────────────────────────────────────────── #
# Semantic Search                                                               #
# ──────────────────────────────────────────────────────────────────────────── #

class SemanticSearch:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def search(
        self,
        query: str,
        top_k: int = 10,
        cik: Optional[str] = None,
        sector: Optional[str] = None,
        period_label: Optional[str] = None,
    ) -> List[dict]:
        """
        Find the most semantically similar MD&A chunks to the query.

        Filters (all optional):
          cik          — limit to one company
          sector       — limit to one sector
          period_label — e.g. "Q3-2023"
        """
        # Embed the query
        query_vec = _embed_texts([query])[0]
        vector_str = "[" + ",".join(str(v) for v in query_vec) + "]"

        # Build filter clauses
        filters = ["1=1"]
        params: dict = {
            "query_vec": vector_str,
            "top_k": top_k,
        }

        if cik:
            filters.append("mc.cik = :cik")
            params["cik"] = str(int(cik))
        if sector:
            filters.append("c.sector = :sector")
            params["sector"] = sector
        if period_label:
            filters.append("f.fiscal_period_label = :period_label")
            params["period_label"] = period_label

        where = " AND ".join(filters)

        sql = text(f"""
            SELECT
                mc.chunk_id,
                mc.cik,
                c.ticker,
                c.name             AS company_name,
                c.sector,
                f.form_type,
                f.fiscal_period_label AS period_label,
                f.period_end,
                mc.chunk_index,
                mc.text,
                1 - (ce.embedding <=> :query_vec::vector) AS similarity
            FROM chunk_embeddings ce
            JOIN mda_chunks mc   ON mc.chunk_id = ce.chunk_id
            JOIN filings f       ON f.filing_id = mc.filing_id
            JOIN companies c     ON c.cik = mc.cik
            WHERE {where}
            ORDER BY ce.embedding <=> :query_vec::vector
            LIMIT :top_k
        """)

        rows = (await self.session.execute(sql, params)).mappings().all()
        return [dict(row) for row in rows]


# ──────────────────────────────────────────────────────────────────────────── #
# Text utilities                                                                 #
# ──────────────────────────────────────────────────────────────────────────── #

def _chunk_text(
    text: str, chunk_size: int = 512, overlap: int = 64
) -> List[Tuple[str, int, int]]:
    """
    Split text into overlapping chunks based on approximate token count.
    Returns list of (text, char_start, char_end).
    """
    char_chunk = chunk_size * AVG_CHARS_PER_TOKEN
    char_overlap = overlap * AVG_CHARS_PER_TOKEN

    chunks = []
    start = 0
    while start < len(text):
        end = min(start + char_chunk, len(text))

        # Try to break on sentence boundary
        if end < len(text):
            boundary = text.rfind(". ", start, end)
            if boundary > start + char_chunk // 2:
                end = boundary + 2

        chunk_text = text[start:end].strip()
        if chunk_text:
            chunks.append((chunk_text, start, end))

        start = end - char_overlap
        if start >= len(text) - char_overlap:
            break

    return chunks


def _find_primary_doc(index_data: dict) -> Optional[str]:
    """Find the primary HTML document filename from the EDGAR filing index."""
    items = index_data.get("directory", {}).get("item", [])
    # Prefer 10-K or 10-Q typed document
    for item in items:
        doc_type = item.get("type", "").upper()
        name = item.get("name", "")
        if doc_type in ("10-K", "10-Q") and name.endswith((".htm", ".html")):
            return name
    # Fall back to first HTM file
    for item in items:
        if item.get("name", "").endswith((".htm", ".html")):
            return item["name"]
    return None


def _extract_mda_from_html(html: str, form_type: str) -> Optional[str]:
    """
    Extract the MD&A section from SEC filing HTML.

    10-K: Item 7
    10-Q: Item 2
    """
    item_num = "7" if form_type == "10-K" else "2"

    # Strip HTML tags
    clean = re.sub(r"<[^>]+>", " ", html)
    clean = re.sub(r"&nbsp;", " ", clean)
    clean = re.sub(r"&amp;", "&", clean)
    clean = re.sub(r"\s+", " ", clean).strip()

    # Find MD&A section boundaries
    start_patterns = [
        rf"ITEM\s+{item_num}[\.\s].*?MANAGEMENT.S DISCUSSION",
        rf"Item\s+{item_num}[\.\s].*?Management.s Discussion",
        rf"MANAGEMENT.S DISCUSSION AND ANALYSIS",
    ]
    end_patterns = [
        r"ITEM\s+\d+[A-Z]?\.",
        r"Item\s+\d+[A-Z]?\.",
        r"QUANTITATIVE AND QUALITATIVE DISCLOSURES",
    ]

    start = None
    for pattern in start_patterns:
        m = re.search(pattern, clean, re.IGNORECASE)
        if m:
            start = m.start()
            break

    if start is None:
        return None

    # Find where the next item starts (end of MD&A)
    end = len(clean)
    for pattern in end_patterns:
        m = re.search(pattern, clean[start + 100:], re.IGNORECASE)
        if m:
            candidate = start + 100 + m.start()
            if candidate > start + 200:  # must be meaningfully after start
                end = min(end, candidate)
                break

    mda = clean[start:end].strip()
    return mda if len(mda) > 500 else None   # skip if too short
