import { Link, useParams } from 'react-router-dom'

import { api } from '../api/client.js'
import { useApi } from '../api/useApi.js'
import { Async, Badge, Empty, Panel, Sample, Stat } from '../components/primitives.jsx'
import { count, date, duration, relative, usd } from '../lib/format.js'

/** One creator's history (§58). */
export default function CreatorDetail() {
  const { wallet } = useParams()
  const state = useApi(() => api.creator(wallet), [wallet])

  return (
    <Async state={state} rows={6}>
      {(c) => (
        <>
          <div className="page-head">
            <Link to="/creators" className="faint" style={{ fontSize: 'var(--text-xs)' }}>← Creators</Link>
            <div className="row gap-3 wrap">
              <h2 className="page-head__title mono" style={{ fontSize: 'var(--text-lg)' }}>{c.wallet}</h2>
              {c.is_repeat_winner && <Badge status="rising" variant="outline">Repeat winner</Badge>}
            </div>
            <p className="page-head__sub">
              Active {date(c.first_launch_at)} – {date(c.last_launch_at)}.
            </p>
          </div>

          <div className="grid grid--stats">
            <Stat label="Total launches" value={count(c.total_launches)} />
            <Stat
              label="Success rate"
              value={<Sample sample={c.sample ?? { count: c.wins_100k, total: c.total_launches, frequency: c.success_rate }} />}
              foot={<span className="muted">$100k+ over all launches</span>}
            />
            <Stat label="Best result" value={usd(c.best_market_cap)} />
            <Stat
              label="Launch cadence"
              value={c.median_hours_between_launches ? duration(c.median_hours_between_launches * 3600) : null}
              unknown="Too few launches"
              foot={<span className="muted">median gap</span>}
            />
          </div>

          <div className="detail">
            <Panel title="Launches" meta={`${count(c.recent_tokens?.length)} shown`} flush>
              {!c.recent_tokens?.length ? (
                <Empty title="No launches recorded" />
              ) : (
                <div className="table-wrap">
                  <table className="table table--responsive">
                    <thead>
                      <tr>
                        <th>Token</th>
                        <th>Launchpad</th>
                        <th className="num">Peak</th>
                        <th>Outcome</th>
                        <th>Launched</th>
                      </tr>
                    </thead>
                    <tbody>
                      {c.recent_tokens.map((t) => (
                        <tr key={t.mint}>
                          <td className="primary" data-label="Token">
                            <Link to={`/tokens/${t.mint}`}>
                              <strong>{t.symbol || t.name || 'Unnamed'}</strong>
                            </Link>
                          </td>
                          <td data-label="Launchpad">{t.launchpad_slug || '—'}</td>
                          <td className="num" data-label="Peak">{usd(t.peak_market_cap)}</td>
                          <td data-label="Outcome">
                            {t.is_qualified
                              ? <Badge status="verified" variant="outline">Qualified</Badge>
                              : <span className="faint" style={{ fontSize: 'var(--text-xs)' }}>Below threshold</span>}
                          </td>
                          <td data-label="Launched" className="faint">{relative(t.launched_at)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Panel>

            <div className="stack gap-5">
              <Panel title="Results by tier">
                <dl className="deflist">
                  <dt>$100k+</dt><dd>{count(c.wins_100k)}</dd>
                  <dt>$250k+</dt><dd>{count(c.wins_250k)}</dd>
                  <dt>$500k+</dt><dd>{count(c.wins_500k)}</dd>
                  <dt>$1M+</dt><dd>{count(c.wins_1m)}</dd>
                </dl>
                <p className="faint" style={{ fontSize: 'var(--text-2xs)', marginTop: 'var(--space-3)' }}>
                  Tiers are cumulative. A $1M token also counts in every tier below it.
                </p>
              </Panel>

              <Panel title="Platform history">
                {!c.launchpad_history?.length ? (
                  <span className="faint" style={{ fontSize: 'var(--text-xs)' }}>Not computed</span>
                ) : (
                  <div className="stack gap-2">
                    {c.launchpad_history.map((h, i) => (
                      <div key={i} className="row between gap-3" style={{ fontSize: 'var(--text-sm)' }}>
                        <Link to={`/launchpads/${h.slug}`}>{h.slug}</Link>
                        <span className="mono faint">{count(h.launches)}</span>
                      </div>
                    ))}
                  </div>
                )}
              </Panel>
            </div>
          </div>
        </>
      )}
    </Async>
  )
}
