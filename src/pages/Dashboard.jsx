import { useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../api/client.js'
import { useApi } from '../api/useApi.js'
import {
  Async,
  Badge,
  Delta,
  Empty,
  Maturity,
  Panel,
  Sample,
  Sparkline,
  Stat,
} from '../components/primitives.jsx'
import { count, dateTime, humanise, relative, tierLabel, usd } from '../lib/format.js'

const WINDOWS = [1, 7, 14, 30, 90]

/**
 * Dashboard (§56).
 *
 * Ordered by what should change a decision, not by what is easiest to count.
 *
 * 1. Degraded capabilities, if any — every number below is suspect while a
 *    provider is down, so this cannot sit at the bottom of the page.
 * 2. Success counts by tier, with the previous period beside them. A count
 *    without its comparison invites the reader to supply their own baseline.
 * 3. What is moving: rising, new, declining.
 * 4. What needs a person: anomalies and pending research.
 */
export default function Dashboard() {
  const [windowDays, setWindowDays] = useState(7)
  const state = useApi(() => api.dashboard(windowDays), [windowDays])

  return (
    <>
      <div className="page-head">
        <div className="row between wrap gap-3">
          <h2 className="page-head__title">What changed</h2>
          <div className="segmented" role="group" aria-label="Comparison window">
            {WINDOWS.map((days) => (
              <button
                key={days}
                className={days === windowDays ? 'is-active' : ''}
                onClick={() => setWindowDays(days)}
              >
                {days}d
              </button>
            ))}
          </div>
        </div>
        <p className="page-head__sub">
          Successful tokens and the characteristics moving among them, over the last{' '}
          {windowDays} day{windowDays === 1 ? '' : 's'} against the preceding period.
        </p>
      </div>

      <Async state={state} rows={6}>
        {(data) => (
          <>
            {data.degraded_capabilities?.length > 0 && (
              <div className="error-panel">
                <span className="error-panel__title">
                  {data.degraded_capabilities.length} capabilit
                  {data.degraded_capabilities.length === 1 ? 'y is' : 'ies are'} unavailable
                </span>
                <p className="error-panel__detail">
                  Figures below may be incomplete for the affected sources.
                </p>
                <div className="stack gap-1">
                  {data.degraded_capabilities.map((cap) => (
                    <span key={cap.key} className="row gap-2" style={{ fontSize: 'var(--text-xs)' }}>
                      <Badge status={cap.status}>{cap.status}</Badge>
                      <span>{cap.label}</span>
                      {cap.missing_env_vars.length > 0 && (
                        <code className="mono faint">{cap.missing_env_vars.join(', ')}</code>
                      )}
                    </span>
                  ))}
                </div>
                <Link to="/health" className="btn btn--sm" style={{ alignSelf: 'flex-start' }}>
                  System health
                </Link>
              </div>
            )}

            <TierCounts
              counts={data.counts_by_tier}
              previous={data.counts_by_tier_previous}
              windowDays={windowDays}
            />

            <div className="grid grid--halves">
              <TrendList
                title="Rising"
                meta={`${count(data.trends_rising)} total`}
                trends={data.rising_trends}
                emptyBody="Nothing is climbing meaningfully against its baseline in this window."
              />
              <TrendList
                title="Newly detected"
                meta={`${count(data.trends_new)} total`}
                trends={data.new_trends}
                emptyBody="No characteristics crossed the detection threshold in this window."
              />
            </div>

            <div className="grid grid--halves">
              <TrendList
                title="Declining"
                meta={`${count(data.trends_declining)} total`}
                trends={data.declining_trends}
                emptyBody="Nothing established is fading in this window."
              />
              <EmergingLaunchpads launchpads={data.emerging_launchpads} />
            </div>

            <div className="grid grid--halves">
              <Anomalies items={data.open_anomalies} />
              <PendingResearch items={data.pending_tasks} />
            </div>

            <RecentFindings notes={data.recent_notes} />

            <p className="faint" style={{ fontSize: 'var(--text-xs)' }}>
              Ingestion last ran {relative(data.last_ingestion_at)}. Trend engine last ran{' '}
              {relative(data.last_trend_run_at)}.
            </p>
          </>
        )}
      </Async>
    </>
  )
}

/* -------------------------------------------------------------------------- */

function TierCounts({ counts, previous, windowDays }) {
  const tiers = ['100000', '250000', '500000', '1000000']
  return (
    <div className="grid grid--stats">
      {tiers.map((tier) => {
        const now = counts?.[tier]
        const before = previous?.[tier]
        // A delta needs both sides. Showing "+3" when the prior period is
        // unknown would invent a comparison.
        const delta =
          now !== undefined && before !== undefined && before !== null ? now - before : null
        return (
          <Stat
            key={tier}
            label={`${tierLabel(tier)}+`}
            value={now === undefined || now === null ? null : count(now)}
            unknown="Not measured"
            foot={
              delta === null ? (
                <span className="faint">no prior period</span>
              ) : (
                <>
                  <Delta value={delta} format={(v) => `${v > 0 ? '+' : ''}${v}`} />
                  <span className="muted">vs previous {windowDays}d</span>
                </>
              )
            }
          />
        )
      })}
    </div>
  )
}

function TrendList({ title, meta, trends, emptyBody }) {
  return (
    <Panel title={title} meta={meta} flush>
      {!trends || trends.length === 0 ? (
        <Empty title="Nothing to report" body={emptyBody} />
      ) : (
        <div className="stack">
          {trends.map((trend) => (
            <TrendRow key={trend.id} trend={trend} />
          ))}
        </div>
      )}
    </Panel>
  )
}

function TrendRow({ trend }) {
  return (
    <Link
      to={`/trends/${trend.slug}`}
      className="row gap-3"
      style={{
        padding: 'var(--space-3) var(--space-4)',
        borderBottom: '1px solid var(--border-subtle)',
        alignItems: 'flex-start',
      }}
    >
      <div className="grow stack gap-1">
        <div className="row gap-2 wrap">
          <span style={{ fontWeight: 'var(--weight-medium)' }}>{trend.name}</span>
          <Badge status={trend.status} dot />
        </div>
        <div className="row gap-3 wrap">
          <Sample sample={trend.recent} />
          <Delta value={trend.change} />
          <span className="faint" style={{ fontSize: 'var(--text-2xs)' }}>
            from {trend.baseline?.frequency !== null ? `${(trend.baseline.frequency * 100).toFixed(1)}%` : '—'} baseline
          </span>
        </div>
        <Maturity value={trend.maturity} />
      </div>
      <div style={{ width: 84, flexShrink: 0 }}>
        <Sparkline
          values={trend.recent_series}
          baseline={trend.baseline?.frequency}
          status={trend.status}
        />
      </div>
    </Link>
  )
}

function EmergingLaunchpads({ launchpads }) {
  return (
    <Panel title="Emerging launchpads" flush>
      {!launchpads || launchpads.length === 0 ? (
        <Empty
          title="No new platforms"
          body="No launch platform outside the known set gained meaningful share in this window."
        />
      ) : (
        <div className="table-wrap">
          <table className="table table--responsive">
            <thead>
              <tr>
                <th>Launchpad</th>
                <th className="num">Launches</th>
                <th className="num">Qualified</th>
                <th className="num">Share</th>
              </tr>
            </thead>
            <tbody>
              {launchpads.map((lp) => (
                <tr key={lp.id}>
                  <td className="primary" data-label="Launchpad">
                    <Link to={`/launchpads/${lp.slug}`} className="row gap-2">
                      <span>{lp.name}</span>
                      {!lp.is_known && <Badge status="new">Unknown</Badge>}
                    </Link>
                  </td>
                  <td className="num" data-label="Launches">{count(lp.launch_count)}</td>
                  <td className="num" data-label="Qualified">{count(lp.qualified_count)}</td>
                  <td className="num" data-label="Share">
                    {lp.market_share !== null ? `${(lp.market_share * 100).toFixed(1)}%` : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  )
}

function Anomalies({ items }) {
  return (
    <Panel title="Anomalies" meta={items?.length ? `${items.length} open` : undefined} flush>
      {!items || items.length === 0 ? (
        <Empty title="Nothing unusual" body="No detections above the severity threshold." />
      ) : (
        <div className="stack">
          {items.map((a) => (
            <div
              key={a.id}
              className="stack gap-1"
              style={{ padding: 'var(--space-3) var(--space-4)', borderBottom: '1px solid var(--border-subtle)' }}
            >
              <div className="row gap-2 wrap">
                <Badge status="new" variant="outline">{humanise(a.kind)}</Badge>
                <span style={{ fontWeight: 'var(--weight-medium)' }}>{a.title}</span>
              </div>
              {a.description && <span className="secondary" style={{ fontSize: 'var(--text-sm)' }}>{a.description}</span>}
              <span className="faint" style={{ fontSize: 'var(--text-2xs)' }}>
                {relative(a.detected_at)} · sample {count(a.sample_size)}
                {a.research_task_id && ' · research task created'}
              </span>
            </div>
          ))}
        </div>
      )}
    </Panel>
  )
}

function PendingResearch({ items }) {
  return (
    <Panel
      title="Research queue"
      actions={<Link to="/research" className="btn btn--sm">Open</Link>}
      flush
    >
      {!items || items.length === 0 ? (
        <Empty title="Queue is empty" body="Annie has no open investigations." />
      ) : (
        <div className="stack">
          {items.map((task) => (
            <div
              key={task.id}
              className="stack gap-1"
              style={{ padding: 'var(--space-3) var(--space-4)', borderBottom: '1px solid var(--border-subtle)' }}
            >
              <div className="row gap-2 wrap">
                <Badge status={task.status} />
                <span className="faint mono" style={{ fontSize: 'var(--text-2xs)' }}>
                  P{task.priority !== null ? task.priority.toFixed(2) : '—'}
                </span>
              </div>
              <span style={{ fontSize: 'var(--text-sm)' }}>{task.question}</span>
              <span className="faint" style={{ fontSize: 'var(--text-2xs)' }}>
                {humanise(task.origin)} · {relative(task.created_at)}
              </span>
            </div>
          ))}
        </div>
      )}
    </Panel>
  )
}

function RecentFindings({ notes }) {
  return (
    <Panel title="Recent findings" meta="Research memory" flush>
      {!notes || notes.length === 0 ? (
        <Empty
          title="No findings yet"
          body="Findings appear here once Annie completes an investigation with supporting evidence."
        />
      ) : (
        <div className="stack">
          {notes.map((note) => (
            <article
              key={note.id}
              className="stack gap-2"
              style={{ padding: 'var(--space-4)', borderBottom: '1px solid var(--border-subtle)' }}
              data-claim={note.claim_type}
            >
              <div className="row gap-2 wrap">
                <Badge status={note.claim_type === 'fact' ? 'verified' : 'pending'} variant="outline">
                  {note.claim_type}
                </Badge>
                <span style={{ fontWeight: 'var(--weight-medium)' }}>{note.title}</span>
                <span className="faint" style={{ fontSize: 'var(--text-2xs)', marginLeft: 'auto' }}>
                  {relative(note.created_at)}
                </span>
              </div>
              <p
                style={{
                  fontSize: 'var(--text-sm)',
                  lineHeight: 'var(--leading-relaxed)',
                  color: 'var(--claim-fg)',
                  fontStyle: 'var(--claim-style)',
                }}
              >
                {note.body}
              </p>
              {note.sample_size !== null && note.sample_size !== undefined && (
                <span className="faint mono" style={{ fontSize: 'var(--text-2xs)' }}>
                  n = {count(note.sample_size)}
                  {note.period_start && ` · ${dateTime(note.period_start)}–${dateTime(note.period_end)}`}
                  {` · confidence ${note.confidence}`}
                </span>
              )}
            </article>
          ))}
        </div>
      )}
    </Panel>
  )
}
