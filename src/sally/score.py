"""Score — reseller triage under the DM cap.

Group by pipeline position, rank within group on the axes that matter there, then
fill the daily DM slots top-down with Rule C channel routing.

  0  Deals in flight  (Negotiating, Call Booked)  rank: value  — protect near-revenue first
  1  Revive warm      (Warm, Replied)             rank: 0.6 value + 0.4 urgency
  2  Revival          (Ghosted)                   rank: 0.5 value + 0.5 recency
  3  Cold             (New, Contacted)            rank: value  — DM only top-quartile whales,
                                                  and only with slots left after warm/revival

Rule C fill: walk the ordered list — DM-eligible lead takes a slot until the cap is
full; after that, email-reachable leads go to email (off-cap), handle-only defers.
"""

from __future__ import annotations

import pandas as pd

DM_CAP = 40
URGENCY_HORIZON_DAYS = 60     # days-since-touch at which urgency maxes out (tunable)
WHALE_VALUE_THRESHOLD = 0.75  # cold leads need value >= this to earn a scarce DM (tunable)

IN_FLIGHT = {"Negotiating", "Call Booked"}
REVIVE_WARM = {"Warm", "Replied"}
REVIVE_COLD = {"Ghosted"}
COLD = {"New", "Contacted"}
EXCLUDE = {"Won", "Lost"}

_GROUP = [(IN_FLIGHT, 0, "Deals in flight"), (REVIVE_WARM, 1, "Revive warm"),
          (REVIVE_COLD, 2, "Revival"), (COLD, 3, "Cold")]

# buying-power composite (spend-led; spend is 100% present, the direct GMV proxy)
VALUE_WEIGHTS = {"est_monthly_spend_gbp": 0.60, "sales_velocity_30d": 0.25, "followers": 0.15}


def _group(stage: str):
    for stages, order, label in _GROUP:
        if stage in stages:
            return order, label
    return None, None


def as_of_date(df: pd.DataFrame) -> pd.Timestamp:
    """The pipeline's 'now' — latest date in the data (so staleness is meaningful on
    this frozen sample). Override in production with the real run date."""
    dates = pd.concat([df["last_touch_date"], df["first_seen_date"]]).dropna()
    return dates.max() if len(dates) else pd.Timestamp.today().normalize()


def compute_axes(df: pd.DataFrame, as_of: pd.Timestamp,
                 horizon: int = URGENCY_HORIZON_DAYS) -> pd.DataFrame:
    df = df.copy()
    # value: weighted blend of percentile ranks; missing signals impute to median (0.5)
    value = pd.Series(0.0, index=df.index)
    for col, w in VALUE_WEIGHTS.items():
        value = value + w * df[col].rank(pct=True).fillna(0.5)
    df["value"] = value

    days = (as_of - df["last_touch_date"]).dt.days
    df["days_since_touch"] = days
    df["urgency"] = (days / horizon).clip(upper=1.0).fillna(0.0)        # 0 if never touched
    df["recency"] = (1 - (days / horizon).clip(upper=1.0)).fillna(0.3)  # fresh touch -> high
    return df


def _rank(row) -> float:
    g = row["group_order"]
    if g == 0:
        return row["value"]
    if g == 1:
        return 0.6 * row["value"] + 0.4 * row["urgency"]
    if g == 2:
        return 0.5 * row["value"] + 0.5 * row["recency"]
    return row["value"]


def _reason(row) -> str:
    bits = [row["group_label"], row["stage"], f"value p{int(row['value']*100)}"]
    if row["group_order"] in (1, 2) and pd.notna(row["days_since_touch"]):
        bits.append(f"{int(row['days_since_touch'])}d quiet")
    if row["group_order"] == 3:
        bits.append("whale" if row["value"] >= WHALE_VALUE_THRESHOLD else "below-whale")
    return " · ".join(bits)


def score_resellers(df: pd.DataFrame, dm_cap: int = DM_CAP,
                    as_of: pd.Timestamp | None = None,
                    horizon: int = URGENCY_HORIZON_DAYS,
                    whale_threshold: float = WHALE_VALUE_THRESHOLD) -> tuple[pd.DataFrame, dict]:
    """Score + route resellers. Input may be the full deduped/classified frame or a
    pre-filtered eligible pool; non-resellers and Won/Lost are dropped here."""
    r = df[df["lead_type"] == "reseller"].copy()
    r = r[~r["stage"].isin(EXCLUDE)]
    if "manual_status" in r.columns:
        r = r[r["manual_status"].fillna("") != "do_not_contact"]

    as_of = as_of or as_of_date(df)
    r = compute_axes(r, as_of, horizon)
    groups = r["stage"].map(_group)
    r["group_order"] = groups.map(lambda t: t[0])
    r["group_label"] = groups.map(lambda t: t[1])
    r = r[r["group_order"].notna()]
    r["rank_score"] = r.apply(_rank, axis=1)

    r = r.sort_values(["group_order", "rank_score"], ascending=[True, False]).reset_index(drop=True)

    # Rule C fill, with cold-whale gating
    channel, dm_rank = [], []
    used = 0
    for _, row in r.iterrows():
        chans = str(row.get("available_channels", "")).split(",")
        can_dm, has_email = "dm" in chans, "email" in chans
        is_cold = row["group_order"] == 3
        dm_eligible = can_dm and (not is_cold or row["value"] >= whale_threshold)
        if dm_eligible and used < dm_cap:
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

    dmd = r[r["action_channel"] == "dm"]
    report = {
        "as_of": as_of.date().isoformat(),
        "urgency_horizon_days": horizon,
        "eligible_resellers": len(r),
        "dm_today": len(dmd),
        "email_today": int((r["action_channel"] == "email").sum()),
        "deferred": int((r["action_channel"] == "defer").sum()),
        "dm_by_group": dmd["group_label"].value_counts().to_dict(),
    }
    return r, report
