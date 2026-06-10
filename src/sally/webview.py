"""Focused queue UI — process the day's outreach one lead at a time.

Not a dashboard: a single-task view. For each lead it shows who, why, and the
drafted message, with two controls:
  - Done  -> mark the action done (enters cooldown; won't resurface)
  - Skip  -> move on without completing (lead stays pending, resurfaces next run)
Both write straight to the SQLite state. Run with: `make web` (or
`streamlit run src/sally/webview.py`).
"""

from __future__ import annotations

import os

import streamlit as st

from sally import store

DB = os.getenv("SALLY_DB", store.DEFAULT_DB)


def _display_name(a: dict) -> str:
    return a.get("store_name") or a.get("handle_norm") or a.get("contact_name") or a.get("lead_key")


def _contact(a: dict) -> str:
    return {"dm": f"@{a.get('handle_norm','')}", "email": a.get("email") or "",
            "call": a.get("phone") or a.get("city") or ""}.get(a["channel"], "")


def main() -> None:
    st.set_page_config(page_title="Sally — daily queue", page_icon="✉️", layout="centered")
    st.title("✉️ Sally — today's queue")

    if "queue" not in st.session_state:
        st.session_state.queue = store.pending_actions(db_path=DB)
        st.session_state.i = 0

    queue, i = st.session_state.queue, st.session_state.i

    if not queue:
        st.info("No pending actions. Run `sally run` to build today's queue.")
        return
    if i >= len(queue):
        st.success(f"🎉 All done — {len(queue)} actions processed.")
        if st.button("Reload queue"):
            del st.session_state.queue
            st.rerun()
        return

    a = queue[i]
    st.progress((i) / len(queue), text=f"{i} of {len(queue)} done")

    icon = {"dm": "📱 Instagram DM", "email": "✉️ Email", "call": "📞 Call"}.get(a["channel"], a["channel"])
    st.caption(f"{icon}  ·  {a.get('stage','')}")
    st.subheader(_display_name(a))
    st.write(f"**Why:** {a.get('reason','')}")
    st.write(f"**Contact:** {_contact(a)}")

    label = "Call note" if a["channel"] == "call" else "Message"
    st.text_area(label, value=a.get("message_draft") or "", height=160, key=f"msg_{a['action_id']}")

    c1, c2, _ = st.columns([1, 1, 3])
    if c1.button("✅ Done", use_container_width=True):
        store.set_action_status(a["action_id"], "done", DB)
        st.session_state.i += 1
        st.rerun()
    if c2.button("⏭️ Skip", use_container_width=True):
        store.set_action_status(a["action_id"], "skipped", DB)  # skip == next: advance, stays out of today
        st.session_state.i += 1
        st.rerun()


main()
