import { Link, useParams } from 'react-router-dom'

import { api } from '../api/client.js'
import { useApi } from '../api/useApi.js'
import { Async, Badge, Delta, Empty, Panel, Sample, Stat } from '../components/primitives.jsx'
import { address, count, date, minutes, percent, relative, tierLabel, usd } from '../lib/format.js'

/** One launch platform (§59). */
export default function LaunchpadDetail() {
  const { slug } = useParams()
  const state = useApi(() => api.launchpad(slug), [slug])

  return (
    <Async state={state} rows={6}>
      {(lp) => (
        <>
          <div className="page-head">
            <Link to="/launchpads" className="faint" style={{ fontSize: 'var(--text-xs)' }}>← Launchpads</Link>
            <div className="row gap-3 wrap">
              <h2 className="page-head__title">{lp.name}</h2>
              <Badge status={lp.lifecycle} dot />
              {!lp.is_known && <Badge status="new" variant="outline">Discovered</Badge>}
            </div>
            <p className="page-head__sub">
              First seen {date(lp.first_seen_at)}
              {lp.discovered_by && ` · discovered by ${lp.discovered_by}`}
            </p>
          </div>

          <div className="grid grid--stats">
            <Stat label="Launches" value={count(lp.launch_count)} />
            <Stat
              label="Success rate"
              value={<Sample sample={{ count: lp.qualified_count, total: lp.launch_count, frequency: lp.success_rate }} decimals={2} />}
            />
            <Stat label="Market share" value={percent(lp.market_share)} foot={<Delta value={lp.growth_rate_7d} />} />
            <Stat
              label="Time to first milestone"
              value={lp.median_minutes_to_first_milestone ? minutes(lp.median_minutes_to_first_milestone) : null}
              unknown="Not computed"
              foot={<span className="muted">median</span>}
            />
          </div>

          <div className="detail">
            <div className="stack gap-5">
              <Panel title="Recent qualified tokens" flush>
                {!lp.recent_tokens?.length ? (
                  <Empty title="No qualified tokens" body="Nothing from this platform has met the research threshold." />
                ) : (
                  <div className="table-wrap">
                    <table className="table table--responsive">
                      <thead>
                        <tr>
                          <th>Token</th>
                          <th className="num">Peak</th>
                          <th>Qualified</th>
                        </tr>
                      </thead>
                      <tbody>
                        {lp.recent_tokens.map((t) => (
                          <tr key={t.mint}>
                            <td className="primary" data-label="Token">
                              <Link to={`/tokens/${t.mint}`}><strong>{t.symbol || t.name}</strong></Link>
                            </td>
                            <td className="num" data-label="Peak">{usd(t.peak_market_cap)}</td>
                            <td data-label="Qualified" className="faint">{relative(t.qualified_at)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </Panel>

              <Panel title="Top creators" flush>
                {!lp.top_creators?.length ? (
                  <Empty title="No creator data" />
                ) : (
                  <div className="table-wrap">
                    <table className="table table--responsive">
                      <thead>
                        <tr>
                          <th>Wallet</th>
                          <th className="num">Launches</th>
                          <th className="num">$100k+</th>
                        </tr>
                      </thead>
                      <tbody>
                        {lp.top_creators.map((c) => (
                          <tr key={c.wallet}>
                            <td className="primary" data-label="Wallet">
                              <Link to={`/creators/${c.wallet}`} className="mono">{address(c.wallet, { head: 6, tail: 4 })}</Link>
                            </td>
                            <td className="num" data-label="Launches">{count(c.total_launches)}</td>
                            <td className="num" data-label="$100k+">{count(c.wins_100k)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </Panel>
            </div>

            <div className="stack gap-5">
              <Panel title="Results by tier">
                {!lp.counts_by_tier || !Object.keys(lp.counts_by_tier).length ? (
                  <span className="faint" style={{ fontSize: 'var(--text-xs)' }}>Not computed</span>
                ) : (
                  <dl className="deflist">
                    {Object.entries(lp.counts_by_tier).map(([tier, n]) => (
                      <div key={tier} style={{ display: 'contents' }}>
                        <dt>{tierLabel(tier)}+</dt>
                        <dd>{count(n)}</dd>
                      </div>
                    ))}
                  </dl>
                )}
              </Panel>

              <Panel title="Migration destinations" meta="§19">
                {!lp.migration_destinations?.length ? (
                  <span className="faint" style={{ fontSize: 'var(--text-xs)' }}>None recorded</span>
                ) : (
                  <div className="stack gap-2">
                    {lp.migration_destinations.map((d, i) => (
                      <div key={i} className="row between gap-3" style={{ fontSize: 'var(--text-sm)' }}>
                        <span>{d.dex}</span>
                        <span className="mono faint">{percent(d.share)}</span>
                      </div>
                    ))}
                  </div>
                )}
              </Panel>

              {lp.notes && (
                <Panel title="Notes">
                  <p style={{ fontSize: 'var(--text-sm)', lineHeight: 'var(--leading-relaxed)' }}>{lp.notes}</p>
                </Panel>
              )}
            </div>
          </div>
        </>
      )}
    </Async>
  )
}
