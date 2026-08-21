import { Link, useParams } from 'react-router-dom'

import { api } from '../api/client.js'
import { useApi } from '../api/useApi.js'
import {
  Async,
  Badge,
  Caveats,
  Delta,
  Empty,
  Maturity,
  Panel,
  Sample,
  Sparkline,
  Stat,
} from '../components/primitives.jsx'
import { count, date, dateTime, humanise, multiple, percent, relative, tierLabel, usd } from '../lib/format.js'

/**
 * One trend, with everything needed to judge whether to believe it.
 *
 * Layout puts the statistical support *above* the pretty chart. The chart is
 * persuasive and the p-value is not, which is precisely why the p-value goes
 * first — a reader who scrolls no further should still have seen the sample
 * size and the caveats.
 */
export default function TrendDetail() {
  const { slug } = useParams()
  const state = useApi(() => api.trend(slug), [slug])

  return (
    <Async state={state} rows={8}>
      {(trend) => {
        const series = (trend.observations || []).map((o) => o.frequency)
        return (
          <>
            <div className="page-head">
              <div className="row gap-2 wrap">
                <Link to="/trends" className="faint" style={{ fontSize: 'var(--text-xs)' }}>
                  ← Trends
                </Link>
              </div>
              <div className="row gap-3 wrap">
                <h2 className="page-head__title">{trend.name}</h2>
                <Badge status={trend.status} dot />
                <Maturity value={trend.maturity} />
              </div>
              <p className="page-head__sub">{trend.description}</p>
            </div>

            <Caveats items={trend.caveats} />

            <div className="grid grid--stats">
              <Stat
                label="Recent frequency"
                value={<Sample sample={trend.recent} />}
                foot={<span className="muted">last {trend.recent_window_days ?? '—'}d</span>}
              />
              <Stat
                label="Baseline"
                value={<Sample sample={trend.baseline} />}
                foot={<span className="muted">prior period</span>}
              />
              <Stat
                label="Change"
                value={<Delta value={trend.change} />}
                foot={
                  trend.lift !== null && trend.lift !== undefined ? (
                    <span className="muted">{multiple(trend.lift)} baseline</span>
                  ) : null
                }
              />
              <Stat
                label="Cohort"
                value={trend.cohort_threshold_usd ? `${tierLabel(trend.cohort_threshold_usd)}+` : null}
                foot={<span className="muted">measured over this tier only</span>}
              />
            </div>

            <div className="detail">
              <div className="stack gap-5">
                <Panel
                  title="Frequency history"
                  meta={`${count(trend.observations?.length)} observations`}
                >
                  {series.length < 2 ? (
                    <Empty
                      title="Not enough history"
                      body="A direction cannot be read from fewer than two observations. This trend was detected recently."
                    />
                  ) : (
                    <div className="stack gap-3">
                      <div style={{ height: 120 }}>
                        <Sparkline
                          values={series}
                          baseline={trend.baseline?.frequency}
                          status={trend.status}
                          height={120}
                        />
                      </div>
                      <div className="row between faint" style={{ fontSize: 'var(--text-2xs)' }}>
                        <span>{date(trend.observations[0]?.observed_on)}</span>
                        <span>dashed line = baseline {percent(trend.baseline?.frequency)}</span>
                        <span>{date(trend.observations.at(-1)?.observed_on)}</span>
                      </div>
                    </div>
                  )}
                </Panel>

                <Panel title="Status history" flush>
                  {!trend.history?.length ? (
                    <Empty title="No transitions" body="This trend has not changed status since detection." />
                  ) : (
                    <div style={{ padding: 'var(--space-4)' }}>
                      <div className="timeline">
                        {trend.history.map((h, i) => (
                          <div className="timeline__item" key={i}>
                            <span className="timeline__dot" data-status={h.to_status} />
                            <div className="timeline__body">
                              <span className="timeline__title">
                                {h.from_status ? `${humanise(h.from_status)} → ` : ''}
                                {humanise(h.to_status)}
                              </span>
                              {h.reason && (
                                <span className="secondary" style={{ fontSize: 'var(--text-sm)' }}>
                                  {h.reason}
                                </span>
                              )}
                              <span className="timeline__meta">{dateTime(h.changed_at)}</span>
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                </Panel>

                <Panel title="Example tokens" meta="from the recent window" flush>
                  {!trend.example_tokens?.length ? (
                    <Empty title="No examples recorded" />
                  ) : (
                    <div className="table-wrap">
                      <table className="table table--responsive">
                        <thead>
                          <tr>
                            <th>Token</th>
                            <th>Launchpad</th>
                            <th className="num">Peak</th>
                            <th>Qualified</th>
                          </tr>
                        </thead>
                        <tbody>
                          {trend.example_tokens.map((t) => (
                            <tr key={t.mint}>
                              <td className="primary" data-label="Token">
                                <Link to={`/tokens/${t.mint}`}>
                                  <strong>{t.symbol || t.name || 'Unnamed'}</strong>{' '}
                                  <span className="mono faint">{t.mint.slice(0, 4)}…</span>
                                </Link>
                              </td>
                              <td data-label="Launchpad">{t.launchpad_slug || '—'}</td>
                              <td className="num" data-label="Peak">{usd(t.peak_market_cap)}</td>
                              <td data-label="Qualified" className="faint">{relative(t.qualified_at)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </Panel>
              </div>

              <div className="stack gap-5">
                <Panel title="Statistical support">
                  <dl className="deflist">
                    <dt>Sample size</dt>
                    <dd>{count(trend.recent?.total)}</dd>
                    <dt>p-value</dt>
                    <dd>
                      {trend.p_value === null || trend.p_value === undefined
                        ? 'not computed'
                        : trend.p_value.toFixed(4)}
                    </dd>
                    <dt>Effect size</dt>
                    <dd>
                      {trend.effect_size === null || trend.effect_size === undefined
                        ? '—'
                        : trend.effect_size.toFixed(3)}
                    </dd>
                    <dt>95% interval</dt>
                    <dd>
                      {trend.ci_low !== null && trend.ci_low !== undefined
                        ? `${percent(trend.ci_low)} – ${percent(trend.ci_high)}`
                        : '—'}
                    </dd>
                    <dt>Persistence</dt>
                    <dd>{count(trend.persistence_days)} days</dd>
                    <dt>Peak frequency</dt>
                    <dd>{percent(trend.peak_frequency)}</dd>
                  </dl>
                  {(trend.p_value === null || trend.p_value === undefined) && (
                    <p className="faint" style={{ fontSize: 'var(--text-xs)', marginTop: 'var(--space-3)' }}>
                      No significance test was run — the sample did not meet the minimum. This is
                      an observation, not a finding.
                    </p>
                  )}
                </Panel>

                <Panel title="Memory">
                  <dl className="deflist">
                    <dt>First detected</dt>
                    <dd>{date(trend.first_detected_at)}</dd>
                    <dt>Last observed</dt>
                    <dd>{relative(trend.last_observed_at)}</dd>
                    <dt>Revivals</dt>
                    <dd>{count(trend.revival_count)}</dd>
                    <dt>Category</dt>
                    <dd>{humanise(trend.category)}</dd>
                  </dl>
                  {trend.revival_count > 0 && (
                    <p className="secondary" style={{ fontSize: 'var(--text-xs)', marginTop: 'var(--space-3)' }}>
                      This pattern has returned after being declared dead {trend.revival_count} time
                      {trend.revival_count === 1 ? '' : 's'}.
                    </p>
                  )}
                </Panel>
              </div>
            </div>
          </>
        )
      }}
    </Async>
  )
}
