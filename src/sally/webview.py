"""Focused queue UI — process the day's outreach one lead at a time, plus two
kanban boards (resellers, stores) for an at-a-glance view of the pipeline.

The Queue tab is a single-task view: for each lead it shows who, why, the drafted
message and the signals behind its score, with Done (mark contacted -> cooldown),
Skip (move on, resurfaces), and Previous/Next to browse. The board tabs lay the
leads out by stage. A sidebar drives the pipeline (run day 1 / drop day 2 / reset)
and posts the Slack digest. Run with `make web`, then open http://localhost:8501.
"""

from __future__ import annotations

import glob
import json
import os

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from sally import notify, pipeline, store

load_dotenv()
DB = os.getenv("SALLY_DB", store.DEFAULT_DB)
FILE = os.getenv("SALLY_FILE", "data/raw/pipeline_data.xlsx")

BOARD_STAGES = ["New", "Contacted", "Replied", "Warm", "Call Booked",
                "Negotiating", "Won", "Ghosted", "Lost"]


# --- formatting helpers (NaN-safe; rows may be dicts or pandas Series) ----------

def _present(v) -> bool:
    return v is not None and not (isinstance(v, float) and pd.isna(v)) and str(v).strip() != ""


def _fmt_money(v) -> str | None:
    if not _present(v):
        return None
    v = float(v)
    return f"~£{v/1000:.1f}k/mo".replace(".0k", "k") if v >= 1000 else f"~£{int(v)}/mo"


def _display_name(a) -> str:
    for k in ("store_name", "handle_norm", "contact_name"):
        v = a.get(k)
        if _present(v):
            return str(v)
    return str(a.get("lead_key") or "lead")


def _contact(a: dict) -> str:
    return {"dm": f"@{a.get('handle_norm','')}", "email": a.get("email") or "",
            "call": a.get("phone") or a.get("city") or ""}.get(a["channel"], "")


# --- sidebar controls -----------------------------------------------------------

def _post_digest():
    counts = store.action_counts(db_path=DB)
    rc = store.run_counts(DB)["last_run"] or {}
    briefs = sorted(glob.glob(os.path.join(os.path.dirname(DB) or ".", "brief_*.md")))
    brief = briefs[-1] if briefs else "data/out/brief.md"
    return notify.send_digest({
        "actions_total": sum(counts.values()), "dm": counts.get("dm", 0),
        "email": counts.get("email", 0), "call": counts.get("call", 0),
        "top_visit_cities": {}, "new": rc.get("new_leads", 0),
        "updated": rc.get("updated_leads", 0),
        "cooldown": len(store.leads_in_cooldown(4, DB)),
    }, brief, rc.get("at", "")[:10] or "today")


def _sidebar():
    st.sidebar.header("Demo controls")
    if st.sidebar.button("▶️ Run Day 1 (main pipeline)", use_container_width=True):
        pipeline.run_pipeline(FILE, sheet="pipeline", db=DB); st.rerun()
    if st.sidebar.button("➕ Drop Day 2 (new leads)", use_container_width=True):
        pipeline.run_pipeline(FILE, sheet="new_drop_day2", db=DB); st.rerun()

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


# --- stats + run trace ----------------------------------------------------------

def _stats_header():
    rc = store.run_counts(DB)
    counts = store.action_counts(db_path=DB)
    last = rc["last_run"] or {}
    remaining = len(store.pending_actions(db_path=DB))
    queued = last.get("actions_total") or sum(counts.values())
    recently_contacted = len(store.leads_in_cooldown(4, DB))  # live: grows as you mark Done

    cols = st.columns(4)
    cols[0].metric("Leads in store", rc["leads_total"])
    cols[1].metric("Queued today", queued)
    cols[2].metric("Remaining", remaining)
    cols[3].metric("Recently contacted", recently_contacted,
                   help="Leads contacted (marked Done) in the last few days, on cooldown and not re-contacted yet.")
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
            f"- **Skipped** {t['cooldown_skipped']} recently-contacted (cooldown)\n"
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
    bits = [line]

    # the raw numbers behind the score
    sig = []
    m = _fmt_money(a.get("est_monthly_spend_gbp"))
    if m:
        sig.append(f"spend {m}")
    if _present(a.get("followers")):
        sig.append(f"{int(a['followers'])} followers")
    if _present(a.get("active_listings")):
        sig.append(f"{int(a['active_listings'])} listings")
    if _present(a.get("sales_velocity_30d")):
        sig.append(f"sells {int(a['sales_velocity_30d'])}/30d")
    if a.get("days_quiet") is not None:
        sig.append(f"last touch {int(a['days_quiet'])}d ago")
    else:
        sig.append("not yet contacted")
    if sig:
        bits.append("**Signals (feed the score):** " + " · ".join(sig))

    if _present(a.get("last_inbound_text")):
        bits.append(f"**Last reply:** \"{a['last_inbound_text']}\"")

    if (a.get("merged_count") or 1) > 1:
        bits.append(f"**Identity:** merged from {a['merged_count']} records "
                    f"({a.get('merged_lead_ids')}); stages seen: {a.get('merged_stages')}")
    else:
        bits.append("**Identity:** single record (no duplicates)")
    bits.append(f"**Channels available:** {a.get('available_channels') or '—'}")
    return "\n\n".join(bits)


