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
import streamlit.components.v1 as components
from dotenv import load_dotenv

from sally import notify, pipeline, store
from sally.intent import INTENT_LABEL, classify_intent
from sally.research import research_signals
from sally.score import score_components, score_total

load_dotenv()
DB = os.getenv("SALLY_DB", store.DEFAULT_DB)
FILE = os.getenv("SALLY_FILE", "data/raw/pipeline_data.xlsx")

BOARD_STAGES = ["New", "Contacted", "Replied", "Warm", "Call Booked",
                "Negotiating", "Won", "Ghosted", "Lost"]

STAGE_COLOURS = {
    "New": "#9aa0a6", "Contacted": "#6b7a8f", "Replied": "#4a90d9", "Warm": "#e8943a",
    "Call Booked": "#3a78c2", "Negotiating": "#7e57c2", "Won": "#2e9e5b",
    "Ghosted": "#8d6e63", "Lost": "#c0504d",
}

APP_CSS = """
<style>
/* calmer chrome */
#MainMenu, footer, [data-testid="stToolbar"] {visibility:hidden;}
[data-testid="stMainBlockContainer"]{padding-top:2.2rem; padding-bottom:3rem; max-width:1500px;}
html, body, [class*="css"]{font-family:'Inter','SF Pro Text',-apple-system,sans-serif;}

/* app header */
.app-h{display:flex;align-items:baseline;gap:12px;margin-bottom:2px;}
.app-h .t{font-size:24px;font-weight:700;color:#1f2430;}
.app-h .s{font-size:13px;color:#8a8f98;}

/* metric cards */
[data-testid="stMetric"]{background:#f7f9fc;border:1px solid #e7ebf3;border-radius:10px;
    padding:12px 14px;}
[data-testid="stMetricLabel"]{color:#8a8f98;font-size:12px;}

/* bordered containers -> cards */
[data-testid="stVerticalBlockBorderWrapper"]{border-radius:12px;border-color:#e7ebf3;}

/* buttons */
.stButton button{border-radius:8px;font-weight:600;border-color:#e7ebf3;}

/* board lanes + pills + panel */
.lane{color:#fff;padding:5px 10px;border-radius:7px;font-weight:600;font-size:12.5px;
      margin-bottom:8px;text-align:center;letter-spacing:.02em;}
.chip{background:#eef1f6;color:#3a4150;padding:1px 8px;border-radius:10px;font-size:12px;}
.badge{font-size:12px;margin-left:4px;}
.sec{font-size:10.5px;letter-spacing:.06em;text-transform:uppercase;color:#9aa0ac;
     margin:11px 0 3px;font-weight:700;}
.pill{display:inline-block;background:#eef1f6;color:#3a4150;padding:2px 10px;border-radius:12px;
      font-size:12px;margin:2px 5px 2px 0;}
.pill-pos{background:#e6f4ea;color:#1e7e44;}
.pill-neg{background:#fde8e8;color:#b3261e;}
.pill-neu{background:#eef0f3;color:#555;}
.why{background:#f4f6fb;border-left:3px solid #4a6cf7;padding:9px 13px;border-radius:6px;
     font-size:13.5px;line-height:1.35;margin:2px 0;}
.score{position:relative;cursor:help;display:inline-block;color:#fff;border-radius:13px;
       padding:7px 16px;font-size:30px;font-weight:800;line-height:1;text-align:center;min-width:62px;}
.score .l{display:block;font-size:9.5px;font-weight:700;letter-spacing:.09em;
          text-transform:uppercase;margin-top:4px;opacity:.92;}
.tip{visibility:hidden;opacity:0;position:absolute;top:110%;right:0;width:262px;
     background:#1f2430;color:#fff;font-size:11.5px;font-weight:500;line-height:1.45;
     letter-spacing:normal;text-transform:none;text-align:left;padding:11px 13px;border-radius:9px;
     z-index:1000;box-shadow:0 6px 20px rgba(15,23,42,.25);transition:opacity .1s;}
.score:hover .tip{visibility:visible;opacity:1;}
.bkhd{font-weight:700;font-size:10px;letter-spacing:.05em;text-transform:uppercase;opacity:.65;margin-bottom:6px;}
.bkrow{display:flex;justify-content:space-between;gap:10px;font-weight:600;}
.bkp{font-variant-numeric:tabular-nums;flex:none;}
.bkd{font-size:10px;opacity:.72;margin:1px 0 6px;font-weight:400;}
.bktot{border-top:1px solid rgba(255,255,255,.22);margin-top:4px;padding-top:6px;
       font-weight:700;display:flex;justify-content:space-between;}

/* sidebar last-run summary */
.runsum{background:#f7f9fc;border:1px solid #e7ebf3;border-radius:10px;padding:10px 12px;
        font-size:12px;line-height:1.55;color:#3a4150;}
.runsum b{color:#1f2430;}
.runsum .hd{font-size:10.5px;letter-spacing:.06em;text-transform:uppercase;color:#9aa0ac;
            font-weight:700;margin-bottom:4px;}

/* kanban */
.klane{background:#f7f9fc;border:1px solid #eef1f6;border-radius:12px;padding:11px 11px 5px;min-height:90px;}
.kcard{background:#fff;border:1px solid #e7ebf3;border-left-width:4px;border-radius:9px;
       padding:10px 12px;margin-bottom:10px;box-shadow:0 1px 2px rgba(20,30,60,.04);}
.krow{display:flex;justify-content:space-between;align-items:center;gap:8px;}
.kname{font-weight:600;font-size:13.5px;color:#1f2430;line-height:1.3;flex:1;min-width:0;
       overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.kscore{color:#fff;border-radius:8px;padding:2px 8px;font-size:13px;font-weight:800;flex:none;}
.kmeta{margin-top:6px;font-size:12px;color:#6b7280;}
.kmore{font-size:11px;color:#9aa0ac;text-align:center;padding:4px;}
.bdg{display:inline-block;border-radius:10px;padding:1px 7px;font-size:11px;margin-left:4px;}
.bdg-pos{background:#e6f4ea;color:#1e7e44;} .bdg-neu{background:#eef0f3;color:#555;}
.bdg-book{background:#e8eefe;color:#3a52cc;}
</style>
"""


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
        "pipeline_spend": store.queue_value(db_path=DB)["total"],
    }, brief, rc.get("at", "")[:10] or "today")


