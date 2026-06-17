"""Queue — the daily output: what to do next for each lead, ready to act on.

Two formats from the same action list:
  - actions_<date>.csv : machine-readable (an agent / Lemlist / a rep's sheet)
  - brief_<date>.md    : a readable morning brief grouped by channel
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

QUEUE_COLUMNS = [
    "priority", "lead_type", "name", "contact", "channel", "action_type",
    "stage", "monthly_spend", "reason", "due_date", "message", "lead_key",
]


def _name(row) -> str:
    for c in ("store_name", "handle_norm", "contact_name", "email"):
        v = row.get(c)
        if pd.notna(v) and str(v).strip():
            return str(v)
    return row.get("lead_key", "")


def _contact(row) -> str:
    ch = row.get("channel")
    if ch == "dm":
        return "@" + str(row.get("handle_norm", ""))
    if ch == "email":
        return str(row.get("email", ""))
    if ch == "call":
        return str(row.get("phone") or row.get("email") or row.get("city") or "")
    return str(row.get("handle_norm") or row.get("email") or "")


def build_action_rows(resellers: pd.DataFrame, shops: pd.DataFrame, due_date: str) -> pd.DataFrame:
    """Assemble today's actionable leads (resellers with a DM/email, shops with a
    next step) into one queue, ordered by priority."""
    rows = []

    rdo = resellers[resellers["action_channel"].isin(["dm", "email"])]
    for _, r in rdo.iterrows():
        rows.append({
            "lead_type": "reseller", "name": _name(r), "contact_name": r.get("contact_name"),
            "handle_norm": r.get("handle_norm"), "email": r.get("email"),
            "phone": r.get("phone"), "city": r.get("city"),
            "channel": r["action_channel"],
            "action_type": {"Deals in flight": "advance_deal", "Revive warm": "re_engage",
                            "Revival": "re_engage", "Cold": "outreach"}.get(r.get("group_label"), "outreach"),
            "stage": r["stage"], "reason": r["reason"],
            "monthly_spend": r.get("est_monthly_spend_gbp"),
            "last_inbound_text": r.get("last_inbound_text"),
            "group_label": r.get("group_label"), "value": r.get("value"),
            "urgency": r.get("urgency"), "days_quiet": r.get("days_since_touch"),
            "priority": round(float(r["rank_score"]), 3),
            "dm_rank": r.get("dm_rank"), "due_date": due_date, "message": "",
            "lead_key": r["lead_key"],
        })

    sdo = shops[shops["next_step"].isin(["email", "call"])]
    for _, s in sdo.iterrows():
        rows.append({
            "lead_type": "shop", "name": _name(s), "contact_name": s.get("contact_name"),
            "handle_norm": s.get("handle_norm"), "email": s.get("email"),
            "phone": s.get("phone"), "city": s.get("city"),
            "channel": s["next_step"], "action_type": s["next_step"],
            "stage": s["stage"], "reason": s.get("step_note", ""),
            "monthly_spend": s.get("est_monthly_spend_gbp"),
            "last_inbound_text": s.get("last_inbound_text"),
            "group_label": "Shop", "value": s.get("value"),
            "urgency": s.get("urgency"), "days_quiet": s.get("days_since_touch"),
            "priority": round(float(s["priority"]), 3),
            "dm_rank": None, "due_date": due_date, "message": "",
            "lead_key": s["lead_key"],
        })

    df = pd.DataFrame(rows)
    if not df.empty:
        df["contact"] = df.apply(_contact, axis=1)
        df = df.sort_values(["lead_type", "priority"], ascending=[True, False]).reset_index(drop=True)
    return df


def write_queue(df: pd.DataFrame, out_dir: str | Path, run_date: str,
                visit_plan: pd.DataFrame | None = None) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"actions_{run_date}.csv"
    md_path = out_dir / f"brief_{run_date}.md"
    paths = {"csv": str(csv_path), "brief": str(md_path)}

    cols = [c for c in QUEUE_COLUMNS if c in df.columns]
    df.to_csv(csv_path, columns=cols, index=False)

    if visit_plan is not None and len(visit_plan):
        visits_path = out_dir / f"visits_{run_date}.csv"
        vcols = [c for c in ["city", "store_name", "stage", "priority", "phone", "email"]
                 if c in visit_plan.columns]
        visit_plan.to_csv(visits_path, columns=vcols, index=False)
        paths["visits"] = str(visits_path)

    md_path.write_text(_render_brief(df, run_date, visit_plan))
    return paths


_CHANNEL_TITLES = {"dm": "Instagram DMs", "email": "Emails", "call": "Calls"}


def _render_brief(df: pd.DataFrame, run_date: str, visit_plan: pd.DataFrame | None = None) -> str:
    lines = [f"# Sally — daily action brief · {run_date}", ""]
    if df.empty:
        lines.append("_No actions due today._")
    else:
        lines.append(f"**{len(df)} actions today.**  " +
                     " · ".join(f"{ch}: {n}" for ch, n in df["channel"].value_counts().items()))
        lines.append("")
        for ch in ["dm", "email", "call"]:
            sub = df[df["channel"] == ch]
            if sub.empty:
                continue
            lines.append(f"## {_CHANNEL_TITLES.get(ch, ch)} ({len(sub)})")
            lines.append("")
            lines.append("| # | Lead | Stage | Contact | Why |")
            lines.append("|---|---|---|---|---|")
            for i, (_, r) in enumerate(sub.iterrows(), 1):
                lines.append(f"| {i} | {r['name']} | {r['stage']} | {r['contact']} | {r['reason']} |")
            lines.append("")

    # forward-looking visit plan (planning, not a daily action)
    if visit_plan is not None and len(visit_plan):
        lines.append(f"## Visit plan — {len(visit_plan)} shops worth a trip, by city")
        lines.append("")
        for city, grp in visit_plan.groupby("city", sort=False):
            names = ", ".join(grp.sort_values("priority", ascending=False)["store_name"].astype(str))
            lines.append(f"- **{city}** ({len(grp)}): {names}")
        lines.append("")
    return "\n".join(lines)
