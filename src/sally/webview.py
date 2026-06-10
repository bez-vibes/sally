"""Focused queue UI — process the day's outreach one lead at a time.

Not a dashboard: a single-task view. For each lead it shows who, why, the drafted
message and its history, with two controls:
  - Done  -> mark done (enters cooldown; won't resurface)
  - Skip  -> move on without sending (lead stays pending, resurfaces next run)
Both write straight to SQLite.

A sidebar of demo controls drives the pipeline (run day 1 / drop day 2 / reset)
and posts the Slack digest. Run with: `make web` (or
`streamlit run src/sally/webview.py`), then open http://localhost:8501.
"""

from __future__ import annotations

import glob
import os
from collections import Counter
from datetime import datetime, timezone

import streamlit as st
from dotenv import load_dotenv

from sally import notify, pipeline, store

load_dotenv()
DB = os.getenv("SALLY_DB", store.DEFAULT_DB)
FILE = os.getenv("SALLY_FILE", "data/raw/pipeline_data.xlsx")


def _display_name(a: dict) -> str:
    return a.get("store_name") or a.get("handle_norm") or a.get("contact_name") or a.get("lead_key")


def _contact(a: dict) -> str:
    return {"dm": f"@{a.get('handle_norm','')}", "email": a.get("email") or "",
            "call": a.get("phone") or a.get("city") or ""}.get(a["channel"], "")


def _reload():
    st.session_state.queue = store.pending_actions(db_path=DB)
    st.session_state.i = 0


def _post_digest():
    counts = store.action_counts(db_path=DB)
    rc = store.run_counts(DB)["last_run"] or {}
    briefs = sorted(glob.glob(os.path.join(os.path.dirname(DB), "brief_*.md")))
    brief = briefs[-1] if briefs else "data/out/brief.md"
    return notify.send_digest({
        "actions_total": sum(counts.values()), "dm": counts.get("dm", 0),
        "email": counts.get("email", 0), "call": counts.get("call", 0),
        "top_visit_cities": {}, "new": rc.get("new_leads", 0),
        "updated": rc.get("updated_leads", 0),
        "cooldown": len(store.leads_in_cooldown(4, DB)),
    }, brief, datetime.now(timezone.utc).strftime("%Y-%m-%d"))


def _sidebar():
    st.sidebar.header("Demo controls")
    if st.sidebar.button("▶️ Run Day 1 (main pipeline)", use_container_width=True):
        pipeline.run_pipeline(FILE, sheet="pipeline", db=DB)
        _reload(); st.rerun()
    if st.sidebar.button("➕ Drop Day 2 (new leads)", use_container_width=True):
        pipeline.run_pipeline(FILE, sheet="new_drop_day2", db=DB)
        _reload(); st.rerun()

    st.sidebar.divider()
    if st.sidebar.button("🔁 Soft reset (refill queue)", use_container_width=True):
        n = store.reset_actions_to_drafted(db_path=DB)
        _reload(); st.sidebar.success(f"Reset {n} actions"); st.rerun()
    if st.sidebar.button("🧨 Hard reset (wipe DB)", use_container_width=True):
        if os.path.exists(DB):
            os.remove(DB)
        st.session_state.queue = []; st.session_state.i = 0
        st.sidebar.warning("DB wiped — run Day 1 to rebuild"); st.rerun()

    st.sidebar.divider()
    if st.sidebar.button("📤 Post Slack digest", use_container_width=True):
        r = _post_digest()
        (st.sidebar.success if r["sent"] else st.sidebar.info)(
            "Posted to Slack ✓" if r["sent"] else f"Preview only ({r.get('reason')})")


def _stats_header():
    rc = store.run_counts(DB)
    counts = store.action_counts(db_path=DB)
    last = rc["last_run"] or {}
    remaining = len(store.pending_actions(db_path=DB))
    queued = last.get("actions_total") or sum(counts.values())
    skipped = last.get("skipped_cooldown") or 0

    cols = st.columns(4)
    cols[0].metric("Leads in store", rc["leads_total"])
    cols[1].metric("Queued today", queued)
    cols[2].metric("Remaining", remaining)
    cols[3].metric("Skipped (already handled)", skipped)
    st.caption(
        f"Latest run: {last.get('new_leads',0)} new · {last.get('updated_leads',0)} updated  |  "
        f"queued by channel — DM {counts.get('dm',0)} · email {counts.get('email',0)} "
        f"· call {counts.get('call',0)}"
    )


def main() -> None:
    st.set_page_config(page_title="Sally — daily queue", page_icon="✉️", layout="centered")
    st.title("✉️ Sally — today's queue")
    _sidebar()

    if "queue" not in st.session_state:
        _reload()
    _stats_header()

    # channel filter
    chan = st.radio("Channel", ["All", "dm", "email", "call"], horizontal=True, key="chan")
    if st.session_state.get("_lastchan") != chan:
        st.session_state._lastchan = chan
        st.session_state.i = 0
    queue = [a for a in st.session_state.queue if chan == "All" or a["channel"] == chan]

    st.divider()
    if not queue:
        st.info("No pending actions for this filter. Use the sidebar to run the pipeline.")
        return
    i = st.session_state.i
    if i >= len(queue):
        st.success(f"🎉 All done — {len(queue)} actions processed.")
        return

    a = queue[i]
    st.progress(i / len(queue), text=f"{i} of {len(queue)} done")
    icon = {"dm": "📱 Instagram DM", "email": "✉️ Email", "call": "📞 Call"}.get(a["channel"], a["channel"])
    st.caption(f"{icon}  ·  {a.get('stage','')}")
    st.subheader(_display_name(a))
    st.write(f"**Why:** {a.get('reason','')}")
    st.write(f"**Contact:** {_contact(a)}")

    label = "Call note" if a["channel"] == "call" else "Message"
    st.text_area(label, value=a.get("message_draft") or "", height=160, key=f"msg_{a['action_id']}")

    with st.expander("Lead history"):
        evs = store.lead_events(a["lead_key"], DB)
        if evs:
            for e in evs:
                st.write(f"- `{e['at'][:10]}` **{e['type']}** — {e['detail']}")
        else:
            st.caption("No history yet.")

    c1, c2, _ = st.columns([1, 1, 3])
    if c1.button("✅ Done", use_container_width=True):
        store.set_action_status(a["action_id"], "done", DB)
        st.session_state.i += 1; st.rerun()
    if c2.button("⏭️ Skip", use_container_width=True):
        store.set_action_status(a["action_id"], "skipped", DB)
        st.session_state.i += 1; st.rerun()


main()
