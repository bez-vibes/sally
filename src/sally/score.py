"""Score — reseller triage under the DM cap.

Tier by pipeline stage, rank within tier on the axes that matter for that tier,
then fill the daily DM slots top-down with Rule C channel routing.

  Tier 1  Active & warm  (Negotiating, Call Booked, Warm, Replied)  rank: value + urgency
  Tier 2  Revival        (Ghosted)                                  rank: value + recency
  Tier 3  Cold           (New, Contacted)                           rank: value

Rule C fill: walk the ordered list — has handle -> DM until the cap is full; after
that, email-reachable leads go to email (off-cap), handle-only leads defer.
"""

from __future__ import annotations

import pandas as pd

DM_CAP = 40
TIER1 = {"Negotiating", "Call Booked", "Warm", "Replied"}
TIER2 = {"Ghosted"}
TIER3 = {"New", "Contacted"}
EXCLUDE = {"Won", "Lost"}

# buying-power composite (spend-led; spend is 100% present, the direct GMV proxy)
VALUE_WEIGHTS = {"est_monthly_spend_gbp": 0.60, "sales_velocity_30d": 0.25, "followers": 0.15}


def _tier(stage: str) -> int | None:
    if stage in TIER1:
        return 1
    if stage in TIER2:
        return 2
    if stage in TIER3:
        return 3
    return None  # Won/Lost/Unknown — not in the DM triage


def as_of_date(df: pd.DataFrame) -> pd.Timestamp:
    """The pipeline's 'now' — latest date in the data (so staleness is meaningful on
    this frozen sample). Override in production with the real run date."""
    dates = pd.concat([df["last_touch_date"], df["first_seen_date"]]).dropna()
    return dates.max() if len(dates) else pd.Timestamp.today().normalize()


def compute_axes(df: pd.DataFrame, as_of: pd.Timestamp) -> pd.DataFrame:
    df = df.copy()
    # value: weighted blend of percentile ranks; missing signals impute to median (0.5)
    value = pd.Series(0.0, index=df.index)
    for col, w in VALUE_WEIGHTS.items():
        pct = df[col].rank(pct=True)
        value = value + w * pct.fillna(0.5)
    df["value"] = value

    days = (as_of - df["last_touch_date"]).dt.days
    df["days_since_touch"] = days
    df["urgency"] = (days / 30).clip(upper=1.0).fillna(0.0)        # 0 if never touched
    df["recency"] = (1 - (days / 30).clip(upper=1.0)).fillna(0.3)  # fresh touch -> high
    return df


def _within_tier_rank(row) -> float:
    if row["tier"] == 1:
        return 0.5 * row["value"] + 0.5 * row["urgency"]
    if row["tier"] == 2:
        return 0.5 * row["value"] + 0.5 * row["recency"]
    return row["value"]  # tier 3


def _reason(row) -> str:
    t = {1: "Active/warm", 2: "Revival", 3: "Cold"}[row["tier"]]
    bits = [f"T{row['tier']} {t}", row["stage"], f"value p{int(row['value']*100)}"]
    if row["tier"] in (1, 2) and pd.notna(row["days_since_touch"]):
        bits.append(f"{int(row['days_since_touch'])}d quiet")
    return " · ".join(bits)


def score_resellers(df: pd.DataFrame, dm_cap: int = DM_CAP,
                    as_of: pd.Timestamp | None = None) -> tuple[pd.DataFrame, dict]:
    """Score + route resellers. Returns (scored_df, report). Input may be the full
    deduped/classified frame or a pre-filtered eligible pool; non-resellers and
    Won/Lost are dropped here."""
    r = df[df["lead_type"] == "reseller"].copy()
    r = r[~r["stage"].isin(EXCLUDE)]
    if "manual_status" in r.columns:
        r = r[r["manual_status"].fillna("") != "do_not_contact"]

    as_of = as_of or as_of_date(df)
    r = compute_axes(r, as_of)
    r["tier"] = r["stage"].map(_tier)
    r = r[r["tier"].notna()]
    r["rank_score"] = r.apply(_within_tier_rank, axis=1)

    r = r.sort_values(["tier", "rank_score"], ascending=[True, False]).reset_index(drop=True)

    # Rule C fill
    channel, dm_rank = [], []
    used = 0
    for _, row in r.iterrows():
        chans = str(row.get("available_channels", "")).split(",")
        can_dm, has_email = "dm" in chans, "email" in chans
        if can_dm and used < dm_cap:
            used += 1
            channel.append("dm"); dm_rank.append(used)
        elif has_email:
            channel.append("email"); dm_rank.append(None)
        elif can_dm:
            channel.append("defer"); dm_rank.append(None)
        else:
            channel.append("review"); dm_rank.append(None)
    r["action_channel"] = channel
    r["dm_rank"] = dm_rank
    r["reason"] = r.apply(_reason, axis=1)

    report = {
        "as_of": as_of.date().isoformat(),
        "eligible_resellers": len(r),
        "dm_today": int((r["action_channel"] == "dm").sum()),
        "email_today": int((r["action_channel"] == "email").sum()),
        "deferred": int((r["action_channel"] == "defer").sum()),
        "by_tier": r["tier"].value_counts().sort_index().to_dict(),
        "dm_by_tier": r[r["action_channel"] == "dm"]["tier"].value_counts().sort_index().to_dict(),
    }
    return r, report