def _sidebar():
    st.sidebar.header("Demo controls")
    if st.sidebar.button("▶️ Run Day 1 (main pipeline)", use_container_width=True):
        pipeline.run_pipeline(FILE, sheet="pipeline", db=DB); st.rerun()
    if st.sidebar.button("➕ Drop Day 2 (new leads)", use_container_width=True):
        pipeline.run_pipeline(FILE, sheet="new_drop_day2", db=DB); st.rerun()
    if st.sidebar.button("🌱 Add real seed leads", use_container_width=True):
        pipeline.run_pipeline("data/raw/seed_real_leads.csv", db=DB); st.rerun()

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

    st.sidebar.divider()
    _sidebar_run_summary()


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
    val = store.queue_value(db_path=DB)
    money = (f"💷 today's queue reaches **~£{val['total']/1000:.0f}k/mo** of buyer spend "
             f"(~£{val['dm']/1000:.0f}k/mo in the {counts.get('dm',0)} DMs)") if val["total"] else ""
    st.caption(
        f"{money}  \nLatest run: {last.get('new_leads',0)} new · {last.get('updated_leads',0)} updated  |  "
        f"queued — DM {counts.get('dm',0)} · email {counts.get('email',0)} · call {counts.get('call',0)}"
    )


def _latest_trace() -> dict | None:
    files = sorted(glob.glob(os.path.join(os.path.dirname(DB) or ".", "trace_*.json")))
    if not files:
        return None
    try:
        return json.loads(open(files[-1]).read())
    except Exception:
        return None


def _sidebar_run_summary():
    """Compact 'under the hood' funnel, in the sidebar where run-metadata belongs."""
    t = _latest_trace()
    if not t:
        return
    c, d, cl, sc, up = t["clean"], t["dedupe"], t["classify"], t["score"], t["store"]
    st.sidebar.markdown(
        "<div class='runsum'>"
        f"<div class='hd'>Under the hood · last run ({t['batch']})</div>"
        f"Ingested <b>{t['ingested_rows']}</b> → cleaned ({c['stage_labels_in']}→{c['canonical_stages']} stages) "
        f"→ deduped <b>{d['rows_out']}</b> (−{d['duplicates_removed']} dupes)<br>"
        f"<b>{cl['resellers']}</b> resellers · <b>{cl['shops']}</b> shops "
        f"({cl['reseller_has_email']} off-cap by email)<br>"
        f"<b>{up['new']}</b> new · {up['updated']} updated · {t['cooldown_skipped']} skipped (cooldown)<br>"
        f"Scored <b>{sc['dm']}</b> DM · {sc['email']} email · {sc['deferred']} deferred<br>"
        f"Drafted <b>{t['actions_total']}</b> messages"
        "</div>", unsafe_allow_html=True)


