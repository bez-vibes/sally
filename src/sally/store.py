"""Store — the SQLite state that gives Sally memory between runs.

This is what turns Sally from a one-off dashboard into something that can run
every morning: each lead is keyed on its stable `lead_key`, so a re-run never
re-adds or re-messages someone already handled.

Four tables:
  leads   — current state per lead_key: cleaned lead data + Sally's action cache
            (sally_last_action, next_action…) + manual override columns.
  events  — append-only log of what HAPPENED TO the lead (stage_change, reply_received,
            first_seen, override_set). The lead's history.
  actions — append-only log of what SALLY DID/RECOMMENDED (channel, drafted message,
            status drafted→approved→sent→skipped). The work-log the daily queue is
            built from, and what re-messaging idempotency keys off.
  runs    — one row per run (timestamp, file, new vs updated counts).

Update policy (agreed):
  * Rule A — non-regressing stage: on re-run a lead never slides backwards down the
    funnel (Won/Lost always override, since deals legitimately close or die). Every
    change is logged to `events`.
  * Manual overrides win: if a human set `manual_stage` / `manual_status`, that beats
    Sally's automatic logic.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .identity import STAGE_RANK

DEFAULT_DB = "data/out/sally.db"

# lead columns persisted from the deduped frame (everything except internal helpers)
_LEAD_FIELDS = [
    "lead_key", "lead_id", "source", "handle", "handle_norm", "store_name",
    "contact_name", "email", "email_valid", "phone", "phone_valid", "city", "country",
    "followers", "active_listings", "avg_listing_price_gbp", "sales_velocity_30d",
    "est_monthly_spend_gbp", "stage", "num_touches", "first_seen_date",
    "last_touch_date", "last_inbound_text", "assigned_bdr", "notes", "channel",
    "alt_emails", "alt_phones", "alt_handles", "merged_sources", "merged_batches",
    "merged_stages", "merge_conflict", "merged_notes", "alt_inbound_texts",
    "merged_lead_ids", "merged_count",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _conn(db_path: str | Path) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(db_path))
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def init_db(db_path: str | Path = DEFAULT_DB) -> None:
    with _conn(db_path) as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS leads (
                lead_key TEXT PRIMARY KEY,
                lead_id TEXT, source TEXT, handle TEXT, handle_norm TEXT,
                store_name TEXT, contact_name TEXT,
                email TEXT, email_valid INTEGER, phone TEXT, phone_valid INTEGER,
                city TEXT, country TEXT,
                followers REAL, active_listings REAL, avg_listing_price_gbp REAL,
                sales_velocity_30d REAL, est_monthly_spend_gbp REAL,
                stage TEXT, num_touches REAL,
                first_seen_date TEXT, last_touch_date TEXT,
                last_inbound_text TEXT, assigned_bdr TEXT, notes TEXT, channel TEXT,
                alt_emails TEXT, alt_phones TEXT, alt_handles TEXT,
                merged_sources TEXT, merged_batches TEXT, merged_stages TEXT,
                merge_conflict INTEGER, merged_notes TEXT, alt_inbound_texts TEXT,
                merged_lead_ids TEXT, merged_count INTEGER,
                -- Sally action cache (current state; full log in `actions`)
                sally_last_action TEXT, last_action_at TEXT, times_actioned INTEGER DEFAULT 0,
                next_action TEXT, next_action_date TEXT, priority_score REAL,
                -- manual overrides (set via UI; win over automatic logic)
                manual_stage TEXT, manual_status TEXT, snooze_until TEXT,
                override_note TEXT, override_at TEXT,
                -- bookkeeping
                first_added_at TEXT, first_added_run TEXT,
                last_seen_at TEXT, last_seen_run TEXT
            );

            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_key TEXT NOT NULL, run_id TEXT, at TEXT,
                type TEXT,            -- stage_change | reply_received | first_seen | override_set
                detail TEXT
            );

            CREATE TABLE IF NOT EXISTS actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_key TEXT NOT NULL, run_id TEXT, at TEXT,
                channel TEXT, action_type TEXT, due_date TEXT,
                message_draft TEXT,
                status TEXT DEFAULT 'drafted',  -- drafted | approved | sent | skipped | done
                priority_score REAL, reason TEXT
            );

            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY, at TEXT, file TEXT, batch TEXT,
                rows_in INTEGER, leads_total INTEGER,
                new_leads INTEGER, updated_leads INTEGER, stage_advanced INTEGER,
                actions_total INTEGER, skipped_cooldown INTEGER
            );

            CREATE INDEX IF NOT EXISTS idx_events_lead ON events(lead_key);
            CREATE INDEX IF NOT EXISTS idx_actions_lead ON actions(lead_key);
            CREATE INDEX IF NOT EXISTS idx_actions_status ON actions(status);
            """
        )


