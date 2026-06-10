# Sally — the daily outreach engine

Sally takes a messy B2B sales pipeline (an Instagram export + CRM dump), works out
**who to contact today and what to say**, and hands you a ready-to-action queue —
then **remembers what it did** so nobody gets messaged twice when you run it again
tomorrow.

Built for Fleek's GTM Acquisition team: a B2B marketplace for secondhand and vintage
clothing selling to two very different leads — **online resellers** (Instagram-only,
40 DMs/day cap) and **physical shops** (email, phone, visitable).

> Status: **Phase 1 — core engine** (in progress). See build phases below.

## Quickstart

```bash
pip install -e .
make run                      # run the morning routine on the sample pipeline
make ingest FILE=new.xlsx     # drop a new batch in — Sally skips anyone already handled
```

No API keys required to run: drafting falls back to deterministic templates and
enrichment is skipped. Add keys in `.env` (see `.env.example`) to turn on
LLM-drafted re-engagement messages, enrichment, and the Slack digest.

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
