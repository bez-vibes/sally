"""Sequence — shop outreach (no daily cap, but ranked) + visit-day clustering.

Shops have email, phone and a city, so the motion is email -> call -> visit. The
next step is a state machine keyed on stage (gated by which channels the shop
actually has), and shops needing an in-person/call touch are grouped by city so a
rep can plan a day of visits.
"""

from __future__ import annotations

import pandas as pd

from .score import as_of_date, compute_axes

EXCLUDE = {"Won", "Lost"}

# next step by stage (preferred channel); gated by available_channels at runtime
_NEXT = {
    "New": ("email", "first outreach"),
    "Contacted": ("call", "emailed, no reply — call"),
    "Ghosted": ("call", "went quiet — try a call"),
    "Replied": ("book_visit", "engaged — book a visit"),
    "Warm": ("book_visit", "warm — book a visit"),
    "Call Booked": ("visit", "call booked — attend / convert to visit"),
    "Negotiating": ("visit", "in negotiation — visit to close"),
}
VISIT_STEPS = {"book_visit", "visit"}


def _resolve_step(stage: str, channels: set[str]) -> tuple[str, str]:
    step, note = _NEXT.get(stage, ("email", "follow up"))
    # gate by what the shop actually has
    if step == "call" and "call" not in channels:
        step, note = ("email", note + " (no phone — email)")
    if step in VISIT_STEPS and "visit" not in channels:
        step, note = ("call" if "call" in channels else "email", note + " (no address — remote)")
    if step == "email" and "email" not in channels:
        step, note = ("call" if "call" in channels else "review", note)
    return step, note


def sequence_shops(df: pd.DataFrame, as_of: pd.Timestamp | None = None
                   ) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Returns (shops_with_next_step, visit_day_plan, report)."""
    s = df[df["lead_type"] == "shop"].copy()
    s = s[~s["stage"].isin(EXCLUDE)]
    if "manual_status" in s.columns:
        s = s[s["manual_status"].fillna("") != "do_not_contact"]

    as_of = as_of or as_of_date(df)
    s = compute_axes(s, as_of)
    # shop priority: spend-led value, nudged by warmth (stage tier prior)
    stage_prior = {"Negotiating": 1.0, "Call Booked": 0.9, "Warm": 0.8, "Replied": 0.75,
                   "Ghosted": 0.5, "Contacted": 0.45, "New": 0.4}
    s["stage_prior"] = s["stage"].map(stage_prior).fillna(0.4)
    s["priority"] = 0.6 * s["value"] + 0.4 * s["stage_prior"]

    steps = s.apply(
        lambda r: _resolve_step(r["stage"], set(str(r.get("available_channels", "")).split(","))),
        axis=1, result_type="expand",
    )
    s["next_step"], s["step_note"] = steps[0], steps[1]
    s = s.sort_values("priority", ascending=False).reset_index(drop=True)

    # visit-day plan: shops needing an in-person touch, grouped by city
    visitable = s[s["next_step"].isin(VISIT_STEPS)].copy()
    plan = (
        visitable.groupby("city")
        .agg(shops=("lead_key", "count"), avg_priority=("priority", "mean"))
        .reset_index()
        .sort_values(["shops", "avg_priority"], ascending=[False, False])
    )

    report = {
        "shops_active": len(s),
        "by_next_step": s["next_step"].value_counts().to_dict(),
        "visit_ready": len(visitable),
        "top_visit_cities": plan.head(6).set_index("city")["shops"].to_dict(),
    }
    return s, plan, report