def _ser(v):
    """Serialise a pandas value for SQLite (NaT/NA -> None, Timestamp -> ISO date)."""
    if v is None or (not isinstance(v, (list, dict)) and pd.isna(v)):
        return None
    if isinstance(v, pd.Timestamp):
        return v.date().isoformat()
    if isinstance(v, (bool,)):
        return int(v)
    return v


def _resolve_stage(old: str | None, new: str | None) -> str:
    """Rule A: never regress. Won/Lost override. Returns the stage to keep."""
    if old is None:
        return new
    if new is None:
        return old
    if new in ("Won", "Lost"):
        return new
    if old in ("Won", "Lost"):
        return old
    return new if STAGE_RANK.get(new, -1) >= STAGE_RANK.get(old, -1) else old


def upsert_leads(df: pd.DataFrame, run_id: str, file: str = "", batch: str = "",
                 db_path: str | Path = DEFAULT_DB) -> dict:
    """Insert new leads, update existing ones (non-regressing stage), log changes.

    Returns a report: new / updated / stage_advanced / reply counts.
    """
    init_db(db_path)
    now = _now()
    new_n = upd_n = adv_n = reply_n = 0

    with _conn(db_path) as c:
        existing = {r["lead_key"]: dict(r) for r in c.execute(
            "SELECT lead_key, stage, last_inbound_text, manual_stage FROM leads"
        )}

        for _, row in df.iterrows():
            key = row["lead_key"]
            rec = {f: _ser(row.get(f)) for f in _LEAD_FIELDS if f in df.columns}

            if key not in existing:
                rec["first_added_at"] = now
                rec["first_added_run"] = run_id
                rec["last_seen_at"] = now
                rec["last_seen_run"] = run_id
                cols = ", ".join(rec.keys())
                ph = ", ".join("?" for _ in rec)
                c.execute(f"INSERT INTO leads ({cols}) VALUES ({ph})", list(rec.values()))
                c.execute("INSERT INTO events (lead_key, run_id, at, type, detail) VALUES (?,?,?,?,?)",
                          (key, run_id, now, "first_seen", f"stage={rec.get('stage')}"))
                new_n += 1
                continue

            # --- existing lead: refresh data, non-regressing stage, log changes ---
            prev = existing[key]
            incoming_stage = rec.get("stage")
            resolved = _resolve_stage(prev["stage"], incoming_stage)
            # manual override wins
            if prev.get("manual_stage"):
                resolved = prev["manual_stage"]
            rec["stage"] = resolved

            if resolved != prev["stage"]:
                adv_n += 1 if STAGE_RANK.get(resolved, -1) > STAGE_RANK.get(prev["stage"], -1) else 0
                c.execute("INSERT INTO events (lead_key, run_id, at, type, detail) VALUES (?,?,?,?,?)",
                          (key, run_id, now, "stage_change", f"{prev['stage']} -> {resolved}"))

            new_inbound = rec.get("last_inbound_text")
            if new_inbound and new_inbound != prev.get("last_inbound_text"):
                reply_n += 1
                c.execute("INSERT INTO events (lead_key, run_id, at, type, detail) VALUES (?,?,?,?,?)",
                          (key, run_id, now, "reply_received", str(new_inbound)[:200]))

            rec["last_seen_at"] = now
            rec["last_seen_run"] = run_id
            sets = ", ".join(f"{k} = ?" for k in rec.keys())
            c.execute(f"UPDATE leads SET {sets} WHERE lead_key = ?", list(rec.values()) + [key])
            upd_n += 1

        total = c.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        c.execute(
            "INSERT OR REPLACE INTO runs (run_id, at, file, batch, rows_in, leads_total, "
            "new_leads, updated_leads, stage_advanced) VALUES (?,?,?,?,?,?,?,?,?)",
            (run_id, now, file, batch, len(df), total, new_n, upd_n, adv_n),
        )

    return {"new": new_n, "updated": upd_n, "stage_advanced": adv_n,
            "replies": reply_n, "leads_total": total}


