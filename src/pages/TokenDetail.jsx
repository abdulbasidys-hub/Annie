import { Link, useParams } from 'react-router-dom'

import { api } from '../api/client.js'
import { useApi } from '../api/useApi.js'
import {
  Async,
  Badge,
  CopyableAddress,
  Empty,
  Markdown,
  Panel,
  Stat,
} from '../components/primitives.jsx'
import { count, dateTime, relative, usd } from '../lib/format.js'

/**
 * One token.
 *
 * Two sources, and the order on the page reflects which one matters. The
 * ledger row (top) is what was measured: peaks, tiers, how many times it was
 * checked. The memory file (below) is what Annie made of it — and only exists
 * for tokens that did something, because writing a file for every launch is
 * exactly the "too much data" this system was rebuilt to stop being.
 *
 * A token with no memory file is not an error state. It means she saw it,
 * priced it, and did not think it warranted writing down. That is the system
 * working, and the page says so rather than showing an empty panel.
 */
export default function TokenDetail() {
  const { mint } = useParams()
  const state = useApi(() => api.token(mint), [mint])

  return (
    <Async state={state} rows={6}>
      {(t) => (
        <>
          <div className="page-head">
            <Link to="/tokens" className="faint" style={{ fontSize: 'var(--text-xs)' }}>
              ← Tokens
            </Link>
            <div className="row gap-3 wrap">
              <h2 className="page-head__title">{t.symbol || t.name || 'Unnamed'}</h2>
              {t.is_qualified ? (
                <Badge status="verified" variant="outline">
                  Cleared {usd(t.peak_tier)}
                </Badge>
              ) : (
                <Badge status={t.status} variant="plain">
                  {t.status || 'watching'}
                </Badge>
              )}
              {t.round_tripped && <Badge status="declining" variant="outline">Round-tripped</Badge>}
            </div>
            {t.name && t.symbol && <span className="secondary">{t.name}</span>}
            <p className="page-head__sub row gap-2 wrap">
              <CopyableAddress value={t.mint} full className="mono" />
            </p>
          </div>

          <div className="grid grid--stats">
            <Stat
              label="Peak market cap"
              value={usd(t.peak_market_cap)}
              foot={<span className="muted">highest ever observed</span>}
            />
            <Stat
              label="Latest"
              value={usd(t.market_cap)}
              foot={<span className="muted">{relative(t.first_seen)} old</span>}
            />
            <Stat
              label="Tier cleared"
              value={t.peak_tier ? usd(t.peak_tier) : null}
              unknown="None"
              foot={<span className="muted">{relative(t.qualified_at)}</span>}
            />
            <Stat
              label="Times checked"
              value={count(t.checks)}
              foot={<span className="muted">batched price lookups</span>}
            />
          </div>

          <div className="detail">
            <div className="stack gap-5">
              <Panel
                title="What Annie wrote"
                meta={t.memory ? t.memory.path : 'only written for tokens that moved'}
              >
                {t.memory ? (
                  <Markdown text={t.memory.body} />
                ) : (
                  <Empty
                    title="Nothing written about this one"
                    body="She saw it and priced it, but it did not do enough to be worth remembering. Most launches end here — that is the filtering working, not data missing."
                  />
                )}
              </Panel>

              {t.mentioned_in?.length > 0 && (
                <Panel title="Mentioned elsewhere in memory" flush>
                  <div className="stack">
                    {t.mentioned_in.map((hit) => (
                      <div key={hit.path} className="listrow" style={{ cursor: 'default' }}>
                        <div className="stack" style={{ gap: 2 }}>
                          <span className="row gap-2">
                            <strong>{hit.title}</strong>
                            <Badge>{hit.section}</Badge>
                          </span>
                          <span className="faint mono" style={{ fontSize: 'var(--text-2xs)' }}>
                            {hit.path}
                          </span>
                          <span className="faint">{hit.snippet}</span>
                        </div>
                      </div>
                    ))}
                  </div>
                </Panel>
              )}
            </div>

            <div className="stack gap-5">
              <Panel title="Origin">
                <dl className="deflist">
                  <dt>Launchpad</dt>
                  <dd>
                    {t.launchpad_slug ? (
                      <Link to={`/launchpads/${t.launchpad_slug}`}>{t.launchpad_slug}</Link>
                    ) : (
                      '—'
                    )}
                  </dd>
                  <dt>Creator</dt>
                  <dd>
                    {t.creator_wallet ? (
                      <Link to={`/creators/${t.creator_wallet}`}>
                        <CopyableAddress value={t.creator_wallet} head={6} tail={6} />
                      </Link>
                    ) : (
                      '—'
                    )}
                  </dd>
                  <dt>First seen</dt>
                  <dd>{dateTime(t.first_seen)}</dd>
                  <dt>Qualified</dt>
                  <dd>{t.qualified_at ? dateTime(t.qualified_at) : '—'}</dd>
                  <dt>Themes</dt>
                  <dd>
                    {t.themes?.length ? (
                      <span className="row gap-1 wrap">
                        {t.themes.map((theme) => (
                          <Badge key={theme} status="stable" variant="plain">
                            {theme}
                          </Badge>
                        ))}
                      </span>
                    ) : (
                      '—'
                    )}
                  </dd>
                </dl>
                <p
                  className="faint"
                  style={{ fontSize: 'var(--text-2xs)', marginTop: 'var(--space-3)' }}
                >
                  Themes are derived from the name and ticker on read, never stored. Storing them
                  per token is what the old feature subcollections did, at a document each.
                </p>
              </Panel>

              {t.creator && (
                <Panel title="Creator's record">
                  <dl className="deflist">
                    <dt>Launches</dt>
                    <dd>{count(t.creator.launches)}</dd>
                    <dt>Winners</dt>
                    <dd>{count(t.creator.winners)}</dd>
                    <dt>Best result</dt>
                    <dd>{usd(t.creator.best_market_cap)}</dd>
                    <dt>Tracked</dt>
                    <dd>{t.creator.tracked ? 'Yes' : 'Not yet'}</dd>
                  </dl>
                </Panel>
              )}
            </div>
          </div>
        </>
      )}
    </Async>
  )
}