# --- kanban board ---------------------------------------------------------------

def _board(lead_type: str):
    df = store.load_leads(DB)
    if df.empty or "lead_type" not in df.columns:
        st.info("No leads yet — run Day 1 from the sidebar."); return
    df = df[df["lead_type"] == lead_type]
    if df.empty:
        st.info(f"No {lead_type}s yet — run Day 1 from the sidebar."); return

    st.caption(f"{len(df)} {lead_type}{'s' if not df.empty else ''}, by stage. Cards show the highest-spend leads first.")
    cols = st.columns(len(BOARD_STAGES))
    for col, stage in zip(cols, BOARD_STAGES):
        sub = df[df["stage"] == stage].copy()
        sub["_sp"] = pd.to_numeric(sub["est_monthly_spend_gbp"], errors="coerce").fillna(0)
        sub = sub.sort_values("_sp", ascending=False)
        with col:
            st.markdown(f"**{stage}**  \n`{len(sub)} leads`")
            for _, r in sub.head(12).iterrows():
                with st.container(border=True):
                    st.markdown(f"**{_display_name(r)}**")
                    if lead_type == "reseller":
                        extra = f" · {int(r['followers'])} foll" if _present(r.get("followers")) else ""
                    else:
                        extra = f" · {r['city']}" if _present(r.get("city")) else ""
                    st.caption(f"{_fmt_money(r.get('est_monthly_spend_gbp')) or 'spend n/a'}{extra}")
            if len(sub) > 12:
                st.caption(f"+{len(sub) - 12} more")


# --- focused queue --------------------------------------------------------------

def _queue_view():
    _stats_header()
    _under_the_hood()

    pending = store.pending_actions(db_path=DB)
    counts = store.action_counts(db_path=DB)
    queued_today = sum(counts.values())

    chan = st.radio("Channel", ["All", "dm", "email", "call"], horizontal=True, key="chan")
    if st.session_state.get("_lastchan") != chan:
        st.session_state._lastchan = chan
        st.session_state.nav_i = 0
    filtered = [a for a in pending if chan == "All" or a["channel"] == chan]
    chan_total = queued_today if chan == "All" else counts.get(chan, 0)
    chan_done = chan_total - len(filtered)
    scope = "" if chan == "All" else f" ({chan})"

    st.divider()
    if not pending:
        st.success(f"🎉 All {queued_today} actions processed. Use the sidebar to run again."); return
    if not filtered:
        st.info(f"Nothing left in **{chan}** — switch the filter ({len(pending)} still pending elsewhere)."); return

    nav_i = max(0, min(st.session_state.get("nav_i", 0), len(filtered) - 1))
    st.session_state.nav_i = nav_i

    _, mid, _ = st.columns([1, 2, 1])
    with mid:
        a = filtered[nav_i]
        st.progress(chan_done / chan_total if chan_total else 0,
                    text=f"{chan_done} of {chan_total} processed{scope}")
        st.caption(f"Viewing {nav_i + 1} of {len(filtered)} in the queue")
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

        prev_c, done_c, skip_c, next_c = st.columns(4)
        if prev_c.button("⬅️ Previous", use_container_width=True, disabled=nav_i == 0):
            st.session_state.nav_i = nav_i - 1; st.rerun()
        if done_c.button("✅ Done", use_container_width=True):
            store.set_action_status(a["action_id"], "done", DB); st.rerun()
        if skip_c.button("⏭️ Skip", use_container_width=True):
            store.set_action_status(a["action_id"], "skipped", DB); st.rerun()
        if next_c.button("Next ➡️", use_container_width=True, disabled=nav_i >= len(filtered) - 1):
            st.session_state.nav_i = nav_i + 1; st.rerun()


def main() -> None:
    st.set_page_config(page_title="Sally — daily queue", page_icon="✉️", layout="wide")
    st.title("✉️ Sally — today's queue")
    _sidebar()
    tab_q, tab_r, tab_s = st.tabs(["📥 Queue", "🧑‍💻 Resellers board", "🏬 Stores board"])
    with tab_q:
        _queue_view()
    with tab_r:
        _board("reseller")
    with tab_s:
        _board("shop")


main()
