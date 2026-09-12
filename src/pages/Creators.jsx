import { useState } from 'react'

import { api } from '../api/client.js'
import { useApi, useDebounced } from '../api/useApi.js'
import { Async, Badge, ClickableRow, CopyableAddress, Empty, Panel, Sample } from '../components/primitives.jsx'
import { count, relative, usd } from '../lib/format.js'

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
  const [mode, setMode] = useState('all')
  const [window, setWindow] = useState(0)
  const debounced = useDebounced(query, 300)

  const state = useApi(
    () =>
      api.creators({
        limit: 200,
        // Explicit either way. The API now defaults this on, so sending
        // `undefined` for "everyone" would quietly filter the tab that
        // promises not to.
        winners_only: mode !== 'everyone',
        tracked_only: mode === 'tracked' || undefined,
        window_hours: window || undefined,
      }),
    [mode, window]
  )

  const items = (state.data?.items ?? []).filter(
    (c) => !debounced || c.wallet.toLowerCase().includes(debounced.toLowerCase())
  )

  return (
    <>
      <div className="page-head">
        <h2 className="page-head__title">Creators</h2>
        <p className="page-head__sub">
          Every wallet seen deploying, with every launch counted. Tracked wallets are the ones
          Annie decided are worth following — high-volume launchers, and anyone who produced a
          winner.
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
          <button className={mode === 'all' ? 'is-active' : ''} onClick={() => setMode('all')}>
            Produced a winner
          </button>
          <button
            className={mode === 'everyone' ? 'is-active' : ''}
            onClick={() => setMode('everyone')}
            title="Includes the tens of thousands of wallets that have launched and never landed one."
          >
            Everyone
          </button>
          <button className={mode === 'tracked' ? 'is-active' : ''} onClick={() => setMode('tracked')}>
            Tracked
          </button>
        </div>
        <select
          className="select"
          value={window}
          onChange={(e) => setWindow(Number(e.target.value))}
          aria-label="Window"
        >
          <option value={0}>Lifetime totals</option>
          <option value={24}>Busiest today</option>
          <option value={168}>Busiest this week</option>
        </select>
      </div>

      <Async
        state={state}
        empty={<Panel><Empty title="No creators match" /></Panel>}
      >
        {() => (
          <Panel
            title={`${count(items.length)} creators`}
            meta={window ? 'ordered by launches in window' : 'ordered by winners, then best result'}
            flush
          >
            <div className="table-wrap">
              <table className="table table--responsive">
                <thead>
                  <tr>
                    <th>Wallet</th>
                    <th className="num">Launches</th>
                    <th className="num">{window ? 'In window' : 'Hit rate'}</th>
                    <th className="num">Winners</th>
                    <th className="num">Best</th>
                    <th>Last seen</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((c) => (
                    <ClickableRow key={c.wallet} to={`/creators/${c.wallet}`}>
                      <td className="primary" data-label="Wallet">
                        <div className="row gap-2">
                          <CopyableAddress value={c.wallet} head={6} tail={4} />
                          {c.is_tracked && <Badge status="rising" variant="outline">Tracked</Badge>}
                        </div>
                      </td>
                      <td className="num" data-label="Launches">{count(c.total_launches)}</td>
                      <td className="num" data-label={window ? 'In window' : 'Hit rate'}>
                        {window ? (
                          count(c.launches_in_window)
                        ) : (
                          <Sample
                            sample={{
                              count: c.winners,
                              total: c.total_launches,
                              frequency: c.success_rate,
                            }}
                          />
                        )}
                      </td>
                      <td className="num" data-label="Winners">{count(c.winners)}</td>
                      <td className="num" data-label="Best">{usd(c.best_market_cap)}</td>
                      <td data-label="Last seen" className="faint">{relative(c.last_seen)}</td>
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
