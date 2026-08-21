import { api } from '../api/client.js'
import { useApi } from '../api/useApi.js'
import { Async, Badge, Panel } from '../components/primitives.jsx'

/**
 * Data Sources (§55).
 *
 * Explains what each provider is *for* and what is lost without it. The health
 * page answers "is it up"; this one answers "does it matter", which is the
 * question an operator deciding whether to pay for a key actually has.
 *
 * The role descriptions are static because they describe the architecture, not
 * runtime state — only the status badges come from the API.
 */
const ROLES = {
  helius: {
    title: 'Helius',
    role: 'Primary blockchain infrastructure',
    uses: ['Token metadata', 'Deployer resolution', 'Holder concentration', 'On-chain verification'],
    without: 'No chain-level verification. Creator attribution falls back to indexer fee-payer data, which is often the launchpad relayer rather than the actual deployer.',
    trust: 'Highest — read directly from chain.',
  },
  bitquery: {
    title: 'Bitquery',
    role: 'Primary market, launch and DEX data',
    uses: ['Launch discovery', 'Launchpad discovery', 'Migration events', 'OHLCV', 'Peak market caps'],
    without: 'No launch discovery. The pipeline has nothing to qualify, and the trend engine has no new cohorts.',
    trust: 'High — indexed chain data.',
  },
  dexscreener: {
    title: 'DexScreener',
    role: 'Cross-validation and discovery',
    uses: ['Second-opinion market caps', 'Liquidity', 'Pair discovery'],
    without: 'Market caps rest on a single source. Qualification still works but is recorded as unverified rather than cross-verified.',
    trust: 'Medium — derived from pair liquidity.',
  },
  birdeye: {
    title: 'Birdeye',
    role: 'Optional secondary market data',
    uses: ['Token statistics', 'Historical prices', 'Additional cross-validation'],
    without: 'One fewer corroborating source. Nothing depends on it exclusively.',
    trust: 'Medium.',
  },
  tavily: {
    title: 'Tavily',
    role: 'External web research',
    uses: ['Ecosystem events', 'Narrative context', 'Explanations for observed migrations'],
    without: 'Annie can describe what changed but not look for a public event that coincides with it.',
    trust: 'Lowest — external commentary, never a substitute for chain data.',
  },
  openai: {
    title: 'OpenAI',
    role: 'Reasoning and image analysis',
    uses: ['Annie', 'Image feature extraction', 'Narrative categorisation', 'Report writing'],
    without: 'Ingestion and the trend engine still run. Annie does not, and image characteristics are not extracted.',
    trust: 'Model output — features from here are labelled as model-generated, and trends resting on them are identifiable as such.',
  },
}

export default function DataSources() {
  const health = useApi(() => api.health(), [])

  return (
    <>
      <div className="page-head">
        <h2 className="page-head__title">Data sources</h2>
        <p className="page-head__sub">
          What each provider contributes, how much it is trusted, and what is lost without it.
          Providers sit behind adapters — replacing one does not touch the trend engine,
          research engine, Annie or the database.
        </p>
      </div>

      <Async state={health}>
        {(data) => {
          const items = data.items || data
          const byName = Object.fromEntries(items.map((p) => [p.provider, p]))
          return (
            <div className="grid grid--halves">
              {Object.entries(ROLES).map(([key, info]) => {
                const live = byName[key]
                return (
                  <Panel
                    key={key}
                    title={info.title}
                    actions={live ? <Badge status={live.status} dot /> : <Badge status="disabled">Unknown</Badge>}
                  >
                    <div className="stack gap-3">
                      <span className="secondary" style={{ fontSize: 'var(--text-sm)' }}>{info.role}</span>

                      <div className="stack gap-1">
                        <span className="faint" style={{ fontSize: 'var(--text-2xs)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
                          Used for
                        </span>
                        <div className="row gap-1 wrap">
                          {info.uses.map((u) => (
                            <Badge key={u} status="stable" variant="plain">{u}</Badge>
                          ))}
                        </div>
                      </div>

                      <div className="stack gap-1">
                        <span className="faint" style={{ fontSize: 'var(--text-2xs)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
                          Without it
                        </span>
                        <span style={{ fontSize: 'var(--text-sm)', lineHeight: 'var(--leading-relaxed)' }}>
                          {info.without}
                        </span>
                      </div>

                      <div className="stack gap-1">
                        <span className="faint" style={{ fontSize: 'var(--text-2xs)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
                          Trust level
                        </span>
                        <span className="secondary" style={{ fontSize: 'var(--text-sm)' }}>{info.trust}</span>
                      </div>

                      {live?.missing_env_vars?.length > 0 && (
                        <div className="caveats">
                          <span className="caveats__label">Not configured</span>
                          <span>Set {live.missing_env_vars.join(', ')} to enable.</span>
                        </div>
                      )}
                    </div>
                  </Panel>
                )
              })}
            </div>
          )
        }}
      </Async>
    </>
  )
}
