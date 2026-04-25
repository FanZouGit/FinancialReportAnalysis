"""
config/taxonomy.py

Maps canonical concept names to the list of US-GAAP XBRL tags companies
actually use.  Companies are inconsistent — one uses Revenues, another uses
RevenueFromContractWithCustomerExcludingAssessedTax.  We resolve all variants
to a single canonical name so every query uses one column.

Priority within each list: earlier = preferred.  When a filing reports multiple
tags that map to the same canonical, we take the first match in priority order.
"""

from typing import Dict, List

# --------------------------------------------------------------------------- #
# Core 10 financial concepts (Phase 2)                                         #
# --------------------------------------------------------------------------- #

CONCEPT_ALIASES: Dict[str, List[str]] = {

    # ── Income statement ─────────────────────────────────────────────────── #

    "revenue": [
        "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
        "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax",
        "us-gaap:Revenues",
        "us-gaap:SalesRevenueNet",
        "us-gaap:SalesRevenueGoodsNet",
        "us-gaap:SalesRevenueServicesNet",
        "us-gaap:RevenueNet",
        "us-gaap:NetRevenues",
    ],

    "gross_profit": [
        "us-gaap:GrossProfit",
    ],

    "operating_income": [
        "us-gaap:OperatingIncomeLoss",
        "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
    ],

    "net_income": [
        "us-gaap:NetIncomeLoss",
        "us-gaap:NetIncomeLossAvailableToCommonStockholdersBasic",
        "us-gaap:ProfitLoss",
        "us-gaap:NetIncome",
    ],

    "eps_basic": [
        "us-gaap:EarningsPerShareBasic",
    ],

    "eps_diluted": [
        "us-gaap:EarningsPerShareDiluted",
    ],

    # ── Balance sheet ────────────────────────────────────────────────────── #

    "total_assets": [
        "us-gaap:Assets",
    ],

    "total_liabilities": [
        "us-gaap:Liabilities",
        # us-gaap:LiabilitiesAndStockholdersEquity removed — it maps to a total,
        # not liabilities alone, and appears in total_equity aliases too, making
        # the reverse lookup ambiguous.
    ],

    "total_equity": [
        "us-gaap:StockholdersEquity",
        "us-gaap:StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        # us-gaap:LiabilitiesAndStockholdersEquity removed — same reason as above.
    ],

    # ── Cash flow statement ──────────────────────────────────────────────── #

    "operating_cash_flow": [
        "us-gaap:NetCashProvidedByUsedInOperatingActivities",
        "us-gaap:NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ],

    "capex": [
        "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment",
        "us-gaap:CapitalExpendituresIncurredButNotYetPaid",
        "us-gaap:PaymentsToAcquireProductiveAssets",
    ],

    "free_cash_flow": [],   # Derived: operating_cash_flow - capex (no direct XBRL tag)
}


# Reverse lookup: XBRL tag → canonical name
TAG_TO_CANONICAL: Dict[str, str] = {
    tag: canonical
    for canonical, tags in CONCEPT_ALIASES.items()
    for tag in tags
}


# SIC code → GICS-style sector mapping (abbreviated)
SIC_TO_SECTOR: Dict[str, str] = {
    # Agriculture
    **{str(c): "Agriculture" for c in range(100, 1000)},
    # Mining
    **{str(c): "Energy & Mining" for c in range(1000, 1500)},
    # Construction
    **{str(c): "Industrials" for c in range(1500, 2000)},
    # Manufacturing
    **{str(c): "Industrials" for c in range(2000, 4000)},
    # Transportation & Utilities
    **{str(c): "Utilities & Transportation" for c in range(4000, 5000)},
    # Wholesale & Retail
    **{str(c): "Consumer" for c in range(5000, 6000)},
    # Finance & Insurance & Real Estate
    **{str(c): "Financials" for c in range(6000, 7000)},
    # Services
    **{str(c): "Services" for c in range(7000, 8000)},
    # Tech-adjacent services
    **{str(c): "Technology" for c in range(7370, 7380)},
    # Public Administration
    **{str(c): "Government" for c in range(9000, 10000)},
}

# SIC industry group overrides for important tech SIC codes
SIC_OVERRIDES: Dict[str, str] = {
    "7372": "Software",
    "7371": "Software",
    "7374": "IT Services",
    "3674": "Semiconductors",
    "3672": "Computer Hardware",
    "3669": "Communications Equipment",
    "4813": "Telecom",
    "6022": "Banking",
    "6311": "Insurance",
    "5912": "Pharmacy Retail",
    "2836": "Biotechnology",
    "2835": "Biotechnology",
}
