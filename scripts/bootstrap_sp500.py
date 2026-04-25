#!/usr/bin/env python3
"""
scripts/bootstrap_sp500.py

Phase 1: End-to-end pipeline validation on S&P 500 companies.

Runs synchronously (no job queue) for simplicity.
Switch to the ARQ job queue for production.

Usage:
    python scripts/bootstrap_sp500.py
    python scripts/bootstrap_sp500.py --limit 10   # first 10 companies only
    python scripts/bootstrap_sp500.py --ticker AAPL # single company
"""

import asyncio
import logging
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
from rich.table import Table

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import settings
from database.engine import AsyncSessionLocal, init_db
from extraction.normaliser import MetricsComputer
from ingestion.downloader import FilingDownloader
from ingestion.edgar_client import EdgarClient

logging.basicConfig(level=logging.WARNING)
log = logging.getLogger(__name__)
console = Console()
app = typer.Typer()


async def _run_bootstrap(limit: int, ticker: Optional[str]) -> None:
    console.print("\n[bold cyan]EDGAR Financial Intelligence Platform[/bold cyan]")
    console.print("[dim]Phase 1: S&P 500 Bootstrap[/dim]\n")

    # Step 1: initialise DB
    console.print("→ Initialising database...")
    await init_db()
    console.print("  [green]✓[/green] Database ready\n")

    # Step 2: resolve CIKs
    async with EdgarClient() as client:
        if ticker:
            raw = await client.get_all_company_tickers()
            ticker_to_cik = {
                row[2].upper(): str(row[0])
                for row in raw.get("data", [])
            }
            cik = ticker_to_cik.get(ticker.upper())
            if not cik:
                console.print(f"[red]Ticker {ticker} not found[/red]")
                return
            ciks = [cik]
            console.print(f"→ Single company mode: {ticker} (CIK {cik})\n")
        else:
            console.print("→ Fetching S&P 500 CIK list...")
            ciks = await client.get_sp500_ciks()
            if limit:
                ciks = ciks[:limit]
            console.print(f"  [green]✓[/green] {len(ciks)} companies to process\n")

    # Step 3: ingest each company
    total_filings = 0
    total_facts = 0
    errors = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Ingesting filings...", total=len(ciks))

        async with EdgarClient() as client:
            for cik in ciks:
                async with AsyncSessionLocal() as session:
                    try:
                        # sync_company now handles peer group building internally
                        downloader = FilingDownloader(session, client)
                        stats = await downloader.sync_company(cik)
                        await session.commit()
                        total_filings += stats["new_filings"]
                        total_facts += stats["new_facts"]

                        computer = MetricsComputer(session)
                        await computer.compute_for_company(cik)
                        await session.commit()

                        progress.advance(task)

                    except Exception as exc:
                        errors.append((cik, str(exc)))
                        log.error("Failed CIK %s: %s", cik, exc)
                        progress.advance(task)

    # Step 4: summary
    console.print("\n[bold]Bootstrap complete[/bold]\n")

    summary = Table(title="Results", show_header=True)
    summary.add_column("Metric", style="cyan")
    summary.add_column("Value", style="green")
    summary.add_row("Companies processed", str(len(ciks)))
    summary.add_row("New filings ingested", str(total_filings))
    summary.add_row("Raw facts stored", f"{total_facts:,}")
    summary.add_row("Errors", str(len(errors)))
    console.print(summary)

    if errors:
        console.print("\n[yellow]Failed CIKs:[/yellow]")
        for cik, msg in errors[:10]:
            console.print(f"  CIK {cik}: {msg[:80]}")

    console.print("\n[dim]Next steps:[/dim]")
    console.print("  uvicorn api.main:app --reload")
    console.print("  curl http://localhost:8000/companies/AAPL/metrics")
    console.print("  curl http://localhost:8000/sectors/Technology/benchmark?metric=net_margin&period_label=Q3-2023\n")


@app.command()
def main(
    limit: int = typer.Option(0, help="Limit to first N companies (0 = all S&P 500)"),
    ticker: str = typer.Option(None, help="Process a single ticker only"),
):
    """Bootstrap the EDGAR platform with S&P 500 company filings."""
    asyncio.run(_run_bootstrap(limit, ticker))


if __name__ == "__main__":
    app()
