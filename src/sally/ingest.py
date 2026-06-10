"""Ingest — load a batch of leads from xlsx/csv and tag it.

This step does NO cleaning. It loads raw rows verbatim, standardises the column
names, and stamps each row with which batch/file it came from and when, so later
steps (and the state store) can tell a day-1 lead from a day-2 drop. Cleaning,
deduping and classification all happen downstream.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

# The 21 columns we expect from the pipeline export. Anything outside this set is
# kept as-is; anything missing is created empty, so a sparse day-2 batch still loads.
EXPECTED_COLUMNS = [
    "lead_id", "source", "handle", "store_name", "contact_name", "email", "phone",
    "city", "country", "followers", "active_listings", "avg_listing_price_gbp",
    "sales_velocity_30d", "est_monthly_spend_gbp", "stage", "first_seen_date",
    "last_touch_date", "num_touches", "last_inbound_text", "assigned_bdr", "notes",
]


def _standardise_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Lowercase/strip/snake_case column headers so a slightly-renamed export still maps."""
    df = df.rename(
        columns=lambda c: str(c).strip().lower().replace(" ", "_").replace("-", "_")
    )
    return df


def load_batch(
    path: str | Path,
    sheet: str | None = None,
    batch: str | None = None,
) -> pd.DataFrame:
    """Load one batch of leads.

    Args:
        path:  xlsx or csv file.
        sheet: sheet name if xlsx (defaults to the first sheet).
        batch: a label for this batch (defaults to the sheet/file name). Used so the
               state store can distinguish, e.g., the main pipeline from a day-2 drop.

    Returns a DataFrame with standardised columns plus `_batch` and `_loaded_at`.
    """
    path = Path(path)
    if path.suffix.lower() in {".xlsx", ".xls"}:
        df = pd.read_excel(path, sheet_name=sheet if sheet is not None else 0)
        batch = batch or (sheet if sheet is not None else path.stem)
    elif path.suffix.lower() in {".csv", ".tsv"}:
        sep = "\t" if path.suffix.lower() == ".tsv" else ","
        df = pd.read_csv(path, sep=sep)
        batch = batch or path.stem
    else:
        raise ValueError(f"Unsupported file type: {path.suffix} (use .xlsx or .csv)")

    df = _standardise_columns(df)

    # ensure every expected column exists so downstream code never KeyErrors on a
    # sparse batch; extra columns are left untouched.
    for col in EXPECTED_COLUMNS:
        if col not in df.columns:
            df[col] = pd.NA

    df["_batch"] = str(batch)
    df["_loaded_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return df


def load_workbook_tab_names(path: str | Path) -> list[str]:
    """List sheet names in an xlsx (handy for the CLI / debugging)."""
    return pd.ExcelFile(path).sheet_names
