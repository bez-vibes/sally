"""Score — one consistent convergence score for every lead (0-100).

The same components and weights apply to every lead, so a given input always
contributes the same points and scores are comparable across the whole pipeline.
Pipeline position is just one component (a stage bonus), so "work live deals first"
is expressed as points, not a separate formula. The score directly orders the
queue; the daily DM slots are then filled top-down with Rule C channel routing.

  Stage bonus      Negotiating/Call Booked high … New low   (encodes the tier)
  Buying power     spend-led value × 35
  Going cold       urgency (days since touch) × 20
  Buying intent    from the last reply (+15 buying … -20 objection)
  Research         +5 per sourced signal, capped +15

score_components() is the single source of truth: the scorer sums it for the score
AND the UI renders it for the breakdown, so what you see is exactly what scored.
"""

from __future__ import annotations

import pandas as pd

from .intent import classify_intent
from .research import research_signals

DM_CAP = 40
URGENCY_HORIZON_DAYS = 60  # days-since-touch at which "going cold" maxes out (tunable)
EXCLUDE = {"Won", "Lost"}

# --- component weights (points out of 100; all tunable) -------------------------
STAGE_BONUS = {"Negotiating": 30, "Call Booked": 28, "Warm": 18, "Replied": 16,
               "Ghosted": 12, "Contacted": 6, "New": 3}
BUYING_POWER_MAX = 35
GOING_COLD_MAX = 20
INTENT_POINTS = {"buying": 15, "scheduling": 12, "qualifying": 6,
                 "none": 0, "deferral": -6, "objection": -20}
RESEARCH_PER_SIGNAL, RESEARCH_MAX = 5, 15

VALUE_WEIGHTS = {"est_monthly_spend_gbp": 0.60, "sales_velocity_30d": 0.25, "followers": 0.15}

# coarse display tier (not used in scoring; just a label for the UI)
_TIER = [({"Negotiating", "Call Booked"}, "Deals in flight"), ({"Warm", "Replied"}, "Revive warm"),
         ({"Ghosted"}, "Revival"), ({"New", "Contacted"}, "Cold")]


def as_of_date(df: pd.DataFrame) -> pd.Timestamp:
    dates = pd.concat([df["last_touch_date"], df["first_seen_date"]]).dropna()
    return dates.max() if len(dates) else pd.Timestamp.today().normalize()


def compute_axes(df: pd.DataFrame, as_of: pd.Timestamp,
                 horizon: int = URGENCY_HORIZON_DAYS) -> pd.DataFrame:
    df = df.copy()
    value = pd.Series(0.0, index=df.index)
    for col, w in VALUE_WEIGHTS.items():
        value = value + w * df[col].rank(pct=True).fillna(0.5)
    df["value"] = value
    days = (as_of - df["last_touch_date"]).dt.days
    df["days_since_touch"] = days
    df["urgency"] = (days / horizon).clip(upper=1.0).fillna(0.0)
    df["recency"] = (1 - (days / horizon).clip(upper=1.0)).fillna(0.3)
    return df


def tier_label(stage: str) -> str:
    for stages, label in _TIER:
        if stage in stages:
            return label
    return "Other"


def _fmt_spend(v) -> str:
    if pd.isna(v):
        return "spend unknown"
    v = float(v)
    return f"~£{v/1000:.1f}k/mo".replace(".0k", "k") if v >= 1000 else f"~£{int(v)}/mo"


def _days(row):
    d = row.get("days_since_touch")
    if not pd.notna(d):
        d = row.get("days_quiet")
    return int(d) if pd.notna(d) else None


def _buying_driver(row) -> str:
    bits = [_fmt_spend(row.get("est_monthly_spend_gbp"))]
    for col, lab in [("sales_velocity_30d", "sold/30d"), ("followers", "followers")]:
        v = row.get(col)
        if pd.notna(v):
            bits.append(f"{int(v):,} {lab}")
    return ", ".join(bits)


