"""CLI — `sally run` ties the whole morning routine together.

  ingest -> clean -> dedupe -> classify -> upsert to store -> (skip cooldown) ->
  score resellers + sequence shops -> record actions -> write the daily queue.

Run it again with a new batch and it picks up new leads, skips anyone already
handled, and updates what to do next.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import typer
from rich.console import Console
from rich.table import Table

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

app = typer.Typer(add_completion=False, help="Sally — the daily outreach engine.")
console = Console()


@app.callback()
def _main():
    """Sally — the daily outreach engine."""

_DATE_COLS = ["first_seen_date", "last_touch_date"]


def _load_store_frame(db_path: str) -> pd.DataFrame:
    df = store.load_leads(db_path)
    for c in _DATE_COLS:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors="coerce")
    return df


@app.command()
def run(
    file: str = typer.Option("data/raw/pipeline_data.xlsx", help="xlsx/csv batch to ingest"),
    sheet: str = typer.Option(None, help="sheet name (xlsx); defaults to the first sheet"),
    db: str = typer.Option(store.DEFAULT_DB, help="SQLite state file"),
    out: str = typer.Option("data/out", help="output dir for the queue"),
    dm_cap: int = typer.Option(40, help="Instagram DMs per day"),
    cooldown: int = typer.Option(4, help="days before an actioned lead resurfaces"),
):
    """Run the daily routine against a batch and write today's action queue."""
    run_id = "run_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # 1-4: ingest -> clean -> dedupe -> classify -> upsert
    raw = load_batch(file, sheet=sheet)
    cleaned, clean_rep = clean(raw)
    deduped, dd_rep = dedupe(cleaned)
    classified, _ = classify(deduped)
    up = store.upsert_leads(classified, run_id, file=file, batch=str(raw["_batch"].iloc[0]), db_path=db)

    # 5: work over the WHOLE store (not just this batch), re-deriving classification
    allleads = _load_store_frame(db)
    allleads, _ = classify(allleads)
    as_of = as_of_date(allleads)

    # 6: drop leads in cooldown (already handled, not yet due)
    cold = store.leads_in_cooldown(cooldown, db)
    eligible = allleads[~allleads["lead_key"].isin(cold)].copy()

    # 7: score + sequence the eligible pool
    resellers, r_rep = score_resellers(eligible, dm_cap=dm_cap, as_of=as_of)
    shops, plan, s_rep = sequence_shops(eligible, as_of=as_of)

    # 8-9: assemble today's queue, draft each message, record actions so re-runs skip them
    actions = build_action_rows(resellers, shops, due_date=run_date)
    actions = draft_all(actions)
    for _, a in actions.iterrows():
        store.record_action(a["lead_key"], run_id, a["channel"], a["action_type"],
                            a["due_date"], a.get("message", ""), float(a["priority"]),
                            a["reason"], db_path=db)

    # 10: write outputs + post the Slack digest (or preview it if no webhook)
    paths = write_queue(actions, out, run_id, visit_plan=plan)
    ch = actions["channel"].value_counts().to_dict() if len(actions) else {}
    digest = send_digest({
        "actions_total": len(actions), "dm": ch.get("dm", 0),
        "email": ch.get("email", 0), "call": ch.get("call", 0),
        "top_visit_cities": s_rep["top_visit_cities"], "new": up["new"],
        "updated": up["updated"], "cooldown": len(cold),
    }, paths["brief"], run_date)

    _summary(up, r_rep, s_rep, plan, len(cold), actions, paths, run_date, digest)


def _summary(up, r_rep, s_rep, plan, n_cold, actions, paths, run_date, digest=None):
    t = Table(title=f"Sally run · {run_date}", show_header=False)
    t.add_row("Store", f"{up['leads_total']} leads ({up['new']} new, {up['updated']} updated, "
                       f"{up['stage_advanced']} advanced, {up['replies']} new replies)")
    t.add_row("In cooldown (skipped)", str(n_cold))
    t.add_row("Resellers", f"DM {r_rep['dm_today']} · email {r_rep['email_today']} · "
                          f"deferred {r_rep['deferred']}  (as of {r_rep['as_of']})")
    t.add_row("  DM by group", str(r_rep["dm_by_group"]))
    t.add_row("Shops", f"{s_rep['shops_active']} active · {s_rep['visit_ready']} visit-ready")
    t.add_row("  Top visit cities", str(s_rep["top_visit_cities"]))
    t.add_row("Actions queued today", str(len(actions)))
    t.add_row("Queue files", f"{paths['csv']}\n{paths['brief']}")
    if digest:
        status = "sent ✓" if digest.get("sent") else f"preview only ({digest.get('reason')})"
        t.add_row(f"Slack digest [{status}]", digest.get("preview", ""))
    console.print(t)


if __name__ == "__main__":
    app()
