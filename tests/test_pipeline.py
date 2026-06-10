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
