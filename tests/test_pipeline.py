"""Core guarantees: stage cleaning, dedup merge (rule A), and run idempotency."""

import pandas as pd
import pytest

from sally.classify import classify
from sally.clean import clean, normalise_stage
from sally.identity import dedupe
from sally.ingest import EXPECTED_COLUMNS
from sally import store


def _frame(rows):
    df = pd.DataFrame(rows)
    for c in EXPECTED_COLUMNS:
        if c not in df.columns:
            df[c] = pd.NA
    df["_batch"] = "test"
    df["_loaded_at"] = "2026-01-01T00:00:00"
    return df


def test_stage_normalisation_keyword_and_unknown():
    assert normalise_stage("Closed Won") == "Won"
    assert normalise_stage("in negotiation") == "Negotiating"
    assert normalise_stage("no response") == "Ghosted"
    assert normalise_stage("call-booked") == "Call Booked"
    assert normalise_stage("brand new spelling we have never seen") == "New"  # contains 'new'
    assert normalise_stage("totally unmappable xyz") == "Unknown"


def test_date_year_inferred_from_data_not_hardcoded():
    df = _frame([
        {"lead_id": "1", "handle": "a", "stage": "new", "first_seen_date": "2025-12-01", "last_touch_date": "Dec 20"},
        {"lead_id": "2", "handle": "b", "stage": "new", "first_seen_date": "2026-01-15", "last_touch_date": "Jan 5"},
    ])
    c, _ = clean(df)
    # Dec -> 2025, Jan -> 2026, derived from the explicit dates in this frame
    assert c.loc[0, "last_touch_date"] == pd.Timestamp("2025-12-20")
    assert c.loc[1, "last_touch_date"] == pd.Timestamp("2026-01-05")


def test_dedupe_merges_on_handle_with_rule_a():
    df = _frame([
        {"lead_id": "1", "handle": "@dupe", "stage": "New", "num_touches": 5,
         "first_seen_date": "2026-01-01", "last_touch_date": "2026-01-10"},
        {"lead_id": "2", "handle": "dupe", "stage": "Negotiating", "num_touches": 4,
         "first_seen_date": "2026-01-05", "last_touch_date": "2026-02-01"},
    ])
    c, _ = clean(df)
    out, rep = dedupe(c)
    assert rep["rows_out"] == 1
    row = out.iloc[0]
    assert row["stage"] == "Negotiating"          # furthest-along wins
    assert row["num_touches"] == 5                 # max, not sum
    assert row["first_seen_date"] == pd.Timestamp("2026-01-01")  # earliest
    assert row["last_touch_date"] == pd.Timestamp("2026-02-01")  # latest


def test_classify_channel_by_data_not_label():
    df = _frame([
        {"lead_id": "1", "handle": "onlyhandle", "stage": "New", "followers": 1000},
        {"lead_id": "2", "store_name": "A Shop", "email": "a@shop.com", "phone": "+447000000000",
         "city": "London", "stage": "New"},
        {"lead_id": "3", "handle": "reseller2", "email": "r@x.com", "followers": 50, "stage": "New"},
    ])
    c, _ = clean(df)
    c, _ = classify(c)
    types = dict(zip(c["lead_id"], c["lead_type"]))
    assert types["1"] == "reseller" and types["2"] == "shop" and types["3"] == "reseller"
    # reseller #3 has a valid email -> reachable off the DM cap
    assert bool(c.loc[c.lead_id == "3", "reseller_has_email"].iloc[0]) is True


def test_drafting_keyless_falls_back_to_templates(monkeypatch):
    from sally import draft
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    cold = {"name": "vintagejo", "channel": "dm", "action_type": "outreach",
            "stage": "New", "last_inbound_text": None}
    msg, how = draft.draft_for(cold, cache={})
    assert how == "template" and "Fleek" in msg

    reengage = {"name": "vintagejo", "channel": "dm", "action_type": "re_engage",
                "stage": "Warm", "last_inbound_text": "what brands do you take?"}
    msg, how = draft.draft_for(reengage, cache={})
    assert how == "template" and "circling back" in msg  # LLM unavailable -> template

    call = {"name": "Old Rail", "channel": "call", "action_type": "call",
            "stage": "Warm", "reason": "warm — call to book a visit", "monthly_spend": 5000}
    msg, how = draft.draft_for(call, cache={})
    assert msg.startswith("Call Old Rail")


