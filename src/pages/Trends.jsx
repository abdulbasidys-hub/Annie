import { useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../api/client.js'
import { useApi } from '../api/useApi.js'
import { Async, Badge, Delta, Empty, Maturity, Panel, Sample } from '../components/primitives.jsx'
import { count, relative, tierLabel } from '../lib/format.js'

const STATUSES = ['all', 'rising', 'new', 'stable', 'declining', 'dead']
const TIERS = [
  { value: '', label: 'All tiers' },
  { value: '100000', label: '$100k+' },
  { value: '250000', label: '$250k+' },
  { value: '500000', label: '$500k+' },
  { value: '1000000', label: '$1M+' },
]

/**
 * Trend Explorer (§60).
 *
 * The tier filter is prominent rather than tucked into an advanced panel,
 * because §27 makes cohort the most consequential dimension here: the same
 * characteristic can be rising among $100k tokens and flat among $1M ones, and
 * a reader who does not know which cohort they are looking at has learned
 * nothing.
 */
export default function Trends() {
  const [status, setStatus] = useState('rising')
  const [tier, setTier] = useState('100000')
  const [minMaturity, setMinMaturity] = useState('candidate')

  const state = useApi(
    () =>
      api.trends({
        status: status === 'all' ? undefined : status,
        cohort_threshold: tier || undefined,
        min_maturity: minMaturity === 'all' ? undefined : minMaturity,
        limit: 100,
      }),
    [status, tier, minMaturity]
  )

  return (
    <>
      <div className="page-head">
        <h2 className="page-head__title">Trends</h2>
        <p className="page-head__sub">
          Characteristics recurring among tokens that reached each market-cap tier, measured
          against their own historical baseline.
        </p>
      </div>

      <div className="filters">
        <div className="segmented" role="group" aria-label="Status">
          {STATUSES.map((s) => (
            <button key={s} className={s === status ? 'is-active' : ''} onClick={() => setStatus(s)}>
              {s === 'all' ? 'All' : s[0].toUpperCase() + s.slice(1)}
            </button>
          ))}
        </div>

        <select className="select" value={tier} onChange={(e) => setTier(e.target.value)} aria-label="Cohort">
          {TIERS.map((t) => (
            <option key={t.value} value={t.value}>{t.label}</option>
          ))}
        </select>

        <select
          className="select"
          value={minMaturity}
          onChange={(e) => setMinMaturity(e.target.value)}
          aria-label="Minimum evidence"
        >
          <option value="all">Any evidence</option>
          <option value="candidate">Candidate and above</option>
          <option value="validated">Validated only</option>
        </select>
      </div>

      <Async
        state={state}
        empty={
          <Panel>
            <Empty
              title="No trends match"
              body={
                minMaturity === 'validated'
                  ? 'Nothing has met the validation bar yet. Validation needs statistical significance sustained across several days — try lowering the evidence filter.'
                  : 'No characteristics in this cohort match the selected status.'
              }
            />
          </Panel>
        }
      >
        {(data) => (
          <Panel
            title={`${count(data.total)} trend${data.total === 1 ? '' : 's'}`}
            meta={tier ? `${tierLabel(tier)}+ cohort` : 'All cohorts'}
            flush
          >
            <div className="table-wrap">
              <table className="table table--responsive table--clickable">
                <thead>
                  <tr>
                    <th>Characteristic</th>
                    <th>Status</th>
                    <th>Evidence</th>
                    <th className="num">Recent</th>
                    <th className="num">Baseline</th>
                    <th className="num">Change</th>
                    <th className="num">Days</th>
                    <th>Last seen</th>
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((trend) => (
                    <tr key={trend.id}>
                      <td className="primary" data-label="Characteristic">
                        <Link to={`/trends/${trend.slug}`} className="stack gap-1">
                          <strong>{trend.name}</strong>
                          <span className="faint" style={{ fontSize: 'var(--text-2xs)' }}>
                            {trend.category}
                          </span>
                        </Link>
                      </td>
                      <td data-label="Status"><Badge status={trend.status} dot /></td>
                      <td data-label="Evidence"><Maturity value={trend.maturity} /></td>
                      <td className="num" data-label="Recent"><Sample sample={trend.recent} /></td>
                      <td className="num" data-label="Baseline"><Sample sample={trend.baseline} /></td>
                      <td className="num" data-label="Change"><Delta value={trend.change} /></td>
                      <td className="num" data-label="Days">{count(trend.persistence_days)}</td>
                      <td data-label="Last seen" className="faint">{relative(trend.last_observed_at)}</td>
                    </tr>
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