def load_leads(db_path: str | Path = DEFAULT_DB) -> pd.DataFrame:
    init_db(db_path)
    with _conn(db_path) as c:
        return pd.read_sql_query("SELECT * FROM leads", c)


def record_action(lead_key: str, run_id: str, channel: str, action_type: str,
                  due_date: str | None, message_draft: str, priority_score: float,
                  reason: str, db_path: str | Path = DEFAULT_DB) -> None:
    """Append a recommended action and refresh the lead's action cache."""
    now = _now()
    with _conn(db_path) as c:
        c.execute(
            "INSERT INTO actions (lead_key, run_id, at, channel, action_type, due_date, "
            "message_draft, status, priority_score, reason) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (lead_key, run_id, now, channel, action_type, due_date, message_draft,
             "drafted", priority_score, reason),
        )
        c.execute(
            "UPDATE leads SET sally_last_action = ?, last_action_at = ?, "
            "times_actioned = COALESCE(times_actioned,0) + 1, next_action = ?, "
            "next_action_date = ?, priority_score = ? WHERE lead_key = ?",
            (action_type, now, action_type, due_date, priority_score, lead_key),
        )


def has_pending_action(lead_key: str, db_path: str | Path = DEFAULT_DB) -> bool:
    """True if this lead already has a live (drafted/approved/sent) action — the
    re-messaging guard. A 'skipped'/'done' action does not block re-surfacing."""
    with _conn(db_path) as c:
        n = c.execute(
            "SELECT COUNT(*) FROM actions WHERE lead_key = ? AND status IN "
            "('drafted','approved','sent')", (lead_key,)
        ).fetchone()[0]
    return n > 0


def update_run_stats(run_id: str, actions_total: int, skipped_cooldown: int,
                     db_path: str | Path = DEFAULT_DB) -> None:
    """Record the run's actual queued + skipped-as-already-handled counts."""
    with _conn(db_path) as c:
        try:
            c.execute(
                "UPDATE runs SET actions_total = ?, skipped_cooldown = ? WHERE run_id = ?",
                (actions_total, skipped_cooldown, run_id),
            )
        except sqlite3.OperationalError:
            pass  # older db without the columns


def latest_run_id(db_path: str | Path = DEFAULT_DB) -> str | None:
    with _conn(db_path) as c:
        row = c.execute("SELECT run_id FROM runs ORDER BY at DESC LIMIT 1").fetchone()
    return row["run_id"] if row else None


