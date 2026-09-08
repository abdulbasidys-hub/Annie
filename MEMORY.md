# Annie's memory system

*Written 2026-09-08, when Annie stopped being a database and started being a
notebook.*

---

## Why this exists

The Firestore bill came from a design that recorded everything. Roughly
16,000 tokens launch on Solana every day; each one was a document write plus a
read to check it wasn't already there, then 13-47 more documents for its
extracted features, then a trend engine that re-read all of those on every
pass. On the Spark (free) plan — 20,000 writes and 50,000 reads per day — the
ingest path alone was over budget before anything else ran.

The fix is not a cheaper database. It is keeping less.

> "Too much data" — and now I don't want it to be too much memory, I want too
> much intelligence.

So: Annie sees everything and keeps almost nothing. What she keeps, she keeps
as markdown you can open and read.

---

## The shape of it

```
ANNIE_MEMORY_DIR/                 (a Railway Volume in production)
├── README.md                     written on first boot
├── annie.db                      SQLite: ledger + search index (disposable)
├── core/                         what she currently believes
│   ├── market-model.md
│   ├── whats-working.md
│   ├── open-questions.md
│   └── watchlist.md
├── playbook/                     what has actually worked, with evidence
├── narratives/                   one file per live theme
├── creators/<wallet>.md          dossiers for tracked wallets
├── tokens/<mint>.md              only tokens that moved; carries CA + creator
├── daily/2026-09-08.md
├── weekly/2026-W36.md
├── monthly/2026-09.md
└── notes/                        loose thinking not yet promoted
```

Three storage tiers, and which one a thing lands in is the whole design:

| Tier | Holds | Cost | Lifetime |
|---|---|---|---|
| **Markdown files** | Annie's judgement | free | permanent, revised deliberately |
| **SQLite** (`annie.db`) | every sighting, every creator movement, price checks, signals, the search index | free | sightings pruned at 48h; creator movements kept a year; index rebuildable |
| **Firestore** | settings, bot sessions, conversations, research tasks/notes, reports, launchpads, narratives, and a mirror of the markdown | metered | permanent |

---

## What happens each cycle

Four times a day (00:00 / 06:00 / 12:00 / 18:00 WAT), and **one** model call:

```
free   recompute signals from the ledger          ~0.1s, pure statistics
free   build the digest                           ~40 rows out of ~16,000
free   retrieve relevant memory                   keys first, FTS second
PAID   one bounded call: digest -> memory edits    ~1,700 in / ~800 out
free   refresh tracked creators' dossiers
free   update the watchlist, prune, reindex
free   mirror changed files to Firestore          only files whose hash moved
```

A quiet window skips the paid call entirely. Weekly and monthly rollups add
one call each — and the monthly reads the weeklies, never raw data, which is
what keeps cost flat as history grows.

Measured at production scale (16,000 launches, 400 winners, 560 memory files
representing six months of accumulation):

```
ingest 16,000 launches         12.5s   (1,281/s vs ~11/s real rate)
one watch pass (900 prices)     0.34s
signals.recompute               0.08s
digest.build                    0.07s  ->  6,862 chars ~= 1,715 tokens
200 key lookups                26ms
disk                           17.8 MB
Firestore operations            0
```

---

## How she finds things

Two tiers, cheapest first. This is the answer to "she shouldn't have to search
through all files every cycle".

**1. Keys.** Every memory declares the exact handles it is about — mint
addresses, creator wallets, tickers, narrative slugs — in its `keys:` header,
and addresses are additionally harvested from the prose so this never depends
on the model remembering to fill a field in. Looking up a contract address is
one indexed lookup: no scan, no ranking, no model.

**2. Full text.** FTS5 with porter stemming, BM25-ranked, blended with each
memory's own `importance`. Only consulted when the question is fuzzy or the
key lookup came back empty.

Retrieval returns *excerpts*, never whole files, and `recall()` has a hard
budget. That budget is the guard against the obvious failure of this
rewrite — quietly widening until the whole notebook is in the prompt again.

---

## Reading it yourself

- **Website** → Memory page. Cards showing an excerpt of each file; click one
  to read it whole, edit it in place, or delete it. "New memory" creates one
  by hand.
- **Chat / Discord / Telegram** → `search_memory`. Paste a CA or a wallet and
  it resolves straight to the file about it.
- **Your machine** → `python -m tools.memory_pull`, which walks the API and
  writes the same files locally. `--search <CA>` prints excerpts instead.

---

## Writing to it yourself

The notebook is not only Annie's. You know things she cannot infer from
watching the market, and a memory only she can write is missing half its
content — so you can dictate it, from the website or from chat.

From Telegram or Discord, the conversation looks like this:

> **you:** make me a file called crowded narratives
> **Annie:** Created `notes/crowded-narratives.md`. What should go in it?
> **you:** stop launching into AI agents, every desk is doing it, the last
> four I watched round-tripped inside an hour
> **Annie:** Saved to `notes/crowded-narratives.md`.

The rules she follows:

- **Creating it empty is normal.** She makes the file, tells you the path,
  and asks what goes in it — because that is how the conversation actually
  goes.
- **She writes what you said, not her summary of it.** Compressing your words
  into her own would lose them.
- **Appending is the default.** "Rewrite it", "replace that", "start it
  over" mean replace; anything else adds. An accidental overwrite loses prose
  nobody can recover, so the safe operation is the one that happens by
  default.
