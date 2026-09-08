/**
 * Fixture server.
 *
 * Implements the API contract in `app/api/schemas.py` with generated
 * data, so the interface can be built and reviewed without Firestore, Redis
 * and a handful of provider credentials.
 *
 * This is a *separate process*, not a fallback inside the app. The frontend
 * still requires VITE_API_BASE_URL and still fails loudly without it — you
 * simply point it here instead of at the Python API. That distinction matters:
 * a fallback baked into the client is how a misconfigured build ships looking
 * healthy.
 *
 *   node tools/fixture-server/server.js
 *   # then: VITE_API_BASE_URL=http://localhost:8000
 *
 * The data deliberately includes the awkward cases, because those are the ones
 * whose rendering is easy to get wrong and hard to notice:
 *   - null market caps (must render as “—”, never $0)
 *   - thin samples (must be flagged, not quietly shown as a percentage)
 *   - a disputed provider disagreement
 *   - a degraded capability with missing env vars
 *   - a trend with no significance test at all
 *   - a day excluded from trend comparisons for poor coverage
 */

import { createServer } from 'node:http'

const PORT = Number(process.env.PORT || 8000)
const now = Date.now()
const iso = (msAgo) => new Date(now - msAgo).toISOString()
const HOUR = 3600_000
const DAY = 24 * HOUR

/* ------------------------------------------------------------------ data -- */

const LAUNCHPADS = [
  { id: 1, slug: 'pumpfun', name: 'Pump.fun', lifecycle: 'declining', launch_count: 48210, qualified_count: 412, success_rate: 0.00854, market_share: 0.612, growth_rate_7d: -0.081, growth_rate_30d: -0.164, is_known: true, first_seen_at: iso(400 * DAY), last_seen_at: iso(2 * HOUR) },
  { id: 2, slug: 'bonk-launch', name: 'BonkLaunch', lifecycle: 'growing', launch_count: 9840, qualified_count: 143, success_rate: 0.01453, market_share: 0.214, growth_rate_7d: 0.187, growth_rate_30d: 0.402, is_known: true, first_seen_at: iso(120 * DAY), last_seen_at: iso(1 * HOUR) },
  { id: 3, slug: 'unknown-9fk2mq1a', name: 'unknown-9fk2mq1a', lifecycle: 'emerging', launch_count: 1204, qualified_count: 38, success_rate: 0.03156, market_share: 0.058, growth_rate_7d: 0.612, growth_rate_30d: null, is_known: false, discovered_by: 'bitquery.discover_launchpads', first_seen_at: iso(11 * DAY), last_seen_at: iso(3 * HOUR) },
  { id: 4, slug: 'meteora-dlmm', name: 'Meteora DLMM', lifecycle: 'stable', launch_count: 6120, qualified_count: 71, success_rate: 0.0116, market_share: 0.094, growth_rate_7d: 0.012, growth_rate_30d: 0.031, is_known: true, first_seen_at: iso(280 * DAY), last_seen_at: iso(5 * HOUR) },
  { id: 5, slug: 'moonshot', name: 'Moonshot', lifecycle: 'dead', launch_count: 2210, qualified_count: 4, success_rate: 0.0018, market_share: 0.002, growth_rate_7d: -0.44, growth_rate_30d: -0.79, is_known: true, first_seen_at: iso(300 * DAY), last_seen_at: iso(26 * DAY) },
]

const SYMBOLS = [
  ['NEURAL', 'Neural Net Coin', ['ai', 'internet_meme']],
  ['CAPY', 'Capybara Chill', ['animal']],
  ['GIGACHAD', 'Gigachad Finance', ['internet_meme', 'crypto_culture']],
  ['SOLDOG', 'Solana Dog', ['animal', 'crypto_culture']],
  ['AGENT', 'Agent Protocol', ['ai']],
  ['MOGGED', 'Mogged', ['internet_meme']],
  ['PENGU', 'Pengu Party', ['animal']],
  ['RIZZLER', 'The Rizzler', ['internet_meme']],
  ['GPTSOL', 'GPT on Solana', ['ai']],
  ['BROKE', 'Perpetually Broke', ['absurd']],
  ['HIPPO', 'Moo Deng Redux', ['animal', 'internet_meme']],
  ['ASI', 'Artificial Superintelligence', ['ai']],
  ['WOJAK', 'Wojak Returns', ['internet_meme']],
  ['GOONER', 'Gooner Coin', ['internet_meme', 'absurd']],
  ['SIGMA', 'Sigma Grindset', ['internet_meme']],
]

const TOKENS = SYMBOLS.map(([symbol, name, themes], i) => {
  const lp = LAUNCHPADS[i % 4]
  const qualifiedAt = (i * 9 + 4) * HOUR
  // A few deliberately have no peak reading, and a few never qualified — the
  // UI must render both as unknown/watching rather than as zero. Since the
  // memory rewrite this list is *sightings*, not qualified-token documents:
  // most of what the ledger holds never cleared a tier.
  const noPeak = i === 5 || i === 11
  const qualified = !noPeak && i % 5 !== 3
  const peakNum = noPeak ? null : 120000 + i * 137000 + (i % 3) * 410000
  const nowNum = peakNum === null ? null : Math.round(peakNum * (i % 4 === 1 ? 0.18 : 0.72))
  return {
    id: i + 1,
    mint: `${symbol.slice(0, 4)}${'x'.repeat(2)}${(i * 7919).toString(36).padStart(6, '0')}Zk4Qv9Lm2Rt8Wp3Nc7Hb${i}`,
    name,
    symbol,
    launchpad_slug: lp.slug,
    creator_wallet: `Cr${(i * 104729).toString(36)}tRw9Km4Pz7Vn2Lb8Qs5Xd3Fj6Hg1Ay${i}`,
    first_seen: iso(qualifiedAt + 6 * HOUR),
    qualified_at: qualified ? iso(qualifiedAt) : null,
    market_cap: nowNum,
    peak_market_cap: peakNum,
    peak_tier: !qualified ? null : peakNum > 1000000 ? 1000000 : 250000,
    is_qualified: qualified,
    status: qualified ? 'qualified' : noPeak ? 'faded' : 'watching',
    checks: 4 + i * 3,
    round_tripped: peakNum !== null && nowNum !== null && nowNum < peakNum * 0.5,
    themes,
  }
})

const TREND_DEFS = [
  ['ai-narrative-100k', 'AI (token)', 'narrative', 'rising', 'validated', 'high', 0.231, 0.118, 14, 61, 88, 746, 0.0004, 9, []],
  ['capybara-100k', 'Capybara (name)', 'name', 'rising', 'candidate', 'medium', 0.098, 0.041, 6, 61, 31, 746, 0.021, 4, ['Observed on 4 day(s); 5 needed before validation.']],
  ['ticker-short-100k', 'Short ticker (≤3 chars)', 'ticker', 'stable', 'validated', 'high', 0.312, 0.298, 19, 61, 222, 746, 0.61, 26, []],
  ['unknown-lp-100k', 'unknown-9fk2mq1a (launchpad)', 'launchpad', 'new', 'observation', 'low', 0.164, 0.008, 10, 61, 6, 746, null, 2, ['Recent sample of 61 is adequate, but the baseline of 6 occurrences is too thin for a significance test.', 'This launchpad has existed for 11 days. Treat as an observation.']],
  ['political-100k', 'Politics (token)', 'narrative', 'declining', 'candidate', 'medium', 0.049, 0.142, 3, 61, 106, 746, 0.008, 11, ['Only 3 occurrences observed (minimum 5).']],
  ['brand-parody-1m', 'Brand parody (image)', 'image', 'rising', 'candidate', 'medium', 0.222, 0.071, 4, 18, 12, 168, 0.032, 3, ['Recent sample of 18 is below the minimum of 20; no significance computed.', 'Only 4 occurrences observed (minimum 5).']],
  ['pepe-derivative-100k', 'Pepe derivative (name)', 'name', 'dead', 'observation', 'low', 0.0, 0.089, 0, 61, 66, 746, null, 0, ['No occurrences in 24 days.']],
]

