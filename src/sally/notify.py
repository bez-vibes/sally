"""Notify — post the morning digest to Slack.

After a run, send a short summary (DMs / emails / calls / visit plan) to a Slack
channel via an incoming webhook, pointing at the brief the user opens to start
processing. Keyless-safe: with no SLACK_WEBHOOK_URL it just returns the preview
text (printed by the CLI) so the pipeline never depends on Slack being configured.
"""

from __future__ import annotations

import json
import os
import urllib.request


def build_digest_text(summary: dict, brief_path: str, run_date: str) -> str:
    """Plain-text Slack message (Slack renders *bold* / `code`)."""
    s = summary
    lines = [
        f":wave: *Sally — daily brief · {run_date}*",
        f"*{s.get('actions_total', 0)} actions today* — "
        f"{s.get('dm', 0)} Instagram DMs · {s.get('email', 0)} emails · {s.get('call', 0)} calls",
    ]
    if s.get("pipeline_spend"):
        lines.append(f":pound: today's queue reaches *~£{s['pipeline_spend']/1000:.0f}k/mo* of buyer spend")
    cities = s.get("top_visit_cities") or {}
    if cities:
        lines.append("*Visit plan:* " + " · ".join(f"{c} {n}" for c, n in cities.items()))
    lines.append(
        f":inbox_tray: {s.get('new', 0)} new · {s.get('updated', 0)} updated · "
        f"{s.get('cooldown', 0)} in cooldown (skipped)"
    )
    lines.append(f"Start processing → `{brief_path}`")
    return "\n".join(lines)


def send_digest(summary: dict, brief_path: str, run_date: str,
                webhook_url: str | None = None) -> dict:
    """Post the digest to Slack if a webhook is configured, else return the preview.

    Returns {"sent": bool, "reason"?: str, "preview": str}.
    """
    text = build_digest_text(summary, brief_path, run_date)
    webhook_url = webhook_url or os.getenv("SLACK_WEBHOOK_URL")
    if not webhook_url:
        return {"sent": False, "reason": "no SLACK_WEBHOOK_URL", "preview": text}

    payload = json.dumps({"text": text}).encode()
    req = urllib.request.Request(
        webhook_url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            ok = resp.status == 200
        return {"sent": ok, "preview": text} if ok else {
            "sent": False, "reason": f"HTTP {resp.status}", "preview": text}
    except Exception as e:  # network/credential failure — never break the run
        return {"sent": False, "reason": str(e), "preview": text}
