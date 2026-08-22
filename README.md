# Annie

**A Solana memecoin research system with an AI research assistant on top of it.**

Annie studies tokens that reached meaningful market caps, finds what they
repeatedly have in common, tracks how that changes week to week, and tells you
what is worth investigating next.

**She is not a trading bot.** There is no execution path, no portfolio, no price
prediction, and no seam to add them.

Built to the specification in [Build.md](Build.md) — including its amendments
(§75-§78), which record where this build deliberately diverged from the
original spec and why.

---

**Contents**

- [What Annie is](#what-annie-is)
- [What Annie can do](#what-annie-can-do)
- [How the system works](#how-the-system-works)
- [What makes her not a chatbot](#what-makes-her-not-a-chatbot)
- [The web app](#the-web-app)
- [What's actually built and working](#whats-actually-built-and-working)
- [What is not built](#what-is-not-built)
- [What I need from you](#what-i-need-from-you)
- [Setup, step by step](#setup-step-by-step)
- [Running locally](#running-locally)
- [Deploying](#deploying)
- [Architecture](#architecture)
- [Design decisions worth knowing](#design-decisions-worth-knowing)
- [Picking up the unfinished work](#picking-up-the-unfinished-work)

---

## What Annie is

Solana produces thousands of new tokens a day, almost all of them on Pump.fun,
almost all of them worthless. A handful reach real market caps. Annie exists to
answer one question about that handful, continuously, as new data arrives:

> What do Solana tokens that reach meaningful market-cap milestones repeatedly
> have in common, how is that changing right now, and what's worth
> investigating next?

She is the conversational front end of a larger pipeline that watches the
chain, records what actually happened, and runs statistics over it. She does
not decide what counts as a pattern — the database and the statistical engine
do that. Her job is to read what they found and explain it to you in plain
language, correctly hedged, with the numbers to back it up.

Think of her less like a chatbot with opinions and more like a research
assistant who has read every file in the cabinet, remembers where everything
is, and will tell you "I don't know" instead of making something up to sound
useful.

---

## What Annie can do

**Ask her anything about the data the system has collected.** Concretely, in
one conversation she can:

- Give you a live summary — tokens collected, tokens qualified, how many
  trends are currently rising, new, or declining.
- Search and filter tokens: qualified only, by launchpad, by minimum
  market-cap tier ($100k / $250k / $500k / $1M).
- Pull the full record on one token by its mint address — launch info,
  creator wallet, qualification evidence, peak market cap, verification
  status, and its milestone history ($100k reached at, migrated at, and so on).
- List and rank trends by how much they've moved recently, filtered by status
  (new / rising / stable / declining / dead), and pull full detail on any one
  of them — sample size, statistical significance, effect size, how long it's
  persisted, and its caveats.
- List creator wallets and flag repeat winners — wallets with more than one
  token that reached $100k or $1M.
- Pull detail on a launchpad: lifecycle stage, how many tokens it's launched,
  how many qualified, its success rate, and its growth over the last 7 days.
- Check prior research findings before answering, so she doesn't re-derive
  something already established, and cite it if it applies.
- Search the public web for outside context (news, an event, a narrative) —
  only if `TAVILY_API_KEY` is configured, and always clearly separated from
  what the database itself says.

**What she deliberately will not do:** state a number that didn't come from
one of the tool calls above in that same conversation, give trading advice,
suggest an entry point or a price target, imply a finding will make you money,
or quietly average away a disagreement between two data providers. If a tool
fails or isn't configured, she says so rather than guessing — this has been
verified against a live run, not just written into the prompt: asked "how many
tokens have you collected" with the underlying query failing, she answered *"I
couldn't get that — the system's not set up for that right now"* rather than
inventing a number.

**Where you can talk to her:**

- **The web app's Annie page** — a chat interface like any other assistant.
- **Telegram** — DM the bot directly, no `/commands` needed; just talk. Each
  chat keeps its own conversation history.
- **Discord** — DMs are always answered; in a server channel she answers when
  @mentioned. Same underlying agent, same rules, same data.

All three run through the same code (`app/annie/service.py`) — a conversation
started on Telegram and continued on the web app would get the same answers,
because there both is only one Annie, not three. There is currently no access
restriction on either bot: anyone who has the bot's Telegram handle or is in
its Discord server can chat with Annie, at your OpenAI cost. If you need that
locked down, that is a change to make before pointing a bot token at a public
group.

**What the website lets *you* (the operator) do**, separately from chatting
with Annie: the **System Health** page has "Run now" buttons for Discovery,
Enrichment, and Trend analysis — the same three operations that would
otherwise need a terminal and a `curl` command against
`/api/system/run/discovery`, `/run/enrichment`, `/run/trends`. This is the
whole point of that page existing: once this system is deployed and working,
day-to-day operation — checking on it, nudging the pipeline forward, reading
what it found — should not require opening the codebase at all.

---

## How the system works

Six stages, cheap work before expensive work:

1. **Discovery** — a Helius webhook pushes a `CREATE` event the instant a new
   mint happens on a known launchpad program (currently Pump.fun; see
   [What is not built](#what-is-not-built) for the honest scope of this).
   Signature polling exists too, but only as a backfill — it structurally
   cannot keep up with Pump.fun's real transaction volume (measured directly:
   1000 signatures covered 6 seconds of chain time), which is why the webhook
   is the primary mechanism.
2. **Qualification** — did it cross $100k / $250k / $500k / $1M market cap?
   Record *why* we believe so, from which provider, and whether anything
   disagreed.
3. **Enrichment** — only for tokens that qualified: metadata, creator wallet,
   deterministic name/ticker/description features.
4. **Analysis** — the same deterministic feature extraction, feeding the trend
   engine.
5. **Trend engine** — compare each characteristic's recent frequency against
   its own historical baseline. Assign a direction *and* an evidence grade.
6. **Research engine** — Annie's chat agent answers questions against this
   data with a bounded, logged tool-calling loop. (The *autonomous* research
   task runner — Annie picking her own questions unprompted — is not built;
   see below.)

Annie sits at the end and explains what the system found.

---

## What makes her not a chatbot

**She is not the source of truth.** The chain, the providers, the database and
the statistical engine are. She interprets what they produced.

The rules are enforced in her system prompt
([`app/annie/persona.py`](app/annie/persona.py)) and in the agent loop itself
([`app/annie/agent.py`](app/annie/agent.py)), not just requested politely:

- **Every number she states comes from a tool call in that conversation.** She
  has no memory of market caps or percentages. If a tool fails or isn't
  configured, she says so rather than guessing.
- **The final answer is forced through a JSON schema** requiring
  `claim_type` / `confidence` / `citations` — a second, structured OpenAI
  call that restates the reasoning-loop's conclusion. A prompt asking a model
  to self-label its certainty is a request; a schema the API refuses to
  violate is a guarantee. See `agent.py`'s module docstring.
- **She never states a percentage without its denominator.** "23% (14 of 61)",
  never a bare "23%" — enforced by `SampleRef` in the API contract.
- **She distinguishes association from causation** and actively looks for a
  duller explanation — is the pattern concentrated in one launchpad, one
  creator, one week?
- **Every tool call is logged** (`tool_calls` collection) with its arguments,
  success/failure, and which conversation it belongs to (§47).

The data model enforces the same discipline the prompt asks for:

- A trend carries **two independent axes**: `status` (NEW / RISING / STABLE /
  DECLINING / DEAD) and `maturity` (OBSERVATION / CANDIDATE / VALIDATED). A
  characteristic can be rising and still only an observation.
- **Small samples cannot be promoted.** Below 20 qualifying tokens in the
  window, or 5 occurrences of the characteristic, no significance test runs at
  all — the observation is recorded and explicitly marked unsupported.
- **A day with poor ingestion coverage is excluded** from trend windows rather
  than averaged over, because a hole in the denominator looks exactly like a
  real shift.
- **Providers are never silently reconciled.** If two market-data providers
  disagree materially, both values are recorded and the token is marked
  `disputed`. Nothing is averaged.
- **$100k findings are not $1M findings.** They are separate cohorts and
  separate documents.

---

## The web app

Twelve destinations in the sidebar, grouped by what you're doing rather than
by data model:

| Group | Pages |
|---|---|
| — | **Dashboard** (overview + freshness), **Annie** (chat) |
| Intelligence | Trends, Research, Reports |
| Catalogue | Tokens, Launchpads, Creators, Narratives |
| System | Data Sources, System Health, Settings |

Tokens, Trends, Launchpads and Creators each open onto a detail page for one
record (`/tokens/:mint`, `/trends/:slug`, and so on) — not separate nav
destinations, but real pages with their own URLs. **System Health** is where
you check whether every provider is reachable and configured, and where the
three pipeline stages (Discovery / Enrichment / Trend analysis) can be run
on demand. Six of the twelve destinations also appear in a mobile tab bar.

Logging in requires the single operator credential you set as
`AUTH_USERNAME` / `AUTH_PASSWORD` — see [Design decisions worth
knowing](#design-decisions-worth-knowing) for how sessions actually work.

---

## What's actually built and working

Verified by actually running it — a real Firestore project, a real OpenAI
call, a real Helius webhook delivery — not just code that looks plausible in
review.

### Backend — Python / FastAPI / Firestore

```
app/
  config.py               Tiered capability config — Firestore + auth + all providers
  auth.py                 Bearer-token session issuance/verification
  main.py                 App entry, lifespan, router-level auth guard

  db/
    firestore.py           Async Firestore client + service-account auth
    base.py                 Money/slug helpers, generic dataclass<->doc conversion
    enums.py                 Engine-agnostic enums
    repo.py                  The Firestore repository — every read/write
    models/                  Plain dataclasses, one per collection

  providers/
    helius.py                RPC + discovery backfill; KNOWN_LAUNCHPAD_PROGRAMS
    dexscreener.py            Default market-data source, no key required
    tavily.py, openai_provider.py
    registry.py               market_primary -> DexScreener, launches -> Helius

  bots/
    telegram_bot.py           Long-polling bot, shares app/annie/service.py
    discord_bot.py             Gateway client, same shared service

  pipeline/
    qualification.py         Provider-only, no DB coupling
    discovery.py              Stage 1 — webhook-primary, polling backfill
    enrichment.py              Stage 2/3, Firestore-backed

  trends/
    lifecycle.py              Pure functions, no DB coupling
    engine.py                  Firestore cohort queries + the same statistics

  annie/
    persona.py                System prompt and voice
    agent.py                   The chat agent loop — tool-calling + schema-forced answers

  api/
    schemas.py                IDs are strings (mint/slug/wallet, not autoincrement)
    routes/
      auth.py                  Login/logout/session check
      webhooks.py               Helius's server-to-server callback (§76)
      system.py, catalogue.py, intelligence.py, annie.py
```

### Frontend — Vite + React

The 12-destination interface described [above](#the-web-app), plus
[`src/pages/Login.jsx`](src/pages/Login.jsx) and an auth gate in `App.jsx`
wrapping the whole app.

**Frontend location note:** the frontend is **not** in a `frontend/`
directory — it lives at the repository root (`index.html`, `vite.config.js`,
`package.json`, `src/`). The backend is at `app/`, also at the root, not under
`backend/`.

### Verified end-to-end

- Backend boots cleanly against a live Firestore project (service account
  auth confirmed working).
- Login issues a Bearer token; every protected route correctly 401s without
  one and 200s with a valid one, cross-origin, across two unrelated domains
  (Vercel + Railway).
- A real chat turn ran the full tool-calling loop against live OpenAI, logged
  the tool call, and produced a correctly-labeled answer when the underlying
  query failed — cost and latency were recorded per turn.
- A real Helius webhook delivery — not a simulated payload — landed in
  Firestore as a fully-parsed token (mint, creator wallet, launchpad, launch
  signature) roughly 6 seconds after the on-chain event. See
  [What is not built](#what-is-not-built) for how the correct webhook filter
  was found.
- Both Telegram and Discord bots hold a conversation across multiple
  messages, sharing the same underlying agent as the web chat.

---

## What is not built

Be direct about this before relying on anything.

| Area | State |
|---|---|
| Firestore persistence, repository layer | **Complete, verified against a live project** |
| Provider adapters, registry, failover | Complete. Helius + DexScreener **verified reachable** — the only two adapters this deployment has; Bitquery/Birdeye were removed entirely rather than kept unused (§75) |
| Statistical engine, trend lifecycle | Complete — pure functions |
| Qualification | Complete — provider-only, no DB coupling |
| Discovery (Stage 1) | **Working, narrow — webhook-driven.** A Helius webhook (`transactionTypes: ["CREATE"]`) pushes new Pump.fun mints in real time, verified against a real delivery; signature polling remains only as a backfill (§76). No automatic discovery of *unknown* launchpads |
| Enrichment (Stage 2/3) | **Working.** Metadata, creator wallet, deterministic features |
| Trend engine | **Working.** Not yet run against real qualified-token volume |
| **Annie's chat agent** | **Built and verified against live OpenAI**, on web, Telegram and Discord |
| Single-operator authentication | **Built and verified** — Bearer-token login/session/route-guarding confirmed working cross-origin |
| API routes | Complete for all read paths + the routes above |
| Frontend (12 destinations + login) | Built; `npm run build` and the visual-check harness (`tools/shoot.js`) have not been re-run recently — see [Picking up the unfinished work](#picking-up-the-unfinished-work) |
| **Autonomous research task runner** | **Not written.** `ResearchTask` documents can be created (manually, via the API) but nothing picks one up and works it — Annie only answers what she's asked, in the moment |
| **Report generator** | **Not written** — §41/§42 |
| **Narrative clustering** | **Not written** — the `narratives` collection exists but nothing populates it; trend detection uses the deterministic `token.theme` feature as a stand-in |
| **Scheduler / background workers** | **Not written.** No queue is configured (Redis was removed — nothing consumed it, see Build.md §75). Use the manual trigger endpoints instead (`POST /api/system/run/discovery`, `/run/enrichment`, `/run/trends`), or the same buttons on System Health |
| **Firestore composite indexes** | Declared in `firestore.indexes.json`; deploy them once — see [Setup](#step-2-deploy-firestore-indexes) |
| **Tests** | **Not written** |

Three specific things to know before trusting output:

- **Discovery only sees Pump.fun right now.** `app/providers/helius.py`'s
  `KNOWN_LAUNCHPAD_PROGRAMS` is a short, explicit list, and the Helius webhook
  (see `app/api/routes/webhooks.py`) is registered against exactly that list's
  program IDs. A launch on a program not in that list is invisible — not
  filtered out, not deprioritized, *invisible*. This directly limits Build.md
  §5's "must not be limited to Pump.fun" goal until either more program IDs
  are added (and the webhook's `accountAddresses` updated to match) or
  Bitquery is reinstated as the discovery source (§76 explains the trade).
- **The webhook's `transactionTypes` filter matters and isn't documented
  anywhere authoritative.** A real Pump.fun create transaction classifies as
  `type: "CREATE"`, `source: "PUMP_FUN"` in Helius's enhanced parser —
  confirmed empirically against a real delivery, not from Helius's docs (the
  initial guess, `TOKEN_MINT`, silently produced zero deliveries for hours).
  If discovery ever goes quiet again, checking the registered webhook's
  `transactionTypes` against a fresh empirical sample (fetch a known-new mint
  from `frontend-api-v3.pump.fun/coins`, walk its signature history back to
  genesis, diff the raw tx logs against Helius's enhanced parse of that same
  signature) is the reliable way to re-derive the correct value — not
  re-reading Helius's docs.
- **Without Firestore's composite indexes deployed, list/dashboard queries
  will 500** with a `FAILED_PRECONDITION: The query requires an index` error.
  This is normal, expected Firestore behavior, not a bug — see the setup step
  below. The error message itself contains a direct link to create the
  specific index it's missing, so even skipping the batch-deploy step, the
  app tells you exactly what to click.

---

## What I need from you

**Never paste these into a chat, a commit or an issue.** They go in `.env`
locally and into your host's secret store in production. `.env` is already
gitignored, and so is any file matching `*firebase-adminsdk*.json` — the
service-account key Firebase gives you as a download lands in that pattern by
default.

| Variable | What it is | Where from | Needed? |
|---|---|---|---|
| `FIREBASE_SERVICE_ACCOUNT_FILE` or `_JSON` | Firestore server credential | Firebase Console → Project settings → Service accounts | **Required to start** |
| `AUTH_SECRET` | Signs your login session | You generate it | **Required to start** |
| `AUTH_USERNAME` / `AUTH_PASSWORD` | Your login | You choose | **Required to start** |
| `OPENAI_API_KEY` | Annie + reasoning | platform.openai.com | Primary |
| `HELIUS_API_KEY` + `HELIUS_RPC_URL` | Discovery + chain truth (§76) | helius.dev | Primary — **without this, nothing is ever discovered** |
| `HELIUS_WEBHOOK_SECRET` | Authenticates Helius's webhook calls to `/api/webhooks/helius` | You choose, then pass the same value as `authHeader` when registering the webhook (Setup Step 4) | Primary — **without this, discovery silently gets nothing** even with a valid `HELIUS_API_KEY` |
| `TAVILY_API_KEY` | Annie's web research | tavily.com | Optional |
| `TELEGRAM_BOT_TOKEN` / `DISCORD_BOT_TOKEN` | Chat with Annie from either | BotFather / Discord Developer Portal | Optional |
| `VITE_API_BASE_URL` | Where the frontend finds the API | Your backend's URL | **Required to build** |

**DexScreener needs no account and no key** — it's this deployment's default
market-data source (§76).

You can start with just the three REQUIRED rows, boot it, and let the
**System Health** page (or `GET /api/system/capabilities`) tell you exactly
what else to add.

---

## Setup, step by step

### Step 1 — Firebase / Firestore (required)

1. **https://console.firebase.google.com** → create a project (or use an
   existing one) → enable **Cloud Firestore** (Native mode, not Datastore
   mode) from the console if it isn't already.
2. **Project settings** (gear icon) → **Service accounts** tab → **Generate
   new private key**. This downloads a JSON file — this is your server
   credential. It is **not** the same thing as the "Firebase SDK snippet" you
   get from Project settings → General → Your apps, which is a browser/client
   config and cannot authenticate a backend.
3. **Local dev:** move the downloaded file into the repo root and point at it:
   ```env
   FIREBASE_SERVICE_ACCOUNT_FILE=your-project-firebase-adminsdk-xxxxx.json
   ```
   It matches `.gitignore`'s `*firebase-adminsdk*.json` pattern automatically.
4. **Hosted deploys** can't upload a file — paste the file's exact JSON
   contents (all on one line) into `FIREBASE_SERVICE_ACCOUNT_JSON` in your
   host's environment variables instead, and leave `_FILE` unset.

### Step 2 — Deploy Firestore indexes

Several list/dashboard queries filter on one field and sort by another, which
Firestore requires a composite index for. `firestore.indexes.json` at the repo
root declares the ones this codebase needs.

**Fastest path — let Firestore tell you as you go.** Every query missing an
index fails with a `FAILED_PRECONDITION` error containing a direct console
link that pre-fills the exact index to create. Click it, wait ~1-2 minutes for
it to build, retry. You'll hit a handful of these the first time you load the
dashboard; after that, done.

**Batch path — deploy them all at once**, if you have Node available:
```bash
npx firebase-tools login          # one-time, opens a browser
npx firebase-tools deploy --only firestore:indexes --project your-project-id
```

### Step 3 — Generate your auth secret and pick credentials

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```
```env
AUTH_SECRET=<the long random string>
AUTH_USERNAME=<pick something>
AUTH_PASSWORD=<pick a real password>
```
Use a **different** `AUTH_SECRET` in production than locally — it signs every
session token, and a leaked local one shouldn't grant access to production.

### Step 4 — Helius (primary — discovery and chain truth)

1. **https://helius.dev** → sign up → dashboard (usually
   **https://dashboard.helius.dev**) → create an API key. Free tier is fine
   to start.
2. **You need two values from this one key:**
   ```env
   HELIUS_API_KEY=1234abcd-5678-...
   HELIUS_RPC_URL=https://mainnet.helius-rpc.com/?api-key=1234abcd-5678-...
   ```
   The key appearing twice is correct — setting only one reports `DEGRADED`,
   not `AVAILABLE`.
3. **Register the discovery webhook** — a one-time REST call against Helius's
   API, not something the app does for you. Pick a `HELIUS_WEBHOOK_SECRET`
   (any long random string) and set it in `.env` first, then:
   ```bash
   curl -X POST "https://api.helius.xyz/v0/webhooks?api-key=<HELIUS_API_KEY>" \
     -H "Content-Type: application/json" \
     -d '{
       "webhookURL": "https://<your-railway-domain>/api/webhooks/helius",
       "transactionTypes": ["CREATE"],
       "accountAddresses": ["6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"],
       "webhookType": "enhanced",
       "authHeader": "<HELIUS_WEBHOOK_SECRET>"
     }'
   ```
   `transactionTypes` must be `["CREATE"]`, not the more obvious-sounding
   `TOKEN_MINT` — see [What is not built](#what-is-not-built) for how that was
   confirmed. The response includes a `webhookID`; save it, you'll need it to
   update `accountAddresses` later if you add launchpads. Free tier includes
   exactly one webhook.
4. **Watch your credit usage.** Helius bills webhook deliveries by credit, and
   this webhook fires on every real Pump.fun token creation — a real, ongoing
   volume, not a one-off. Check `dashboard.helius.dev`'s usage page after a
   day of live traffic and confirm it fits your plan before assuming it's
   free forever.

### Step 5 — OpenAI (primary — Annie herself)

1. **https://platform.openai.com** → add a payment method and buy a few
   dollars of credit first — a zero-balance account returns quota errors that
   look like a broken integration, not an empty wallet.
2. **API keys** → **Create new secret key** → copy it immediately.
   ```env
   OPENAI_API_KEY=sk-proj-...
   ```

### Step 6 — Optional: Tavily and the bots

```env
TAVILY_API_KEY=tvly-...       # https://tavily.com
TELEGRAM_BOT_TOKEN=...        # BotFather on Telegram -> /newbot
DISCORD_BOT_TOKEN=...         # Discord Developer Portal -> your app -> Bot page
```

Both bots need the **Message Content Intent** enabled in their respective
developer portals to read what's said to them, and both run as background
tasks inside the same backend process — no separate deployment needed.

### Step 7 — Fill in `.env`

```bash
cp .env.example .env
```
Every variable is documented in the file with its tier and what breaks
without it. Two application settings matter as much as the keys:

```env
CORS_ORIGINS=http://localhost:5180
VITE_API_BASE_URL=http://localhost:8000
```

### Step 8 — First run

```bash
python -m venv .venv
.venv\Scripts\activate        # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Configuration is either complete, or the process **refuses to start naming the
missing required variable**, or it **starts with a warning banner** listing
every degraded capability.

Then:

```bash
npm install
npm run dev
```

Open **http://localhost:5180** → you'll land on the login screen first (the
`AUTH_USERNAME` / `AUTH_PASSWORD` you set in Step 3) → **Dashboard**.

---

## Running locally

### Frontend only, against the fixture server (no backend, no keys)

Two things do not travel with the project, because both are gitignored:
`node_modules/` and `.env`. On a new machine, do this first.

```bash
npm install
cp .env.example .env          # PowerShell: Copy-Item .env.example .env
```

Then, in two terminals:

```bash
# Terminal 1 — the fake API
npm run fixtures

# Terminal 2 — the app
npm run dev
```

Open **http://localhost:5180**. No Python, no Firebase project, no API keys.
The fixture server (`tools/fixture-server/server.js`) implements the real API
contract with generated data — including a login stub, so the auth gate does
not block this path.

**If something is wrong, the screen tells you which:**

| What you see | Cause | Fix |
|---|---|---|
| "Annie can't start — Missing required environment variable `VITE_API_BASE_URL`" | No `.env` file | `cp .env.example .env`, restart `npm run dev` |
| App loads to the login screen but login fails against the fixture server | Fixture server not running | Start Terminal 1: `npm run fixtures` |
| `Port 5180 is already in use` | Usually an old copy of this same server running already | Check your browser before killing anything |
| `vite: not found` / `Cannot find module` | Dependencies not installed | `npm install` |

### Frontend + real backend

```bash
# Terminal 1 — backend
uvicorn app.main:app --reload

# Terminal 2 — frontend
npm run dev
```

### Local URLs

| What | URL |
|---|---|
| Frontend | **http://localhost:5180** |
| API | http://localhost:8000 |

### Why port 5180, not Vite's default 5173

Multiple Vite projects on one machine all try to claim 5173; the losers don't
error, they bind a *different network interface* on the same port, so
`localhost:5173` silently serves whichever project won. `vite.config.js` pins
this project to **5180** with `strictPort: true`, so a collision fails loudly
instead of moving. Use `localhost:5180`, not `127.0.0.1:5180` — the dev server
binds by name, and Windows resolves `localhost` to `::1` first.

### Verifying frontend changes

```bash
cd tools && node shoot.js ./shots
```
Renders every route at 1440px and 390px, both themes, and fails on console
errors, page exceptions, failed requests or horizontal overflow. Run it before
trusting a frontend change.

---

## Deploying

Two pieces: a **Python API** and a **static frontend**, deployable to entirely
different hosts on entirely different domains — nothing about this system
needs them to share a domain.

### ⚠ Read this before deploying the frontend

**Vite bakes `VITE_API_BASE_URL` into the JavaScript at build time.** Set it
in your host's **build** environment variables, not runtime — changing it
requires a rebuild and redeploy, not just a restart.

### ⚠ Read this before deploying the backend

**`CORS_ORIGINS` must be the exact frontend origin**, `https://` included, no
trailing slash — a mismatch here is the single most common cause of "Can't
reach the API" once both pieces are actually deployed.

There is no cookie/`SameSite` configuration to get right, deliberately: the
session is a **Bearer token** in the `Authorization` header, not a cookie
(see [Design decisions worth knowing](#design-decisions-worth-knowing)), so a
frontend and backend on two completely unrelated domains (a Vercel domain and
a Railway domain, say) work the same way a same-domain deployment would. CORS
runs with `allow_credentials=False` accordingly — there is nothing
credential-related for a browser to block.

### Backend — Railway

1. Push this repo to GitHub.
2. **https://railway.app** → New Project → Deploy from GitHub repo.
3. Root directory: repository root (the backend is `app/` at the root, not
   under a `backend/` subdirectory).
4. Start command:
   ```
   uvicorn app.main:app --host 0.0.0.0 --port $PORT
   ```
5. Variables:

   | Variable | Value |
   |---|---|
   | `FIREBASE_SERVICE_ACCOUNT_JSON` | the service-account file's exact contents, one line |
   | `AUTH_SECRET` | a **new** random string, not your local one |
   | `AUTH_USERNAME` / `AUTH_PASSWORD` | your login |
   | `OPENAI_API_KEY` | Step 5 |
   | `HELIUS_API_KEY` / `HELIUS_RPC_URL` / `HELIUS_WEBHOOK_SECRET` | Step 4 |
   | `TAVILY_API_KEY` | optional |
   | `TELEGRAM_BOT_TOKEN` / `DISCORD_BOT_TOKEN` | optional |
   | `CORS_ORIGINS` | your frontend URL, e.g. `https://annie.vercel.app` |

6. Note the public URL — that is your `VITE_API_BASE_URL`.
7. **Set Replicas to 1.** More than one replica means more than one process
   long-polling Telegram (or holding a Discord Gateway connection)
   simultaneously with the same bot token — Telegram in particular rejects
   the second poller with a `409 Conflict` loop.
8. Once deployed, register the Helius webhook (Setup Step 4.3) against this
   service's public URL, not `localhost`.

### Backend — Render

| Setting | Value |
|---|---|
| Root directory | repository root |
| Build command | `pip install -r requirements.txt` |
| Start command | `uvicorn app.main:app --host 0.0.0.0 --port $PORT` |
| Environment | Python 3 |

Same variables and same single-instance note as above.

### Frontend — Vercel

| Setting | Value |
|---|---|
| Root directory | repository root |
| Framework preset | Vite |
| Build command | `npm run build` |
| Output directory | `dist` |

Environment variable — exactly one, under **Build**:

| Variable | Value |
|---|---|
| `VITE_API_BASE_URL` | your backend URL, e.g. `https://annie-api.up.railway.app` |

No trailing slash. Include `https://`.

Then **go back to the backend, set `CORS_ORIGINS` to the Vercel URL, and
redeploy the backend.** Until you do, the browser blocks every call.

### Frontend — Netlify / Cloudflare Pages

Root directory the repo root, build `npm run build`, publish `dist`, and the
same `VITE_API_BASE_URL` under **Build** variables.
[`public/_redirects`](public/_redirects) is already committed — without it,
refreshing on `/trends` returns 404, because the app uses client-side
routing. Vercel infers this and ignores the file.

### Wiring checklist

All four must be true:

- [ ] Backend deployed, and its `/health` returns `{"status":"ok"}`
- [ ] Firestore composite indexes deployed or clicked-through (Setup Step 2)
- [ ] Frontend's `VITE_API_BASE_URL` is the exact backend URL, no trailing
      slash, **and the frontend was rebuilt after setting it**
- [ ] Backend's `CORS_ORIGINS` contains the exact frontend origin, and the
      backend was restarted after setting it

If the app loads but every panel says "Can't reach the API," it's almost
always CORS or a stale frontend build. If login itself returns a clean error
response rather than hanging, read the error — with Bearer tokens there is no
silent cross-origin cookie failure mode left to debug.

---

## Architecture

```
                    SOLANA ECOSYSTEM
                           │
                  known launchpad programs
                     (Pump.fun today)
                           │
                        HELIUS
                 (webhook: CREATE events,
                  + RPC backfill/truth)
                           │
                           ▼
                    DATA INGESTION
                           ↓
                     QUALIFICATION  ←──── DEXSCREENER (market data, no key)
                           ↓
                       FIRESTORE
                           │
       ┌───────────────────┼───────────────────┐
       ↓                   ↓                   ↓
   Token Data          Creator Data       Launchpad Data
       │                   │                   │
       └───────────────────┼───────────────────┘
                           ↓
                    FEATURE ANALYSIS
                           ↓
                 STATISTICAL ENGINE
                           ↓
                    TREND ENGINE
                           ↓
                    TREND MEMORY
                           ↓
                  RESEARCH TOOLS
                (read-only, logged)
                           │
                 ┌─────────┴─────────┐
                 ↓                   ↓
            Web Research        AI Reasoning
             (Tavily, opt.)      (OpenAI)
                 │                   │
                 └─────────┬─────────┘
                           ↓
                         ANNIE
                    (agent.py loop)
                           │
              Bearer-token authenticated
                           │
          ┌────────────────┼────────────────┐
          ↓                ↓                ↓
   REACT FRONTEND      TELEGRAM          DISCORD
```

Providers sit behind adapters (§70). Swapping DexScreener for Bitquery as
`market_primary`, or adding a second discovery source alongside Helius, is a
change to `registry.py` — the trend engine, research tools, Annie, the
frontend and the schema are untouched. This is not theoretical: it's exactly
what happened between the original Postgres/Bitquery design and this one.

---

## Design decisions worth knowing

Load-bearing. Several exist specifically to prevent a failure that looks fine
in code review.

**Two axes for trends, never one.** Direction and evidential standing are
separate fields and separate UI marks. Collapsing them is how a two-day fluke
gets displayed as a finding.

**A rate never travels without its denominator.** `SampleRef` carries `count`,
`total` and `frequency` together; `<Sample>` is the only way to render one.

**Money is a string over the wire and in Firestore, `Decimal` in Python.**
Never a float, never a native Firestore number. A token must not drift across
the $100k line through representation error — see Build.md §75.

**Null is unknown, never zero.** A missing market cap renders `—`.

**Providers are never silently reconciled.** No averaging, and no value
returned without saying where it came from.

**Firestore IDs are natural keys.** A token's document ID is its mint, not an
autoincrement integer — see Build.md §75. The frontend never depended on IDs
being numeric (it routes on mint/slug/wallet already), so this cost zero
frontend changes.

**Annie's final answer is schema-forced, not prompt-requested.** The
tool-calling loop can produce anything; the finishing call cannot skip
`claim_type`/`confidence`/`citations` — the API rejects a response that omits
them. See `app/annie/agent.py`.

**Auth is a Bearer token, not a cookie — and that's one router-level
dependency, not per-route discipline.** The frontend and backend run on two
unrelated domains, where cross-origin cookies are unreliable by default in
every major browser. The `Authorization` header sidesteps that entirely; the
frontend stores the token in `localStorage` and attaches it itself
(`src/api/client.js`). A route added later is protected automatically because
the guard is attached where the router is mounted (`app/main.py`), not
copy-pasted into each handler. See `app/auth.py`'s module docstring for the
full reasoning.

**Discovery is push, not pull.** A Helius webhook, not a poll loop, is the
primary way new tokens are found — Pump.fun's real transaction volume is high
enough that polling structurally cannot keep up. See Build.md §76.

**Design split.** Data surfaces are near-monochrome and colour means status,
verification or direction. Annie's chat is the only warm surface and owns the
only use of `--annie`. Personality lives in her voice, not in your charts.

---

## Picking up the unfinished work

Each item is reachable without touching the others.

1. **Broaden discovery beyond Pump.fun.** Add program IDs to
   `KNOWN_LAUNCHPAD_PROGRAMS` in `app/providers/helius.py`, then update the
   registered webhook's `accountAddresses` to match (`PUT
   https://api.helius.xyz/v0/webhooks/<webhookID>?api-key=...` with the full
   new list — see Step 4 in Setup) as you identify them, or reinstate Bitquery
   as the discovery source (§76) for indexed, ecosystem-wide coverage instead
   of a fixed list.

2. **The autonomous research task runner.** `ResearchTask` documents exist and
   `budget_exhausted` is implemented on the dataclass; nothing polls the
   `queued` status and works a task end-to-end without a user typing a
   question. Build.md §35-§36 describes the bounded-loop shape it should take
   — largely the same pattern `agent.py`'s single-turn loop already
   demonstrates, run against a stored question instead of a live chat message.

3. **Background workers / scheduler.** No queue is configured — Redis was
   removed from this deployment entirely (nothing consumed it; add it back
   if a worker needs it). The daily cycle is Build.md §40; today it's the
   three manual trigger endpoints (`/api/system/run/discovery`,
   `/run/enrichment`, `/run/trends`), also exposed as buttons on System
   Health. A cron calling those three endpoints in order is the fastest path
   to "automatic" without building a queue.

4. **Report generator.** Build.md §41-§42. `Report` documents and the API
   routes to serve them exist; nothing writes one yet.

5. **Narrative clustering.** The `narratives` collection and its API routes
   exist and return correctly-shaped empty results. A stage that actually
   clusters the deterministic `theme` features into named narratives (§16)
   would populate it.

6. **Re-verify the frontend.** `npm run build` and `tools/shoot.js` — run
   these before trusting a visual claim about the app.

7. **Tests.** Start with `app/analysis/stats.py` and `app/trends/lifecycle.py`
   — pure functions, and where a subtle error does the most damage.

---

## What this system will not do

No trading, no execution, no portfolio management, no price prediction, no
tick-level storage, no wallet-graph analysis. Build.md §71 defers all of it and
the architecture has no seam for it.