const TRENDS = TREND_DEFS.map(([slug, name, category, status, maturity, confidence, rf, bf, rc, rt, bc, bt, p, persistence, caveats], i) => ({
  id: i + 1,
  slug,
  name,
  category,
  status,
  maturity,
  confidence,
  cohort_threshold_usd: slug.endsWith('-1m') ? '1000000' : '100000',
  recent: { count: rc, total: rt, frequency: rf },
  baseline: { count: bc, total: bt, frequency: bf },
  recent_window_days: 7,
  baseline_window_days: 90,
  change: rf - bf,
  relative_change: bf ? (rf - bf) / bf : null,
  lift: bf ? rf / bf : null,
  first_detected_at: iso((20 + i * 6) * DAY),
  last_observed_at: iso(status === 'dead' ? 24 * DAY : 3 * HOUR),
  persistence_days: persistence,
  p_value: p,
  effect_size: p === null ? null : 0.34 - i * 0.03,
  ci_low: Math.max(0, rf - 0.06),
  ci_high: Math.min(1, rf + 0.06),
  revival_count: i === 6 ? 2 : 0,
  peak_frequency: rf + 0.03,
  peak_frequency_at: iso((5 + i) * DAY),
  caveats,
  description: `Frequency of ${name} among tokens reaching ${slug.endsWith('-1m') ? '$1,000,000' : '$100,000'}.`,
  // Index 0 is the OLDEST point and the series ends at the current frequency,
  // so a rising trend must start below `rf` and climb toward it. Getting this
  // backwards draws a falling line under a RISING badge, which is worse than
  // no sparkline at all.
  recent_series: Array.from({ length: 14 }, (_, d) => {
    const slope = status === 'rising' ? 0.011 : status === 'declining' ? -0.009 : 0
    const age = 13 - d
    return Math.max(0, rf - slope * age + Math.sin(d * 1.3) * 0.008)
  }),
  observations: Array.from({ length: 14 }, (_, d) => ({
    observed_on: iso((13 - d) * DAY),
    window_days: 7,
    count: Math.max(0, Math.round(rc - (13 - d) * 0.4)),
    total: rt,
    frequency: Math.max(0, rf - (13 - d) * (status === 'rising' ? 0.009 : -0.004) + Math.sin(d * 1.3) * 0.012),
    baseline_frequency: bf,
    p_value: p,
  })),
  history: [
    { changed_at: iso((20 + i * 6) * DAY), from_status: null, to_status: 'new', to_maturity: 'observation', reason: 'First detected with sufficient occurrences to monitor.' },
    ...(persistence > 3 ? [{ changed_at: iso(9 * DAY), from_status: 'new', to_status: status === 'dead' ? 'stable' : status, to_maturity: maturity, reason: `Frequency ${(rf * 100).toFixed(1)}% vs baseline ${(bf * 100).toFixed(1)}%, sustained over ${persistence} day(s).` }] : []),
    ...(status === 'dead' ? [{ changed_at: iso(24 * DAY), from_status: 'declining', to_status: 'dead', to_maturity: 'observation', reason: 'No occurrences in 24 days.' }] : []),
  ],
  example_tokens: TOKENS.slice(i, i + 4),
  evidence: { recent_count: rc, recent_total: rt, baseline_count: bc, baseline_total: bt, p_value: p, caveats },
}))

const CREATORS = Array.from({ length: 12 }, (_, i) => {
  const launches = i === 0 ? 312 : i === 1 ? 1 : 4 + i * 11
  const winners = i === 0 ? 41 : i === 1 ? 1 : Math.max(0, 6 - Math.floor(i / 2))
  return {
    id: i + 1,
    wallet: `Cr${(i * 104729).toString(36)}tRw9Km4Pz7Vn2Lb8Qs5Xd3Fj6Hg1Ay${i}`,
    total_launches: launches,
    launches_in_window: Math.max(0, Math.round(launches / 30)),
    winners,
    success_rate: launches ? winners / launches : null,
    best_market_cap: i === 1 ? null : 2_400_000 - i * 180_000,
    best_mint: i === 1 ? null : TOKENS[i % TOKENS.length].mint,
    // "Tracked" replaced "repeat winner": a wallet earns it by launching a
    // lot OR by having produced one real winner, and is never demoted.
    is_tracked: i < 3 || launches >= 25,
    first_seen: iso((300 - i * 12) * DAY),
    last_seen: iso((i + 1) * DAY),
    dossier_path: i < 3 ? `creators/Cr${(i * 104729).toString(36)}tRw9Km4Pz7Vn2Lb8Qs5Xd3Fj6Hg1Ay${i}.md` : null,
  }
})

const CREATOR_MOVEMENTS = (wallet) =>
  Array.from({ length: 24 }, (_, n) => ({
    at: iso((n * 7 + 2) * HOUR),
    kind: n % 8 === 3 ? 'qualified' : 'launch',
    mint: TOKENS[n % TOKENS.length].mint,
    market_cap: n % 8 === 3 ? 340000 : null,
    wallet,
  }))

const NARRATIVES = [
  { id: 1, slug: 'ai', label: 'AI', category: 'technology', token_count: 1840, qualified_count: 88, share_of_qualified: 0.231, baseline_share: 0.118, is_emergent: false, first_seen_at: iso(210 * DAY), last_seen_at: iso(2 * HOUR) },
  { id: 2, slug: 'animal', label: 'Animal', category: 'classic', token_count: 6210, qualified_count: 194, share_of_qualified: 0.264, baseline_share: 0.281, is_emergent: false, first_seen_at: iso(400 * DAY), last_seen_at: iso(1 * HOUR) },
  { id: 3, slug: 'brainrot', label: 'Brainrot slang', category: null, token_count: 412, qualified_count: 29, share_of_qualified: 0.116, baseline_share: 0.038, is_emergent: true, first_seen_at: iso(31 * DAY), last_seen_at: iso(4 * HOUR) },
  { id: 4, slug: 'politics', label: 'Politics', category: 'current events', token_count: 980, qualified_count: 12, share_of_qualified: 0.049, baseline_share: 0.142, is_emergent: false, first_seen_at: iso(180 * DAY), last_seen_at: iso(2 * DAY) },
  { id: 5, slug: 'capybara', label: 'Capybara', category: null, token_count: 118, qualified_count: 9, share_of_qualified: 0.098, baseline_share: 0.041, is_emergent: true, first_seen_at: iso(18 * DAY), last_seen_at: iso(6 * HOUR) },
]

const TASKS = [
  { id: 1, question: 'Are creators moving away from Pump.fun toward BonkLaunch?', reason: 'Pump.fun 30-day share fell 16.4pp while BonkLaunch rose 40.2%.', origin: 'trend_change', status: 'completed', priority: 0.87, confidence: 'medium', created_at: iso(2 * DAY), started_at: iso(2 * DAY), completed_at: iso(47 * HOUR), cost_usd: '0.42', result: 'Partially. 61 of 240 creators (25.4%) who launched on Pump.fun in the prior 30 days launched on BonkLaunch in the last 14, against a 9.1% base rate for cross-platform movement. That is a real shift, but it is concentrated: 34 of the 61 are single-launch wallets with no prior qualifying token, so this may be new entrants choosing BonkLaunch rather than established creators leaving Pump.fun.', result_claim_type: 'inference', limitations: 'Creator identity rests on deployer resolution, which was unavailable for 12% of launches in the window.' },
  { id: 2, question: 'Why did unknown-9fk2mq1a appear and grow so quickly?', reason: 'Uncatalogued launch program reached 5.8% share within 11 days.', origin: 'anomaly', status: 'researching', priority: 0.94, confidence: null, created_at: iso(6 * HOUR), started_at: iso(5 * HOUR), completed_at: null, cost_usd: '0.18' },
  { id: 3, question: 'Do repeat-success creators produce successful tokens more often?', reason: 'Standing hypothesis, re-tested weekly.', origin: 'scheduled', status: 'queued', priority: 0.61, confidence: null, created_at: iso(1 * DAY), started_at: null, completed_at: null, cost_usd: null },
  { id: 4, question: 'Does the Bitquery/DexScreener market-cap disagreement on SOLDOG affect its tier?', reason: 'Providers disagree by 34% on a token near the $250k boundary.', origin: 'provider_conflict', status: 'queued', priority: 0.58, confidence: null, created_at: iso(9 * HOUR), started_at: null, completed_at: null, cost_usd: null },
  { id: 5, question: 'Is the brainrot-slang narrative distinct from internet_meme?', reason: 'Emergent narrative overlaps 71% with an existing one.', origin: 'annie', status: 'failed', priority: 0.33, confidence: null, created_at: iso(4 * DAY), started_at: iso(4 * DAY), completed_at: iso(4 * DAY), cost_usd: '0.09', failure_reason: 'Budget exhausted after 6 iterations without reaching the completion condition. Overlap measured but no separating characteristic found.' },
]

