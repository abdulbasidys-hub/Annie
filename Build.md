# Build Specification — Annie

## Solana Memecoin Intelligence & Research System

---

# 1. Project Overview

Build a continuously learning **Solana Memecoin Intelligence System** operated through an AI assistant named **Annie**.

The system's primary purpose is to research successful Solana memecoins, discover recurring patterns, monitor changing narratives and launchpad ecosystems, and help generate research-backed ideas.

The central question is:

> **What can we learn from Solana tokens that successfully reach meaningful market-cap milestones, what characteristics do they repeatedly share, and what new patterns are emerging right now?**

The system is primarily a **research and intelligence engine**, not a trading bot.

It should continuously evolve from:

> "Here are successful tokens."

to:

> "Here are the characteristics currently associated with successful tokens."

and eventually:

> "Here are the strongest current patterns, how they are changing, what is emerging, and which findings are worth investigating further."

---

# 2. Core Philosophy

The system should prioritize:

1. High-quality data
2. Reliable blockchain information
3. Large historical datasets
4. Statistical analysis
5. Pattern discovery
6. Trend detection
7. Independent verification
8. AI-assisted research
9. Continuous learning
10. Cost efficiency

The AI must **not invent patterns simply because they sound plausible**.

The database and analytical engine are the source of truth.

Annie interprets, investigates, compares and explains what the data shows.

---

# 3. Scope

The system must NOT be limited to Pump.fun.

It should cover the broader **Solana memecoin launch ecosystem**.

This includes:

* Pump.fun
* Raydium
* PumpSwap
* Meteora
* Orca
* Jupiter-related activity
* New launchpads
* Emerging launch platforms
* New migration mechanisms
* Other relevant Solana token-launch ecosystems

The system should automatically monitor the ecosystem for new launchpads and significant changes.

If a new launchpad becomes important, Annie should be able to recognize it as an emerging research subject.

---

# 4. Success Qualification

The initial research dataset should focus on tokens that achieve meaningful success.

The primary threshold is:

```text
$100,000+ market capitalization
```

The system should classify successful tokens into:

```text
$100k+
$250k+
$500k+
$1M+
```

The system should NOT assume that $100k is the only definition of success.

Higher milestones exist so the system can investigate:

> What separates ordinary winners from exceptional winners?

The exact qualification logic must be configurable.

The system must record the evidence used to determine qualification.

---

# 5. Launchpad-Agnostic Architecture

The system must not hardcode the assumption that successful tokens originate from Pump.fun.

Every token should have a:

```text
launchpad
migration_platform
destination_dex
ecosystem
```

where available.

The system should be capable of discovering:

* New launchpads
* Launchpad growth
* Launchpad decline
* Migration patterns
* Migration destinations
* Changes in creator behavior
* Movement of creators between platforms
* Movement of traders between ecosystems

Example research question:

> "Are creators moving away from Pump.fun toward another launchpad?"

Another:

> "Did a new launchpad become significantly more successful during the last 14 days?"

Another:

> "What event or change appears to coincide with the migration?"

---

# 6. Token Data

For every qualifying token, store:

* Mint/contract address
* Name
* Ticker
* Description
* Image URL
* Creator wallet
* Launchpad
* Destination DEX
* Launch timestamp
* Launch date
* Migration timestamp
* Migration destination
* Time from launch to migration
* Market cap at qualification
* Timestamp qualification threshold was reached
* Highest observed market cap
* Timestamp of peak
* Liquidity
* Volume at important milestones
* Token age at milestones
* Available social links
* Website
* Telegram
* X/Twitter
* Other public links
* Data sources
* Verification status

---

# 7. Market-Cap Milestones

Track important milestones rather than unnecessary tick-by-tick data.

At minimum:

```text
Launch
Migration
$100k
$250k
$500k
$1M
Peak
```

For each milestone, where data is available:

* Timestamp
* Market cap
* Liquidity
* Volume
* Token age
* Holder count
* Relevant market information

The system should avoid excessive time-series storage during the initial version.

The objective is research, not reconstructing every transaction.

---

# 8. Historical Database

The system must permanently retain normalized research data.

There must be a distinction between:

### Historical Dataset

All successfully collected research subjects.

### Current Intelligence Dataset

Recent data used for detecting current trends.

The current intelligence window must **NOT be limited to 30 days**.

The system should support configurable comparison windows such as:

```text
1 day
3 days
7 days
14 days
30 days
90 days
180 days
1 year
All-time
```

The user should eventually be able to build a dataset covering multiple years.

---

# 9. Data Provider & API Architecture

The system must NOT rely primarily on scraping.

Structured APIs, blockchain infrastructure and indexing providers should provide the core data.

Web research should be used selectively.

Provider-specific code must be isolated behind provider adapters.

---

## 9.1 Primary Blockchain Infrastructure — Helius

Use Helius as the initial primary Solana infrastructure provider.

Potential uses:

* Solana RPC
* Transaction retrieval
* Account information
* Token information
* Digital asset queries
* Webhooks
* Blockchain verification
* Real-time monitoring where appropriate

Environment variables:

```env
HELIUS_API_KEY=
HELIUS_RPC_URL=
```

Create:

```text
BlockchainProvider
```

with a Helius implementation:

```text
HeliusAdapter
```

The rest of the application must never depend directly on Helius-specific code.

