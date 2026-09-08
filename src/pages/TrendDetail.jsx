import { Link, useParams } from 'react-router-dom'

import { api } from '../api/client.js'
import { useApi } from '../api/useApi.js'
import {
  Async,
  Badge,
  CopyableAddress,
  Delta,
  Empty,
  Maturity,
  Panel,
  Sample,
  Sparkline,
  Stat,
} from '../components/primitives.jsx'
import { count, date, percent, relative, tierLabel, usd } from '../lib/format.js'

/**
 * One signal — a characteristic's frequency among tokens that cleared a tier,
 * against its own baseline.
 *
 * This page previously rendered five panels for fields the API stopped
 * returning when trends became signals: `observations`, `history`,
 * `example_tokens`, `caveats`, `description`. Each showed a permanent "No X
 * recorded" — an empty state promising data that could never arrive, which is
 * worse than not having the panel, because it reads as "nothing happened yet"
 * rather than "this was never wired up".
 *
 * What is here now is what the signal actually has: the daily series, the
 * comparison and its statistics, the tokens the characteristic was found in,
 * and anything Annie has written about it. The last of those is usually the
 * most useful thing on the page — the numbers say a theme is 3x baseline, but
 * her note says whether she thinks it is real.
 */
export default function TrendDetail() {
  const { slug } = useParams()
  const state = useApi(() => api.signal(slug), [slug])

  return (
    <Async state={state} rows={6}>
      {(s) => {
        const series = (s.series || []).map((p) => p.freq).filter((v) => v !== null).reverse()
        return (
          <>
            <div className="page-head">
              <Link to="/signals" className="faint" style={{ fontSize: 'var(--text-xs)' }}>
                ← Signals
              </Link>
              <div className="row gap-3 wrap">
                <h2 className="page-head__title">{s.name}</h2>
                <Badge status={s.status} dot />
                <Maturity value={s.maturity} />
              </div>
              <p className="page-head__sub">
                How often <code className="mono">{s.subject_namespace}.{s.subject_key}</code>
                {s.subject_value ? (
                  <>
                    {' = '}
                    <code className="mono">{s.subject_value}</code>
                  </>
                ) : null}{' '}
                appears among tokens that reached{' '}
                {s.cohort_threshold_usd ? `${tierLabel(s.cohort_threshold_usd)}+` : 'a tier'},
                against its own 90-day baseline.
              </p>
            </div>

            {s.thin_sample && (
              <Panel title="Too thin to mean anything yet">
                This cohort has {count(s.recent?.total)} tokens in it. Below about 20, a
                frequency is not distinguishable from chance — the number is recorded so
                it can be watched, not so it can be cited. Treat anything here as an
                observation, never a finding.
              </Panel>
            )}

            <div className="grid grid--stats">
              <Stat
                label="Recent"
                value={<Sample sample={s.recent} />}
                foot={<span className="muted">last {s.recent_window_days ?? '—'}d</span>}
              />
              <Stat
                label="Baseline"
                value={
                  s.baseline?.frequency !== null && s.baseline?.frequency !== undefined
                    ? percent(s.baseline.frequency)
                    : null
                }
                unknown="No baseline"
                foot={<span className="muted">prior {s.baseline_window_days ?? '—'}d</span>}
              />
              <Stat
                label="Change"
                value={<Delta value={s.change} />}
                foot={
                  s.lift !== null && s.lift !== undefined ? (
                    <span className="muted">{s.lift.toFixed(2)}x baseline</span>
                  ) : (
                    <span className="muted">nothing to compare against</span>
                  )
                }
              />
              <Stat
                label="Persistence"
                value={s.persistence_days ? `${s.persistence_days}d` : null}
                unknown="First window"
                foot={<span className="muted">consecutive days present</span>}
              />
            </div>

            <div className="detail">
              <div className="stack gap-5">
                <Panel
                  title="Frequency over time"
                  meta={`${count(s.series?.length)} daily observation(s)`}
                >
                  {series.length < 2 ? (
                    <Empty
                      title="Not enough history to plot"
                      body="A signal gets one point per cycle it is observed in. Two days of running is enough for a line."
                    />
                  ) : (
                    <>
                      <Sparkline
                        values={series}
                        baseline={s.baseline?.frequency}
                        status={s.status}
                        height={64}
                      />
                      <div className="row between faint" style={{ fontSize: 'var(--text-2xs)' }}>
                        <span>{date(s.series.at(-1)?.day)}</span>
                        <span>
                          dashed line = baseline {percent(s.baseline?.frequency)}
                        </span>
                        <span>{date(s.series[0]?.day)}</span>
                      </div>
                    </>
                  )}
                </Panel>

                <Panel
                  title="Tokens with this characteristic"
                  meta="from the recent window"
                  flush={s.example_tokens?.length > 0}
                >
                  {!s.example_tokens?.length ? (
                    <Empty
                      title="No qualifying tokens carry it yet"
                      body="The frequency above is computed over the cohort; once a token in it clears a tier with this characteristic, it appears here."
                    />
                  ) : (
                    <div className="table-wrap">
                      <table className="table table--responsive">
                        <thead>
                          <tr>
                            <th>Token</th>
                            <th className="num">Peak</th>
                            <th>Launchpad</th>
                            <th>Qualified</th>
                          </tr>
                        </thead>
                        <tbody>
                          {s.example_tokens.map((t) => (
                            <tr key={t.mint}>
                              <td className="primary" data-label="Token">
                                <Link to={`/tokens/${t.mint}`} className="row gap-2">
                                  <strong>{t.symbol || t.name || t.mint.slice(0, 8)}</strong>
                                  <CopyableAddress value={t.mint} className="mono faint" />
                                </Link>
                              </td>
                              <td className="num" data-label="Peak">
                                {usd(t.peak_market_cap)}
                              </td>
                              <td data-label="Launchpad">{t.launchpad || '—'}</td>
                              <td data-label="Qualified" className="faint">
                                {t.qualified_at ? relative(t.qualified_at) : '—'}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </Panel>
              </div>

              <div className="stack gap-5">
                {/* Deliberately above the statistics. The numbers say a theme
                    is 3x baseline; her note says whether she thinks it is real. */}
                <Panel title="What Annie has written about this">
                  {!s.related_memories?.length ? (
                    <Empty
                      title="Nothing written yet"
                      body="She writes about a characteristic once it has survived more than one window. A number on its own is not something she has an opinion about."
                    />
                  ) : (
                    <div className="stack" style={{ gap: 10 }}>
                      {s.related_memories.map((h) => (
                        <Link key={h.path} to="/memory" className="stack" style={{ gap: 2 }}>
                          <span className="row gap-2">
                            <strong style={{ fontSize: 'var(--text-sm)' }}>{h.title}</strong>
                            <Badge>{h.section}</Badge>
                          </span>
                          <span className="faint" style={{ fontSize: 'var(--text-2xs)' }}>
                            {h.snippet}
                          </span>
                        </Link>
                      ))}
                    </div>
                  )}
                </Panel>

                <Panel title="The statistics">
                  <dl className="deflist">
                    <dt>Cohort size</dt>
                    <dd>{count(s.recent?.total)}</dd>
                    <dt>Occurrences</dt>
                    <dd>{count(s.recent?.count)}</dd>
                    <dt>p-value</dt>
                    <dd>
                      {s.p_value === null || s.p_value === undefined
                        ? 'Not computed'
                        : s.p_value.toFixed(4)}
                    </dd>
                    <dt>Lift</dt>
                    <dd>
                      {s.lift === null || s.lift === undefined
                        ? 'No baseline'
                        : `${s.lift.toFixed(2)}x`}
                    </dd>
                    <dt>Confidence</dt>
                    <dd>{s.confidence || '—'}</dd>
                    <dt>First detected</dt>
                    <dd>{s.first_detected_at ? relative(s.first_detected_at) : '—'}</dd>
                    <dt>Last observed</dt>
                    <dd>{s.last_observed_at ? relative(s.last_observed_at) : '—'}</dd>
                  </dl>
                  <p
                    className="faint"
                    style={{ fontSize: 'var(--text-2xs)', marginTop: 'var(--space-3)' }}
                  >
                    A p-value is only computed once the cohort clears the minimum sample.
                    Below that it is left out rather than reported as a weak result — a
                    p-value on n=6 is not a weak signal, it is a meaningless one.
                  </p>
                </Panel>
              </div>
            </div>
          </>
        )
      }}
    </Async>
  )
}