const NOTES = [
  { id: 1, title: 'AI narratives roughly doubled against baseline', body: 'AI-themed tokens are 23.1% of $100k+ qualifiers over the last 7 days (14 of 61), against an 11.8% baseline over the preceding 90 days (88 of 746). p = 0.0004, sustained across 9 days. This is an association, not a cause — it is also concentrated on BonkLaunch, which grew over the same period, so platform and narrative are confounded here.', claim_type: 'fact', confidence: 'high', category: 'narrative', tags: ['ai', 'narrative'], sample_size: 61, period_start: iso(7 * DAY), period_end: iso(0), created_at: iso(4 * HOUR), is_current: true, evidence: {}, counter_evidence: {} },
  { id: 2, title: 'Short tickers are common but not distinguishing', body: 'Tickers of 3 characters or fewer appear in 31.2% of $100k+ tokens — but also in 29.8% of the baseline population. The difference is 1.4pp and not significant. Worth recording precisely because it looks like a pattern and is not.', claim_type: 'fact', confidence: 'high', category: 'ticker', tags: ['ticker'], sample_size: 61, period_start: iso(7 * DAY), period_end: iso(0), created_at: iso(1 * DAY), is_current: true, evidence: {}, counter_evidence: {} },
  { id: 3, title: 'Brand parody may be stronger among $1M+ tokens', body: 'Brand-parody imagery appears in 4 of 18 $1M+ tokens this window versus a 7.1% baseline. The direction is interesting but 18 is below the reporting floor and 4 occurrences is below the minimum — this is an observation to re-check next week, not a finding.', claim_type: 'hypothesis', confidence: 'low', category: 'image', tags: ['image', '1m'], sample_size: 18, period_start: iso(7 * DAY), period_end: iso(0), created_at: iso(2 * DAY), is_current: true, evidence: {}, counter_evidence: {} },
]

const HYPOTHESES = [
  { id: 1, slug: 'repeat-creator-advantage', statement: 'Creators with a prior $100k+ token are more likely to produce another one.', rationale: 'Distribution access and audience carry over between launches.', status: 'supported', confidence: 'medium', sample_size: 1841, supporting_observations: 212, contradicting_observations: 74, p_value: 0.0031, effect_size: 0.28, test_method: 'Two-proportion z-test, repeat vs first-time creators, 90-day window.', first_tested_at: iso(60 * DAY), last_tested_at: iso(1 * DAY), evidence: {}, counter_evidence: {} },
  { id: 2, slug: 'fast-migration-predicts-peak', statement: 'Tokens migrating within 2 hours of launch reach higher peak market caps.', rationale: 'Faster migration implies stronger early demand.', status: 'weakening', confidence: 'low', sample_size: 604, supporting_observations: 88, contradicting_observations: 96, p_value: 0.41, effect_size: 0.04, test_method: 'Median peak comparison, Mann-Whitney U.', first_tested_at: iso(40 * DAY), last_tested_at: iso(1 * DAY), evidence: {}, counter_evidence: {} },
  { id: 3, slug: 'image-text-hurts', statement: 'Text-heavy token images are less likely to qualify.', rationale: 'Proposed after an early observation; has not held up.', status: 'rejected', confidence: 'medium', sample_size: 2210, supporting_observations: 41, contradicting_observations: 188, p_value: 0.72, effect_size: 0.01, test_method: 'Two-proportion z-test.', first_tested_at: iso(80 * DAY), last_tested_at: iso(3 * DAY), evidence: {}, counter_evidence: {} },
]

const ANOMALIES = [
  { id: 1, kind: 'launchpad_growth', title: 'Uncatalogued launch program reached 5.8% share in 11 days', description: 'Program 9fk2mq1a… had no recorded activity before this month and now accounts for 38 qualifying tokens.', detected_at: iso(6 * HOUR), severity: 0.91, magnitude: 0.058, sample_size: 1204, acknowledged: false, research_task_id: 2, evidence: {} },
  { id: 2, kind: 'provider_discrepancy', title: 'Bitquery and DexScreener disagree 34% on SOLDOG market cap', description: 'Token sits near the $250k tier boundary. Recorded as disputed; no provider was preferred.', detected_at: iso(9 * HOUR), severity: 0.64, magnitude: 0.34, sample_size: 2, acknowledged: false, research_task_id: 4, evidence: {} },
  { id: 3, kind: 'narrative_spike', title: 'Brainrot slang tripled its share of qualifiers', description: 'From 3.8% baseline to 11.6% over 7 days.', detected_at: iso(1 * DAY), severity: 0.55, magnitude: 0.078, sample_size: 61, acknowledged: true, research_task_id: null, evidence: {} },
]

const REPORTS = Array.from({ length: 14 }, (_, i) => ({
  id: i + 1,
  kind: 'daily',
  title: `Daily intelligence — ${new Date(now - i * DAY).toDateString()}`,
  period_start: iso((i + 1) * DAY),
  period_end: iso(i * DAY),
  headline_finding: i === 0 ? 'An uncatalogued launch program reached 5.8% of qualifying tokens in 11 days. It is the fastest share gain recorded since tracking began, and nothing in the catalogue explains what it is.' : 'AI narratives continued to climb.',
  biggest_change: i === 0 ? 'Pump.fun share fell 8.1pp week-on-week — the largest single-week decline recorded for it.' : null,
  tokens_qualified: 61 - i,
  counts_by_tier: { 100000: 61 - i, 250000: 24 - i, 500000: 11, 1000000: 3 },
  trends_new: 2, trends_rising: 4, trends_declining: 2, tasks_created: 3,
  limitations: 'Enrichment coverage was 71% on the 4th; that day is excluded from trend comparisons. Creator attribution unavailable for 12% of launches.',
  generated_by_model: 'gpt-5.6-luna',
  markdown: `# Daily Solana Memecoin Intelligence\n\n## Successful Tokens\n\n$100k+: ${61 - i}\n$250k+: ${24 - i}\n$500k+: 11\n$1M+: 3\n\n## Rising Trends\n\n1. AI narrative — 23.1% (14/61) vs 11.8% baseline, p=0.0004\n2. Capybara names — 9.8% (6/61) vs 4.1% baseline, p=0.021\n\n## New Trends\n\n1. unknown-9fk2mq1a launchpad — observation only, baseline too thin to test\n\n## Declining Trends\n\n1. Politics — 4.9% (3/61) vs 14.2% baseline\n\n## Confidence / Limitations\n\nSample of 61 qualifying tokens. Coverage 71% on one day of the window, excluded.\n`,
  sections: {},
  summary: null,
}))

const CAPABILITIES = [
  { key: 'database', label: 'Database', tier: 'required', status: 'available', description: 'Permanent normalised research storage. Source of truth.', missing_env_vars: [] },
  { key: 'jobs', label: 'Background jobs', tier: 'primary', status: 'available', description: 'Ingestion, enrichment, trend calculation, report generation.', missing_env_vars: [] },
  { key: 'ai', label: 'AI reasoning', tier: 'primary', status: 'available', description: "Annie's interpretation, narrative categorisation, image analysis.", missing_env_vars: [] },
  { key: 'blockchain', label: 'Blockchain (Helius)', tier: 'primary', status: 'available', description: 'On-chain verification, transactions, accounts, token metadata.', missing_env_vars: [] },
  { key: 'market_primary', label: 'Market & launch data (Bitquery)', tier: 'primary', status: 'available', description: 'Launch discovery, migration events, DEX trades, OHLCV, liquidity.', missing_env_vars: [] },
  { key: 'market_secondary_dexscreener', label: 'Cross-validation (DexScreener)', tier: 'optional', status: 'available', description: 'Secondary price/liquidity/market-cap verification and discovery.', missing_env_vars: [] },
  { key: 'market_secondary_birdeye', label: 'Cross-validation (Birdeye)', tier: 'optional', status: 'disabled', description: 'Secondary market statistics and historical market information.', missing_env_vars: ['BIRDEYE_API_KEY'] },
  { key: 'web_research', label: 'Web research (Tavily)', tier: 'optional', status: 'disabled', description: 'External context for ecosystem events and narrative explanations.', missing_env_vars: ['TAVILY_API_KEY'] },
]

