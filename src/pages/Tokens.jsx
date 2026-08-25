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

const SORTS = [
  { value: 'qualified_at', label: 'Recently qualified' },
  { value: 'peak_market_cap', label: 'Highest peak' },
  { value: 'launched_at', label: 'Recently launched' },
]

/** Token Explorer (§57). */
export default function Tokens() {
  const [query, setQuery] = useState('')
  const [tier, setTier] = useState('')
  const [sort, setSort] = useState('qualified_at')
  const debounced = useDebounced(query, 300)

  const state = useApi(
    () => api.tokens({ q: debounced || undefined, min_tier: tier || undefined, sort, qualified_only: true, limit: 100 }),
    [debounced, tier, sort]
  )

  return (
    <>
      <div className="page-head">
        <h2 className="page-head__title">Tokens</h2>
        <p className="page-head__sub">
          Every token that met the research threshold, with the evidence used to qualify it.
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
        <select className="select" value={sort} onChange={(e) => setSort(e.target.value)} aria-label="Sort">
          {SORTS.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
        </select>
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
                  : 'No tokens have qualified yet. Qualified tokens appear after the ingestion pipeline has run.'
              }
            />
          </Panel>
        }
      >
        {(data) => (
          <Panel title={`${count(data.total)} tokens`} flush>
            <div className="table-wrap">
              <table className="table table--responsive">
                <thead>
                  <tr>
                    <th>Token</th>
                    <th>Launchpad</th>
                    <th className="num">At qualification</th>
                    <th className="num">Peak</th>
                    <th>Themes</th>
                    <th>Evidence</th>
                    <th>Qualified</th>
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((t) => (
                    <ClickableRow key={t.mint} to={`/tokens/${t.mint}`}>
                      <td className="primary" data-label="Token">
                        <div className="row gap-3">
                          {t.image_url ? (
                            <img
                              src={t.image_url}
                              alt=""
                              loading="lazy"
                              width={24}
                              height={24}
                              style={{ borderRadius: '50%', flexShrink: 0, objectFit: 'cover' }}
                              onError={(e) => { e.currentTarget.style.visibility = 'hidden' }}
                            />
                          ) : (
                            <span
                              style={{
                                width: 24, height: 24, borderRadius: '50%',
                                background: 'var(--bg-inset)', flexShrink: 0,
                              }}
                            />
                          )}
                          <span className="stack" style={{ gap: 0, minWidth: 0 }}>
                            <strong className="truncate">{t.symbol || 'Unnamed'}</strong>
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
                      <td className="num" data-label="At qualification">{usd(t.qualified_market_cap)}</td>
                      <td className="num" data-label="Peak">{usd(t.peak_market_cap)}</td>
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
                      <td data-label="Evidence"><Badge status={t.verification_status} /></td>
                      <td data-label="Qualified" className="faint">{relative(t.qualified_at)}</td>
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
