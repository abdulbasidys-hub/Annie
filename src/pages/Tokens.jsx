import { useState } from 'react'

import { api } from '../api/client.js'
import { useApi, useDebounced } from '../api/useApi.js'
import { Async, Badge, ClickableRow, CopyableAddress, Empty, Panel } from '../components/primitives.jsx'
import { count, relative, usd } from '../lib/format.js'

const TIERS = [
  { value: '', label: 'All qualified' },
  { value: '100000', label: '$100k+' },
  { value: '250000', label: '$250k+' },
  { value: '500000', label: '$500k+' },
  { value: '1000000', label: '$1M+' },
]

const WINDOWS = [
  { value: 24, label: 'Last 24h' },
  { value: 168, label: 'Last 7 days' },
  { value: 720, label: 'Last 30 days' },
  { value: 2160, label: 'Everything held' },
]

/**
 * Token Explorer.
 *
 * Reads the ledger, so this lists what Annie is *holding* — tokens that
 * actually moved. The thousands of launches a day that never trade are seen,
 * counted, and dropped within 48 hours, so their absence here is the design
 * working, not data missing. Filtering happens client-side against a single
 * windowed fetch: the whole held set is small by construction.
 */
export default function Tokens() {
  const [query, setQuery] = useState('')
  const [tier, setTier] = useState('')
  const [hours, setHours] = useState(168)
  const [qualifiedOnly, setQualifiedOnly] = useState(false)
  const debounced = useDebounced(query, 300)

  const state = useApi(
    () => api.tokens({ hours, qualified_only: qualifiedOnly || undefined, limit: 200 }),
    [hours, qualifiedOnly]
  )

  const items = (state.data?.items ?? []).filter((t) => {
    if (tier && (t.peak_market_cap ?? 0) < Number(tier)) return false
    if (!debounced) return true
    const needle = debounced.toLowerCase()
    return [t.symbol, t.name, t.mint, t.creator_wallet]
      .filter(Boolean)
      .some((field) => String(field).toLowerCase().includes(needle))
  })

  return (
    <>
      <div className="page-head">
        <h2 className="page-head__title">Tokens</h2>
        <p className="page-head__sub">
          What Annie is holding — the ones that moved. Everything else she saw is counted and
          forgotten within 48 hours.
        </p>
      </div>

      <div className="filters">
        <input
          className="input"
          type="search"
          placeholder="Search name, ticker, mint, or creator…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          aria-label="Search tokens"
        />
        <select className="select" value={tier} onChange={(e) => setTier(e.target.value)} aria-label="Tier">
          {TIERS.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
        </select>
        <select
          className="select"
          value={hours}
          onChange={(e) => setHours(Number(e.target.value))}
          aria-label="Window"
        >
          {WINDOWS.map((w) => <option key={w.value} value={w.value}>{w.label}</option>)}
        </select>
        <div className="segmented">
          <button className={!qualifiedOnly ? 'is-active' : ''} onClick={() => setQualifiedOnly(false)}>
            All held
          </button>
          <button className={qualifiedOnly ? 'is-active' : ''} onClick={() => setQualifiedOnly(true)}>
            Cleared a tier
          </button>
        </div>
      </div>

      <Async
        state={state}
        empty={
          <Panel>
            <Empty
              title="No tokens match"
              body={
                debounced
                  ? `Nothing matched “${debounced}”.`
                  : 'Nothing has moved in this window yet. Launches are sighted continuously; a token appears here once it actually trades.'
              }
            />
          </Panel>
        }
      >
        {() => (
          <Panel
            title={`${count(items.length)} tokens`}
            meta={`of ${count(state.data?.total ?? 0)} held in this window`}
            flush
          >
            <div className="table-wrap">
              <table className="table table--responsive">
                <thead>
                  <tr>
                    <th>Token</th>
                    <th>Launchpad</th>
                    <th className="num">Now</th>
                    <th className="num">Peak</th>
                    <th>Themes</th>
                    <th>Status</th>
                    <th>First seen</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((t) => (
                    <ClickableRow key={t.mint} to={`/tokens/${t.mint}`}>
                      <td className="primary" data-label="Token">
                        <div className="row gap-3">
                          <span className="stack" style={{ gap: 0, minWidth: 0 }}>
                            <strong className="truncate">{t.symbol || t.name || `${t.mint?.slice(0, 6)}…`}</strong>
                            <span className="row gap-1" style={{ minWidth: 0 }}>
                              <span className="faint truncate" style={{ fontSize: 'var(--text-2xs)' }}>
                                {t.name}
                              </span>
                              <CopyableAddress value={t.mint} className="mono faint" />
                            </span>
                          </span>
                        </div>
                      </td>
                      <td data-label="Launchpad">{t.launchpad_slug || '—'}</td>
                      <td className="num" data-label="Now">{usd(t.market_cap)}</td>
                      <td className="num" data-label="Peak">
                        {usd(t.peak_market_cap)}
                        {t.round_tripped && (
                          <span className="faint" style={{ fontSize: 'var(--text-2xs)' }}>
                            {' '}round-tripped
                          </span>
                        )}
                      </td>
                      <td data-label="Themes">
                        <span className="row gap-1 wrap">
                          {(t.themes || []).slice(0, 2).map((theme) => (
                            <Badge key={theme} status="stable" variant="plain">{theme}</Badge>
                          ))}
                          {(t.themes?.length ?? 0) > 2 && (
                            <span className="faint" style={{ fontSize: 'var(--text-2xs)' }}>
                              +{t.themes.length - 2}
                            </span>
                          )}
                        </span>
                      </td>
                      <td data-label="Status">
                        {t.is_qualified ? (
                          <Badge status="verified" variant="outline">
                            {usd(t.peak_tier)}
                          </Badge>
                        ) : (
                          <Badge status={t.status} variant="plain">{t.status}</Badge>
                        )}
                      </td>
                      <td data-label="First seen" className="faint">{relative(t.first_seen)}</td>
                    </ClickableRow>
                  ))}
                </tbody>
              </table>
            </div>
          </Panel>
        )}
      </Async>
    </>
  )
}
