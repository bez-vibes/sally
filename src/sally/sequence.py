"""Sequence — shop outreach + visit planning.

Shop daily actions are just email or call: you email a cold shop, you call a
warmer one (to chase, or to book a visit). A visit isn't a daily action — it's a
planned event — so visits live in the visit-day plan (engaged shops grouped by
city), which the rep books via those calls and executes on a planned trip.
"""

from __future__ import annotations

import pandas as pd

from .score import as_of_date, compute_axes, score_value

EXCLUDE = {"Won", "Lost"}

# daily action by stage: email (cold first touch) or call (chase / book a visit).
# Call Booked has no daily outreach action — it's a scheduled meeting (visit plan).
_NEXT = {
    "New": ("email", "first outreach"),
    "Contacted": ("call", "emailed, no reply — call to chase"),
    "Ghosted": ("call", "went quiet — call to re-engage"),
    "Replied": ("call", "engaged — call to book a visit"),
    "Warm": ("call", "warm — call to book a visit"),
    "Negotiating": ("call", "in negotiation — call to advance / book a visit"),
    "Call Booked": (None, "meeting booked — see visit plan"),
}
# stages worth an in-person visit (feed the visit-day plan, if they have an address)
VISIT_READY_STAGES = {"Replied", "Warm", "Negotiating", "Call Booked"}


def _resolve_step(stage: str, channels: set[str]) -> tuple[str | None, str]:
    step, note = _NEXT.get(stage, ("email", "follow up"))
    if step == "call" and "call" not in channels:
        step, note = ("email", note + " (no phone — email instead)")
    if step == "email" and "email" not in channels:
        step = "call" if "call" in channels else None
    return step, note


def sequence_shops(df: pd.DataFrame, as_of: pd.Timestamp | None = None
                   ) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Returns (shops_with_next_step, visit_plan_shops, report).

    shops_with_next_step: daily actions (email/call).
    visit_plan_shops: visit-ready shops (with an address) for the city-clustered plan.
    """
    s = df[df["lead_type"] == "shop"].copy()
    s = s[~s["stage"].isin(EXCLUDE)]
    if "manual_status" in s.columns:
        s = s[s["manual_status"].fillna("") != "do_not_contact"]

    as_of = as_of or as_of_date(df)
    s = compute_axes(s, as_of)
    # same consistent convergence score as resellers, so board scores are comparable
    s["priority"] = s.apply(score_value, axis=1)

    steps = s.apply(
        lambda r: _resolve_step(r["stage"], set(str(r.get("available_channels", "")).split(","))),
        axis=1, result_type="expand",
    )
    s["next_step"], s["step_note"] = steps[0], steps[1]
    s = s.sort_values("priority", ascending=False).reset_index(drop=True)

    # visit-day plan: visit-ready shops that have an address, grouped later by city
    visit_plan = s[
        s["stage"].isin(VISIT_READY_STAGES)
        & s["available_channels"].str.contains("visit", na=False)
    ].copy()

    by_city = (
        visit_plan.groupby("city").size().sort_values(ascending=False)
        if len(visit_plan) else pd.Series(dtype=int)
    )
    report = {
        "shops_active": len(s),
        "by_next_step": s["next_step"].value_counts(dropna=False).to_dict(),
        "visit_ready": len(visit_plan),
        "top_visit_cities": by_city.head(6).to_dict(),
    }
    return s, visit_plan, report