def score_components(row) -> list[dict]:
    """The convergence stack for one lead: [{label, points, driver}]. Same for all leads."""
    stage = row.get("stage")
    comps = []

    sb = STAGE_BONUS.get(stage, 0)
    sd = ("In negotiation — a live deal" if stage == "Negotiating" else
          "Call booked — a live deal" if stage == "Call Booked" else str(stage))
    comps.append({"label": "Pipeline stage", "points": sb, "driver": sd})

    comps.append({"label": "Buying power", "points": round((row.get("value") or 0) * BUYING_POWER_MAX),
                  "driver": _buying_driver(row)})

    days = _days(row)
    comps.append({"label": "Going cold", "points": round((row.get("urgency") or 0) * GOING_COLD_MAX),
                  "driver": f"last contacted {days} days ago" if days is not None else "not yet contacted"})

    bucket, _ = classify_intent(row.get("last_inbound_text"))
    reply = row.get("last_inbound_text")
    has_reply = pd.notna(reply) and str(reply).strip() and str(reply).lower() != "nan"
    comps.append({"label": "Buying intent", "points": INTENT_POINTS.get(bucket, 0),
                  "driver": f'replied "{reply}"' if has_reply else "no reply yet"})

    rs = research_signals(row)
    comps.append({"label": "Research signals",
                  "points": min(RESEARCH_MAX, RESEARCH_PER_SIGNAL * len(rs["signals"])),
                  "driver": " · ".join(s["source_label"] for s in rs["signals"]) or "none found"})
    return comps


def score_total(components: list[dict]) -> int:
    return max(0, min(100, sum(c["points"] for c in components)))


def score_value(row) -> float:
    """The 0-1 priority used for ranking (= score / 100)."""
    return score_total(score_components(row)) / 100.0


def _reason(row) -> str:
    spend = _fmt_spend(row.get("est_monthly_spend_gbp"))
    stage = row["stage"]
    days = _days(row)
    if stage in ("Negotiating", "Call Booked"):
        base = f"Live deal ({stage.lower()}), {spend} — keep it moving"
    elif stage in ("Warm", "Replied"):
        base = (f"{stage}, quiet {days} days, {spend} — re-engage before they go cold"
                if days is not None else f"{stage}, {spend} — follow up")
    elif stage == "Ghosted":
        base = f"Ghosted, {spend} — worth a re-engagement nudge"
    else:
        base = f"New lead, {spend}"
    bucket, _ = classify_intent(row.get("last_inbound_text"))
    if bucket in ("buying", "scheduling"):
        base += " · showed buying interest" if bucket == "buying" else " · wants to talk"
    elif bucket == "objection":
        base += " · ⚠ objection in last reply"
    return base


def score_resellers(df: pd.DataFrame, dm_cap: int = DM_CAP,
                    as_of: pd.Timestamp | None = None,
                    horizon: int = URGENCY_HORIZON_DAYS) -> tuple[pd.DataFrame, dict]:
    r = df[df["lead_type"] == "reseller"].copy()
    r = r[~r["stage"].isin(EXCLUDE)]
    if "manual_status" in r.columns:
        r = r[r["manual_status"].fillna("") != "do_not_contact"]

    as_of = as_of or as_of_date(df)
    r = compute_axes(r, as_of, horizon)
    r["group_label"] = r["stage"].map(tier_label)
    r["rank_score"] = r.apply(score_value, axis=1)
    r = r.sort_values("rank_score", ascending=False).reset_index(drop=True)

    # Rule C fill: top-down by score — DM until the cap, then email overflow, else defer
    channel, dm_rank, used = [], [], 0
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

    dmd = r[r["action_channel"] == "dm"]
    report = {
        "as_of": as_of.date().isoformat(), "urgency_horizon_days": horizon,
        "eligible_resellers": len(r), "dm_today": len(dmd),
        "email_today": int((r["action_channel"] == "email").sum()),
        "deferred": int((r["action_channel"] == "defer").sum()),
        "dm_by_group": dmd["group_label"].value_counts().to_dict(),
    }
    return r, report