- **You can correct `core/`.** Those are her standing beliefs and you are
  entitled to change them. On her own initiative she stays in `notes/` and
  lets the scheduled cycle promote anything durable.
- **"That was wrong, delete it" works.** The four seeded `core/` files are
  the exception — every cycle reads them as context, so they get emptied
  rather than removed.
- **She always confirms with the path**, so you know where it landed.

One thing she will tell you rather than silently work around:
`core/watchlist.md` is regenerated from the ledger every cycle, so hand
edits there get overwritten. She suggests a different file instead.

The tools behind this are `create_memory`, `write_memory`, `delete_memory`
and `list_memory` in `app/annie/agent.py`; the rules she reads are in
`app/annie/persona.py` under "Being told what to remember".

### Standing instructions

A *note* is something she knows. An *instruction* is something she does —
every cycle, whether or not that moment happened to remind her of it.

> **you:** from now on, keep track of which narratives are getting crowded
> **Annie:** Noted — I'll track that from here. It's in
> `core/instructions.md`, so I read it at the start of every cycle.

These go to `core/instructions.md`, which is loaded **whole** into every
cycle prompt and every conversation rather than retrieved by search. That
distinction is the entire point: an instruction that only surfaces when a
query matches it is not standing. It sits last in the cycle prompt, where it
is the most recent thing read before she decides what to write.

Instructions outrank her own judgement about what is worth keeping — if you
ask her to track something she would not have bothered with, she tracks it.

---

## When there is nothing

"No data" has several causes that need completely different actions, and
undistinguished they all produce the same useless reply: a list of zeros.
`app/memory/health.py` classifies which one it is, and Annie leads with that
instead of reciting counts.

| State | What it means | What to do |
|---|---|---|
| `never_started` | Nothing has ever arrived — a wiring problem, not a quiet market | Check the Helius webhook URL and `HELIUS_WEBHOOK_SECRET` |
| `memory_not_durable` | Data arrives but nothing survives a redeploy | Attach the Railway Volume |
| `stream_stopped` | It worked and went quiet — the webhook died | Same webhook checks |
| `warming_up` | Real data, just not enough of it yet | Nothing; wait |
| `healthy` | Arriving and being kept | — |

`stream_stopped` is the one that used to be invisible: a dead webhook and a
quiet market produce identical numbers, so without something classifying
them, a broken pipeline looked exactly like a slow night.

Surfaced three ways — a banner on System Health (silent when healthy),
`GET /api/system/pipeline-status`, and the `system_status` tool so Annie can
answer "why don't you have anything?" with a cause rather than a shrug.

---

## Forgetting, on purpose

A memory system that only accumulates is a database with extra steps.

- Sightings that never traded are **deleted** after `WATCH_TTL_HOURS` (48).
  Including a tracked creator's — exempting them was the obvious-looking rule
  and it is wrong, since a wallet is tracked precisely *because* it launches a
  lot, so the exemption would spare the largest share of the junk.
- Signals are capped at 400; the weakest and deadest are dropped first.
- Daily files are pruned after 90 days, by which point the weeklies and
  monthlies above them are the record.
- The learning step is explicitly allowed to say "delete this, it was wrong",
  and rollups nominate files to drop.

Kept regardless: anything that ever cleared a tier, anything that ever traded
above the watch floor, every creator, and a year of creator movements.

---

## The parts that would break quietly

Each of these has a test pinning it, because none of them stays true by
accident:

1. **Ingest must touch Firestore zero times.**
   `test_a_delivery_costs_no_firestore_operations`
2. **Price lookups must stay batched** (30 mints per request, not one each).
   `test_lookups_are_batched_not_per_mint`
3. **The cycle prompt must stay small.** `test_the_cycle_prompt_is_bounded`
   fails above ~4,000 tokens.
4. **A CA or wallet in memory must resolve by that exact string.**
   `test_a_contract_address_resolves_in_one_key_lookup`
5. **Pruning must delete the dead and keep the evidence.**
   `test_prune_deletes_the_dead_and_keeps_the_evidence`
6. **Window bounds must not drop the newest row.**
   `TestWindowBoundaries` — see below.

---

## One bug worth remembering

Window queries compared timestamps as `>= start AND < end`. The clock has
finite resolution (coarse on Windows), so a token qualified moments before a
cycle started could carry a timestamp *identical* to that cycle's `now`. It
was dropped from that window — and because the next window begins at the same
instant, from that one too. Permanently invisible to the signal engine.

It surfaced as the same code returning 2, 6, 9 and 10 out of 10 on successive
runs. `qualified_in_window` now defaults to an inclusive end bound, and the
one caller that needs adjacent non-overlapping windows opts out explicitly.

---

## Operating it

**Attach a Railway Volume** and set `ANNIE_MEMORY_DIR` to its mount path
(e.g. `/data/memory`). Without one, memory lives in the container and is wiped
on redeploy — it restores from the Firestore mirror on boot, but that is a
backup, not the real thing. System Health says which state you are in.

**Clear the old collections** with `python -m tools.firestore_cleanup`
(dry run by default). Deletes count against the daily quota too, so it batches
and is resumable — run it across a few days.

**Watch the cost panel** on System Health: writes against budget, what the
ledger holds, whether memory is durable, and whether the launch stream is
actually arriving (a silently dead webhook used to look exactly like a quiet
market).

**Budgets** are enforced in-process (`app/db/budget.py`). Over budget, a write
is skipped and logged rather than retried — everything routed through there is
either reconstructible or a snapshot the next cycle takes again, and the
markdown on disk is unaffected either way.
