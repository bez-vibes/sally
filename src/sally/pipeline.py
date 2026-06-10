"""The run flow, callable from both the CLI and the UI.

ingest -> clean -> dedupe -> classify -> upsert -> skip cooldown -> score + sequence
-> draft -> record actions -> write queue -> Slack digest.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from . import store
from .classify import classify
from .clean import clean
from .draft import draft_all
from .identity import dedupe
from .ingest import load_batch
from .notify import send_digest
from .queue import build_action_rows, write_queue
from .score import as_of_date, score_resellers
from .sequence import sequence_shops

_DATE_COLS = ["first_seen_date", "last_touch_date"]


def _load_store_frame(db_path: str) -> pd.DataFrame:
    df = store.load_leads(db_path)
    for c in _DATE_COLS:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors="coerce")
    return df


def run_pipeline(file: str, sheet: str | None = None, db: str = store.DEFAULT_DB,
                 out: str = "data/out", dm_cap: int = 40, cooldown: int = 4) -> dict:
    """Run one batch end-to-end. Returns a summary dict (counts, paths, digest)."""
    run_id = "run_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    raw = load_batch(file, sheet=sheet)
    cleaned, _ = clean(raw)
    deduped, _ = dedupe(cleaned)
    classified, _ = classify(deduped)
    up = store.upsert_leads(classified, run_id, file=file,
                            batch=str(raw["_batch"].iloc[0]), db_path=db)

    allleads = _load_store_frame(db)
    allleads, _ = classify(allleads)
    as_of = as_of_date(allleads)

    cold = store.leads_in_cooldown(cooldown, db)
    eligible = allleads[~allleads["lead_key"].isin(cold)].copy()

    resellers, r_rep = score_resellers(eligible, dm_cap=dm_cap, as_of=as_of)
    shops, plan, s_rep = sequence_shops(eligible, as_of=as_of)

    actions = build_action_rows(resellers, shops, due_date=run_date)
    actions = draft_all(actions)
    for _, a in actions.iterrows():
        store.record_action(a["lead_key"], run_id, a["channel"], a["action_type"],
                            a["due_date"], a.get("message", ""), float(a["priority"]),
                            a["reason"], db_path=db)

    store.update_run_stats(run_id, len(actions), len(cold), db_path=db)

    paths = write_queue(actions, out, run_id, visit_plan=plan)
    ch = actions["channel"].value_counts().to_dict() if len(actions) else {}
    digest = send_digest({
        "actions_total": len(actions), "dm": ch.get("dm", 0),
        "email": ch.get("email", 0), "call": ch.get("call", 0),
        "top_visit_cities": s_rep["top_visit_cities"], "new": up["new"],
        "updated": up["updated"], "cooldown": len(cold),
    }, paths["brief"], run_date)

    return {"run_id": run_id, "run_date": run_date, "upsert": up,
            "reseller_report": r_rep, "shop_report": s_rep, "cooldown_count": len(cold),
            "actions_total": len(actions), "paths": paths, "digest": digest}