def _pills(items, cls="pill") -> str:
    return "".join(f"<span class='{cls}'>{x}</span>" for x in items if x)


def _score_colour(s: int) -> str:
    return "#2e9e5b" if s >= 75 else "#e8943a" if s >= 50 else "#6b7a8f" if s >= 30 else "#9aa0ac"


def _breakdown_html(a: dict) -> str:
    """The A+B breakdown (each component's points + the real-world driver behind it)."""
    comps = score_components(a)
    raw = sum(c["points"] for c in comps)
    total = score_total(comps)
    rows = "<div class='bkhd'>How this score is built</div>"
    for c in comps:
        p = c["points"]
        sign = f"+{p}" if p > 0 else str(p)
        rows += f"<div class='bkrow'><span>{c['label']}</span><span class='bkp'>{sign} pts</span></div>"
        if c["driver"]:
            rows += f"<div class='bkd'>← {c['driver']}</div>"
    cap = f"  (capped from {raw})" if raw != total else ""
    rows += f"<div class='bktot'><span>Total</span><span>{total} / 100{cap}</span></div>"
    return rows


def _score_badge(a: dict) -> str:
    s = int(round((a.get("priority_score") or 0) * 100))
    tier = "hot" if s >= 75 else "warm" if s >= 50 else "cool" if s >= 30 else "cold"
    return (f"<div class='score' style='background:{_score_colour(s)}'>"
            f"{s}<span class='l'>{tier}</span><span class='tip'>{_breakdown_html(a)}</span></div>")


def _account_panel(a: dict):
    """The convergence stack (fit, timing, intent, research) + identity/contact.
    Score and the headline reason are shown by the caller in the card header."""
    # FIT — the buying-power signals
    fit = []
    m = _fmt_money(a.get("est_monthly_spend_gbp"))
    if m:
        fit.append(m)
    for col, lab in [("followers", "followers"), ("active_listings", "listings"),
                     ("sales_velocity_30d", "sold/30d")]:
        if _present(a.get(col)):
            fit.append(f"{int(a[col])} {lab}")
    if a.get("value") is not None:
        fit.append(f"value p{int(a['value']*100)}")
    if fit:
        st.markdown("<div class='sec'>Fit</div>" + _pills(fit), unsafe_allow_html=True)

    # TIMING
    timing = [a.get("stage", "")]
    if a.get("days_quiet") is not None:
        timing.append(f"quiet {int(a['days_quiet'])}d")
    elif a.get("group_label") == "Cold":
        timing.append("not yet contacted")
    st.markdown("<div class='sec'>Timing</div>" + _pills(timing), unsafe_allow_html=True)

    # INTENT — from the last reply
    bucket = classify_intent(a.get("last_inbound_text"))[0]
    cls = {"buying": "pill pill-pos", "scheduling": "pill pill-pos", "qualifying": "pill",
           "objection": "pill pill-neg", "deferral": "pill pill-neg",
           "none": "pill pill-neu"}[bucket]
    html = f"<span class='{cls}'>{INTENT_LABEL[bucket]}</span>"
    if _present(a.get("last_inbound_text")):
        html += f"<span class='pill pill-neu'>“{a['last_inbound_text']}”</span>"
    st.markdown("<div class='sec'>Intent (from last reply)</div>" + html, unsafe_allow_html=True)

    # RESEARCH — dated + sourced, boost-only
    rs = research_signals(a)
    st.markdown("<div class='sec'>Research signals</div>", unsafe_allow_html=True)
    if rs["signals"]:
        for s in rs["signals"]:
            st.markdown(f"- {s['claim']}  \n  [{s['source_label']}]({s['source_url']}) · {s['date']}")
    else:
        st.caption("No external signals found.")

    # IDENTITY + CONTACT
    foot = []
    if (a.get("merged_count") or 1) > 1:
        foot.append(f"merged from {a['merged_count']} records")
    foot.append(_contact(a))
    st.markdown("<div class='sec'>Contact & identity</div>" + _pills(foot), unsafe_allow_html=True)


# --- kanban board ---------------------------------------------------------------

def _badge_html(lead_key, manual_status, smap: dict) -> str:
    if str(manual_status) == "visit_booked":
        return "<span class='bdg bdg-book'>📅 booked</span>"
    txt = {"done": ("✅ contacted", "bdg-pos"), "sent": ("✅ contacted", "bdg-pos"),
           "skipped": ("⏭️ skipped", "bdg-neu"), "drafted": ("🔵 queued", "bdg-neu")}.get(
               smap.get(lead_key))
    return f"<span class='bdg {txt[1]}'>{txt[0]}</span>" if txt else ""


