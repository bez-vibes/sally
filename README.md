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

### Resellers (the 40 DM/day cap), in `src/sally/score.py`

Resellers are split into groups by pipeline stage, ranked inside each group, and
the day's DM slots are filled from the top down. Won and Lost are excluded.

| Group | Stages | Ranked by |
|---|---|---|
| Deals in flight | Negotiating, Call Booked | `value` (closest to revenue, so they go first) |
| Revive warm | Warm, Replied | `0.6·value + 0.4·urgency` |
| Revival | Ghosted | `0.5·value + 0.5·recency` (ghosts that went quiet more recently rank higher) |
| Cold | New, Contacted | `value` (only top-quarter spenders, and only if slots are left) |

Two inputs, each scaled 0 to 1:

- **value**: a measure of buying power, led by spend. It is a weighted percentile
  rank of `est_monthly_spend_gbp` (60%, since it is the best guide to what a reseller
  would spend and every reseller has it), `sales_velocity_30d` (25%) and `followers`
  (15%). Missing values are filled with the median.
- **urgency**: days since the last touch divided by 60, capped at 1. The 60-day
  window is fixed and easy to change. It is 0 for leads never contacted, since they
  have nothing going cold. `recency` is the inverse, used for ghosted leads.

Filling the 40 DM slots (Rule C): go down the ranked list. A lead with an Instagram
handle takes a DM slot until the 40 are used. After that, leads that also have an
email are emailed instead (email has no daily cap), and handle-only leads wait for a
later day. Cold leads only get a DM if their value is in the top quarter (0.75 or
above) and there are slots left after the warmer groups.

On day one this means all 40 DMs go to warm and active leads, and cold outreach only
starts once that backlog is worked through. Each lead's reason is written out in the
queue (for example, "Warm then went quiet 66 days, ~£9k/mo, re-engage before they go
cold") and broken down in the UI's "Why this lead" panel.

### Shops, in `src/sally/sequence.py`

Shops have no DM cap. The next step follows the stage: New gets a first email, cold
shops (Contacted, Ghosted) get a call to chase, and warm shops (Replied, Warm,
Negotiating) get a call to book a visit. Each step is limited to the channels a shop
actually has. Shop priority is `0.6·value + 0.4·stage_prior`, with warmer stages
weighted higher. Shops worth visiting are grouped by city into a visit plan.

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