const PROVIDERS = [
  { provider: 'helius', capability_label: 'Blockchain', status: 'ok', configured: true, requests_24h: 18420, errors_24h: 31, rate_limited_24h: 4, error_rate_24h: 0.00168, p50_latency_ms: 84, p95_latency_ms: 412, estimated_cost_24h_usd: null, last_success_at: iso(40_000), last_error_at: iso(3 * HOUR), last_error_message: null, data_freshness_seconds: 240, missing_env_vars: [] },
  { provider: 'bitquery', capability_label: 'Market & launch data', status: 'degraded', configured: true, requests_24h: 2140, errors_24h: 189, rate_limited_24h: 142, error_rate_24h: 0.0883, p50_latency_ms: 1840, p95_latency_ms: 11200, estimated_cost_24h_usd: null, last_success_at: iso(120_000), last_error_at: iso(600_000), last_error_message: 'HTTP 429: query points exhausted for current window', data_freshness_seconds: 1800, missing_env_vars: [] },
  { provider: 'dexscreener', capability_label: 'Cross-validation', status: 'ok', configured: true, requests_24h: 9840, errors_24h: 12, rate_limited_24h: 0, error_rate_24h: 0.00122, p50_latency_ms: 148, p95_latency_ms: 620, estimated_cost_24h_usd: '0.00', last_success_at: iso(20_000), last_error_at: null, last_error_message: null, data_freshness_seconds: 180, missing_env_vars: [] },
  { provider: 'birdeye', capability_label: 'Cross-validation', status: 'disabled', configured: false, requests_24h: 0, errors_24h: 0, rate_limited_24h: 0, error_rate_24h: null, p50_latency_ms: null, p95_latency_ms: null, estimated_cost_24h_usd: null, last_success_at: null, last_error_at: null, last_error_message: null, data_freshness_seconds: null, missing_env_vars: ['BIRDEYE_API_KEY'] },
  { provider: 'tavily', capability_label: 'Web research', status: 'disabled', configured: false, requests_24h: 0, errors_24h: 0, rate_limited_24h: 0, error_rate_24h: null, p50_latency_ms: null, p95_latency_ms: null, estimated_cost_24h_usd: null, last_success_at: null, last_error_at: null, last_error_message: null, data_freshness_seconds: null, missing_env_vars: ['TAVILY_API_KEY'] },
  { provider: 'openai', capability_label: 'AI reasoning', status: 'ok', configured: true, requests_24h: 412, errors_24h: 2, rate_limited_24h: 1, error_rate_24h: 0.00485, p50_latency_ms: 2100, p95_latency_ms: 8400, estimated_cost_24h_usd: '4.18', last_success_at: iso(900_000), last_error_at: iso(8 * HOUR), last_error_message: null, data_freshness_seconds: null, missing_env_vars: [] },
]

const DATA_QUALITY = Array.from({ length: 14 }, (_, i) => {
  const bad = i === 4
  const attempted = 1200 + i * 40
  const succeeded = bad ? Math.round(attempted * 0.71) : Math.round(attempted * (0.96 + (i % 3) * 0.01))
  return {
    measured_on: iso(i * DAY),
    stage: 'enrichment',
    attempted,
    succeeded,
    failed: attempted - succeeded,
    coverage: succeeded / attempted,
    is_usable_for_trends: !bad,
    notes: bad ? 'Bitquery rate limits during the ingestion window; coverage below the 80% floor.' : null,
  }
})

const SETTINGS = [
  { key: 'qualification_tiers', value: [100000, 250000, 500000, 1000000], description: 'Market-cap thresholds defining research subjects and cohort boundaries.', updated_at: iso(30 * DAY) },
  { key: 'qualification_min_liquidity_usd', value: 1000, description: 'Below this, a market cap is treated as unrealisable and held for verification.', updated_at: iso(30 * DAY) },
  { key: 'trend_min_recent_sample', value: 20, description: 'Minimum qualifying tokens in the recent window before any significance is computed.', updated_at: iso(12 * DAY) },
  { key: 'trend_min_occurrences', value: 5, description: 'Minimum occurrences of a characteristic before it can leave observation status.', updated_at: iso(12 * DAY) },
  { key: 'trend_alpha_validated', value: 0.01, description: 'Significance level required for validation, combined with the persistence requirement.', updated_at: iso(12 * DAY) },
  { key: 'autonomous_research_enabled', value: false, description: 'Master switch for Annie creating and running her own research tasks.', updated_at: iso(2 * DAY) },
  { key: 'autonomous_max_cost_usd_per_day', value: 5.0, description: 'Hard daily spend ceiling for autonomous research.', updated_at: iso(2 * DAY) },
  { key: 'autonomous_max_tasks_per_day', value: 8, description: 'Maximum tasks Annie may open without being asked.', updated_at: iso(2 * DAY) },
]

/* ------------------------------------------------------------ Annie chat -- */

const REPLIES = [
  {
    match: /change|today|new/i,
    content: `Three things.\n\nAn uncatalogued launch program — 9fk2mq1a… — went from nothing to 5.8% of qualifying tokens in 11 days. That's the fastest share gain since tracking started. I've got a task open on it.\n\nAI narratives are at 23.1% of $100k+ qualifiers this week (14 of 61), up from an 11.8% baseline (88 of 746). p=0.0004, held for 9 days. Real, but it's concentrated on BonkLaunch — platform and narrative are tangled here, so I wouldn't call it a pure narrative shift yet.\n\nPump.fun lost 8.1 points of share week-on-week. Biggest single-week drop I have for it.`,
    claim_type: 'fact',
    confidence: 'high',
    citations: [
      { kind: 'trend', id: 'ai-narrative-100k', label: 'AI (token) — rising', sample: { count: 14, total: 61, frequency: 0.231 } },
      { kind: 'launchpad', id: 'unknown-9fk2mq1a', label: 'unknown-9fk2mq1a — emerging' },
      { kind: 'launchpad', id: 'pumpfun', label: 'Pump.fun — declining' },
    ],
    tool_calls: [
      { tool: 'query_trends', arguments: { status: 'rising', window_days: 7 }, result_rows: 4, succeeded: true, duration_ms: 84 },
      { tool: 'query_launchpads', arguments: { order: 'growth_7d' }, result_rows: 5, succeeded: true, duration_ms: 61 },
      { tool: 'query_anomalies', arguments: { open: true }, result_rows: 3, succeeded: true, duration_ms: 22 },
    ],
  },
  {
    match: /1m|million|separate|differ/i,
    content: `Honestly? Not much I'd stand behind yet.\n\nThe $1M+ cohort is 18 tokens this window. That's below my reporting floor of 20, so nothing from it gets a significance test.\n\nWhat I can see: brand-parody imagery shows up in 4 of those 18 (22.2%) against a 7.1% baseline. Directionally interesting. But 4 occurrences is under the minimum of 5, so I'm filing it as a hypothesis to re-check, not a finding.\n\nThe honest answer is that $1M tokens are rare enough that I need a few more weeks before I can say anything about them that isn't noise.`,
    claim_type: 'hypothesis',
    confidence: 'low',
    citations: [
      { kind: 'trend', id: 'brand-parody-1m', label: 'Brand parody (image) — $1M+ cohort', sample: { count: 4, total: 18, frequency: 0.222 } },
    ],
    tool_calls: [
      { tool: 'compare_cohorts', arguments: { tiers: [100000, 1000000], window_days: 7 }, result_rows: 42, succeeded: true, duration_ms: 210 },
    ],
  },
  {
    match: /launchpad|momentum|pump/i,
    content: `BonkLaunch is the clear one — 21.4% share, up 18.7% over 7 days, and its success rate (1.45%) is nearly double Pump.fun's (0.85%).\n\nThe more interesting one is 9fk2mq1a…, which I can't name because it isn't in any catalogue. 1,204 launches in 11 days, 38 qualifying, 3.16% success rate. That success rate is high enough that I'd want to check it isn't an artefact before believing it.\n\nPump.fun is still 61% of the market. It's declining, not collapsing.`,
    claim_type: 'fact',
    confidence: 'medium',
    citations: [
      { kind: 'launchpad', id: 'bonk-launch', label: 'BonkLaunch — growing', sample: { count: 143, total: 9840, frequency: 0.01453 } },
      { kind: 'launchpad', id: 'unknown-9fk2mq1a', label: 'unknown-9fk2mq1a — emerging', sample: { count: 38, total: 1204, frequency: 0.03156 } },
    ],
    tool_calls: [{ tool: 'query_launchpads', arguments: { order: 'growth_7d' }, result_rows: 5, succeeded: true, duration_ms: 58 }],
  },
]

const FALLBACK_REPLY = {
  content: `I don't have that in the database, and I'm not going to guess at it.\n\nWhat I can actually answer right now: current trends by cohort, launchpad share and momentum, creator track records, narrative frequencies, and anything in research memory.\n\nIf you want me to go and find out, I can queue it as a research task — those run under a fixed budget and write up what they established, including what they couldn't.`,
  claim_type: 'fact',
  confidence: 'high',
  citations: [],
  tool_calls: [{ tool: 'search_research_memory', arguments: {}, result_rows: 0, succeeded: true, duration_ms: 31 }],
}

/* ---------------------------------------------------------------- server -- */