**§9.2 Amendment — Helius also serves discovery in this deployment.** With
Bitquery not used by default (§10's amendment below), Stage 1 discovery's
primary path is a Helius **webhook** (`app/api/routes/webhooks.py`,
`transactionTypes: ["CREATE"]`) pushing new mints on known launchpad program
IDs in real time; RPC polling (`getSignaturesForAddress` +
parsed-transaction inspection) survives only as a backfill, since it
measurably can't keep pace with Pump.fun's real transaction volume — see
§76's fuller account of why. Either way this is narrower than an indexer:
only programs explicitly listed in `KNOWN_LAUNCHPAD_PROGRAMS` (currently
Pump.fun) are seen. See `app/providers/helius.py` and README's "What is not
built" for the honest scope of this.

---

# 10. Primary Market and DEX Intelligence — Bitquery

Use Bitquery as the primary indexed Solana market/DEX data provider.

Potential uses:

* Solana token launches
* Pump.fun launches
* Pump.fun activity
* Bonding/migration events
* Raydium activity
* PumpSwap activity
* DEX trades
* New pairs
* OHLCV
* Liquidity
* Volume
* Market-cap information
* Launchpad discovery
* Wallet activity where appropriate
* Streaming events

Environment variable:

```env
BITQUERY_API_KEY=
```

Create adapters such as:

```text
MarketDataProvider
LaunchDataProvider
DexDataProvider
```

with:

```text
BitqueryAdapter
```

The intelligence engine must not depend directly on Bitquery's GraphQL schema.

**Amendment — not used by default in this deployment.** The adapter is fully
built (`app/providers/bitquery.py`), but discovery and market data run off
Helius + DexScreener instead (§9.2, §11). Set `BITQUERY_API_KEY` and it joins
cross-validation automatically; nothing else changes.

---

# 11. Secondary Market Provider — DexScreener

Support DexScreener as a secondary market-data provider.

Potential uses:

* Token discovery
* Pair discovery
* Price
* Liquidity
* Volume
* Market-cap information
* DEX information
* Cross-validation

Environment variable:

```env
DEXSCREENER_API_KEY=
```

The integration should remain optional where authentication is not required.

Create:

```text
DexScreenerAdapter
```

It should be used primarily for:

* Secondary verification
* Discovery
* Data gaps

**Amendment — this is the primary market-data source in this deployment**
(§9.2), not secondary: it requires no key, so it is the qualification
engine's default `market_primary` (see `app/providers/registry.py`). Bitquery
remains the spec's intended primary and can be restored by setting
`BITQUERY_API_KEY` and swapping the two properties in the registry.

---

# 12. Secondary Market Provider — Birdeye

Support Birdeye as another optional market-data provider.

Potential uses:

* Token market data
* Price
* Volume
* Liquidity
* Historical market information
* Token statistics
* Cross-validation

Environment variable:

```env
BIRDEYE_API_KEY=
```

Create:

```text
BirdeyeAdapter
```

Do not make the entire system dependent on Birdeye.

---

# 13. External Web Research — Tavily

Use Tavily for Annie's external web research.

Tavily should NOT replace blockchain data.

Use it for questions such as:

* What happened to this launchpad?
* Why is a narrative suddenly gaining attention?
* Did a relevant event occur?
* What is happening in the Solana ecosystem?
* What are people discussing publicly?
* Did a new launchpad recently appear?
* What external event may explain a trend?

Environment variable:

```env
TAVILY_API_KEY=
```

Create:

```text
WebResearchProvider
TavilyAdapter
```

Annie should call external web research selectively.

It should not search the web for every token.

---

# 14. Social Data

Social data is supplementary.

The architecture should support:

```text
SocialDataProvider
```

Potential future providers:

* X/Twitter
* Telegram
* Other public social-data APIs

Social data should be used for:

* Narrative discovery
* Attention analysis
* Social presence
* External context
* Trend validation

Missing social data must never automatically disqualify a token.

---

# 15. Image Analysis

Token images should be analyzed when available.

The system should extract structured characteristics such as:

* Animal
* Human
* Celebrity
* Political figure
* Cartoon
* Existing meme
* Internet culture
* AI-generated style
* Character-based
* Text-heavy
* Brand parody
* Simple graphic
* Abstract
* Absurd
* Other discovered categories

The system should allow new categories to emerge.

Do not create an unnecessarily complicated computer-vision system in version one.

The objective is:

> Identify visual characteristics that occur disproportionately among successful tokens.

---

# 16. Narrative Analysis

Analyze:

### Name

Look for:

* Recurring words
* Themes
* Cultural references
* Current events
* Animals
* AI
* Politics
* Celebrities
* Crypto culture
* Gaming
* Finance
* Parodies
* Internet memes
* Absurd concepts

### Ticker

Analyze:

* Length
* Structure
* Repeated words
* Abbreviations
* Number/letter combinations
* Narrative-specific patterns
* Unusual recurring structures

### Description

Analyze:

* Recurring words
* Phrases
* Narrative positioning
* References
* Current events
* Meme language
* Marketing language

The system should discover categories rather than relying exclusively on hardcoded categories.

---

# 17. Creator Intelligence

For each creator wallet, maintain:

* Total launches
* Previous $100k+ winners
* Previous $250k+ winners
* Previous $500k+ winners
* Previous $1M+ winners
* Success rate
* Number of launches
* Launchpad history
* Recent activity
* Time between launches
* Repeat-success patterns

The system should investigate questions such as:

> Do successful creators produce successful tokens more frequently?

> Are successful creators moving between launchpads?

> Does previous creator success increase the probability of another successful launch?

Do not build complex wallet-graph intelligence initially.

---

# 18. Launchpad Intelligence

Create a dedicated launchpad research system.

Track:

* Launch count
* Successful token count
* $100k+ count
* $250k+ count
* $500k+ count
* $1M+ count
* Success rate
* Average time to milestone
* Creator activity
* Repeat creators
* Migration destinations
* Growth rate
* Decline rate
* Narrative concentration
* Relative share of successful launches

The system should identify:

```text
Emerging launchpads
Growing launchpads
Stable launchpads
Declining launchpads
Dead/irrelevant launchpads
```

Annie should investigate significant changes.

---

# 19. Ecosystem Migration Detection

The system must monitor migration between platforms.

Examples:

```text
Pump.fun → Raydium
Pump.fun → PumpSwap
New Launchpad → Raydium
New Launchpad → Meteora
```

The system should detect when:

* Creators migrate
* Liquidity migrates
* Successful launches concentrate elsewhere
* Trading activity changes
* New launchpads gain market share

The system should attempt to identify possible explanations using external research.

Explanations must be labeled as:

```text
Observed
Supported hypothesis
Speculation
```

---

# 20. Data Acquisition Strategy

Use a staged pipeline.

### Stage 1 — Discovery

Collect minimal information:

```text
Mint
Creator
Launchpad
Timestamp
Relevant launch event
```

### Stage 2 — Qualification

Determine whether the token satisfies the configured research criteria.

### Stage 3 — Enrichment

For qualifying tokens collect:

```text
Metadata
Description
Image
Creator history
Market milestones
Social links
Narrative
Launchpad information
```

### Stage 4 — Analysis

Run statistical and structured analysis.

### Stage 5 — AI Research

Only interesting patterns, anomalies and questions receive expensive AI/web research.

This is important for controlling API and AI costs.

---

# 21. Data Verification

Important facts should have a source.

Store:

```text
source
source_type
timestamp
verification_status
confidence
```

Possible statuses:

```text
Verified
Cross-verified
Unverified
Disputed
Pending
```

If two providers disagree materially, do not silently choose one.

Record the disagreement.

---

# 22. Permanent Data Storage

Use **Cloud Firestore** as the primary database. (Amended from PostgreSQL —
see §72.5 and §75 for why, and for what that trade costs.)

The system should contain collections for at least:

```text
tokens
token_milestones
creators
creator_launches
launchpads
dexes
narratives
token_features
image_features
social_data
market_snapshots
trends
trend_observations
trend_history
research_notes
research_hypotheses
daily_reports
weekly_reports
provider_events
data_quality
```

The final schema may evolve as development progresses.

---

# 23. Raw Data Storage

Store important raw provider responses when useful for:

* Verification
* Debugging
* Reproducibility
* Research

Do not retain every high-volume raw response indefinitely.

Normalized research data should have much longer retention.

---

# 24. Trend Engine

The Trend Engine is the core analytical component.

It should discover recurring characteristics among successful tokens.

A trend should have:

* Trend ID
* Name
* Description
* Category
* Evidence
* Number of qualifying tokens
* Percentage of successful tokens
* Recent frequency
* Historical frequency
* Baseline
* Change
* Direction
* Confidence
* Sample size
* First detected date
* Last observed date
* Current status

---

# 25. Trend Lifecycle

Every trend can have:

### NEW

Recently detected with enough evidence to monitor.

### RISING

Increasing in frequency or importance.

### STABLE

Consistently present.

### DECLINING

Previously strong but becoming less common.

### DEAD

Previously meaningful but no longer supported by current evidence.

A trend should not become "RISING" simply because of one unusual day.

---

# 26. Statistical Evidence

The system must distinguish:

### Observation

Something happened.

### Candidate Trend

Enough evidence exists to investigate.

### Validated Trend

The pattern has meaningful persistent evidence.

The Trend Engine should consider:

* Sample size
* Baseline frequency
* Recent frequency
* Historical frequency
* Magnitude of change
* Persistence
* Variance
* Statistical significance where appropriate

Avoid overreacting to tiny samples.

---

# 27. Success-Level Comparison

Compare:

```text
$100k+
$250k+
$500k+
$1M+
```

Investigate:

> What characteristics are common across successful tokens?

and:

> What characteristics are disproportionately associated with exceptional winners?

A characteristic associated with $100k success must not automatically be treated as associated with $1M success.

---

# 28. Trend Memory

Create persistent Trend Memory.

Trend Memory must retain the history of trends rather than rediscovering everything from scratch.

Example:

```text
Trend:
AI-related narrative

First detected:
August 2

Current frequency:
21%

Baseline:
12%

Direction:
RISING

Confidence:
HIGH

Sample size:
XXX

Last updated:
August 17
```

The system updates the trend as new data arrives.

Trend Memory is a fundamental part of the continuously learning system.

---

# 29. Research Memory

In addition to Trend Memory, create persistent **Research Memory**.

Research Memory stores:

* Important discoveries
* Investigated hypotheses
* Rejected hypotheses
* Interesting anomalies
* External events
* Historical explanations
* Provider disagreements
* Important ecosystem changes
* Research questions
* Conclusions supported by evidence

Annie should be able to reference this memory when answering future questions.

---

# 30. Annie — AI Assistant

The AI assistant is named:

# Annie

Annie is the interface between the user and the research system.

She should feel like a personal research assistant rather than a generic chatbot.

---

# 31. Annie Personality

Annie should be:

* Friendly
* Quirky
* Cute
* Intelligent
* Curious
* Research-oriented
* Slightly playful
* Direct
* Evidence-driven
* Comfortable saying "I don't know"
* Willing to disagree

She should not act like the user's boss.

She should behave like an intelligent assistant who respects the user's decisions.

---

# 32. Annie's Disagreement Style

Annie should not constantly contradict the user.

When the user's assumption conflicts with available evidence, she should creatively disagree while presenting evidence.

Example:

> "I see why you'd think that, but the data is giving us a slightly different story. Let's look at the numbers."

Then present evidence.

She should avoid:

> "You're wrong."

Prefer:

> "I'm not convinced yet. Here's what the data currently shows."

Annie should distinguish:

```text
Fact
Inference
Hypothesis
Speculation
```

---

# 33. Evidence-First Behavior

When making an important conclusion, Annie should usually provide:

* Supporting data
* Sample size
* Relevant comparison
* Time period
* Confidence
* Data limitations

She should avoid presenting speculation as fact.

Example:

> "AI narratives increased from 11% to 23% among successful tokens over the comparison periods. That's a meaningful increase, but we need more observations before calling it a durable trend."

---

# 34. Annie's Research Depth

Annie should be capable of going beyond obvious observations.

When she detects an interesting pattern, she should ask internally:

```text
Why might this be happening?
Is this actually unusual?
What is the baseline?
Could another variable explain it?
Has this happened before?
Is the pattern concentrated in one launchpad?
Is the pattern concentrated among repeat creators?
Is the pattern stronger among $1M+ tokens?
Did an external event coincide with it?
```

She should investigate before reaching strong conclusions.

---

# 35. Autonomous Research

Annie should be capable of creating her own research tasks.

The system should support:

```text
Research Task Queue
```

A research task can be created when:

* A new trend appears
* A trend changes unusually quickly
* A launchpad gains significant market share
* A new narrative emerges
* A provider reports conflicting information
* An unusual creator pattern appears
* A historical pattern returns
* A major ecosystem change occurs

Annie should be able to prioritize these tasks.

---

# 36. Self-Prompting / Research Planning

Annie should not require the user to explicitly tell her every research step.

Given a research question, she should be able to create an internal research plan.

Example:

```text
User:
"What is happening with this new launchpad?"

Annie:
1. Measure launch activity.
2. Measure successful-launch rate.
3. Compare against established launchpads.
4. Identify creator migration.
5. Analyze recent narratives.
6. Search external sources for ecosystem events.
7. Compare with historical launchpad growth patterns.
8. Produce conclusion with evidence.
```

The implementation must use bounded research loops.

Annie must not recursively research forever.

Each research task should have:

```text
Goal
Maximum iterations
Maximum tool calls
Budget
Time limit
Evidence requirements
Completion condition
```

---

# 37. Research Prioritization

Annie should prioritize research based on:

1. Potential usefulness
2. Strength of evidence
3. Potential economic relevance
4. Novelty
5. Historical significance
6. Confidence that additional research can resolve uncertainty
7. Data availability
8. Research cost

The system should identify findings that could potentially have practical financial relevance.

However:

> Potential profitability must never override evidence quality.

Annie must not claim that a finding will make money simply because it sounds promising.

---

# 38. Money-Oriented Intelligence

The system should have a configurable priority called:

```text
Economic Relevance
```

Annie should prioritize patterns that appear potentially useful for identifying:

* Emerging narratives
* Emerging launchpads
* Creator patterns
* Early ecosystem shifts
* Characteristics disproportionately associated with higher-performing tokens
* Rapidly changing trends
* Recurring patterns among exceptional winners

The system should clearly distinguish:

```text
Research finding
Potential opportunity
Speculative hypothesis
```

It must never present speculation as guaranteed profit.

---

# 39. AI Source of Truth

Annie is NOT the primary source of truth.

The architecture is:

```text
Blockchain / APIs
        ↓
Database
        ↓
Statistical Engine
        ↓
Trend Engine
        ↓
Research Engine
        ↓
Annie
```

Annie interprets evidence generated by the system.

She must not invent database values.

---

# 40. Daily Processing Cycle

The system should continuously process new data.

Daily intelligence processing should:

### Step 1

Identify new relevant Solana launches.

### Step 2

Identify qualifying successful tokens.

### Step 3

Normalize data.

### Step 4

Update creator statistics.

### Step 5

Update launchpad statistics.

### Step 6

Update narrative statistics.

### Step 7

Update image characteristics.

### Step 8

Update market milestones.

### Step 9

Compare current data with historical periods.

### Step 10

Detect new trends.

### Step 11

Update existing trends.

### Step 12

Detect rising/declining trends.

### Step 13

Generate research tasks for significant changes.

### Step 14

Allow Annie to investigate high-priority research tasks.

### Step 15

Update Trend Memory.

### Step 16

Update Research Memory.

### Step 17

Generate the daily intelligence report.

---

# 41. Weekly Intelligence

Generate a weekly research summary.

It should answer:

* What strengthened?
* What weakened?
* What disappeared?
* What emerged?
* What narratives grew?
* What narratives declined?
* What launchpads gained share?
* What launchpads lost share?
* What creator patterns changed?
* What characteristics were common among $100k+ tokens?
* What characteristics were common among $1M+ tokens?
* What surprising patterns appeared?
* Which hypotheses remain unresolved?
* What should Annie investigate next?

---

# 42. Daily Intelligence Report

Example:

```text
# Daily Solana Memecoin Intelligence

## Successful Tokens

$100k+: X
$250k+: X
$500k+: X
$1M+: X

## Rising Trends

1. ...
2. ...
3. ...

## New Trends

1. ...
2. ...

## Declining Trends

1. ...

## Launchpad Intelligence

...

## Creator Intelligence

...

## Narrative Intelligence

...

## Biggest Change

...

## Most Interesting Finding

...

## Research Tasks Created

...

## Confidence / Limitations

...
```

---

# 43. Anti-Bias Principle

The system must distinguish:

> Correlation/discovery

from:

> Causation.

If a characteristic appears frequently among successful tokens, Annie should say:

> "This characteristic is associated with successful tokens."

She should not automatically say:

> "This characteristic causes success."

Annie should actively search for alternative explanations.

---

# 44. Hypothesis Engine

The system should support research hypotheses.

Example:

```text
Hypothesis:
Repeat-success creators have a higher probability of producing another successful token.

Evidence:
...

Counter-evidence:
...

Sample size:
...

Current confidence:
...

Status:
Testing
```

Possible statuses:

```text
New
Testing
Supported
Weakening
Rejected
Validated
```

Hypotheses should be continuously tested as new data arrives.

---

# 45. Anomaly Detection

The system should detect unusual events such as:

* Sudden narrative spikes
* Sudden launchpad growth
* Sudden creator success
* Unusual concentration
* Unexpected decline
* New recurring image pattern
* Unusual ticker pattern
* Sudden migration behavior
* Provider data discrepancies

Anomalies should generate research tasks when sufficiently important.

---

# 46. Research Task Queue

Create a persistent research-task system.

Each task should contain:

```text
Task ID
Question
Reason
Priority
Created At
Status
Evidence
Result
Confidence
Cost
Completed At
```

Statuses:

```text
Queued
Researching
Waiting
Completed
Failed
Rejected
```

Annie should be able to create tasks automatically.

---

# 47. Tool Use

Annie should have controlled access to tools including:

```text
Database Query
Statistical Analysis
Market Data
Blockchain Data
Web Research
Token Metadata
Image Analysis
Research Memory
Trend Memory
```

Every tool call should be logged.

---

# 48. AI Cost Control

Do not send unnecessary data to the LLM.

Prefer:

```text
Raw Data
↓
SQL/statistical processing
↓
Compact evidence
↓
AI interpretation
```

Instead of:

```text
Thousands of raw records
↓
LLM
```

Use the LLM where reasoning adds value.

---

# 49. Provider Failure Handling

If a provider fails:

```text
Primary
↓
Retry
↓
Secondary provider
↓
Queue for verification
```

Critical information should never be silently replaced with unreliable information.

Mark missing information:

```text
Pending Verification
```

---

# 50. API Health Monitoring

Track:

* Provider status
* Requests
* Errors
* Rate limits
* Latency
* Approximate usage/cost
* Last successful request
* Data freshness

The frontend must display provider health.

---

# 51. Environment Variables

Create `.env.example`.

Initial configuration:

```env
OPENAI_API_KEY=

HELIUS_API_KEY=
HELIUS_RPC_URL=

BITQUERY_API_KEY=

DEXSCREENER_API_KEY=

BIRDEYE_API_KEY=

TAVILY_API_KEY=

DATABASE_URL=
REDIS_URL=
```

The application must not require optional providers to be configured before starting.

It should clearly report unavailable capabilities.

Never hardcode API keys.

Never commit `.env`.

---

# 52. Database

Use **Cloud Firestore** (amended from PostgreSQL — §72.5, §75).

The database should be designed for:

* Historical research
* Efficient analytical queries
* Trend calculations
* Creator analysis
* Launchpad analysis
* Research memory
* AI querying
* Frontend dashboards

Use proper indexes.

Avoid storing duplicate data unnecessarily.

---

# 53. Background Jobs

Use a background-job system for:

* Data collection
* Enrichment
* Image analysis
* Trend calculation
* Research tasks
* Report generation
* Provider synchronization

Redis may be used for:

* Queues
* Caching
* Job state
* Rate limiting

---

# 54. Frontend

Build a modern **JavaScript + React frontend**.

The frontend is not merely a dashboard.

It is the user's research control center.

It should allow the user to:

* View collected data
* Search tokens
* Inspect token records
* Inspect creators
* Inspect launchpads
* Inspect trends
* Inspect narratives
* Inspect research tasks
* View reports
* View research memory
* View provider health
* Manage system settings
* Interact with Annie

---

# 55. Frontend Pages

Initial pages should include:

```text
Dashboard
Tokens
Creators
Launchpads
Trends
Narratives
Research
Reports
Annie
Data Sources
System Health
Settings
```

---

# 56. Dashboard

The dashboard should show:

* Number of tokens collected
* Successful token counts
* $100k+ count
* $250k+ count
* $500k+ count
* $1M+ count
* Active trends
* Rising trends
* New trends
* Declining trends
* Emerging launchpads
* Recent research findings
* Pending research tasks
* Data freshness
* Provider health

---

# 57. Token Explorer

Allow the user to:

* Search by token
* Search by ticker
* Search by creator
* Search by launchpad
* Filter by milestone
* Filter by date
* Filter by narrative
* View token characteristics
* View milestone history
* View source evidence
* View related trends

Each token should have a detailed research page.

---

# 58. Creator Explorer

Display:

* Creator wallet
* Number of launches
* Successful launches
* Success levels
* Launchpad history
* Recent launches
* Historical performance
* Related tokens
* Creator patterns

---

# 59. Launchpad Explorer

Display:

* Launch volume
* Successful tokens
* Success rate
* $1M+ tokens
* Creator growth
* Market share
* Trend direction
* Migration patterns
* Historical performance
* Recent changes

---

# 60. Trend Explorer

Allow the user to inspect:

* Active trends
* Rising trends
* Stable trends
* Declining trends
* Dead trends
* Confidence
* Sample size
* Evidence
* Historical timeline
* Related tokens
* Related narratives
* Related launchpads

---

# 61. Research Center

Show:

* Research tasks
* Active investigations
* Completed investigations
* Hypotheses
* Anomalies
* Research notes
* External evidence

Allow the user to manually create research questions for Annie.

---

# 62. Annie Interface

The frontend must provide a dedicated conversational interface.

The user should be able to ask:

> "What changed today?"

> "What are the strongest current trends?"

> "Why is this trend rising?"

> "Which launchpads are gaining momentum?"

> "What separates $1M+ tokens from $100k tokens?"

> "Investigate this narrative."

> "Find something interesting."

Annie should answer from the database and research system.

---

# 63. Annie Autonomous Mode

Provide a setting:

```text
Autonomous Research: ON/OFF
```

When enabled, Annie may:

* Create research tasks
* Investigate anomalies
* Perform scheduled research
* Update research memory
* Investigate trends
* Generate reports

The system must impose configurable:

```text
Maximum research tasks
Maximum tool calls
Maximum AI budget
Maximum execution time
```

---

# 64. Research Priority System

Every research task receives a priority score based on:

```text
Evidence strength
Novelty
Potential usefulness
Economic relevance
Historical importance
Researchability
Cost
```

The system should focus resources on the highest-value unanswered questions.

---

# 65. Future Token Comparison

The architecture must allow a future feature where a newly launched token can be compared against the research database.

Example:

> "This token shares 7 characteristics with historical $100k+ tokens."

Or:

> "This token resembles 83 historical successful tokens."

Or:

> "The narrative associated with this token is currently rising."

This is a future feature and should not be overbuilt in version one.

---

# 66. Security

Implement:

* Environment variables
* API-key protection
* Authentication for frontend
* Server-side API calls
* Database access controls
* Rate limiting
* Input validation
* Audit logging

Never expose private API keys to the React frontend.

---

# 67. Logging

Log:

* Provider requests
* Provider failures
* Data ingestion
* Qualification decisions
* Trend calculations
* Research tasks
* Annie tool calls
* AI responses where appropriate
* Errors
* Configuration changes

Logs should be searchable from the frontend.

---

# 68. Architecture

The initial architecture should resemble:

```text
                    SOLANA ECOSYSTEM
                           │
          ┌────────────────┼────────────────┐
          │                │                │
       Helius          Bitquery        Other APIs
          │                │                │
          └────────────────┼────────────────┘
                           ↓
                    DATA INGESTION
                           ↓
                     QUALIFICATION
                           ↓
                    POSTGRESQL
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
             ┌─────────────┼─────────────┐
             ↓             ↓             ↓
         Trends       Hypotheses      Anomalies
             │             │             │
             └─────────────┼─────────────┘
                           ↓
                    RESEARCH ENGINE
                           │
                 ┌─────────┴─────────┐
                 ↓                   ↓
            Web Research        AI Reasoning
                 │                   │
                 └─────────┬─────────┘
                           ↓
                         ANNIE
                           │
             ┌─────────────┼─────────────┐
             ↓             ↓             ↓
          Reports       Questions     Research
                           │
                           ↓
                    REACT FRONTEND
```

---

# 69. Technology Stack

Initial stack:

### Backend

```text
Python
FastAPI
Cloud Firestore   (amended from PostgreSQL — §75)
Background workers (not yet built — manual trigger endpoints exist instead;
                     Redis was removed rather than kept as unused config — §75)
```

### AI

```text
OpenAI API
```

### Blockchain/Data

```text
Helius        (active — discovery + chain truth, §9.2)
DexScreener   (active — default market data/qualification, no key required)
Bitquery      (adapter built, not used by default — §9.2)
Birdeye       (optional cross-validation)
```

### Web Research

```text
Tavily
```

### Frontend

```text
JavaScript
React
Vite
```

### Deployment

The architecture should support deployment to cloud infrastructure.

Do not hardcode a specific hosting provider.

---

# 70. Provider Abstraction

Create provider interfaces:

```text
BlockchainProvider
MarketDataProvider
LaunchDataProvider
DexDataProvider
TokenMetadataProvider
SocialDataProvider
WebResearchProvider
ImageAnalysisProvider
```

Implement adapters:

```text
HeliusAdapter
BitqueryAdapter
DexScreenerAdapter
BirdeyeAdapter
TavilyAdapter
```

Providers must be replaceable without rewriting:

```text
Trend Engine
Research Engine
Annie
Frontend
Database
```

---

# 71. Development Philosophy

Build the smallest reliable system first.

Do NOT initially build:

* Automated trading
* Buy/sell execution
* Portfolio management
* Complex prediction models
* Tick-level databases
* Full wallet graph analysis
* Complete social-media intelligence
* Hundreds of technical indicators
* Unnecessary AI agents

The first version should answer:

> **What characteristics are repeatedly associated with successful Solana memecoins, and how are those characteristics changing over time?**

---

# 72. Version-One Definition of Success

Version one is successful if it can:

1. Collect relevant Solana token-launch data.
2. Identify qualifying successful tokens.
3. Store clean historical data.
4. Track creators.
5. Track launchpads.
6. Track migration behavior.
7. Analyze names.
8. Analyze tickers.
9. Analyze descriptions.
10. Analyze images.
11. Analyze available social/narrative information.
12. Compare different success levels.
13. Detect recurring patterns.
14. Detect emerging trends.
15. Detect declining trends.
16. Maintain Trend Memory.
17. Maintain Research Memory.
18. Generate daily intelligence.
19. Generate weekly intelligence.
20. Create autonomous research tasks.
21. Allow Annie to investigate those tasks.
22. Provide a React frontend for viewing and managing the collected data.
23. Allow natural-language research through Annie.
24. Preserve historical data for long-term research.

---

# 73. Core Product Principle

The system should progressively evolve from:

> **"Here are successful Solana tokens."**

to:

> **"Here are the characteristics associated with successful Solana tokens."**

to:

> **"Here are the strongest current patterns."**

to:

> **"Here are the patterns changing right now."**

to:

> **"Here are the most interesting things we should investigate next, and here's the evidence."**

Annie's purpose is to help uncover those findings.

---

# 74. Final Principle

Every architectural decision must ultimately support one question:

> **"What can we learn from successful Solana memecoins, how is the ecosystem changing, and what patterns are emerging that deserve further investigation?"**

The system should continuously collect evidence, test hypotheses, remember discoveries, challenge assumptions, and improve the quality of its research as the historical dataset grows.


---

# 75. Amendment — Database: Firestore replaces PostgreSQL

Superseding §22, §52 and §69's original PostgreSQL choice.

**Use Cloud Firestore as the sole persistent database.** Not because
PostgreSQL was wrong for this system — its relational integrity, indexed
JOINs and hand-written analytical SQL were a good fit for §26's statistical
engine — but because the deployment target changed to Firebase, and running
both would mean two sources of truth. §72's original table list is
implemented as Firestore collections; see `app/db/repo.py`'s module
docstring for the exact mapping and, importantly, for what does **not**
translate directly:

* **No joins.** A token's launchpad, creator and features are separate
  documents. Assembling a detail view costs several round trips instead of
  one query.
* **No server-side `GROUP BY`.** The trend engine (§24-§28) fetches a
  cohort's documents and aggregates in Python. The statistics themselves
  (`app/analysis/stats.py`, Wilson intervals, two-proportion z-tests) are
  unchanged — only how the data reaches them changed.
* **Money is a string, not `Numeric`.** Firestore numbers are IEEE-754
  doubles, exactly the representation-error risk §22's `Numeric(20,2)`
  existed to avoid. `Decimal` amounts are stored as their exact string form
  and parsed back on read — see `app/db/base.py`.
* **IDs are natural keys, not autoincrement integers.** A token's document ID
  is its mint; a launchpad's is its slug; a creator's is its wallet.
  Firestore has no sequence primitive, and a deterministic key makes
  "insert if absent" one `.get()` instead of a query.
* **Composite indexes are explicit, not automatic.** Any query combining an
  equality filter with an `order_by` on a different field needs a composite
  index. `firestore.indexes.json` at the repo root declares the ones this
  codebase needs; Firestore's own error message also links directly to
  create whichever one you're missing, the first time a query needs it.

Authentication is a **service account** (Firebase Console → Project settings
→ Service accounts → Generate new private key), never the browser/client SDK
config (`apiKey`, `authDomain`, ...) — that config cannot authenticate a
server and grants it no Firestore access. See `app/db/firestore.py`.

Redis remains not required for the initial version, exactly as §72.7
originally argued — nothing about the Firestore move changes that reasoning.
Later in this build pass it was removed from the codebase entirely rather
than left as unused reserved config: no worker ever consumed it, and a
capability that can never actually be enabled (no code reads `REDIS_URL`)
is worse than no capability at all — it invites setting a value that does
nothing. Re-add it (`app/config.py`'s capability list, the `redis_url`
setting) the day a worker exists to use it, not before.

---

# 76. Amendment — Discovery & market data: Helius + DexScreener replace Bitquery as the default

Superseding parts of §9-§11.

Bitquery remains a fully built adapter (`app/providers/bitquery.py`) but is
not this deployment's default. Instead:

* **Discovery (§5, §20)** runs on Helius, scanning known launchpad program
  IDs (`app/providers/helius.py`'s `KNOWN_LAUNCHPAD_PROGRAMS`, currently just
  Pump.fun). §20's original design assumed polling
  (`getSignaturesForAddress` + parsed-transaction inspection) would be
  enough; in practice it isn't — measured directly, 1000 signatures covered
  only 6 seconds of real chain time against Pump.fun's actual volume, so a
  poll loop can never see more than a sliver of what happened. The primary
  mechanism is now a **Helius webhook** (`app/api/routes/webhooks.py`,
  `POST /api/webhooks/helius`): Helius pushes an enhanced transaction the
  instant one matching the filter happens, instead of this app asking and
  hoping. The filter is `transactionTypes: ["CREATE"]` — confirmed
  empirically against a real delivery (not from Helius's docs, which don't
  pin this down for Pump.fun specifically): a genuine Pump.fun create
  transaction classifies as `type: "CREATE"`, `source: "PUMP_FUN"` in
  Helius's enhanced parser. Polling remains in the codebase as a backfill
  path, not the primary one. This is still a real, working discovery path —
  and a narrower one than an indexer: a launchpad not in
  `KNOWN_LAUNCHPAD_PROGRAMS` (and therefore not in the webhook's
  `accountAddresses`) is invisible to it, and §5's goal of *automatically*
  noticing brand-new, previously-unknown launchpads is not met by this
  approach. That gap is closed either by adding known program IDs as
  they're identified (updating both the dict and the webhook's registered
  `accountAddresses`), or by reinstating Bitquery (set `BITQUERY_API_KEY`,
  point the registry's `launches` property back at it).
* **Market data / qualification (§4, §11)** runs on DexScreener by default —
  it needs no credential, so it's the one provider guaranteed available in
  every deployment. Set `BITQUERY_API_KEY` or `BIRDEYE_API_KEY` and they join
  automatically as extra cross-validation readings (§21); nothing else
  changes.

The provider-abstraction boundary (§70) is exactly what made this swap cheap:
`app/providers/registry.py`'s `market_primary` and `launches` properties are
the only two lines that changed. The trend engine, research engine, Annie and
the frontend do not know or care which adapter answers.

---

# 77. Amendment — Authentication

New; §66 named the requirement ("Authentication for frontend") but no
implementation existed prior to this build pass.

Single-operator auth, matching this system's actual usage — one person, not a
user table:

* `AUTH_USERNAME` / `AUTH_PASSWORD` are literal credentials in configuration,
  compared in constant time (`hmac.compare_digest`), not hashed — hashing a
  secret that already lives in plaintext in the environment would be
  theatre. `AUTH_SECRET` signs a session JWT.
* Every API route requires a valid session except `/api/auth/*`,
  `/api/webhooks/*` (Helius calls that one server-to-server, authenticated by
  its own shared secret instead — §76), and the bare `/health` liveness
  check (`app/main.py`'s router-level `dependencies`, so a route added later
  is protected by default rather than by remembering to add a check).
* This guards the routes that spend money (`/api/annie/chat` calls OpenAI)
  and see the research data — both need to sit behind a login before this
  system is reachable from the public internet.

**Revised mid-build: the session token is a Bearer token, not an httpOnly
cookie.** The original design above shipped as a cookie first, then broke in
production — cross-origin httpOnly cookies are unreliable by default across
unrelated domains in 2026 (Safari blocks third-party cookies unconditionally;
Firefox partitions them), and the frontend (Vercel) and backend (Railway) are
exactly that: unrelated domains, not subdomains of one site. The fix was to
stop relying on the browser's cookie jar entirely — the JWT `AUTH_SECRET`
still signs, `/api/auth/login` still verifies the same credentials, but the
token now comes back in the response body, is stored client-side
(`localStorage`), and is sent back as `Authorization: Bearer <token>` on every
request instead of riding along automatically. CORS
(`app/main.py`) runs with `allow_credentials=False` accordingly — nothing
about this design needs credentialed cross-origin cookies, so nothing asks
for them.

See `app/auth.py`.

---

# 78. Implementation status

This section records what actually exists, so this document stays a
specification an implementer can trust rather than pure aspiration. Detailed,
maintained status (what's built, what's stubbed, what's genuinely missing)
lives in **README.md** — that file changes with the codebase; duplicating it
here would just give it a chance to go stale in two places. As of this build
pass: Firestore persistence, provider adapters (Helius discovery + chain
truth, DexScreener market data, OpenAI + Tavily), the qualification engine,
the trend engine, Annie's chat agent with a bounded tool-calling loop,
single-operator (Bearer-token) authentication, the daily scheduler, the
research-task runner, the report generator, Annie's work memory, and the
Discord workspace (§79) are all real and have been exercised against a live
Firestore project and a live OpenAI call — not merely written. Discovery
specifically has been verified end-to-end against a real, unprompted Helius
webhook delivery in production (§76), not just a hand-simulated payload. Not
yet built: narrative clustering (§16's dedicated stage — trend detection
currently uses the deterministic `token.theme` feature as a proxy), and an
automated test suite covering routes and Firestore-touching code (the pure
decision logic — statistics, trend lifecycle, qualification, scheduler
timing — has one; see README's Tests row).

---

# 79. Amendment — Scheduler, autonomous research, reports, and Annie's work memory

Closes several gaps §78 listed as not yet built, plus a new
memory/personality/Discord-workspace layer not in the original spec at all.

**§40's daily cycle now exists as a real scheduler, not manual triggers.**
`app/scheduling/scheduler.py` is a small in-process loop (no external queue —
Redis stays removed per §75's reasoning; nothing here needed it back),
started the same way the Telegram/Discord bots already are
(`app/main.py`'s `_start_bots`/`_start_scheduler`, both background
`asyncio.Task`s under one `lifespan`). Each job's trigger time is
operator-configurable through the existing `Setting` mechanism — no
redeploy to change when something runs. `app/scheduling/jobs.py` registers
five: daily qualification, a daily activity log, memory consolidation, a
research-task safety sweep, and the Morning Brief.

**Discovery/qualification amendment (superseding part of §76's framing).**
§76 described discovery as "narrower than an indexer" but otherwise left
Stage 1/2 as originally specified: poll or webhook for mints, then
separately decide qualification. In practice, once the Helius webhook (§76)
started actually working, it surfaced two things: qualification had never
been run against the resulting backlog at all (no scheduler existed to run
it), and — checked directly against real chain data — DexScreener returns
zero pairs for any mint still on its Pump.fun bonding curve and a real pair
for every migrated one. The existing $100k+ tier check in
`app/pipeline/qualification.py` was therefore already the correct migration
gate; it just needed to run daily instead of never. No new
migration-detection code was written. The Helius webhook remains exactly as
before — it still records every mint at creation, in real time, for ad-hoc
lookups (`live_token_lookup`, below) — the change is that a token becomes a
*research subject* (qualifies) only after a scheduled pass finds it both
migrated and above threshold.

**§35-§36's research-task runner** (`app/research/runner.py`) executes a
queued `ResearchTask` end-to-end: reuses `AnnieAgent`'s own tool dispatch
(no second implementation of the same tools), driven by the task's budget
(`iterations`/`tool_calls`/`cost_usd`, all pre-existing fields on
`ResearchTask`) rather than a chat turn's fixed round count, writing a
`ResearchNote` and marking the task `completed`/`failed` on exit. Triggered
immediately on creation (`app/api/routes/intelligence.py`) with the daily
sweep as a safety net for a process restart mid-flight.

**§41-§42's report generator** (`app/reports/generator.py`) is deterministic
— every section (new discoveries, rising/declining trends, research
completed, worth investigating, memory promoted, data quality) is a direct
query over the report's time window, no LLM call, matching the "never invent
a number" discipline this system holds everywhere else. It backs two
surfaces from one implementation: the `Report` documents §41-§42 always
specified, and a new Discord-formatted Morning Brief — see below.

**New: Annie's work memory**, not originally in the spec. A `Memory` model
(`memories/{auto_id}`, `long_term`/`daily_log` types) distinct from
`ResearchNote` (findings) and `Conversation`/`Message` (chat history) — see
`app/db/models/research.py`'s module docstring for exactly how these three
relate and why a fourth, parallel "research memory" collection was *not*
added (existing `ResearchTask`/`ResearchNote` already covered that concept).
Consolidation (`_memory_consolidation` in `app/scheduling/jobs.py`) is the
one part of this that needed judgment rather than a query: a single bounded
OpenAI call reviewing recent daily logs, research findings and existing
long-term memories, deciding what's worth promoting permanently and what's
gone stale — restricted to archiving only memory IDs it was actually shown,
never an invented one. Retrieval follows the same on-demand discipline every
other agent tool already used: a new `search_memories` tool, never
preloaded into every turn ("store extensively, retrieve selectively").

**New: operator-configured personality**, layered on top of, never
replacing, the hard rules. `PersonalityConfig` (`personality/config`, one
singleton doc) adjusts voice — tone, communication style, skepticism,
pushback, explanation style — through a new `persona.system_prompt(...,
personality_overrides=...)` parameter. `SOURCE_OF_TRUTH`, `CLAIM_DISCIPLINE`,
`EVIDENCE_STANDARD` and `MONEY` are unconditional regardless of what's
configured — an operator can change how Annie sounds, never what keeps her
honest.

**New: Discord as a workspace, not just a notification channel.**
`DiscordChannel` (`discord_channels/{channel_id}`) records a channel's
configured purpose; a message arriving there folds that purpose into
Annie's context for the turn. Annie can create a channel herself
(`manage_discord_channel`, a new agent tool) — but the tool is only ever
*offered* to the model when `app/bots/discord_bot.py` has already confirmed
the bot holds the Manage Channels permission in that specific guild, never
offered-then-caught-on-failure. This required the one deliberate crack in
`AnnieAgent`'s platform-agnostic design: an optional `PlatformContext`
(`app/annie/platform.py`) that web chat and Telegram never construct, so
nothing about them changes.

**New: bot access control.** Both bots were built §62-era with an explicit
"no access restriction" decision. An operator-editable allowlist
(`app/bots/access_control.py`, Firestore-backed via the `Setting`
mechanism) now exists, defaulting to empty/open — the allowlist can only
ever *add* a restriction the operator explicitly configured, never lock
them out by being turned on with nothing in it.

**New: live token lookup.** `search_tokens`/`get_token` only ever see the
research database — which, per the qualification amendment above, now only
contains migrated, tier-qualified tokens. A user asking about an arbitrary
CA needs a different answer than "not found." `live_token_lookup` queries
the market provider directly, tagged `source: "live_lookup"` so Annie never
conflates a live chain read with a claim from the qualified dataset.

See README.md for setup/operational detail on all of the above (env vars —
none new were required — Firestore indexes, the scheduler's config keys).
