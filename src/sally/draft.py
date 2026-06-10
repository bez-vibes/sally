"""Draft — write the actual next message for each lead.

Hybrid, provider-agnostic, keyless-safe:
  * Cold first touches and call prep notes use deterministic TEMPLATES.
  * Re-engagement (the lead has already said something — last_inbound_text) uses an
    LLM that replies in context. Provider is pluggable (Gemini default, then Groq,
    then Anthropic); if no key/library/network, it falls back to a template so the
    pipeline always produces a message.
  * LLM results are cached on the message inputs, so re-runs are deterministic and free.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pandas as pd

COMPANY = os.getenv("SALLY_COMPANY", "Fleek")
REP = os.getenv("SALLY_REP_NAME", "the Fleek team")
CACHE_PATH = Path(os.getenv("SALLY_DRAFT_CACHE", ".cache/drafts.json"))

# ---------------------------------------------------------------------------
# TWEAK THE VOICE HERE. This system prompt steers every LLM-drafted message;
# the _t_* functions below are the deterministic templates (the keyless default).
# Editing either changes Sally's messaging without touching the pipeline.
# ---------------------------------------------------------------------------
LLM_SYSTEM_PROMPT = (
    f"You are an SDR for {COMPANY}, a B2B marketplace where resellers and vintage "
    f"shops source secondhand/vintage clothing wholesale, in bulk, with buyer "
    f"protection and global logistics. You write short, warm, specific re-engagement "
    f"messages. British English. No em dashes. No emoji spam. Do not invent facts. "
    f"Reply directly to what the lead last said."
)


# --- small helpers --------------------------------------------------------------

def _first_name(row) -> str:
    cn = row.get("contact_name")
    if pd.notna(cn) and str(cn).strip():
        return str(cn).split()[0].strip().title()
    nm = row.get("name")  # resolved display name (handle for resellers, store for shops)
    if pd.notna(nm) and str(nm).strip():
        return str(nm)
    return "there"


def _display_name(row) -> str:
    nm = row.get("name")
    return str(nm) if (pd.notna(nm) and str(nm).strip()) else "this lead"


def _has_inbound(row) -> bool:
    v = row.get("last_inbound_text")
    return pd.notna(v) and str(v).strip() not in ("", "nan")


def _spend_str(row) -> str:
    v = row.get("monthly_spend")
    if pd.isna(v):
        return ""
    v = float(v)
    return f"~£{v/1000:.1f}k/mo".replace(".0k", "k") if v >= 1000 else f"~£{int(v)}/mo"


# --- templates ------------------------------------------------------------------

def _t_dm_cold(row) -> str:
    name = _first_name(row)
    return (f"hey {name}! love what you're building. we're {COMPANY} — resellers "
            f"source secondhand & vintage stock from us by the bundle (100+ pieces, "
            f"buyer protection, worldwide shipping). want me to send over what's "
            f"landing this week?")


def _t_email_cold(row) -> str:
    name = _first_name(row)
    store = _display_name(row)
    city = row.get("city")
    close = f" to show you what's available{f' near {city}' if pd.notna(city) and city else ''}?"
    return (f"Subject: {COMPANY} x {store}\n\n"
            f"Hi {name},\n\n"
            f"Came across {store} — looks like a great spot for vintage.\n\n"
            f"We're {COMPANY}, a B2B marketplace where shops like yours source "
            f"secondhand and vintage stock wholesale, with buyer protection and "
            f"global logistics built in.\n\n"
            f"Worth a quick call this week{close}\n\n"
            f"Cheers,\n{REP}")


def _t_reengage(row) -> str:
    name = _first_name(row)
    said = str(row.get("last_inbound_text") or "").strip()
    snippet = f' you mentioned "{said}" a little while back, and' if said else ""
    return (f"hey {name}, circling back —{snippet} we'd still love to help you source "
            f"vintage stock in bulk. want me to send a few bundles over to take a look?")


def _t_call_note(row) -> str:
    said = str(row.get("last_inbound_text") or "").strip()
    goal = {"call to book a visit": "book an in-person visit",
            "call to chase": "chase the earlier email",
            "call to re-engage": "re-open the conversation"}
    intent = next((g for k, g in goal.items() if k in str(row.get("reason", ""))), "advance the deal")
    last = f' Last said: "{said}".' if said else ""
    spend = _spend_str(row)
    return f"Call {_display_name(row)} ({row.get('stage')}). Goal: {intent}.{last} {spend}".strip()


# --- LLM (pluggable, keyless-safe) ----------------------------------------------

def _llm_complete(system: str, user: str) -> str | None:
    """Return an LLM completion, or None if no provider/key/library is available
    or the call errors. Caller falls back to a template on None."""
    provider = os.getenv("SALLY_LLM_PROVIDER", "gemini").lower()
    try:
        if provider == "gemini" and os.getenv("GEMINI_API_KEY"):
            import google.generativeai as genai
            genai.configure(api_key=os.environ["GEMINI_API_KEY"])
            model = genai.GenerativeModel("gemini-2.0-flash", system_instruction=system)
            return model.generate_content(user).text.strip()
        if provider == "groq" and os.getenv("GROQ_API_KEY"):
            from groq import Groq
            c = Groq(api_key=os.environ["GROQ_API_KEY"])
            r = c.chat.completions.create(model="llama-3.3-70b-versatile", messages=[
                {"role": "system", "content": system}, {"role": "user", "content": user}])
            return r.choices[0].message.content.strip()
        if provider == "anthropic" and os.getenv("ANTHROPIC_API_KEY"):
            import anthropic
            c = anthropic.Anthropic()
            r = c.messages.create(model="claude-haiku-4-5-20251001", max_tokens=300,
                                  system=system, messages=[{"role": "user", "content": user}])
            return r.content[0].text.strip()
    except Exception:
        return None
    return None


def _reengage_prompt(row) -> tuple[str, str]:
    channel = "Instagram DM" if row.get("channel") == "dm" else "email"
    limit = "under 55 words, casual, lowercase is fine" if channel == "Instagram DM" else "under 120 words"
    user = (
        f"Channel: {channel} ({limit}).\n"
        f"Lead: {_first_name(row)} ({row.get('stage')}).\n"
        f"They last said: \"{row.get('last_inbound_text')}\".\n"
        f"Write the next message to re-open the conversation and move toward a call/order.")
    return LLM_SYSTEM_PROMPT, user


# --- cache + public API ---------------------------------------------------------

def _load_cache() -> dict:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text())
        except Exception:
            return {}
    return {}


def _save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=0))


def _cache_key(row) -> str:
    raw = "|".join(str(row.get(k, "")) for k in
                   ("lead_key", "channel", "action_type", "stage", "last_inbound_text"))
    return hashlib.sha1(raw.encode()).hexdigest()


def draft_for(row, cache: dict | None = None) -> tuple[str, str]:
    """Return (message, method) where method is 'template' or 'llm'."""
    channel = row.get("channel")
    if channel == "call":
        return _t_call_note(row), "template"

    if _has_inbound(row):  # re-engagement — try LLM, fall back to template
        key = _cache_key(row)
        if cache is not None and key in cache:
            return cache[key], "llm-cached"
        system, user = _reengage_prompt(row)
        out = _llm_complete(system, user)
        if out:
            if cache is not None:
                cache[key] = out
            return out, "llm"
        return _t_reengage(row), "template"

    # cold first touch
    return (_t_email_cold(row) if channel == "email" else _t_dm_cold(row)), "template"


def draft_all(actions: pd.DataFrame) -> pd.DataFrame:
    """Fill the `message` column for a queue of actions. Caches LLM results."""
    if actions.empty:
        return actions
    cache = _load_cache()
    msgs, methods = [], []
    for _, row in actions.iterrows():
        m, how = draft_for(row, cache)
        msgs.append(m); methods.append(how)
    actions = actions.copy()
    actions["message"] = msgs
    actions["draft_method"] = methods
    _save_cache(cache)
    return actions
