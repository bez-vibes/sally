"""Research — structured, per-account signal mining.

Same searches for every account (a repeatable engine, not ad-hoc):
  shop:     "<store> <city> vintage shop"  +  "<store> <city> reviews"
  reseller: "<handle>" depop OR vinted OR instagram vintage  (+ optional profile fetch)

Signals are boost-only and carry a source_url + date, so a found signal lifts a
lead and a missing/unfound one is neutral (never a penalty). Results are looked up
from a committed seed file (real signals for the demo leads) plus a live cache.

Live enrichment runs only when BRAVE_API_KEY is set; otherwise the seed/cache is
used, so the pipeline never depends on a network call.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from functools import lru_cache
from pathlib import Path

import pandas as pd

SEED_PATH = Path("data/seed_research.json")
CACHE_PATH = Path(os.getenv("SALLY_RESEARCH_CACHE", ".cache/research.json"))
MAX_BOOST = 0.15  # research can lift a lead's rank by at most this much


def _slug(s) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(s).lower()) if pd.notna(s) and str(s).strip() else ""


def research_key(lead) -> str:
    """Stable key for research lookup: handle for resellers, store|city for shops."""
    lt = lead.get("lead_type")
    handle = lead.get("handle_norm")
    if lt == "reseller" or (pd.notna(handle) and str(handle).strip() and lt != "shop"):
        return _slug(handle)
    return f"{_slug(lead.get('store_name'))}|{_slug(lead.get('city'))}"


@lru_cache(maxsize=1)
def _seed() -> dict:
    if SEED_PATH.exists():
        try:
            return {k: v for k, v in json.loads(SEED_PATH.read_text()).items() if not k.startswith("_")}
        except Exception:
            return {}
    return {}


def _cache() -> dict:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text())
        except Exception:
            return {}
    return {}


def research_signals(lead) -> dict:
    """Return {'signals': [...], 'boost': float} for a lead. Empty + 0.0 if none found."""
    key = research_key(lead)
    if not key or key == "|":
        return {"signals": [], "boost": 0.0}
    rec = _cache().get(key) or _seed().get(key)
    if not rec:
        return {"signals": [], "boost": 0.0}
    signals = rec.get("signals", [])
    boost = min(MAX_BOOST, 0.05 * len(signals))  # boost-only, capped
    return {"signals": signals, "boost": round(boost, 3)}


# --- live enrichment (optional; needs BRAVE_API_KEY) ----------------------------

def _brave(query: str, count: int = 5) -> list[dict]:
    key = os.getenv("BRAVE_API_KEY")
    if not key:
        return []
    url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode(
        {"q": query, "count": count})
    req = urllib.request.Request(url, headers={"X-Subscription-Token": key,
                                               "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        return data.get("web", {}).get("results", [])
    except Exception:
        return []


def _queries(lead) -> list[str]:
    if lead.get("lead_type") == "shop":
        store, city = lead.get("store_name"), lead.get("city")
        return [f'"{store}" {city} vintage shop', f'"{store}" {city} reviews']
    return [f'"{lead.get("handle_norm")}" depop OR vinted OR instagram vintage']


def enrich(lead, date: str = "") -> dict:
    """Live structured research for one lead (best-effort). Writes to the cache and
    returns the signal record. No-op (returns seed/empty) without BRAVE_API_KEY."""
    if not os.getenv("BRAVE_API_KEY"):
        return research_signals(lead)
    signals = []
    for q in _queries(lead):
        for r in _brave(q)[:3]:
            title, desc, link = r.get("title", ""), r.get("description", ""), r.get("url", "")
            if not link:
                continue
            signals.append({"layer": "web", "claim": (title + " — " + desc)[:200],
                            "date": date or "live", "source_url": link,
                            "source_label": urllib.parse.urlparse(link).netloc})
    rec = {"type": lead.get("lead_type"), "signals": signals[:4]}
    cache = _cache()
    cache[research_key(lead)] = rec
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=2))
    return research_signals(lead)
