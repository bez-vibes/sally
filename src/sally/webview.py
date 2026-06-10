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
import json
import os
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
        st.rerun()
    if st.sidebar.button("➕ Drop Day 2 (new leads)", use_container_width=True):
        pipeline.run_pipeline(FILE, sheet="new_drop_day2", db=DB)
        st.rerun()

    st.sidebar.divider()
    if st.sidebar.button("🔁 Soft reset (refill queue)", use_container_width=True):
        n = store.reset_actions_to_drafted(db_path=DB)
        st.sidebar.success(f"Reset {n} actions"); st.rerun()
    if st.sidebar.button("🧨 Hard reset (wipe DB)", use_container_width=True):
        out_dir = os.path.dirname(DB) or "."
        for f in [DB, *glob.glob(os.path.join(out_dir, "trace_*.json")),
                  *glob.glob(os.path.join(out_dir, "actions_*.csv")),
                  *glob.glob(os.path.join(out_dir, "brief_*.md")),
                  *glob.glob(os.path.join(out_dir, "visits_*.csv"))]:
            if os.path.exists(f):
                os.remove(f)
        st.sidebar.warning("Wiped — run Day 1 to rebuild"); st.rerun()

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


def _latest_trace() -> dict | None:
    files = sorted(glob.glob(os.path.join(os.path.dirname(DB) or ".", "trace_*.json")))
    if not files:
        return None
    try:
        return json.loads(open(files[-1]).read())
    except Exception:
        return None


def _under_the_hood():
    t = _latest_trace()
    if not t:
        return
    c, d, cl, sc, up = t["clean"], t["dedupe"], t["classify"], t["score"], t["store"]
    by_group = ", ".join(f"{n} {k.lower()}" for k, n in sc["dm_by_group"].items()) or "—"
    methods = ", ".join(f"{n} {k}" for k, n in t["draft_methods"].items()) or "—"
    with st.expander(f"⚙️ Under the hood — what the last run did (batch: {t['batch']})", expanded=True):
        st.markdown(
            f"- **Ingested** {t['ingested_rows']} rows\n"
            f"- **Cleaned** {c['stage_labels_in']} stage spellings → {c['canonical_stages']} canonical · "
            f"{c['emails_repaired']} emails repaired · {c['phones_flagged']} phones flagged\n"
            f"- **Deduped** {d['rows_in']} → {d['rows_out']} ({d['duplicates_removed']} duplicates merged across {d['groups_merged']} groups)\n"
            f"- **Classified** {cl['resellers']} resellers / {cl['shops']} shops "
            f"({cl['reseller_has_email']} resellers reachable off the DM cap by email)\n"
            f"- **Store** {up['new']} new · {up['updated']} updated · {up['stage_advanced']} advanced · "
            f"{up['replies']} new replies → {up['leads_total']} total leads\n"
            f"- **Skipped** {t['cooldown_skipped']} already-handled (cooldown)\n"
            f"- **Scored** {sc['dm']} DM ({by_group}) · {sc['email']} email · {sc['deferred']} deferred\n"
            f"- **Drafted** {t['actions_total']} messages ({methods})"
        )


def _explain(a: dict) -> str:
    line = f"**Triage:** {a.get('group_label') or '—'}"
    if a.get("priority_score") is not None:
        line += f" · priority {a['priority_score']:.2f}"
    if a.get("value") is not None:
        line += f" · value p{int(a['value'] * 100)}"
    if a.get("urgency") is not None:
        line += f" · urgency {a['urgency']:.2f}"
    if a.get("days_quiet") is not None:
        line += f" · {int(a['days_quiet'])}d quiet"
    bits = [line]
    if (a.get("merged_count") or 1) > 1:
        bits.append(f"**Identity:** merged from {a['merged_count']} records "
                    f"({a.get('merged_lead_ids')}); stages seen: {a.get('merged_stages')}")
    else:
        bits.append("**Identity:** single record (no duplicates)")
    bits.append(f"**Channels available:** {a.get('available_channels') or '—'}")
    return "\n\n".join(bits)


def main() -> None:
    st.set_page_config(page_title="Sally — daily queue", page_icon="✉️", layout="centered")
    st.title("✉️ Sally — today's queue")
    _sidebar()
    _stats_header()
    _under_the_hood()

    # always read the DB — the current lead is the highest-priority still-pending action
    pending = store.pending_actions(db_path=DB)
    counts = store.action_counts(db_path=DB)
    queued_today = sum(counts.values())

    chan = st.radio("Channel", ["All", "dm", "email", "call"], horizontal=True, key="chan")
    filtered = [a for a in pending if chan == "All" or a["channel"] == chan]

    # progress reflects the current filter: "All" -> whole queue; a channel -> that channel
    chan_total = queued_today if chan == "All" else counts.get(chan, 0)
    chan_pending = len(filtered)
    chan_done = chan_total - chan_pending
    scope = "" if chan == "All" else f" ({chan})"

    st.divider()
    if not pending:
        st.success(f"🎉 All {queued_today} actions processed. Use the sidebar to run again.")
        return
    if not filtered:
        st.info(f"Nothing left in **{chan}** — switch the filter ({len(pending)} still pending elsewhere).")
        return

    a = filtered[0]
    st.progress(chan_done / chan_total if chan_total else 0,
                text=f"{chan_done} of {chan_total} processed{scope}")
    icon = {"dm": "📱 Instagram DM", "email": "✉️ Email", "call": "📞 Call"}.get(a["channel"], a["channel"])
    st.caption(f"{icon}  ·  {a.get('stage','')}")
    st.subheader(_display_name(a))
    st.write(f"**Why:** {a.get('reason','')}")
    st.write(f"**Contact:** {_contact(a)}")

    label = "Call note" if a["channel"] == "call" else "Message"
    st.text_area(label, value=a.get("message_draft") or "", height=160, key=f"msg_{a['action_id']}")

    with st.expander("🔍 Why this lead"):
        st.markdown(_explain(a))

    with st.expander("🕓 Lead history"):
        evs = store.lead_events(a["lead_key"], DB)
        if evs:
            for e in evs:
                st.write(f"- `{e['at'][:10]}` **{e['type']}** — {e['detail']}")
        else:
            st.caption("No history yet.")

    c1, c2, _ = st.columns([1, 1, 3])
    if c1.button("✅ Done", use_container_width=True):
        store.set_action_status(a["action_id"], "done", DB)
        st.rerun()
    if c2.button("⏭️ Skip", use_container_width=True):
        store.set_action_status(a["action_id"], "skipped", DB)
        st.rerun()


main()