let messageId = 100

function page(items, query) {
  const limit = Number(query.get('limit') || 50)
  const offset = Number(query.get('offset') || 0)
  return { items: items.slice(offset, offset + limit), total: items.length, limit, offset }
}

const MEMORY_SECTIONS = ['core', 'playbook', 'narratives', 'creators', 'tokens', 'daily', 'weekly', 'monthly', 'notes']

const MEMORY_SECTION_DESCRIPTIONS = {
  core: "Annie's standing beliefs about the market.",
  playbook: 'What has actually worked. Patterns with evidence behind them.',
  narratives: 'One file per live narrative/theme.',
  creators: 'One dossier per tracked creator wallet.',
  tokens: 'One file per notable token: the ones that actually moved.',
  daily: 'One file per day.',
  weekly: 'One file per ISO week.',
  monthly: 'One file per calendar month.',
  notes: 'Loose thinking that has not earned a home yet.',
}

const MEMORY_FILES = [
  {
    path: 'core/market-model.md', section: 'core', title: 'Market model',
    kind: 'core', tags: ['core', 'model'], keys: [], importance: 1.0,
    confidence: 'medium', updated: iso(2 * HOUR), created: iso(90 * DAY),
    summary: 'How I currently understand the Solana memecoin market.',
    body: [
      'How I currently understand the Solana memecoin market.',
      '',
      'This file is rewritten in place, not appended to — it is what I think',
      '*now*, and the daily and weekly files are the record of how I got here.',
      '',
      '## What launches',
      '',
      'Around 16,000 a day, overwhelmingly on Pump.fun. Roughly one in 260',
      'clears $100k. The distribution is not close to normal — a handful carry',
      'the entire cohort and the median launch never trades at all.',
      '',
      '## What actually moves',
      '',
      'Two distinct kinds, and they behave differently enough that I have',
      'stopped treating them as one population:',
      '',
      '- **Fast round-trips.** Peak inside the first hour, retrace 80%+ within',
      '  three. Almost always a single-theme burst from a wallet that launches',
      '  in bursts. These dominate the count of "winners" and teach very little.',
      '- **Slow builds.** Six hours or more of steady climb, hold most of the',
      '  peak overnight. Rare. These are the ones worth studying, and the ones',
      '  the fast-round-trip noise makes hard to see in aggregate statistics.',
      '',
      '## What dies',
      '',
      'Everything with no liquidity, which is most of it. A market cap computed',
      'from a $400 pool is not a measurement and I do not treat it as one.',
      '',
      '## Standing beliefs',
      '',
      '- Theme matters less than timing within a theme. Being third into a',
      '  narrative beats being first; being twentieth is fatal.',
      '- Creator history predicts launch *frequency* well and launch *success*',
      '  poorly. I have not found a wallet whose hit rate survives its own',
      '  volume.',
    ].join('\n'),
  },
  {
    path: 'core/whats-working.md', section: 'core', title: "What's working right now",
    kind: 'core', tags: ['core', 'playbook'], keys: [], importance: 1.0,
    confidence: 'low', updated: iso(6 * HOUR), created: iso(40 * DAY),
    summary: 'The current, short-lived edge.',
    body: [
      'The current, short-lived edge. This file is supposed to churn — what',
      'works here has a half-life measured in days.',
      '',
      '## This week',
      '',
      '- **Cat-adjacent names** are running about 3x baseline among $250k+',
      '  tokens, third week in a row. Not generic cats — the specific,',
      '  slightly-off ones ("Quantum Cat", "Cat Lawyer"). Generic ones are',
      '  performing at baseline, which suggests the theme is not the edge; the',
      '  specificity is.',
      '- **Short tickers** remain common among winners and equally common in',
      '  the baseline. Recorded here precisely so I stop re-noticing it.',
      '',
      '## Rolling over',
      '',
      '- Politics. Down to 4.9% of qualifiers from a 14.2% baseline, declining',
      '  for eleven days. I would not launch into this.',
    ].join('\n'),
  },
  {
    path: 'core/open-questions.md', section: 'core', title: 'Open questions',
    kind: 'core', tags: ['core', 'questions'], keys: [], importance: 0.8,
    confidence: 'low', updated: iso(1 * DAY), created: iso(60 * DAY),
    summary: 'Things I have noticed but cannot yet explain.',
    body: [
      'Things I have noticed but cannot yet explain, and what would settle each.',
      '',
      '- **Why do burst-launchers win at all?** A wallet firing eight tokens in',
      '  two hours produces winners at roughly the base rate. If the tokens are',
      '  interchangeable, the wins should be random — and so far they look',
      '  random. Would settle it: compare within-burst winners against',
      '  within-burst losers on every deterministic feature. If nothing',
      '  separates them, that is itself the finding.',
      '- **Is the launchpad or the narrative moving?** AI-themed tokens and',
      '  BonkLaunch grew over the same window. Confounded. Would settle it: the',
      '  same theme comparison restricted to Pump.fun alone.',
    ].join('\n'),
  },
  {
    path: 'core/watchlist.md', section: 'core', title: 'Watchlist',
    kind: 'core', tags: ['core', 'watchlist'], keys: [], importance: 0.9,
    confidence: 'high', updated: iso(2 * HOUR), created: iso(90 * DAY),
    summary: 'Who and what I am actively paying attention to.',
    body: [
      'Who and what I am actively paying attention to. Maintained automatically',
      'from the ledger — tokens by these creators are re-priced regardless of',
      'market cap.',
      '',
      '## Creators',
      '',
      ...CREATORS.filter((c) => c.is_tracked).slice(0, 5).map(
        (c) => `- \`${c.wallet}\` — ${c.total_launches} launches, ${c.winners} winners`
      ),
      '',
      '## Narratives',
      '',
      '- cat-adjacent',
      '- ai-agent',
    ].join('\n'),
  },
  {
    path: 'playbook/what-worked.md', section: 'playbook', title: 'What worked',
    kind: 'playbook', tags: ['playbook'], keys: [], importance: 0.85,
    confidence: 'medium', updated: iso(3 * DAY), created: iso(70 * DAY),
    summary: 'Patterns that survived more than one week of evidence.',
    body: [
      'Patterns that survived more than one week of evidence. Everything here',
      'has to point at something I actually observed.',
      '',
      '## Enter a theme on its second week, not its first',
      '',
      'Across four narratives now, tokens launched in a theme\'s second week',
      'cleared a tier at roughly twice the rate of first-week entrants. The',
      'plausible mechanism is that week one is dominated by the originator and',
      'the immediate copies, week two is when attention arrives and supply has',
      'not yet caught up, week three is saturation.',
      '',
      'Evidence: cat-adjacent (this window), ai-agent (3 weeks ago), capybara,',
      'brainrot-slang. Confidence medium — four observations, one direction, no',
      'counter-example yet, but four is not many.',
    ].join('\n'),
  },
  {
    path: 'daily/2026-09-08.md', section: 'daily', title: 'Daily log — 2026-09-08',
    kind: 'daily', tags: ['daily'], keys: [], importance: 0.35,
    confidence: 'high', updated: iso(4 * HOUR), created: iso(20 * HOUR),
    summary: 'Launches sighted: 15,840. Reached a tier: 61.',
    body: [
      'Launches sighted: 15840. Reached a tier: 61. Currently watching: 2140.',
      'Creator movements recorded: 15840.',
      '',
      '## Qualified today',
      '',
      ...TOKENS.filter((t) => t.is_qualified).slice(0, 4).map(
        (t) => `- **${t.symbol}** — crossed $250k, peaked $${((t.peak_market_cap || 0) / 1000).toFixed(0)}k on ${t.launchpad_slug}\n  - CA: \`${t.mint}\`\n  - Creator: \`${t.creator_wallet}\``
      ),
      '',
      '### 2026-09-08 12:00 UTC',
      '',
      'The cat-adjacent run is still going and the specificity pattern held',
      'again — three of the four qualifiers were oddly-specific cats, the',
      'generic one underperformed. Third week. Moving this from an observation',
      'to the market model if next week holds.',
    ].join('\n'),
  },
  {
    path: 'weekly/2026-W36.md', section: 'weekly', title: 'Week of 2026-09-01',
    kind: 'weekly', tags: ['weekly'], keys: [], importance: 0.7,
    confidence: 'medium', updated: iso(1 * DAY), created: iso(1 * DAY),
    summary: 'The week the cat run became hard to dismiss.',
    body: [
      'The week the cat run became hard to dismiss.',
      '',
      'Qualifiers were up slightly on the prior week (61 vs 54 at $100k+), but',
      'the composition shifted more than the count did: politics fell out',
      'almost entirely and cat-adjacent names took the space.',
      '',
      '## What changed',
      '',
      'I stopped treating "cat theme" as the variable. Generic cat tokens are',
      'performing at baseline; the ones clearing tiers are specific in a way',
      'that reads as a joke rather than a category. That reframing is the most',
      'useful thing this week produced.',
      '',
      '## Lessons',
      '',
      '- **Specificity beats category within a running theme** (medium',
      '  confidence)',
      '  - Evidence: 11 of 14 cat-adjacent qualifiers this week had a modifier;',
      '    the 9 generic ones cleared at 1 in 9.',
    ].join('\n'),
  },
  {
    path: 'notes/burst-launchers.md', section: 'notes', title: 'Burst launchers',
    kind: 'note', tags: ['creator'], keys: [], importance: 0.5,
    confidence: 'low', updated: iso(2 * DAY), created: iso(2 * DAY),
    summary: 'Wallets that fire six to eight tokens in a couple of hours.',
    body: [
      'Wallets that fire six to eight tokens in a couple of hours, then go quiet',
      'for days.',
      '',
      'They account for a large share of total launches and roughly their fair',
      'share of winners — which is the interesting part, because it means the',
      'strategy is not obviously worse than a considered one. Whether it is',
      'better depends on cost per launch, which I cannot see.',
      '',
      'Not promoting this anywhere until I can separate within-burst winners',
      'from losers on something other than luck.',
    ].join('\n'),
  },
  ...TOKENS.filter((t) => t.is_qualified).slice(0, 5).map((t) => ({
    path: `tokens/${t.mint}.md`, section: 'tokens', title: `${t.symbol} (${t.mint.slice(0, 6)}…)`,
    kind: 'token', tags: ['token', t.launchpad_slug], keys: [t.mint, t.creator_wallet, t.symbol.toLowerCase()],
    importance: 0.55, confidence: 'high', updated: iso(5 * HOUR), created: iso(20 * HOUR),
    summary: `${t.symbol} peaked at $${((t.peak_market_cap || 0) / 1000).toFixed(0)}k.`,
    body: [
      `**${t.symbol}**`, '',
      `- CA: \`${t.mint}\``,
      `- Creator: \`${t.creator_wallet}\``,
      `- Launchpad: ${t.launchpad_slug}`,
      `- Peak market cap: $${((t.peak_market_cap || 0) / 1000).toFixed(0)}k`,
    ].join('\n'),
  })),
  ...CREATORS.filter((c) => c.is_tracked).slice(0, 3).map((c) => ({
    path: `creators/${c.wallet}.md`, section: 'creators', title: `Creator ${c.wallet.slice(0, 8)}…`,
    kind: 'creator', tags: ['creator', 'tracked'], keys: [c.wallet],
    importance: 0.6, confidence: 'high', updated: iso(3 * HOUR), created: iso(30 * DAY),
    summary: `${c.total_launches} launches, ${c.winners} winners.`,
    body: [
      `Wallet: \`${c.wallet}\``, '',
      `- Launches recorded: ${c.total_launches}`,
      `- Tokens that reached a tier: ${c.winners}`,
      `- Hit rate: ${(100 * (c.success_rate || 0)).toFixed(1)}%`,
    ].join('\n'),
  })),
]