def test_slack_digest_keyless_previews_local_path(monkeypatch):
    from sally.notify import send_digest
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    r = send_digest({"actions_total": 5, "dm": 3, "email": 2, "call": 0,
                     "top_visit_cities": {}, "new": 5, "updated": 0, "cooldown": 0},
                    "data/out/brief_x.md", "2026-06-10")
    assert r["sent"] is False and r["reason"] == "no SLACK_WEBHOOK_URL"
    assert "data/out/brief_x.md" in r["preview"] and "5 actions today" in r["preview"]


def test_focused_queue_done_cools_skip_resurfaces(tmp_path):
    db = str(tmp_path / "q.db")
    df = _frame([
        {"lead_id": "1", "handle": "x", "stage": "Replied", "est_monthly_spend_gbp": 5000,
         "last_touch_date": "2026-02-01"},
        {"lead_id": "2", "handle": "y", "stage": "Warm", "est_monthly_spend_gbp": 6000,
         "last_touch_date": "2026-02-02"},
    ])
    c, _ = clean(df); out, _ = dedupe(c); out, _ = classify(out)
    store.upsert_leads(out, "run1", db_path=db)
    store.record_action("h:x", "run1", "dm", "re_engage", "2026-02-03", "hi x", 0.9, "why x", db_path=db)
    store.record_action("h:y", "run1", "dm", "re_engage", "2026-02-03", "hi y", 0.8, "why y", db_path=db)

    q = store.pending_actions("run1", db)
    assert len(q) == 2 and q[0]["message_draft"] == "hi x"  # priority ordered

    done_id = [a["action_id"] for a in q if a["lead_key"] == "h:x"][0]
    skip_id = [a["action_id"] for a in q if a["lead_key"] == "h:y"][0]
    store.set_action_status(done_id, "done", db)
    store.set_action_status(skip_id, "skipped", db)

    cold = store.leads_in_cooldown(4, db)
    assert "h:x" in cold        # done -> cooled, won't resurface
    assert "h:y" not in cold    # skipped -> resurfaces next run
    assert store.pending_actions("run1", db) == []  # both cleared from today's queue


def test_demo_helpers(tmp_path):
    db = str(tmp_path / "d.db")
    df = _frame([{"lead_id": "1", "handle": "x", "stage": "Replied", "est_monthly_spend_gbp": 5000,
                  "last_touch_date": "2026-02-01"}])
    c, _ = clean(df); out, _ = dedupe(c); out, _ = classify(out)
    store.upsert_leads(out, "run1", db_path=db)
    store.record_action("h:x", "run1", "dm", "re_engage", "2026-02-03", "hi", 0.9, "why", db_path=db)

    assert store.action_counts(db_path=db) == {"dm": 1}
    assert store.run_counts(db)["leads_total"] == 1
    evs = store.lead_events("h:x", db)
    assert any(e["type"] == "first_seen" for e in evs)

    q = store.pending_actions("run1", db)
    store.set_action_status(q[0]["action_id"], "done", db)
    assert store.pending_actions("run1", db) == []      # done -> off the queue
    assert store.reset_actions_to_drafted("run1", db) == 1
    assert len(store.pending_actions("run1", db)) == 1   # soft reset -> back on the queue


def test_rerun_is_idempotent(tmp_path):
    db = str(tmp_path / "t.db")
    df = _frame([
        {"lead_id": "1", "handle": "x", "stage": "Replied", "followers": 100,
         "est_monthly_spend_gbp": 5000, "last_touch_date": "2026-02-01"},
        {"lead_id": "2", "handle": "y", "stage": "Warm", "followers": 200,
         "est_monthly_spend_gbp": 6000, "last_touch_date": "2026-02-02"},
    ])
    c, _ = clean(df)
    out, _ = dedupe(c)
    out, _ = classify(out)

    r1 = store.upsert_leads(out, "run1", db_path=db)
    r2 = store.upsert_leads(out, "run2", db_path=db)
    assert r1["new"] == 2 and r2["new"] == 0          # re-run adds nothing
    assert store.load_leads(db).lead_key.nunique() == 2

    # action one lead, then it should be in cooldown
    store.record_action("h:x", "run2", "dm", "re_engage", "2026-02-03", "", 0.9, "test", db_path=db)
    assert "h:x" in store.leads_in_cooldown(4, db)
    assert "h:y" not in store.leads_in_cooldown(4, db)
