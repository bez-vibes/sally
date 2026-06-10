"""Identity & dedup — collapse duplicate rows into one record per real lead.

The duplicates in this data are identity-based, not row-id based: the same lead
appears under different lead_ids, sometimes across the IG export and the CRM dump,
with conflicting stages and touch counts.

Approach (agreed for the build):
  * Link rows that share ANY hard key — normalised handle, email, or phone — using
    union-find, so a lead recorded once by handle and once by email still merges
    (cross-key). Keyless shops fall back to a conservative fuzzy match on
    store_name within the same city.
  * Merge each group with rule A: the furthest-along funnel stage wins (Won/Lost
    always win; most-recent last_touch breaks ties); num_touches = max (same
    interactions counted twice, don't double-count); dates = earliest first_seen /
    latest last_touch; everything else coalesces to the richest/most-recent value.
  * Every merged record gets a stable `lead_key` — the cross-run identity the state
    store uses so re-runs never re-add or re-message the same lead.
"""

from __future__ import annotations

import re

import pandas as pd
from rapidfuzz import fuzz

from .clean import FUNNEL_ORDER

# Rank for "furthest-along wins". Ghosted sits just above Contacted (you must have
# been in contact to be ghosted), below real progress like Replied/Warm. Won/Lost
# are handled as explicit overrides before ranking.
STAGE_RANK = {
    "Unknown": -1, "New": 0, "Contacted": 1, "Ghosted": 1.5,
    "Replied": 2, "Warm": 3, "Call Booked": 4, "Negotiating": 5, "Won": 6, "Lost": 99,
}
FUZZY_NAME_THRESHOLD = 92  # store_name similarity (same city) to merge keyless shops


# --- union-find -----------------------------------------------------------------

class _UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _row_keys(row) -> list[str]:
    """Hard keys for a row — exact-match identity signals."""
    keys = []
    if pd.notna(row.get("handle_norm")):
        keys.append("h:" + str(row["handle_norm"]))
    if pd.notna(row.get("email")):
        keys.append("e:" + str(row["email"]))
    if pd.notna(row.get("phone")):
        keys.append("p:" + str(row["phone"]))
    return keys


def _slug(s) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(s).lower()) if pd.notna(s) else ""


def assign_components(df: pd.DataFrame) -> pd.Series:
    """Return a component id per row: rows in the same component are the same lead."""
    df = df.reset_index(drop=True)
    uf = _UnionFind(len(df))

    # 1. link on shared hard keys
    key_to_row: dict[str, int] = {}
    for i, row in df.iterrows():
        for k in _row_keys(row):
            if k in key_to_row:
                uf.union(key_to_row[k], i)
            else:
                key_to_row[k] = i

    # 2. fuzzy fallback: attach keyless rows (no handle/email/phone) to a same-city
    #    store with a near-identical name. Conservative — only for rows that would
    #    otherwise be orphans, so we never merge two genuinely-keyed records.
    keyless = [i for i, row in df.iterrows() if not _row_keys(row) and pd.notna(row.get("store_name"))]
    keyed_shops = [i for i, row in df.iterrows() if pd.notna(row.get("store_name")) and i not in keyless]
    for i in keyless:
        ni, ci = df.at[i, "store_name"], _slug(df.at[i, "city"])
        for j in keyed_shops:
            if _slug(df.at[j, "city"]) == ci and fuzz.token_sort_ratio(str(ni), str(df.at[j, "store_name"])) >= FUZZY_NAME_THRESHOLD:
                uf.union(j, i)
                break

    return pd.Series([uf.find(i) for i in range(len(df))], index=df.index)


# --- merge ----------------------------------------------------------------------

def _coalesce(group: pd.DataFrame, col: str, order_idx: list[int]):
    """First non-null value of `col`, walking rows in the given priority order."""
    for i in order_idx:
        v = group.at[i, col]
        if pd.notna(v) and str(v).strip() != "":
            return v
    return pd.NA


def _distinct(group: pd.DataFrame, col: str, order_idx: list[int], exclude=None) -> list[str]:
    """Distinct non-blank values of `col` (most-recent first), optionally excluding one."""
    out: list[str] = []
    excl = "" if exclude is None or pd.isna(exclude) else str(exclude).strip()
    for i in order_idx:
        v = group.at[i, col]
        if pd.isna(v):
            continue
        s = str(v).strip()
        if s and s != excl and s not in out:
            out.append(s)
    return out


