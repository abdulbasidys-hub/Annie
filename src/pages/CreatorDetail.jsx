import { Link, useParams } from 'react-router-dom'

import { api } from '../api/client.js'
import { useApi } from '../api/useApi.js'
import { Async, Badge, ClickableRow, CopyableAddress, Empty, Markdown, Panel, Sample, Stat } from '../components/primitives.jsx'
import { count, date, duration, relative, usd } from '../lib/format.js'

/**
 * One creator's full record.
 *
 * Two halves, and the second is the point. The stats and launch table are the
 * ledger — complete, because every launch by every wallet is recorded locally.
 * The dossier below them is what Annie actually wrote about this wallet, which
 * is usually the reason it is worth looking at.
 */
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
              <h2 className="page-head__title" style={{ fontSize: 'var(--text-lg)' }}>
                <CopyableAddress value={c.wallet} full className="mono" />
              </h2>
              {c.is_tracked && <Badge status="rising" variant="outline">Tracked</Badge>}
            </div>
            <p className="page-head__sub">
              Active {date(c.first_seen)} – {date(c.last_seen)}.
              {c.is_tracked
                ? ' Tracked, so this wallet’s tokens are re-priced ahead of everything else.'
                : ''}
            </p>
          </div>

          <div className="grid grid--stats">
            <Stat label="Total launches" value={count(c.total_launches)} />
            <Stat
              label="Hit rate"
              value={
                <Sample
                  sample={{
                    count: c.winners,
                    total: c.total_launches,
                    frequency: c.success_rate,
                  }}
                />
              }
              foot={<span className="muted">reached a tier, over all launches</span>}
            />
            <Stat label="Best result" value={usd(c.best_market_cap)} />
            <Stat
              label="Movements recorded"
              value={count(c.movements?.length)}
              foot={<span className="muted">every launch and milestone</span>}
            />
          </div>

          <div className="detail">
            <Panel title="Tokens" meta={`${count(c.tokens?.length)} held`} flush>
              {!c.tokens?.length ? (
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
                        <th>First seen</th>
                      </tr>
                    </thead>
                    <tbody>
                      {c.tokens.map((t) => (
                        <ClickableRow key={t.mint} to={`/tokens/${t.mint}`}>
                          <td className="primary" data-label="Token">
                            <div className="row gap-2">
                              <strong>{t.symbol || t.name || 'Unnamed'}</strong>
                              <CopyableAddress value={t.mint} className="mono faint" />
                            </div>
                          </td>
                          <td data-label="Launchpad">{t.launchpad_slug || '—'}</td>
                          <td className="num" data-label="Peak">{usd(t.peak_market_cap)}</td>
                          <td data-label="Outcome">
                            {t.is_qualified
                              ? <Badge status="verified" variant="outline">Qualified</Badge>
                              : <span className="faint" style={{ fontSize: 'var(--text-xs)' }}>Below threshold</span>}
                          </td>
                          <td data-label="First seen" className="faint">{relative(t.first_seen)}</td>
                        </ClickableRow>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Panel>

            <div className="stack gap-5">
              {/* Annie's own writing about this wallet. Deliberately above the
                  raw movement log — the numbers are already in the stats and
                  the table; the reason this wallet is interesting is here. */}
              <Panel
                title="Dossier"
                meta={c.dossier ? c.dossier.path : 'written once a wallet is tracked'}
              >
                {c.dossier ? (
                  <Markdown text={c.dossier.body} />
                ) : (
                  <Empty
                    title="No dossier yet"
                    body="Annie writes one once a wallet earns tracking — a high volume of launches, or a token that reached a tier."
                  />
                )}
              </Panel>

              <Panel title="Movements" meta={`${count(c.movements?.length)} recorded`}>
                {!c.movements?.length ? (
                  <Empty title="Nothing recorded" />
                ) : (
                  <div className="stack gap-2">
                    {c.movements.slice(0, 40).map((m, i) => (
                      <div
                        key={i}
                        className="row between gap-3"
                        style={{ fontSize: 'var(--text-xs)' }}
                      >
                        <span className="row gap-2" style={{ minWidth: 0 }}>
                          <Badge status={m.kind === 'launch' ? 'stable' : 'rising'} variant="plain">
                            {m.kind}
                          </Badge>
                          {m.mint && <CopyableAddress value={m.mint} className="mono faint" />}
                        </span>
                        <span className="mono faint">{relative(m.at)}</span>
                      </div>
                    ))}
                    {c.movements.length > 40 && (
                      <span className="faint" style={{ fontSize: 'var(--text-2xs)' }}>
                        +{c.movements.length - 40} older, kept but not shown
                      </span>
                    )}
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
