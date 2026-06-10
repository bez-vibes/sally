"""Clean — normalise the messy inherited pipeline.

Design principles agreed for the build:
  * Derive from the data, don't hardcode (e.g. the year for month-only dates is
    inferred from the years actually present in the file, not a literal 2025/2026).
  * When something can't be confidently resolved, FLAG and SURFACE it rather than
    guess silently. Unmapped stages go to an `Unknown` bucket with the raw value
    kept; low-confidence dates are flagged. A run reports these so a human can act.

clean(df) -> (cleaned_df, report) where `report` summarises what needed attention.
"""

from __future__ import annotations

import calendar
import re
from collections import Counter
from datetime import date

import pandas as pd

# --- stage taxonomy -------------------------------------------------------------

# Canonical funnel order (off-ramps Ghosted/Lost/Won handled separately downstream).
FUNNEL_ORDER = ["New", "Contacted", "Replied", "Warm", "Call Booked", "Negotiating", "Won"]
TERMINAL_STAGES = {"Won", "Lost"}
REVIVABLE_STAGES = {"Ghosted"}
ALL_STAGES = FUNNEL_ORDER + ["Lost", "Ghosted", "Unknown"]

# Ordered keyword rules, most-specific first. Each is (canonical, regex on the
# normalised string). Word boundaries stop "won" matching "won't", etc.
_STAGE_RULES: list[tuple[str, re.Pattern]] = [
    ("Won",          re.compile(r"\bwon\b|closed won")),
    ("Lost",         re.compile(r"\blost\b")),
    ("Ghosted",      re.compile(r"ghost|no response|no reply|unrespons")),
    ("Negotiating",  re.compile(r"negotiat")),
    ("Call Booked",  re.compile(r"call.*book|book.*call")),
    ("Warm",         re.compile(r"\bwarm\b")),
    ("Replied",      re.compile(r"repl")),
    ("Contacted",    re.compile(r"contact")),
    ("New",          re.compile(r"\bnew\b")),
]


def normalise_stage(raw) -> str:
    """Map a raw stage string to a canonical stage, or 'Unknown' if no rule matches."""
    if pd.isna(raw):
        return "Unknown"
    s = re.sub(r"[-_]", " ", str(raw).strip().lower())
    s = re.sub(r"\s+", " ", s)
    for canonical, pattern in _STAGE_RULES:
        if pattern.search(s):
            return canonical
    return "Unknown"


# --- dates ----------------------------------------------------------------------

_MONTH_ABBR = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
_MONTH_FULL = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}


def _parse_explicit_date(s: str):
    """Parse a date that carries its own year. Returns date or None."""
    s = s.strip()
    if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", s):  # ISO
        y, m, d = (int(x) for x in s.split("-"))
        return _safe_date(y, m, d)
    if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", s):  # DD/MM/YYYY (UK, day-first)
        d, m, y = (int(x) for x in s.split("/"))
        return _safe_date(y, m, d)
    return None


def _parse_monthless(s: str):
    """Parse 'Dec 31' / 'Jan 5' → (month, day) with no year. Returns (m, d) or None."""
    m = re.fullmatch(r"([A-Za-z]{3,9})\s+(\d{1,2})", s.strip())
    if not m:
        return None
    month = _MONTH_ABBR.get(m.group(1).lower()[:3]) or _MONTH_FULL.get(m.group(1).lower())
    if not month:
        return None
    return month, int(m.group(2))


def _safe_date(y: int, mo: int, d: int):
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def _build_month_year_map(explicit_dates: list[date]) -> dict[int, int]:
    """From the dates that carry a year, learn which year each month belongs to.

    Derived from the data, not hardcoded — if a month appears with more than one
    year, the most common wins.
    """
    by_month: dict[int, Counter] = {}
    for dt in explicit_dates:
        by_month.setdefault(dt.month, Counter())[dt.year] += 1
    return {month: counts.most_common(1)[0][0] for month, counts in by_month.items()}


def clean_date_column(values: pd.Series, month_year_map: dict[int, int],
                      window: tuple[date, date] | None):
    """Return (parsed_series, confidence_series).

    confidence in:
      high        — explicit year in the source
      inferred    — month-only, year resolved from the data's month→year map
      low         — month-only, month absent from the map (best-effort year, flagged)
      missing     — blank in the source (expected, e.g. never-contacted leads)
      unparseable — had a value we couldn't parse (a real problem to surface)
    """
    out, conf = [], []
    for v in values:
        if pd.isna(v) or str(v).strip().lower() in {"", "nan", "nat"}:
            out.append(pd.NaT); conf.append("missing"); continue
        s = str(v).strip()
        explicit = _parse_explicit_date(s)
        if explicit:
            out.append(pd.Timestamp(explicit)); conf.append("high"); continue
        ml = _parse_monthless(s)
        if ml:
            month, day = ml
            if month in month_year_map:                       # confidently inferred
                dt = _safe_date(month_year_map[month], month, day)
                out.append(pd.Timestamp(dt) if dt else pd.NaT)
                conf.append("inferred" if dt else "unparseable"); continue
            # month not seen with a year — pick nearest window year, flag low-confidence
            year = _nearest_year(month, day, month_year_map, window)
            dt = _safe_date(year, month, day) if year else None
            out.append(pd.Timestamp(dt) if dt else pd.NaT)
            conf.append("low" if dt else "unparseable"); continue
        out.append(pd.NaT); conf.append("unparseable")        # had a value, no format matched
    return pd.Series(out, index=values.index), pd.Series(conf, index=values.index)


