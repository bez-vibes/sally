# Architecture

Sally is a daily outreach engine for a two-sided B2B pipeline (online resellers +
physical shops). It turns a messy inherited pipeline into a ranked, ready-to-act
daily queue, and remembers what it did so re-runs never repeat work. This is an
MVP: the core engine is built and verified; the layers marked *future* below are
deliberately scoped out.

## Data flow

```
            ┌─────────────────────────── automated (an agent runs this) ──────────────────────────┐
            │                                                                                       │
 xlsx/csv ─►│ ingest ─► clean ─► dedupe ─► classify ─► UPSERT ──► score (resellers)  ─┐            │
 (a batch)  │  load    normalise  union-   channel    SQLite      sequence (shops)    ├─► draft ─┐ │
            │  + tag   stages,    find on  by data    state       (skip cooldown)     │  message │ │
            │          dates,     handle/  not label  store                           ┘          │ │
            │          money,     email/                                                          │ │
            │          contacts   phone                                                           ▼ │
            │                                                                              queue (CSV │
            │                                                                              + brief.md) │
            │                                                                                  │      │
            │                                                                          record actions │
            │                                                                          to SQLite       │
            │                                                                                  │      │
            │                                                                          Slack digest ──┼──► #channel
            └──────────────────────────────────────────────────────────────────────────────────────┘   (link to brief)
                                                                                                   │
                              ┌────────────────── human / agent steps in ──────────────────────────┘
                              ▼
              send the DMs · make the calls · go on the visits · approve/edit drafts
              (via the focused UI: Done / Skip — writes status + overrides back to SQLite)
```

Everything left of "human / agent steps in" runs unattended — that's the part an
agent can own every morning. People only do what software can't: actually send a
DM (Instagram bans automation), make a call, visit a shop, or approve a message.

## The persistent state (SQLite — `data/out/sally.db`)

The DB is the memory. The CSV/brief are disposable exports of one run.

| Table | Role |
|---|---|
| `leads` | current state per `lead_key`: cleaned lead data + Sally's action cache + manual-override columns (`manual_stage`, `manual_status`, `snooze_until`) |
| `events` | append-only: what happened *to* the lead (`stage_change`, `reply_received`, `first_seen`) |
| `actions` | append-only: what Sally *did/recommended* (channel, drafted message, status `drafted→sent/done`) — the work-log the queue is built from |
| `runs` | one row per run (timestamp, file, new vs updated counts) |

## Identity & idempotency (why re-runs don't repeat work)

- Every lead gets a stable `lead_key` (`handle > email > phone > name|city`). Duplicate
  rows — same lead under different IDs, across the IG export and CRM dump — are linked
  by **union-find on any shared key** and merged into one record.
- The store keys on `lead_key`, so re-ingesting can never add a lead twice.
- A **cooldown** (default 4 days, channel-agnostic) skips leads actioned recently —
  *unless* they replied or advanced since, which re-surfaces them immediately.
- Stage updates are **non-regressing** (a lead never slides backwards down the funnel;
  Won/Lost override), and manual overrides always win.

Net: drop a new batch and run again → new leads added, already-handled leads skipped,
next actions updated, nobody messaged twice.

## The two engines

**Resellers (DM-capped).** Tiered triage, ranked within tier on the axes that matter
there, then the daily DM slots are filled top-down:
1. *Deals in flight* (Negotiating, Call Booked) — by value, protect near-revenue first
2. *Revive warm* (Warm, Replied) — `0.6·value + 0.4·urgency`
3. *Revival* (Ghosted) — `0.5·value + 0.5·recency`
4. *Cold* (New, Contacted) — by value, DM only top-quartile whales, only if slots remain

Channel routing (Rule C): top leads get the DM (native channel); resellers-with-email
below the cap go to email (off the 40/day limit); handle-only leads defer. Urgency uses
a fixed 60-day horizon; value is a spend-led percentile composite.

**Shops (not capped).** Next step is a state machine — `New → email`, cold → `call to
chase`, warm → `call to book a visit` — gated by the channels each shop actually has.
Visit-ready shops are clustered by city into a **visit-day plan** (visits are planned,
not a daily action).

## Messaging

Hybrid and provider-agnostic. Deterministic templates for cold touches and call notes;
an LLM (Gemini default, then Groq/Anthropic) drafts re-engagement replies in context for
leads that have already said something. Keyless-safe: no key → templates. The voice is a
single editable `LLM_SYSTEM_PROMPT` constant. LLM outputs are cached on their inputs.

## Scale (265 → 30,000)

- SQLite handles 30k trivially; the storage layer is thin enough to swap to Postgres.
- The real ceiling is the **human send cap** (40 DMs/account/day), not compute — so at
  scale you add IG accounts and reps, and prioritisation matters *more*, not less.
- Drafting is cached and template-first; an LLM call only fires on re-engagement and only
  when inputs change.
- Enrichment (below) would run **just-in-time on the top-N** about to be actioned, never
  on the whole list.

## Running it

`sally run` does the whole flow. A scheduled GitHub Action (`.github/workflows/daily.yml`)
runs it every morning and posts the Slack digest. The focused UI (`make web`) is where a
human works the queue one lead at a time (Done / Skip), writing status and overrides back
to the DB.

## Future exploration (not in this MVP)

These are deliberately scoped out — the MVP proves the engine and the loop; these would
deepen it:

- **Enrichment, just-in-time on the top-N about to be actioned:**
  - *Shops* — Brave Search / Maps to confirm a shop is still trading, pull a website or a
    better email, and tighten the address for visit routing.
  - *Resellers* — Apify to scrape IG/Depop to fill missing follower/listing/velocity data
    or refresh stale numbers. Note: IG scraping is rate-limited and bannable — the same
    constraint as DMs — so it can't run freely at 30k, which is why it's top-N only.
  These feed the personalisation slot in outreach (the "I saw you…" intent line).
- **Event-driven tracking** — email open/reply webhooks (e.g. SendGrid) for true real-time
  state instead of morning batch diffs. Instagram doesn't expose DM-reply events for cold
  outreach, which is why the reseller side stays batch.
- **Hosted deployment** — Postgres (Supabase) + a hosted UI so the Slack digest deep-links
  straight to the lead being processed, and the cron writes to shared state.