def _merge_group(group: pd.DataFrame) -> dict:
    group = group.copy()
    # priority order: most-recently-touched row first (NaT treated as oldest)
    order_idx = group.sort_values("last_touch_date", na_position="first").index.tolist()[::-1]

    stages = list(group["stage"])
    if "Won" in stages:
        stage = "Won"
    elif "Lost" in stages:
        stage = "Lost"
    else:
        # furthest-along; tie broken by most-recent last_touch (order_idx is most-recent-first)
        stage = max(order_idx, key=lambda i: (STAGE_RANK.get(group.at[i, "stage"], -1),
                                              -order_idx.index(i)))
        stage = group.at[stage, "stage"]

    rec: dict = {}
    # coalesced identity / contact fields, richest-first
    for col in ["handle", "handle_norm", "store_name", "contact_name", "city",
                "country", "source", "assigned_bdr", "notes", "last_inbound_text",
                "followers", "active_listings", "avg_listing_price_gbp",
                "sales_velocity_30d", "est_monthly_spend_gbp"]:
        rec[col] = _coalesce(group, col, order_idx)

    # prefer a VALID email/phone if any duplicate had one
    valid_email_idx = [i for i in order_idx if group.at[i, "email_valid"]]
    rec["email"] = group.at[valid_email_idx[0], "email"] if valid_email_idx else _coalesce(group, "email", order_idx)
    rec["email_valid"] = bool(valid_email_idx) or False
    valid_phone_idx = [i for i in order_idx if group.at[i, "phone_valid"]]
    rec["phone"] = group.at[valid_phone_idx[0], "phone"] if valid_phone_idx else _coalesce(group, "phone", order_idx)
    rec["phone_valid"] = bool(valid_phone_idx) or False

    rec["stage"] = stage
    rec["num_touches"] = pd.to_numeric(group["num_touches"], errors="coerce").max()
    rec["first_seen_date"] = group["first_seen_date"].min()
    rec["last_touch_date"] = group["last_touch_date"].max()
    rec["lead_id"] = group.at[order_idx[0], "lead_id"]
    rec["merged_lead_ids"] = ",".join(sorted(str(x) for x in group["lead_id"].dropna()))
    rec["merged_count"] = len(group)
    rec["_batch"] = group.at[order_idx[0], "_batch"]

    # --- preserved alternate / audit info from the discarded duplicate rows ---
    # alternate contacts: extra ways to reach the lead that the winner dropped
    rec["alt_emails"] = "; ".join(_distinct(group, "email", order_idx, exclude=rec["email"]))
    rec["alt_phones"] = "; ".join(_distinct(group, "phone", order_idx, exclude=rec["phone"]))
    rec["alt_handles"] = "; ".join(_distinct(group, "handle_norm", order_idx, exclude=rec["handle_norm"]))
    # provenance
    rec["merged_sources"] = "; ".join(_distinct(group, "source", order_idx))
    rec["merged_batches"] = "; ".join(_distinct(group, "_batch", order_idx))
    # merge transparency
    merged_stages = _distinct(group, "stage", order_idx)
    rec["merged_stages"] = ", ".join(merged_stages)
    rec["merge_conflict"] = len(merged_stages) > 1
    # preserved free-text intel
    rec["merged_notes"] = " | ".join(_distinct(group, "notes", order_idx))
    rec["alt_inbound_texts"] = " | ".join(
        _distinct(group, "last_inbound_text", order_idx, exclude=rec["last_inbound_text"])
    )

    rec["lead_key"] = _lead_key(rec)
    return rec


def _lead_key(rec: dict) -> str:
    """Stable cross-run identity: handle > email > phone > name|city."""
    if pd.notna(rec.get("handle_norm")):
        return "h:" + str(rec["handle_norm"])
    if pd.notna(rec.get("email")):
        return "e:" + str(rec["email"])
    if pd.notna(rec.get("phone")):
        return "p:" + str(rec["phone"])
    return "n:" + _slug(rec.get("store_name")) + "|" + _slug(rec.get("city"))


def dedupe(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Collapse duplicates. Returns (deduped_df, report)."""
    df = df.reset_index(drop=True)
    df["_component"] = assign_components(df)

    records = [_merge_group(g) for _, g in df.groupby("_component", sort=False)]
    out = pd.DataFrame(records)

    merged_groups = out[out["merged_count"] > 1]
    report = {
        "rows_in": len(df),
        "rows_out": len(out),
        "duplicates_removed": len(df) - len(out),
        "groups_merged": len(merged_groups),
        "largest_group": int(out["merged_count"].max()) if len(out) else 0,
    }
    return out, report