const FIRESTORE_BUDGET = {
  day: new Date().toISOString().slice(0, 10),
  writes: 312,
  write_budget: 4000,
  writes_skipped: 0,
  reads: 840,
  read_budget: 12000,
  write_headroom_pct: 92.2,
  spark_plan_daily_caps: { writes: 20000, reads: 50000, deletes: 20000 },
  note: "Counted for this process only. The Firebase console's Usage tab is the authority on what the project as a whole consumed.",
}

const PIPELINE_RESULTS = {
  discovery: { launches_seen: 184, tokens_created: 171, tokens_already_known: 13, errors: [] },
  watch: { checked: 900, priced: 214, unpriced: 686, qualified_count: 2, newly_qualified: [], new_peaks: 31, errors: [] },
  signals: { cohorts: 3, evaluated: 54, created: 2, updated: 52, promoted: ['token-theme-animal-250k'], faded: [] },
  narratives: { qualified_tokens_scanned: 668, seeded_narratives_updated: 9, emergent_narratives_found: 4 },
  cycle: { learning: { headline: 'Cat-themed names keep outperforming.' } },
}

const routes = [
  ['GET', /^\/api\/dashboard$/, (q) => {
    const windowDays = Number(q.get('window_days') || 7)
    const scale = windowDays / 7
    return {
      launches_seen_24h: 15840,
      currently_watching: 2140,
      tokens_collected: 3120,
      tokens_qualified: 668,
      qualified_24h: 61,
      creators_seen: 18420,
      creators_tracked: 214,
      creator_movements_24h: 15840,
      memory_files: 412,
      launchpads_24h: LAUNCHPADS.slice(0, 4).map((l) => ({ launchpad: l.slug, n: l.launch_count })),
      movers: TOKENS.slice(0, 8).map((t) => ({
        mint: t.mint, symbol: t.symbol, name: t.name,
        peak_market_cap: t.peak_market_cap, market_cap: t.market_cap,
        creator_wallet: t.creator_wallet, launchpad_slug: t.launchpad_slug,
      })),
      counts_by_tier: { 100000: Math.round(61 * scale), 250000: Math.round(24 * scale), 500000: Math.round(11 * scale), 1000000: Math.round(18 * scale / 7 * 7 / 7) || 3 },
      counts_by_tier_previous: { 100000: Math.round(54 * scale), 250000: Math.round(26 * scale), 500000: Math.round(11 * scale), 1000000: 5 },
      window_days: windowDays,
      trends_active: 41, trends_new: 2, trends_rising: 4, trends_declining: 2, trends_meaningful: 6,
      rising_trends: TRENDS.filter((t) => t.status === 'rising'),
      new_trends: TRENDS.filter((t) => t.status === 'new'),
      declining_trends: TRENDS.filter((t) => t.status === 'declining'),
      emerging_launchpads: LAUNCHPADS.filter((l) => l.lifecycle === 'emerging' || l.lifecycle === 'growing'),
      recent_notes: NOTES,
      pending_tasks: TASKS.filter((t) => t.status === 'queued' || t.status === 'researching'),
      open_anomalies: ANOMALIES.filter((a) => !a.acknowledged),
      data_freshness_seconds: 1800,
      last_ingestion_at: iso(30 * 60_000),
      last_trend_run_at: iso(3 * HOUR),
      provider_health: PROVIDERS,
      degraded_capabilities: CAPABILITIES.filter((c) => c.status !== 'available'),
    }
  }],

  ['GET', /^\/api\/tokens$/, (q) => {
    // The ledger holds sightings, so `qualified_only` is a filter here rather
    // than the default the old tokens collection implied.
    let items = TOKENS
    if (q.get('qualified_only') === 'true') items = items.filter((t) => t.is_qualified)
    const lp = q.get('launchpad_slug')
    if (lp) items = items.filter((t) => t.launchpad_slug === lp)
    return { ...page(items, q), window_hours: Number(q.get('hours') || 168) }
  }],
  ['GET', /^\/api\/tokens\/(.+)$/, (q, [mint]) => {
    const t = TOKENS.find((x) => x.mint === mint) || TOKENS[0]
    const creator = CREATORS.find((c) => c.wallet === t.creator_wallet) || CREATORS[0]
    // A token that moved gets a memory file; one that did not, deliberately
    // does not — the page must render that as "nothing worth writing down",
    // not as an error, so the fixture covers both.
    const memory = t.is_qualified
      ? {
          path: `tokens/${t.mint}.md`,
          body: [
            `**${t.symbol}**`,
            '',
            `- CA: \`${t.mint}\``,
            `- Creator: \`${t.creator_wallet}\``,
            `- Launchpad: ${t.launchpad_slug}`,
            `- Peak market cap: $${(t.peak_market_cap / 1000).toFixed(0)}k`,
            '',
            '## What happened',
            '',
            t.round_tripped
              ? 'Ran hard on the first hour then round-tripped almost entirely. The peak was real — liquidity held above $80k throughout — but nothing sustained it. Worth noting the creator sold into the top.'
              : 'Steady climb over about six hours, still holding most of the peak. The name lines up with the theme that has been over-represented among winners this week.',
          ].join('\n'),
        }
      : null
    return {
      ...t,
      memory,
      mentioned_in: t.is_qualified
        ? [
            {
              path: 'daily/2026-09-08.md',
              title: 'Daily log — 2026-09-08',
              section: 'daily',
              snippet: `… **${t.symbol}** — crossed $250k, peaked $${((t.peak_market_cap || 0) / 1000).toFixed(0)}k on ${t.launchpad_slug} …`,
              score: 2.1,
            },
            {
              path: 'core/whats-working.md',
              title: "What's working right now",
              section: 'core',
              snippet: `… the ${t.themes[0]} theme keeps producing, ${t.symbol} being the clearest example this week …`,
              score: 1.7,
            },
          ]
        : [],
      creator: {
        wallet: creator.wallet,
        launches: creator.total_launches,
        winners: creator.winners,
        best_market_cap: creator.best_market_cap,
        tracked: creator.is_tracked,
      },
    }
  }],

  ['GET', /^\/api\/creators$/, (q) => {
    let items = CREATORS
    if (q.get('tracked_only') === 'true') items = items.filter((c) => c.is_tracked)
    if (q.get('winners_only') === 'true') items = items.filter((c) => c.winners > 0)
    return page(items, q)
  }],
  ['GET', /^\/api\/creators\/(.+)$/, (q, [wallet]) => {
    const c = CREATORS.find((x) => x.wallet === wallet) || CREATORS[0]
    return {
      ...c,
      tokens: TOKENS.slice(0, 6),
      movements: CREATOR_MOVEMENTS(c.wallet),
      // The dossier is prose Annie wrote. A wallet only gets one once it is
      // tracked, so the fixture covers the missing case too.
      dossier: c.is_tracked
        ? {
            path: `creators/${c.wallet}.md`,
            body: [
              `Wallet: \`${c.wallet}\``,
              '',
              `- Launches recorded: ${c.total_launches}`,
              `- Tokens that reached a tier: ${c.winners}`,
              `- Hit rate: ${(100 * (c.success_rate || 0)).toFixed(1)}% of launches reached a tier`,
              '',
              '## What I make of this wallet',
              '',
              'Launches in bursts of six to eight over a couple of hours, almost always',
              'around the same theme, then goes quiet for days. The winners are not',
              'obviously different from the failures on name or ticker — what separates',
              'them looks like timing rather than the token itself, which is worth',
              'checking properly before I treat it as a finding.',
              '',
              '## Winners',
              '',
              ...TOKENS.filter((t) => t.is_qualified).slice(0, 3).map(
                (t) => `- ${t.symbol} — $${((t.peak_market_cap || 0) / 1000).toFixed(0)}k peak\n  - CA: \`${t.mint}\``
              ),
            ].join('\n'),
          }
        : null,
    }
  }],

  ['GET', /^\/api\/launchpads$/, (q) => page(LAUNCHPADS, q)],
  ['GET', /^\/api\/launchpads\/(.+)$/, (q, [slug]) => {
    const lp = LAUNCHPADS.find((x) => x.slug === slug) || LAUNCHPADS[0]
    return {
      ...lp,
      website: lp.is_known ? 'https://example.com' : null,
      ecosystem: 'solana',
      median_minutes_to_first_milestone: 188,
      counts_by_tier: { 100000: lp.qualified_count, 250000: Math.round(lp.qualified_count * 0.4), 500000: Math.round(lp.qualified_count * 0.16), 1000000: Math.round(lp.qualified_count * 0.05) },
      migration_destinations: [{ dex: 'raydium', share: 0.62 }, { dex: 'pumpswap', share: 0.31 }, { dex: 'meteora', share: 0.07 }],
      top_creators: CREATORS.slice(0, 5).map((c, n) => ({ ...c, launches_here: 40 - n * 6 })),
      recent_tokens: TOKENS.slice(0, 6),
      share_history: [],
      notes: lp.is_known ? null : 'Discovered by launchpad sweep. No public documentation found — Tavily is not configured in this deployment, so no external research has been attempted.',
    }
  }],

  ['GET', /^\/api\/narratives$/, (q) => page(NARRATIVES, q)],
  ['GET', /^\/api\/narratives\/(.+)$/, (q, [slug]) => {
    const n = NARRATIVES.find((x) => x.slug === slug) || NARRATIVES[0]
    return { ...n, description: null, keywords: ['example'], related_trends: TRENDS.slice(0, 2), recent_tokens: TOKENS.slice(0, 5) }
  }],

  ['GET', /^\/api\/trends$/, (q) => {
    let items = TRENDS
    const status = q.get('status')
    if (status) items = items.filter((t) => t.status === status)
    const tier = q.get('cohort_threshold')
    if (tier) items = items.filter((t) => t.cohort_threshold_usd === tier)
    const minMaturity = q.get('min_maturity')
    if (minMaturity) {
      const order = { observation: 0, candidate: 1, validated: 2 }
      items = items.filter((t) => order[t.maturity] >= order[minMaturity])
    }
    return page(items, q)
  }],
  ['GET', /^\/api\/trends\/(.+)$/, (q, [slug]) => TRENDS.find((t) => t.slug === slug) || TRENDS[0]],

  ['GET', /^\/api\/research\/tasks$/, (q) => page(TASKS, q)],
  ['POST', /^\/api\/research\/tasks$/, (q, m, body) => {
    const task = {
      id: TASKS.length + 1, question: body.question, reason: 'Queued by operator.',
      origin: 'user', status: 'queued', priority: 0.5, confidence: null,
      created_at: new Date().toISOString(), started_at: null, completed_at: null, cost_usd: null,
    }
    TASKS.unshift(task)
    return task
  }],
  ['GET', /^\/api\/research\/notes$/, (q) => page(NOTES, q)],
  ['GET', /^\/api\/research\/hypotheses$/, (q) => page(HYPOTHESES, q)],
  ['GET', /^\/api\/research\/anomalies$/, (q) => page(ANOMALIES, q)],

  ['GET', /^\/api\/reports$/, (q) => page(REPORTS, q)],
  ['GET', /^\/api\/reports\/(\d+)$/, (q, [id]) => REPORTS.find((r) => r.id === Number(id)) || REPORTS[0]],

  ['POST', /^\/api\/annie\/chat$/, (q, m, body) => {
    const reply = REPLIES.find((r) => r.match.test(body.message || '')) || FALLBACK_REPLY
    return {
      conversation_id: 1,
      message: {
        id: ++messageId,
        role: 'annie',
        content: reply.content,
        claim_type: reply.claim_type,
        confidence: reply.confidence,
        citations: reply.citations,
        tool_calls: reply.tool_calls,
        created_at: new Date().toISOString(),
        model: 'gpt-5.6-luna',
        cost_usd: '0.012',
        latency_ms: 1840,
      },
    }
  }],
  ['GET', /^\/api\/annie\/conversations$/, () => ({ items: [], total: 0, limit: 50, offset: 0 })],

  // -- Memory: a folder of markdown files, browsed as one ------------------
  ['GET', /^\/api\/memory$/, () => ({
    sections: MEMORY_SECTIONS.map((name) => ({
      name,
      description: MEMORY_SECTION_DESCRIPTIONS[name],
      files: MEMORY_FILES.filter((f) => f.section === name).map(({ body, ...rest }) => rest),
      count: MEMORY_FILES.filter((f) => f.section === name).length,
    })),
    total_files: MEMORY_FILES.length,
    index: { files: MEMORY_FILES.length, keys: 1515, fts: true, sections: {}, rebuilt_at: iso(2 * HOUR) },
    durability: {
      root: '/data/memory',
      configured: true,
      looks_like_volume: true,
      writable: true,
      note: 'Memory is on a configured directory outside the app tree — this is what an attached Railway Volume looks like.',
    },
  })],
  ['GET', /^\/api\/memory\/search$/, (q) => {
    const needle = (q.get('q') || '').toLowerCase()
    const byKey = MEMORY_FILES.filter((f) => (f.keys || []).some((k) => k.toLowerCase() === needle))
    const hits = (byKey.length ? byKey : MEMORY_FILES.filter(
      (f) => f.body.toLowerCase().includes(needle) || f.title.toLowerCase().includes(needle)
    )).slice(0, 12)
    return {
      query: q.get('q'),
      matched_by: byKey.length ? 'key' : 'text',
      count: hits.length,
      hits: hits.map((f) => ({
        path: f.path, title: f.title, section: f.section,
        snippet: f.body.slice(0, 240).replace(/\n/g, ' '),
        score: 2.4, matched_key: byKey.length ? needle : null,
      })),
    }
  }],
  ['GET', /^\/api\/memory\/file$/, (q) => {
    const f = MEMORY_FILES.find((x) => x.path === q.get('path')) || MEMORY_FILES[0]
    return { ...f }
  }],
  ['POST', /^\/api\/memory\/file$/, (q, m, body) => ({ ...MEMORY_FILES[0], ...body })],
  ['DELETE', /^\/api\/memory\/file$/, () => ({})],
  ['POST', /^\/api\/memory\/reindex$/, () => ({ reindexed: MEMORY_FILES.length })],
  ['GET', /^\/api\/memory\/digest$/, () => ({
    facts: {}, prompt: '# Market window: last 6h\n\n…', prompt_chars: 6862,
    approx_input_tokens: 1715, would_call_model: true,
  })],

  // -- Signals (the trends alias points at the same data) ------------------
  ['GET', /^\/api\/signals$/, (q) => ({ ...page(TRENDS, q), counts: { rising: 4, new: 2, declining: 2, stable: 5, dead: 1, meaningful: 6 } })],
  ['GET', /^\/api\/signals\/(.+)$/, (q, [slug]) => TRENDS.find((t) => t.slug === slug) || TRENDS[0]],

  ['GET', /^\/api\/ledger\/stats$/, () => ({
    ledger: { sightings_total: 3120, sightings_24h: 15840, watching: 2140, qualified_total: 668, qualified_24h: 61, creators_total: 18420, creators_tracked: 214, moves_24h: 15840 },
    signals: { rising: 4, meaningful: 6 },
    memory: { files: MEMORY_FILES.length, keys: 1515, fts: true },
    stream: { window_minutes: 60, sightings: 640, distinct_creators: 410, last_sighting_at: iso(90_000) },
    firestore: FIRESTORE_BUDGET,
  })],

  ['GET', /^\/api\/system\/cost$/, () => ({
    firestore: FIRESTORE_BUDGET,
    memory: { files: MEMORY_FILES.length, keys: 1515, fts: true },
    ledger: { sightings_total: 3120, sightings_24h: 15840, watching: 2140, qualified_total: 668, qualified_24h: 61, creators_total: 18420, creators_tracked: 214, moves_24h: 15840 },
    stream: { window_minutes: 60, sightings: 640, distinct_creators: 410, last_sighting_at: iso(90_000) },
    durability: {
      root: '/data/memory', configured: true, looks_like_volume: true, writable: true,
      note: 'Memory is on a configured directory outside the app tree — this is what an attached Railway Volume looks like.',
    },
    jobs: [
      { name: 'watch', mode: 'interval', last_run_at: iso(4 * 60_000), last_duration_seconds: 8.2, last_result: { checked: 900, priced: 214, qualified_count: 2 } },
      { name: 'cycle', mode: 'fixed_times', last_run_at: iso(2 * HOUR), last_duration_seconds: 31.4, last_result: { learning: { headline: 'Cat-themed names keep outperforming.' } } },
      { name: 'housekeeping', mode: 'daily', last_run_at: iso(9 * HOUR), last_duration_seconds: 12.1, last_result: { pruned: { sightings_dropped: 14210 } } },
    ],
    model_calls_per_day: {
      scheduled: '4 cycle calls (skipped on a quiet window), plus 1 weekly and 1 monthly rollup',
      on_demand: 'chat turns, and token_idea only when asked',
      note: 'Counting, ranking, filtering, statistics and every file write are deterministic Python over local SQLite and cost nothing.',
    },
  })],

  // Run history for the "Run now" buttons. The frontend triggers a run and
  // then polls it by id, so both the list and the single-run shape are
  // needed — a missing detail route showed up only as a console 404 in the
  // screenshot harness, never as a visible failure on the page.
  ['GET', /^\/api\/system\/pipeline-runs$/, (q) => {
    const stage = q.get('stage') || 'discovery'
    const items = Array.from({ length: 4 }, (_, n) => ({
      id: `${stage}-${n}`,
      stage,
      trigger: n === 0 ? 'manual' : 'scheduled',
      status: n === 2 ? 'error' : 'done',
      started_at: iso((n * 6 + 1) * HOUR),
      finished_at: iso((n * 6 + 1) * HOUR - 40_000),
      duration_seconds: 38 + n * 4,
      error: n === 2 ? 'DexScreener returned 429 for 2 of 30 batches; the rest completed.' : null,
      result: PIPELINE_RESULTS[stage] || {},
    }))
    return page(items, q)
  }],
  ['GET', /^\/api\/system\/pipeline-runs\/(.+)$/, (q, [id]) => {
    const stage = id.split('-')[0]
    return {
      id, stage, trigger: 'manual', status: 'done',
      started_at: iso(40_000), finished_at: iso(2_000), duration_seconds: 38,
      error: null, result: PIPELINE_RESULTS[stage] || {},
    }
  }],

  ['GET', /^\/api\/system\/health$/, () => ({ items: PROVIDERS, total: PROVIDERS.length, limit: 50, offset: 0 })],
  ['GET', /^\/api\/system\/capabilities$/, () => ({ items: CAPABILITIES, total: CAPABILITIES.length, limit: 50, offset: 0 })],
  ['GET', /^\/api\/system\/data-quality$/, (q) => page(DATA_QUALITY, q)],
  ['GET', /^\/api\/system\/settings$/, (q) => page(SETTINGS, q)],
  ['PATCH', /^\/api\/system\/settings\/(.+)$/, (q, [key], body) => {
    const s = SETTINGS.find((x) => x.key === key)
    if (s) { s.value = body.value; s.updated_at = new Date().toISOString() }
    return s
  }],

  // Auth is a stub here — the real backend (app/auth.py) is what actually
  // gates anything. This exists only so the auth screen added to App.jsx
  // doesn't block the zero-backend "just look at the interface" path.
  //
  // `token` matters here, not just `authenticated` — api.login() (see
  // src/api/client.js) only stores what comes back under `result.token`.
  // Without one, a single continuous session still looks logged in (Login.jsx
  // flips its local React state directly on a successful call, independent of
  // any stored token), but a full page reload — exactly what tools/shoot.js
  // does for every route, and exactly what a real bookmarked/shared URL does —
  // re-runs AuthGate's check, finds no token, and lands back on the login
  // screen. Found by shoot.js actually failing this way, not by inspection.
  ['GET', /^\/api\/auth\/me$/, () => ({ authenticated: true, username: 'demo' })],
  ['POST', /^\/api\/auth\/login$/, () => ({ authenticated: true, username: 'demo', token: 'fixture-demo-token' })],
  ['POST', /^\/api\/auth\/logout$/, () => ({ authenticated: false })],
]