def _board(lead_type: str):
    df = store.load_leads(DB)
    if df.empty or "lead_type" not in df.columns:
        st.info("No leads yet — run Day 1 from the sidebar."); return
    df = df[df["lead_type"] == lead_type]
    if df.empty:
        st.info(f"No {lead_type}s yet — run Day 1 from the sidebar."); return

    smap = store.action_status_map(DB)
    scoremap = store.action_score_map(DB)
    st.caption(f"{len(df)} {lead_type}s by stage, highest score first.  "
               f"number = today's score · 🔎 researched · ✅ contacted · ⏭️ skipped · 🔵 queued · 📅 booked"
               f"  (Won/Lost hidden)")
    lanes = [s for s in BOARD_STAGES if s not in ("Won", "Lost")]
    cols = st.columns(len(lanes), gap="medium")
    for col, stage in zip(cols, lanes):
        sub = df[df["stage"] == stage].copy()
        sub["_sc"] = pd.to_numeric(sub["priority_score"], errors="coerce").fillna(-1)
        sub["_sp"] = pd.to_numeric(sub["est_monthly_spend_gbp"], errors="coerce").fillna(0)
        sub = sub.sort_values(["_sc", "_sp"], ascending=False)
        colour = STAGE_COLOURS.get(stage, "#666")
        cards = ""
        for _, r in sub.head(10).iterrows():
            if lead_type == "reseller":
                extra = f" · {int(r['followers'])} foll" if _present(r.get("followers")) else ""
            else:
                extra = f" · {r['city']}" if _present(r.get("city")) else ""
            spend = _fmt_money(r.get("est_monthly_spend_gbp")) or "spend n/a"
            researched = " 🔎" if research_signals(r)["signals"] else ""
            badge = _badge_html(r.get("lead_key"), r.get("manual_status"), smap)
            score = r.get("priority_score")
            score_html = ""
            if pd.notna(score):
                n = int(round(score * 100))
                reason = ((scoremap.get(r.get("lead_key")) or {}).get("reason") or "").replace(chr(39), "")
                score_html = (f"<span class='kscore' style='background:{_score_colour(n)}' "
                              f"title='{reason}'>{n}</span>")
            cards += (f"<div class='kcard' style='border-left-color:{colour}'>"
                      f"<div class='krow'><span class='kname'>{_display_name(r)}{researched}</span>{score_html}</div>"
                      f"<div class='kmeta'>{spend}{extra}{badge}</div></div>")
        if not cards:
            cards = "<div class='kmore'>—</div>"
        more = f"<div class='kmore'>+{len(sub) - 10} more</div>" if len(sub) > 10 else ""
        col.markdown(
            f"<div class='lane' style='background:{colour}'>{stage} · {len(sub)}</div>"
            f"<div class='klane'>{cards}</div>{more}", unsafe_allow_html=True)


# --- focused queue --------------------------------------------------------------

_KEYS_JS = """
<script>
const doc = window.parent.document;
if (!doc._sallyKeys) {
  doc._sallyKeys = true;
  doc.addEventListener('keydown', (e) => {
    const tag = (e.target && e.target.tagName) || '';
    if (/INPUT|TEXTAREA/.test(tag)) return;        // don't hijack typing in the draft box
    let label = {'d':'✅ Done', 's':'⏭️ Skip'}[e.key.toLowerCase()];
    if (e.key === 'ArrowRight') label = '➡️';
    if (e.key === 'ArrowLeft') label = '⬅️';
    if (!label) return;
    const b = [...doc.querySelectorAll('button')]
                .find(x => x.innerText.trim().startsWith(label) && !x.disabled);
    if (b) { e.preventDefault(); b.click(); }
  });
}
</script>
"""


def _keyboard_shortcuts():
    components.html(_KEYS_JS, height=0)


