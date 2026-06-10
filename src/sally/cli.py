"""CLI — `sally run` ties the whole morning routine together.

  ingest -> clean -> dedupe -> classify -> upsert to store -> (skip cooldown) ->
  score resellers + sequence shops -> record actions -> write the daily queue.

Run it again with a new batch and it picks up new leads, skips anyone already
handled, and updates what to do next.
"""

from __future__ import annotations

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

load_dotenv()  # pick up SLACK_WEBHOOK_URL / GEMINI_API_KEY etc. from .env

from . import store
from .pipeline import run_pipeline

app = typer.Typer(add_completion=False, help="Sally — the daily outreach engine.")
console = Console()


@app.callback()
def _main():
    """Sally — the daily outreach engine."""


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
    r = run_pipeline(file, sheet=sheet, db=db, out=out, dm_cap=dm_cap, cooldown=cooldown)
    _summary(r)


def _summary(r: dict):
    up, r_rep, s_rep, digest = r["upsert"], r["reseller_report"], r["shop_report"], r["digest"]
    t = Table(title=f"Sally run · {r['run_date']}", show_header=False)
    t.add_row("Store", f"{up['leads_total']} leads ({up['new']} new, {up['updated']} updated, "
                       f"{up['stage_advanced']} advanced, {up['replies']} new replies)")
    t.add_row("In cooldown (skipped)", str(r["cooldown_count"]))
    t.add_row("Resellers", f"DM {r_rep['dm_today']} · email {r_rep['email_today']} · "
                          f"deferred {r_rep['deferred']}  (as of {r_rep['as_of']})")
    t.add_row("  DM by group", str(r_rep["dm_by_group"]))
    t.add_row("Shops", f"{s_rep['shops_active']} active · {s_rep['visit_ready']} visit-ready")
    t.add_row("  Top visit cities", str(s_rep["top_visit_cities"]))
    t.add_row("Actions queued today", str(r["actions_total"]))
    t.add_row("Queue files", f"{r['paths']['csv']}\n{r['paths']['brief']}")
    if digest:
        status = "sent ✓" if digest.get("sent") else f"preview only ({digest.get('reason')})"
        t.add_row(f"Slack digest [{status}]", digest.get("preview", ""))
    console.print(t)


if __name__ == "__main__":
    app()