def _nearest_year(month: int, day: int, month_year_map: dict[int, int],
                  window: tuple[date, date] | None):
    """Best-effort year for a month not present in the data's month→year map."""
    years = sorted({y for y in month_year_map.values()})
    if not years:
        return None
    if not window:
        return years[0]
    mid = window[0] + (window[1] - window[0]) / 2
    return min(years, key=lambda y: abs((_safe_date(y, month, day) or window[0]) - mid))


# --- money / email / phone / handle --------------------------------------------

def clean_money(raw):
    """'£5,170' / '140' / '9000' → 5170 / 140 / 9000 (int) or NA."""
    if pd.isna(raw):
        return pd.NA
    s = re.sub(r"[^\d.]", "", str(raw))
    return int(float(s)) if s else pd.NA


def clean_email(raw):
    """Returns (cleaned, valid). Repairs the obvious '@@' typo; lowercases/trims."""
    if pd.isna(raw):
        return pd.NA, False
    s = str(raw).strip().lower().replace("@@", "@")
    valid = bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", s))
    return s, valid


def clean_phone(raw):
    """Light normalisation → (cleaned, valid). 00→+, UK 0→+44; bare numbers kept but
    flagged uncertain (no country code to trust)."""
    if pd.isna(raw):
        return pd.NA, False
    s = re.sub(r"[^\d+]", "", str(raw))
    if not s:
        return pd.NA, False
    if s.startswith("00"):
        s = "+" + s[2:]
    elif s.startswith("0"):           # UK national → +44
        s = "+44" + s[1:]
    valid = bool(re.fullmatch(r"\+\d{8,15}", s))  # only trust numbers with a country code
    return s, valid


def clean_handle(raw):
    """'@Name' / 'instagram.com/name' / 'https://instagram.com/name/' → 'name'."""
    if pd.isna(raw):
        return pd.NA
    s = str(raw).strip().lower()
    s = re.sub(r"^https?://", "", s)
    s = re.sub(r"^(www\.)?instagram\.com/", "", s)
    s = s.strip("/@ ").replace("@", "")
    return s or pd.NA


# --- orchestration --------------------------------------------------------------

def clean(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Clean an ingested batch. Returns (cleaned_df, report)."""
    df = df.copy()

    # stages: keep the raw, add canonical
    df["stage_raw"] = df["stage"]
    df["stage"] = df["stage"].map(normalise_stage)

    # dates: derive month→year map from the explicit dates across both date columns
    raw_dates = pd.concat([df["first_seen_date"], df["last_touch_date"]]).dropna().astype(str)
    explicit = [d for d in (_parse_explicit_date(s) for s in raw_dates) if d]
    month_year_map = _build_month_year_map(explicit)
    window = (min(explicit), max(explicit)) if explicit else None
    for col in ["first_seen_date", "last_touch_date"]:
        parsed, conf = clean_date_column(df[col], month_year_map, window)
        df[col] = parsed
        df[f"{col}_confidence"] = conf

    # money
    df["est_monthly_spend_gbp"] = df["est_monthly_spend_gbp"].map(clean_money)
    df["avg_listing_price_gbp"] = df["avg_listing_price_gbp"].map(clean_money)

    # email
    email_raw = df["email"]
    cleaned = df["email"].map(clean_email)
    df["email"] = cleaned.map(lambda t: t[0])
    df["email_valid"] = cleaned.map(lambda t: t[1])
    df["email_raw"] = email_raw

    # phone
    phone_raw = df["phone"]
    cleaned_p = df["phone"].map(clean_phone)
    df["phone"] = cleaned_p.map(lambda t: t[0])
    df["phone_valid"] = cleaned_p.map(lambda t: t[1])
    df["phone_raw"] = phone_raw

    # handle
    df["handle_norm"] = df["handle"].map(clean_handle)

    # numeric coercion for reseller metrics (blank for stores → stays NA)
    for col in ["followers", "active_listings", "sales_velocity_30d", "num_touches"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    report = {
        "rows": len(df),
        "month_year_map": month_year_map,
        "unmapped_stages": dict(Counter(
            df.loc[df["stage"] == "Unknown", "stage_raw"].fillna("(blank)").astype(str)
        )),
        "dates_low_confidence": int(
            (df[["first_seen_date_confidence", "last_touch_date_confidence"]] == "low").sum().sum()
        ),
        "dates_missing": int(
            (df[["first_seen_date_confidence", "last_touch_date_confidence"]] == "missing").sum().sum()
        ),
        "dates_unparseable": int(
            (df[["first_seen_date_confidence", "last_touch_date_confidence"]] == "unparseable").sum().sum()
        ),
        "emails_repaired": int((email_raw.astype(str).str.contains("@@", na=False)).sum()),
        "emails_invalid": int((~df["email_valid"] & df["email"].notna()).sum()),
        "phones_uncertain": int((~df["phone_valid"] & df["phone"].notna()).sum()),
    }
    return df, report
