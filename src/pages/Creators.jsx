import { useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../api/client.js'
import { useApi, useDebounced } from '../api/useApi.js'
import { Async, Badge, Empty, Panel, Sample } from '../components/primitives.jsx'
import { address, count, relative, usd } from '../lib/format.js'

/**
 * Creator Explorer (§58).
 *
 * Success rate is rendered through `<Sample>` rather than as a bare
 * percentage. A wallet with one launch and one win is 100% successful, and a
 * column of bare percentages would sort that wallet above someone with 40 wins
 * from 300 launches — which is the opposite of what the operator wants to see.
 */
export default function Creators() {
  const [query, setQuery] = useState('')
  const [repeatOnly, setRepeatOnly] = useState(false)
  const debounced = useDebounced(query, 300)

  const state = useApi(
    () => api.creators({ q: debounced || undefined, repeat_winners: repeatOnly || undefined, limit: 100 }),
    [debounced, repeatOnly]
  )

  return (
    <>
      <div className="page-head">
        <h2 className="page-head__title">Creators</h2>
        <p className="page-head__sub">
          Wallets that deployed at least one token, with wins broken out by tier — a $100k
          record is not evidence of a $1M record.
        </p>
      </div>

      <div className="filters">
        <input
          className="input"
          type="search"
          placeholder="Search wallet…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          aria-label="Search creators"
        />
        <div className="segmented">
          <button className={!repeatOnly ? 'is-active' : ''} onClick={() => setRepeatOnly(false)}>All</button>
          <button className={repeatOnly ? 'is-active' : ''} onClick={() => setRepeatOnly(true)}>Repeat winners</button>
        </div>
      </div>

      <Async
        state={state}
        empty={<Panel><Empty title="No creators match" /></Panel>}
      >
        {(data) => (
          <Panel title={`${count(data.total)} creators`} flush>
            <div className="table-wrap">
              <table className="table table--responsive">
                <thead>
                  <tr>
                    <th>Wallet</th>
                    <th className="num">Launches</th>
                    <th className="num">Success rate</th>
                    <th className="num">$100k</th>
                    <th className="num">$1M</th>
                    <th className="num">Best</th>
                    <th>Last launch</th>
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((c) => (
                    <tr key={c.wallet}>
                      <td className="primary" data-label="Wallet">
                        <Link to={`/creators/${c.wallet}`} className="row gap-2">
                          <span className="mono">{address(c.wallet, { head: 6, tail: 4 })}</span>
                          {c.is_repeat_winner && <Badge status="rising" variant="outline">Repeat</Badge>}
                        </Link>
                      </td>
                      <td className="num" data-label="Launches">{count(c.total_launches)}</td>
                      <td className="num" data-label="Success rate">
                        <Sample sample={{ count: c.wins_100k, total: c.total_launches, frequency: c.success_rate }} />
                      </td>
                      <td className="num" data-label="$100k">{count(c.wins_100k)}</td>
                      <td className="num" data-label="$1M">{count(c.wins_1m)}</td>
                      <td className="num" data-label="Best">{usd(c.best_market_cap)}</td>
                      <td data-label="Last launch" className="faint">{relative(c.last_launch_at)}</td>
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