def pending_actions(run_id: str | None = None, db_path: str | Path = DEFAULT_DB) -> list[dict]:
    """The queue to work through: drafted (not yet done/skipped) actions for a run
    (latest run if none given), joined to their lead, highest priority first."""
    init_db(db_path)
    with _conn(db_path) as c:
        run_id = run_id or latest_run_id(db_path)
        if not run_id:
            return []
        rows = c.execute(
            """
            SELECT a.id AS action_id, a.lead_key, a.channel, a.action_type,
                   a.message_draft, a.reason, a.priority_score, a.status,
                   l.store_name, l.handle_norm, l.contact_name, l.stage, l.city,
                   l.email, l.phone, l.last_inbound_text, l.est_monthly_spend_gbp
            FROM actions a JOIN leads l ON l.lead_key = a.lead_key
            WHERE a.run_id = ? AND a.status = 'drafted'
            ORDER BY a.priority_score DESC
            """,
            (run_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def set_action_status(action_id: int, status: str, db_path: str | Path = DEFAULT_DB) -> None:
    """Mark an action done/sent/skipped from the UI."""
    with _conn(db_path) as c:
        c.execute("UPDATE actions SET status = ? WHERE id = ?", (status, action_id))


def lead_events(lead_key: str, db_path: str | Path = DEFAULT_DB) -> list[dict]:
    """A lead's history (stage changes, replies, first-seen), newest first."""
    with _conn(db_path) as c:
        rows = c.execute(
            "SELECT at, type, detail FROM events WHERE lead_key = ? ORDER BY id DESC",
            (lead_key,),
        ).fetchall()
    return [dict(r) for r in rows]


def reset_actions_to_drafted(run_id: str | None = None, db_path: str | Path = DEFAULT_DB) -> int:
    """Soft demo reset: set a run's done/skipped actions back to 'drafted' so the
    queue refills. Returns the number reset. Does not touch lead/event history."""
    with _conn(db_path) as c:
        run_id = run_id or latest_run_id(db_path)
        if not run_id:
            return 0
        cur = c.execute(
            "UPDATE actions SET status = 'drafted' WHERE run_id = ? AND status != 'drafted'",
            (run_id,),
        )
        return cur.rowcount


def action_counts(run_id: str | None = None, db_path: str | Path = DEFAULT_DB) -> dict:
    """Channel split of all actions for a run (any status) — for the digest."""
    with _conn(db_path) as c:
        run_id = run_id or latest_run_id(db_path)
        rows = c.execute(
            "SELECT channel, COUNT(*) n FROM actions WHERE run_id = ? GROUP BY channel",
            (run_id,),
        ).fetchall()
    return {r["channel"]: r["n"] for r in rows}


def run_counts(db_path: str | Path = DEFAULT_DB) -> dict:
    """Headline stats for the UI: total leads + the latest run's row."""
    init_db(db_path)
    with _conn(db_path) as c:
        total = c.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        last = c.execute("SELECT * FROM runs ORDER BY at DESC LIMIT 1").fetchone()
    return {"leads_total": total, "last_run": dict(last) if last else None}


def leads_in_cooldown(cooldown_days: int = 4, db_path: str | Path = DEFAULT_DB) -> set[str]:
    """Lead keys that were actioned within `cooldown_days` and have NOT replied or
    advanced since — i.e. already handled, don't re-surface them today. A reply or
    stage change after the last action releases the lead immediately.

    This is what stops a re-run re-messaging the same people: yesterday's 40 sit in
    cooldown, so today's slots go to the next-best leads.
    """
    now = datetime.now(timezone.utc)
    in_cd: set[str] = set()
    with _conn(db_path) as c:
        # drafted/sent/done cool the lead; 'skipped' does NOT (a skip resurfaces it)
        rows = c.execute(
            "SELECT lead_key, MAX(at) AS last_at FROM actions "
            "WHERE status IN ('drafted','approved','sent','done') GROUP BY lead_key"
        ).fetchall()
        for r in rows:
            last_at = datetime.fromisoformat(r["last_at"])
            if (now - last_at).days >= cooldown_days:
                continue  # cooled down — eligible again
            changed = c.execute(
                "SELECT COUNT(*) FROM events WHERE lead_key = ? AND "
                "type IN ('reply_received','stage_change') AND at > ?",
                (r["lead_key"], r["last_at"]),
            ).fetchone()[0]
            if changed:
                continue  # replied/advanced since — re-surface
            in_cd.add(r["lead_key"])
    return in_cd
