# Sally — the daily outreach engine

Sally takes a messy B2B sales pipeline (an Instagram export + CRM dump), works out
**who to contact today and what to say**, and hands you a ready-to-action queue —
then **remembers what it did** so nobody gets messaged twice when you run it again
tomorrow.

Built for Fleek's GTM Acquisition team: a B2B marketplace for secondhand and vintage
clothing selling to two very different leads — **online resellers** (Instagram-only,
40 DMs/day cap) and **physical shops** (email, phone, visitable).

> Status: core engine, drafting, focused UI, Slack digest and a scheduled run all
> working end-to-end. See build phases below.

## Quickstart

```bash
pip install -e .

# day 1: run the morning routine on the main pipeline
python -m sally run --sheet pipeline

# day 2: drop the new batch in — new leads are added, already-handled leads are
# skipped (cooldown), and the queue updates. Nobody is messaged twice.
python -m sally run --sheet new_drop_day2
```

Outputs land in `data/out/`: `actions_<run>.csv` (machine-readable) and
`brief_<run>.md` (a channel-grouped morning brief). State persists in
`data/out/sally.db`.

No API keys required to run. Add keys in `.env` (see `.env.example`) to turn on
LLM-drafted messages, enrichment, and the Slack digest.

**Tweaking the messaging:** the message voice lives in `src/sally/draft.py` —
`LLM_SYSTEM_PROMPT` steers LLM-drafted re-engagements, and the `_t_*` functions
are the deterministic templates used by default (and as the keyless fallback).
Edit either to change Sally's voice without touching the pipeline.

```bash
make test    # core guarantees: cleaning, dedup (rule A), run idempotency
```

## Process the queue (the UI)

There's no file to open — the queue is worked through a small local web app:

```bash
make web      # then open http://localhost:8501 in your browser
```

It shows one lead at a time (who, why, and the drafted message) with two buttons:

- **Done** — mark it handled (enters cooldown, won't resurface)
- **Skip** — move on without sending (resurfaces in a future run)

Both write straight to `data/out/sally.db`. (First time only: `pip install -e ".[web]"`
to pull in Streamlit.)

A sidebar of controls drives the pipeline without leaving the page: run day 1,
drop the day-2 batch, soft/hard reset, and post the Slack digest — with a stats
header and a per-lead history timeline.

## What it does

1. **Ingest** any tab/batch of leads.
2. **Clean** — collapse 24 stage spellings to ~9, parse mixed date formats, normalise
   money/handles/emails/phones, flag malformed values.
3. **Dedupe** on identity (handle / email / phone), not row ID — the real duplicates.
4. **Remember** — upsert into a SQLite state store: new leads added, seen leads
   updated, already-handled leads skipped.
5. **Classify** each lead by the contact data it actually has, not its label.
6. **Prioritise** — resellers scored under the 40-DM/day cap; shops sequenced
   (email → call → visit) and grouped by city for visit planning.
7. **Draft** the actual next message for each lead.
8. **Output** a daily action queue: who, channel, action, the message, and why.

## How leads are scored

### The convergence score (every lead, 0–100), in `src/sally/score.py`

Every lead gets one score from the **same five components with the same weights**, so
a given input always contributes the same points and scores are comparable across the
whole pipeline. The score directly orders the queue; pipeline position is just one of
the components (a stage bonus), not a separate formula. Won and Lost are excluded.

| Component | Points | Driver |
|---|---|---|
| Pipeline stage | up to +30 | Negotiating/Call Booked highest (live deals), down to New | 
| Buying power | up to +35 | spend-led: `est_monthly_spend_gbp` (60%) + `sales_velocity_30d` (25%) + `followers` (15%), as percentile ranks; missing filled with the median |
| Going cold | up to +20 | days since last touch ÷ 60, capped; 0 if never contacted |
| Buying intent | +15 … −20 | from the last reply: buying +15, scheduling +12, qualifying +6, deferral −6, objection −20 |
| Research signals | up to +15 | +5 per sourced external signal (boost only — never a penalty) |

Total is clamped to 0–100. The same `score_components()` function produces the points
the scorer sums *and* the breakdown the UI shows on hover, so what you see is exactly
what scored. All weights are tunable constants at the top of `score.py`.

Filling the 40 DM slots (Rule C): sort everyone by score, then go down the list — a
lead with an Instagram handle takes a DM slot until the 40 are used; after that,
leads that also have an email are emailed instead (email has no daily cap), and
handle-only leads wait for a later day.

Because pipeline stage is a bonus rather than a gate, a live deal is usually near the
top but a high-value, cooling warm lead can edge out a *tiny* live deal — which is
the more sensible outcome. Raise the stage bonus if you want live deals to always lead.

### Shops, in `src/sally/sequence.py`

Shops use the **same convergence score** (so board numbers are comparable), but have
no DM cap. Their next step follows the stage: New gets a first email, cold shops
(Contacted, Ghosted) get a call to chase, and warm shops (Replied, Warm, Negotiating)
get a call to book a visit — each gated by the channels a shop actually has. Shops
worth visiting are grouped by city into a visit plan.

### Cooldown

A lead contacted in the last 4 days (on any channel) is skipped on the next run,
unless it has replied or changed stage since, which brings it back straight away.
This is what stops a re-run contacting the same people twice.

## Build phases

- **Phase 1 ✓** — core engine: clean → dedupe → classify → score → next-action → queue, idempotent.
- **Phase 2 ✓** — hybrid message drafting + focused queue UI.
- **Phase 3 ✓** — Slack morning digest. *(Just-in-time enrichment — Brave/Apify — is documented in `ARCHITECTURE.md` as future work, not built.)*
- **Phase 4 ✓** — scheduled GitHub Action + `ARCHITECTURE.md`. *(Hosted deploy is the documented stretch.)*

See `ARCHITECTURE.md` for the data-flow diagram and design notes.
