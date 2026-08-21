import { Link, useParams } from 'react-router-dom'

import { api } from '../api/client.js'
import { useApi } from '../api/useApi.js'
import {
  Async,
  Badge,
  Empty,
  EvidenceRows,
  Panel,
  Stat,
} from '../components/primitives.jsx'
import { address, count, dateTime, humanise, minutes, relative, usd } from '../lib/format.js'

/** One token's research page (§57). */
export default function TokenDetail() {
  const { mint } = useParams()
  const state = useApi(() => api.token(mint), [mint])

  return (
    <Async state={state} rows={8}>
      {(t) => (
        <>
          <div className="page-head">
            <Link to="/tokens" className="faint" style={{ fontSize: 'var(--text-xs)' }}>← Tokens</Link>
            <div className="row gap-4 wrap">
              {t.image_url && (
                <img
                  src={t.image_url}
                  alt=""
                  width={48}
                  height={48}
                  style={{ borderRadius: 'var(--radius)', objectFit: 'cover' }}
                  onError={(e) => { e.currentTarget.style.display = 'none' }}
                />
              )}
              <div className="stack gap-1">
                <div className="row gap-3 wrap">
                  <h2 className="page-head__title">{t.symbol || 'Unnamed'}</h2>
                  <Badge status={t.verification_status} />
                  {t.is_qualified && <Badge status="verified" variant="outline">Qualified</Badge>}
                </div>
                <span className="secondary">{t.name}</span>
                <span className="mono faint" style={{ fontSize: 'var(--text-xs)' }}>{t.mint}</span>
              </div>
            </div>
            {t.description && <p className="page-head__sub">{t.description}</p>}
          </div>

          <div className="grid grid--stats">
            <Stat label="At qualification" value={usd(t.qualified_market_cap)} foot={<span className="muted">{relative(t.qualified_at)}</span>} />
            <Stat label="Peak market cap" value={usd(t.peak_market_cap)} />
            <Stat label="Latest" value={usd(t.latest_market_cap)} foot={<span className="muted">{relative(t.market_data_at)}</span>} />
            <Stat label="Liquidity" value={usd(t.latest_liquidity_usd)} />
          </div>

          <div className="detail">
            <div className="stack gap-5">
              <Panel title="Milestones" meta="§7" flush>
                {!t.milestones?.length ? (
                  <Empty title="No milestones recorded" body="Milestones are written during enrichment." />
                ) : (
                  <div style={{ padding: 'var(--space-4)' }}>
                    <div className="timeline">
                      {t.milestones.map((m, i) => (
                        <div className="timeline__item" key={i}>
                          <span className="timeline__dot" data-status={m.evidence?.verification_status || 'pending'} />
                          <div className="timeline__body">
                            <span className="timeline__title">
                              {m.kind === 'market_cap' ? `Reached ${usd(m.threshold_usd)}` : humanise(m.kind)}
                            </span>
                            <span className="timeline__meta">
                              {dateTime(m.reached_at)}
                              {m.token_age_minutes !== null && ` · ${minutes(m.token_age_minutes)} old`}
                              {m.market_cap && ` · ${usd(m.market_cap)}`}
                              {m.liquidity_usd && ` · liq ${usd(m.liquidity_usd)}`}
                            </span>
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </Panel>

              <Panel title="Characteristics" meta={`${count(t.features?.length)} extracted`}>
                {!t.features?.length ? (
                  <Empty title="Not yet analysed" />
                ) : (
                  <div className="stack gap-4">
                    {['token', 'name', 'ticker', 'description', 'image'].map((ns) => {
                      const items = (t.features || []).filter((f) => f.namespace === ns && f.value)
                      if (!items.length) return null
                      return (
                        <div key={ns} className="stack gap-2">
                          <span className="faint" style={{ fontSize: 'var(--text-2xs)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
                            {ns}
                          </span>
                          <div className="row gap-1 wrap">
                            {items.slice(0, 24).map((f, i) => (
                              <Badge
                                key={i}
                                status={f.source === 'llm' ? 'pending' : 'stable'}
                                variant="plain"
                                title={f.source === 'llm' ? 'Model-generated label' : 'Deterministic extraction'}
                              >
                                {f.key === 'theme' || f.key === 'word' ? f.value : `${f.key}: ${f.value}`}
                              </Badge>
                            ))}
                          </div>
                        </div>
                      )
                    })}
                  </div>
                )}
              </Panel>

              {t.image_features && (
                <Panel title="Image analysis" meta={t.image_features.model || undefined}>
                  {t.image_features.failure_reason ? (
                    <Empty title="Analysis failed" body={t.image_features.failure_reason} />
                  ) : (
                    <div className="stack gap-3">
                      <div className="row gap-1 wrap">
                        {(t.image_features.categories || []).map((c) => (
                          <Badge key={c} status="stable" variant="outline">{c}</Badge>
                        ))}
                      </div>
                      <dl className="deflist">
                        <dt>Subjects</dt><dd>{t.image_features.subjects?.join(', ') || '—'}</dd>
                        <dt>Style</dt><dd>{t.image_features.style || '—'}</dd>
                        <dt>Contains text</dt><dd>{yesNo(t.image_features.has_text)}</dd>
                        <dt>AI-generated look</dt><dd>{yesNo(t.image_features.is_ai_generated_style)}</dd>
                        <dt>Existing meme</dt><dd>{yesNo(t.image_features.references_existing_meme)}</dd>
                      </dl>
                      <p className="faint" style={{ fontSize: 'var(--text-2xs)' }}>
                        Model-generated labels. Trends built on these are marked as resting on
                        model judgement rather than deterministic extraction.
                      </p>
                    </div>
                  )}
                </Panel>
              )}
            </div>

            <div className="stack gap-5">
              <Panel title="Origin">
                <dl className="deflist">
                  <dt>Launchpad</dt>
                  <dd>{t.launchpad_slug ? <Link to={`/launchpads/${t.launchpad_slug}`}>{t.launchpad_slug}</Link> : '—'}</dd>
                  <dt>Creator</dt>
                  <dd>
                    {t.creator_wallet
                      ? <Link to={`/creators/${t.creator_wallet}`}>{address(t.creator_wallet, { head: 6, tail: 6 })}</Link>
                      : '—'}
                  </dd>
                  <dt>Launched</dt><dd>{dateTime(t.launched_at)}</dd>
                  <dt>Migrated</dt><dd>{dateTime(t.migrated_at)}</dd>
                  <dt>Destination</dt><dd>{t.destination_dex_slug || '—'}</dd>
                  <dt>Time to migrate</dt><dd>{minutes(t.minutes_launch_to_migration)}</dd>
                  <dt>Holders</dt><dd>{count(t.latest_holder_count)}</dd>
                  <dt>Pipeline stage</dt><dd>{humanise(t.pipeline_stage)}</dd>
                </dl>
              </Panel>

              <Panel title="Qualification evidence" meta="§4">
                <EvidenceRows data={t.qualification_evidence} />
              </Panel>

              {(t.website || t.twitter || t.telegram) && (
                <Panel title="Links">
                  <div className="stack gap-2">
                    {t.website && <a href={t.website} target="_blank" rel="noreferrer noopener" className="truncate">{t.website}</a>}
                    {t.twitter && <a href={t.twitter} target="_blank" rel="noreferrer noopener" className="truncate">{t.twitter}</a>}
                    {t.telegram && <a href={t.telegram} target="_blank" rel="noreferrer noopener" className="truncate">{t.telegram}</a>}
                  </div>
                </Panel>
              )}

              {t.related_trends?.length > 0 && (
                <Panel title="Related trends" flush>
                  <div className="stack">
                    {t.related_trends.map((tr) => (
                      <Link
                        key={tr.id}
                        to={`/trends/${tr.slug}`}
                        className="row gap-2 between"
                        style={{ padding: 'var(--space-3) var(--space-4)', borderBottom: '1px solid var(--border-subtle)' }}
                      >
                        <span className="truncate">{tr.name}</span>
                        <Badge status={tr.status} dot />
                      </Link>
                    ))}
                  </div>
                </Panel>
              )}

              <Panel title="Data sources">
                {!t.data_sources?.length ? (
                  <span className="faint" style={{ fontSize: 'var(--text-xs)' }}>None recorded</span>
                ) : (
                  <div className="row gap-1 wrap">
                    {t.data_sources.map((s) => <Badge key={s} status="stable" variant="plain">{s}</Badge>)}
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

function yesNo(value) {
  if (value === null || value === undefined) return '—'
  return value ? 'Yes' : 'No'
}
