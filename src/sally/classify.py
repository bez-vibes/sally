"""Classify — determine each lead's type and reachable channels from its data.

Lead type drives which engine handles the lead (reseller scoring under the DM cap,
or shop sequencing). It is decided by the data present, not the source label:
reseller metrics are blank for stores (per the data Readme), so they're the hard
signal.

available_channels is computed independently of type — a reseller can have an email,
and that email is reachable off the 40-DM/day cap. The reseller_has_email flag feeds
the scoring module's channel routing (DM the top-priority handle-only resellers;
email the rest to conserve DM slots).
"""

from __future__ import annotations

import pandas as pd


def _present(v) -> bool:
    return pd.notna(v) and str(v).strip() != ""


def classify_row(row) -> dict:
    has_metrics = any(_present(row.get(c)) for c in ("followers", "active_listings", "sales_velocity_30d"))
    if has_metrics:
        lead_type = "reseller"
    elif _present(row.get("store_name")):
        lead_type = "shop"
    elif _present(row.get("handle_norm")):
        lead_type = "reseller"
    else:
        lead_type = "unknown"

    channels = []
    if _present(row.get("handle_norm")):
        channels.append("dm")
    if _present(row.get("email")) and bool(row.get("email_valid")):
        channels.append("email")
    if _present(row.get("phone")):                     # uncertain numbers are still callable
        channels.append("call")
    if _present(row.get("city")) and lead_type == "shop":
        channels.append("visit")

    reseller_has_email = lead_type == "reseller" and "email" in channels
    return {
        "lead_type": lead_type,
        "available_channels": ",".join(channels),
        "reseller_has_email": reseller_has_email,
    }


def classify(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    df = df.copy()
    derived = df.apply(classify_row, axis=1, result_type="expand")
    for col in ("lead_type", "available_channels", "reseller_has_email"):
        df[col] = derived[col]

    report = {
        "by_type": df["lead_type"].value_counts().to_dict(),
        "reseller_has_email": int(df["reseller_has_email"].sum()),
        "channel_availability": {
            ch: int(df["available_channels"].str.split(",").apply(lambda c: ch in c).sum())
            for ch in ("dm", "email", "call", "visit")
        },
    }
    return df, report