const server = createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`)

  // Access-Control-Allow-Headers previously allowed only Content-Type — a
  // leftover from the old cookie-based auth model (Access-Control-Allow-
  // Credentials is the cookie-era header too, kept here only because
  // removing it doesn't matter either way for a bearer-token app). Every
  // authenticated request now sends an Authorization header instead, which
  // is cross-origin here (5180 vs 8000) and therefore needs an explicit
  // CORS preflight allowance — without "authorization" here, the browser's
  // preflight silently fails and every GET/PATCH/DELETE past the initial
  // login 404s at the network level, not at this server at all. Found by
  // tools/shoot.js actually failing this way on a full page reload — a
  // continuous single-session click-through never re-sends the preflight
  // for a route it already loaded, so this was invisible testing normally.
  res.setHeader('Access-Control-Allow-Origin', req.headers.origin || '*')
  res.setHeader('Access-Control-Allow-Credentials', 'true')
  res.setHeader('Access-Control-Allow-Methods', 'GET,POST,PATCH,DELETE,OPTIONS')
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization')

  if (req.method === 'OPTIONS') { res.writeHead(204).end(); return }

  let body = {}
  if (req.method === 'POST' || req.method === 'PATCH') {
    const chunks = []
    for await (const chunk of req) chunks.push(chunk)
    try { body = JSON.parse(Buffer.concat(chunks).toString() || '{}') } catch { body = {} }
  }

  for (const [method, pattern, handler] of routes) {
    if (req.method !== method) continue
    const match = pattern.exec(url.pathname)
    if (!match) continue
    // A small delay so loading states are actually visible while developing —
    // skeletons that never render are skeletons that were never checked.
    await new Promise((r) => setTimeout(r, 120))
    const payload = handler(url.searchParams, match.slice(1).map(decodeURIComponent), body)
    res.writeHead(200, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify(payload))
    return
  }

  res.writeHead(404, { 'Content-Type': 'application/json' })
  res.end(JSON.stringify({ error: 'Not found', detail: url.pathname, missing_env_vars: [] }))
})

server.listen(PORT, () => {
  console.log(`Annie fixture server → http://localhost:${PORT}`)
  console.log('Set VITE_API_BASE_URL to this address in .env (repo root)')
})
