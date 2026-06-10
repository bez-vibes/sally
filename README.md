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

## Build phases

- **Phase 1** — core engine: clean → dedupe → classify → score → next-action → queue. *(current)*
- **Phase 2** — hybrid message drafting + thin web view.
- **Phase 3** — just-in-time enrichment (Brave/Apify) + Slack morning digest.
- **Phase 4** — hosted deploy + scheduled run + architecture docs.