def _queue_view():
    _stats_header()
    _keyboard_shortcuts()
    st.caption("⌨️ shortcuts: **D** done · **S** skip · **←/→** navigate")

    pending = store.pending_actions(db_path=DB)
    counts = store.action_counts(db_path=DB)
    queued_today = sum(counts.values())

    pend_by = {}
    for a in pending:
        pend_by[a["channel"]] = pend_by.get(a["channel"], 0) + 1
    labels = {"All": f"All · {len(pending)}", "dm": f"📱 DM · {pend_by.get('dm',0)}",
              "email": f"✉️ Email · {pend_by.get('email',0)}", "call": f"📞 Call · {pend_by.get('call',0)}"}
    chan = st.segmented_control("Channel", ["All", "dm", "email", "call"],
                                format_func=lambda o: labels[o], default="All",
                                key="chan", label_visibility="collapsed") or "All"
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

    a = filtered[nav_i]
    pc1, pc2 = st.columns([5, 1])
    pc1.progress(chan_done / chan_total if chan_total else 0,
                 text=f"{chan_done} of {chan_total} processed{scope}")
    pc2.caption(f"#{nav_i + 1} of {len(filtered)}")

    with st.container(border=True):
        h_l, h_r = st.columns([5, 1])
        with h_l:
            icon = {"dm": "📱 Instagram DM", "email": "✉️ Email", "call": "📞 Call"}.get(a["channel"], a["channel"])
            st.caption(f"{icon}  ·  {a.get('stage','')}")
            st.markdown(f"### {_display_name(a)}")
        h_r.markdown(_score_badge(a), unsafe_allow_html=True)
        st.caption("hover the score to see how it's built")
        st.markdown(f"<div class='why'>{a.get('reason','')}</div>", unsafe_allow_html=True)

        body_l, body_r = st.columns([1, 1])
        with body_l:
            _account_panel(a)
        with body_r:
            label = "Call note" if a["channel"] == "call" else "Message"
            st.text_area(label, value=a.get("message_draft") or "", height=150, key=f"msg_{a['action_id']}")
            with st.expander("🕓 Lead history"):
                evs = store.lead_events(a["lead_key"], DB)
                if evs:
                    for e in evs:
                        st.write(f"- `{e['at'][:10]}` **{e['type']}** — {e['detail']}")
                else:
                    st.caption("No history yet.")

        def _mark_undo():
            st.session_state.last_undo = {"action_id": a["action_id"], "lead_key": a["lead_key"]}

        prev_c, done_c, skip_c, next_c = st.columns(4)
        if prev_c.button("⬅️", use_container_width=True, disabled=nav_i == 0, help="Previous"):
            st.session_state.nav_i = nav_i - 1; st.rerun()
        if done_c.button("✅ Done", use_container_width=True, type="primary"):
            _mark_undo(); store.set_action_status(a["action_id"], "done", DB); st.rerun()
        if skip_c.button("⏭️ Skip", use_container_width=True):
            _mark_undo(); store.set_action_status(a["action_id"], "skipped", DB); st.rerun()
        if next_c.button("➡️", use_container_width=True, disabled=nav_i >= len(filtered) - 1, help="Next"):
            st.session_state.nav_i = nav_i + 1; st.rerun()

        book_c, dnc_c, undo_c = st.columns(3)
        if a.get("lead_type") == "shop":
            if book_c.button("📅 Book visit", use_container_width=True, key=f"bv_{a['action_id']}"):
                _mark_undo(); store.set_manual_status(a["lead_key"], "visit_booked", DB)
                store.set_action_status(a["action_id"], "done", DB); st.rerun()
        if dnc_c.button("🚫 Don't contact", use_container_width=True, key=f"dnc_{a['action_id']}",
                        help="Remove from outreach entirely"):
            _mark_undo(); store.set_manual_status(a["lead_key"], "do_not_contact", DB)
            store.set_action_status(a["action_id"], "skipped", DB); st.rerun()
        u = st.session_state.get("last_undo")
        if undo_c.button("↩️ Undo", use_container_width=True, disabled=not u, help="Undo the last action"):
            store.set_action_status(u["action_id"], "drafted", DB)
            store.set_manual_status(u["lead_key"], None, DB)
            st.session_state.last_undo = None; st.rerun()


def main() -> None:
    st.set_page_config(page_title="Sally — daily queue", page_icon="✉️", layout="wide")
    st.markdown(APP_CSS, unsafe_allow_html=True)
    st.markdown("<div class='app-h'><span class='t'>✉️ Sally</span>"
                "<span class='s'>daily outreach engine — who to contact today, and what to say</span></div>",
                unsafe_allow_html=True)
    _sidebar()
    tab_q, tab_r, tab_s = st.tabs(["📥 Queue", "🧑‍💻 Resellers board", "🏬 Stores board"])
    with tab_q:
        _queue_view()
    with tab_r:
        _board("reseller")
    with tab_s:
        _board("shop")


main()
