import { Link } from 'react-router-dom'

import { api } from '../api/client.js'
import { useApi } from '../api/useApi.js'
import { Async, Badge, Delta, Empty, Panel, Sample } from '../components/primitives.jsx'
import { count, percent, relative } from '../lib/format.js'

/**
 * Launchpad Explorer (§59).
 *
 * Unknown platforms are flagged rather than filtered out. §5 forbids assuming
 * success originates from Pump.fun, and the whole value of tracking launchpads
 * is catching the one nobody had heard of last month.
 */
export default function Launchpads() {
  const state = useApi(() => api.launchpads({ limit: 100 }), [])

  return (
    <>
      <div className="page-head">
        <h2 className="page-head__title">Launchpads</h2>
        <p className="page-head__sub">
          Every launch platform seen on chain, including ones discovered mid-ingestion.
        </p>
      </div>

      <Async
        state={state}
        empty={
          <Panel>
            <Empty
              title="No launchpads yet"
              body="Launch platforms are catalogued the first time a token is attributed to them."
            />
          </Panel>
        }
      >
        {(data) => (
          <Panel title={`${count(data.total)} platforms`} flush>
            <div className="table-wrap">
              <table className="table table--responsive">
                <thead>
                  <tr>
                    <th>Platform</th>
                    <th>Lifecycle</th>
                    <th className="num">Launches</th>
                    <th className="num">Success rate</th>
                    <th className="num">Share</th>
                    <th className="num">7d growth</th>
                    <th>Last seen</th>
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((lp) => (
                    <tr key={lp.slug}>
                      <td className="primary" data-label="Platform">
                        <Link to={`/launchpads/${lp.slug}`} className="row gap-2">
                          <strong>{lp.name}</strong>
                          {!lp.is_known && (
                            <Badge status="new" title="Discovered from chain data, not a catalogued platform">
                              Discovered
                            </Badge>
                          )}
                        </Link>
                      </td>
                      <td data-label="Lifecycle"><Badge status={lp.lifecycle} dot /></td>
                      <td className="num" data-label="Launches">{count(lp.launch_count)}</td>
                      <td className="num" data-label="Success rate">
                        <Sample
                          sample={{ count: lp.qualified_count, total: lp.launch_count, frequency: lp.success_rate }}
                          decimals={2}
                        />
                      </td>
                      <td className="num" data-label="Share">{percent(lp.market_share)}</td>
                      <td className="num" data-label="7d growth"><Delta value={lp.growth_rate_7d} /></td>
                      <td data-label="Last seen" className="faint">{relative(lp.last_seen_at)}</td>
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
