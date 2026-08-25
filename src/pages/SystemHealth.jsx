import { useEffect, useRef, useState } from 'react'

import { api } from '../api/client.js'
import { useApi } from '../api/useApi.js'
import { Async, Badge, Empty, ErrorState, Panel, Sample, Stat } from '../components/primitives.jsx'
import { count, duration, humanise, percent, relative, usd } from '../lib/format.js'

const POLL_MS = 1500

/**
 * One pipeline stage trigger (§20, §2026-08-25). Also runs automatically on
 * its own schedule (Settings → Bots & scheduler) — this is for an on-demand
 * check between scheduled runs.
 *
 * "Run now" returns a run_id immediately rather than blocking until the
 * stage finishes (enrichment in particular can run long enough to look
 * identical to a hung request), so this polls the run's own status instead
 * of awaiting the trigger call directly — a real spinner while it's
 * actually still running server-side, not just while the browser is
 * waiting on one HTTP response.
 */
function PipelineAction({ label, hint, stage, onTrigger, formatResult, onRan }) {
  const [runId, setRunId] = useState(null)
  const [run, setRun] = useState(null) // the live PipelineRun once polling starts
  const [history, setHistory] = useState(null)
  const [triggerError, setTriggerError] = useState(null)
  const pollRef = useRef(null)

  async function loadHistory() {
    try {
      const data = await api.pipelineRuns({ stage, limit: 5 })
      setHistory(data.items || data)
    } catch {
      // History is a nice-to-have — a failed fetch here must not block the
      // run-now button itself from working.
    }
  }

  useEffect(() => {
    loadHistory()
    return () => clearTimeout(pollRef.current)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stage])

  function poll(id) {
    pollRef.current = setTimeout(async () => {
      try {
        const data = await api.pipelineRun(id)
        setRun(data)
        if (data.status === 'running') {
          poll(id)
        } else {
          loadHistory()
          onRan?.()
        }
      } catch (err) {
        setRun({ status: 'error', error: err.message || 'Lost track of this run.' })
      }
    }, POLL_MS)
  }

  async function trigger() {
    setTriggerError(null)
    setRun({ status: 'running' })
    try {
      const { run_id } = await onTrigger()
      setRunId(run_id)
      poll(run_id)
    } catch (err) {
      setTriggerError(err)
      setRun(null)
    }
  }

  const isRunning = run?.status === 'running'
  const lastFinished = !isRunning && run ? run : history?.[0]

  return (
    <div
      className="stack gap-2"
      style={{ paddingBottom: 'var(--space-3)', borderBottom: '1px solid var(--border-subtle)' }}
    >
      <div className="row between wrap gap-2">
        <div className="stack" style={{ gap: 0 }}>
          <strong style={{ fontSize: 'var(--text-sm)' }}>{label}</strong>
          <span className="faint" style={{ fontSize: 'var(--text-2xs)' }}>{hint}</span>
        </div>
        <button className="btn btn--sm btn--primary row gap-2" onClick={trigger} disabled={isRunning}>
          {isRunning && <span className="spinner" aria-hidden="true" />}
          {isRunning ? 'Running…' : 'Run now'}
        </button>
      </div>

      {lastFinished?.status === 'done' && lastFinished.result && (
        <div className="row gap-2" style={{ fontSize: 'var(--text-xs)' }}>
          <span aria-hidden="true" style={{ color: 'var(--rise)' }}>✓</span>
          <span className="secondary">{formatResult(lastFinished.result)}</span>
        </div>
      )}
      {lastFinished?.status === 'error' && (
        <div className="row gap-2" style={{ fontSize: 'var(--text-xs)' }}>
          <span aria-hidden="true" style={{ color: 'var(--fall)' }}>✕</span>
          <span className="secondary">{lastFinished.error || 'Failed — see server logs.'}</span>
        </div>
      )}
      {triggerError && <ErrorState error={triggerError} />}

      {history?.length > 0 && (
        <details className="stack gap-1">
          <summary className="faint" style={{ fontSize: 'var(--text-2xs)', cursor: 'pointer' }}>
            History ({history.length})
          </summary>
          <div className="stack gap-1" style={{ paddingLeft: 'var(--space-3)' }}>
            {history.map((h) => (
              <div key={h.id} className="row gap-2" style={{ fontSize: 'var(--text-2xs)' }}>
                <span aria-hidden="true" style={{ color: h.status === 'done' ? 'var(--rise)' : h.status === 'error' ? 'var(--fall)' : 'var(--text-muted)' }}>
                  {h.status === 'done' ? '✓' : h.status === 'error' ? '✕' : '…'}
                </span>
                <span className="faint">{relative(h.started_at)}</span>
                <span className="faint">·</span>
                <span className="faint">{h.trigger}</span>
              </div>
            ))}
          </div>
        </details>
      )}
    </div>
  )
}

function Pipeline({ onRan }) {
  return (
    <Panel
      title="Pipeline"
      meta="also runs automatically on its own schedule (Settings → Bots & scheduler) — use these for an on-demand check between runs"
    >
      <div className="stack gap-4">
        <PipelineAction
          label="1. Discovery"
          hint="Scan known launchpad programs (Helius) for new mints from the last 24 hours."
          stage="discovery"
          onTrigger={() => api.runDiscovery(24)}
          onRan={onRan}
          formatResult={(r) =>
            `${r.launches_seen ?? 0} launch(es) seen, ${r.tokens_created ?? 0} new token(s) created, ` +
            `${r.tokens_already_known ?? 0} already known.` +
            (r.errors?.length ? ` ${r.errors.length} error(s) — see server logs.` : '')
          }
        />
        <PipelineAction
          label="2. Enrichment"
          hint="Qualify and enrich the newest 50 discovered tokens (metadata, creator wallet, features)."
          stage="enrichment"
          onTrigger={() => api.runEnrichment(50)}
          onRan={onRan}
          formatResult={(r) =>
            `${r.evaluated ?? 0} evaluated, ${r.qualified ?? 0} qualified, ${r.enriched ?? 0} enriched.` +
            (r.errors?.length ? ` ${r.errors.length} error(s) — see server logs.` : '')
          }
        />
        <PipelineAction
          label="3. Trend analysis"
          hint="Compare qualified-token cohorts against their historical baselines."
          stage="trends"
          onTrigger={() => api.runTrends()}
          onRan={onRan}
          formatResult={(r) =>
            `${r.trends_created ?? 0} new trend(s), ${r.trends_updated ?? 0} updated, ` +
            `${r.status_changes ?? 0} status change(s).` +
            (r.skipped_windows?.length ? ` Skipped: ${r.skipped_windows.join(', ')}.` : '')
          }
        />
        <PipelineAction
          label="4. Narrative clustering"
          hint="Group qualified tokens into seeded and emergent narratives (name/ticker/description patterns)."
          stage="narratives"
          onTrigger={() => api.runNarratives()}
          onRan={onRan}
          formatResult={(r) =>
            `${r.qualified_tokens_scanned ?? 0} qualified token(s) scanned, ` +
            `${r.seeded_narratives_updated ?? 0} seeded narrative(s) updated, ` +
            `${r.emergent_narratives_found ?? 0} emergent narrative(s) found.`
          }
        />
      </div>
    </Panel>
  )
}

/**
 * System Health (§50).
 *
 * A disabled capability is shown with the exact environment variables that
 * would enable it. "Provider unavailable" tells an operator nothing; "set
 * BIRDEYE_API_KEY" tells them everything.
 */
export default function SystemHealth() {
  const health = useApi(() => api.health(), [])
  const capabilities = useApi(() => api.capabilities(), [])
  const quality = useApi(() => api.dataQuality({ days: 14 }), [])

  return (
    <>
      <div className="page-head">
        <h2 className="page-head__title">System health</h2>
        <p className="page-head__sub">
          Provider status, request volume, and pipeline coverage. Coverage is a research
          input, not just an ops metric — a window with poor enrichment is excluded from
          trend comparisons rather than averaged over.
        </p>
      </div>

      <Pipeline onRan={quality.reload} />

      <Async state={capabilities}>
        {(caps) => {
          const items = caps.items || caps
          const degraded = items.filter((c) => c.status !== 'available')
          return (
            <Panel title="Capabilities" meta={`${degraded.length} of ${items.length} unavailable`} flush>
              <div className="table-wrap">
                <table className="table table--responsive">
                  <thead>
                    <tr>
                      <th>Capability</th>
                      <th>Tier</th>
                      <th>Status</th>
                      <th>Set these to enable</th>
                    </tr>
                  </thead>
                  <tbody>
                    {items.map((c) => (
                      <tr key={c.key}>
                        <td className="primary" data-label="Capability">
                          <div className="stack" style={{ gap: 0 }}>
                            <strong>{c.label}</strong>
                            <span className="faint" style={{ fontSize: 'var(--text-2xs)' }}>{c.description}</span>
                          </div>
                        </td>
                        <td data-label="Tier">
                          {/* Neutral regardless of tier. Tier says how much
                              depends on the capability, not whether anything
                              is wrong — the status column owns that, and
                              colouring both made a healthy required row read
                              as an alert. */}
                          <Badge status="stable" variant="plain">{c.tier}</Badge>
                        </td>
                        <td data-label="Status"><Badge status={c.status} dot /></td>
                        <td data-label="Set these to enable">
                          {c.missing_env_vars?.length ? (
                            <span className="row gap-1 wrap">
                              {c.missing_env_vars.map((v) => (
                                <code
                                  key={v}
                                  className="mono"
                                  style={{
                                    fontSize: 'var(--text-2xs)',
                                    background: 'var(--bg-inset)',
                                    padding: '1px 5px',
                                    borderRadius: 'var(--radius-sm)',
                                  }}
                                >
                                  {v}
                                </code>
                              ))}
                            </span>
                          ) : (
                            <span className="faint">—</span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Panel>
          )
        }}
      </Async>

      <Async state={health} empty={<Panel><Empty title="No provider data" /></Panel>}>
        {(data) => {
          const items = data.items || data
          return (
            <Panel title="Providers" meta="last 24 hours" flush>
              <div className="table-wrap">
                <table className="table table--responsive">
                  <thead>
                    <tr>
                      <th>Provider</th>
                      <th>Status</th>
                      <th className="num">Requests</th>
                      <th className="num">Errors</th>
                      <th className="num">Rate limited</th>
                      <th className="num">p50 / p95</th>
                      <th className="num">Est. cost</th>
                      <th>Last success</th>
                    </tr>
                  </thead>
                  <tbody>
                    {items.map((p) => (
                      <tr key={p.provider}>
                        <td className="primary" data-label="Provider">
                          <div className="stack" style={{ gap: 0 }}>
                            <strong>{p.provider}</strong>
                            {p.capability_label && (
                              <span className="faint" style={{ fontSize: 'var(--text-2xs)' }}>{p.capability_label}</span>
                            )}
                          </div>
                        </td>
                        <td data-label="Status">
                          <Badge status={p.status} dot />
                          {p.last_error_message && (
                            <div className="faint truncate" style={{ fontSize: 'var(--text-2xs)', maxWidth: 220 }} title={p.last_error_message}>
                              {p.last_error_message}
                            </div>
                          )}
                        </td>
                        <td className="num" data-label="Requests">{count(p.requests_24h)}</td>
                        <td className="num" data-label="Errors">
                          <Sample sample={{ count: p.errors_24h, total: p.requests_24h, frequency: p.error_rate_24h }} decimals={2} />
                        </td>
                        <td className="num" data-label="Rate limited">{count(p.rate_limited_24h)}</td>
                        <td className="num" data-label="p50 / p95">
                          {p.p50_latency_ms ? `${p.p50_latency_ms} / ${p.p95_latency_ms}ms` : '—'}
                        </td>
                        <td className="num" data-label="Est. cost">
                          {p.estimated_cost_24h_usd ? usd(p.estimated_cost_24h_usd, { precise: true }) : <span className="faint">not estimated</span>}
                        </td>
                        <td data-label="Last success" className="faint">{relative(p.last_success_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Panel>
          )
        }}
      </Async>

      <Async state={quality} empty={<Panel><Empty title="No coverage data" /></Panel>}>
        {(data) => {
          const items = data.items || data
          const unusable = items.filter((d) => !d.is_usable_for_trends)
          return (
            <Panel
              title="Pipeline coverage"
              meta={unusable.length ? `${unusable.length} day(s) excluded from trends` : 'all days usable'}
              flush
            >
              <div className="table-wrap">
                <table className="table table--responsive">
                  <thead>
                    <tr>
                      <th>Date</th>
                      <th>Stage</th>
                      <th className="num">Attempted</th>
                      <th className="num">Coverage</th>
                      <th>Usable for trends</th>
                    </tr>
                  </thead>
                  <tbody>
                    {items.map((d, i) => (
                      <tr key={i}>
                        <td className="primary" data-label="Date">{relative(d.measured_on)}</td>
                        <td data-label="Stage">{humanise(d.stage)}</td>
                        <td className="num" data-label="Attempted">{count(d.attempted)}</td>
                        <td className="num" data-label="Coverage">
                          <Sample sample={{ count: d.succeeded, total: d.attempted, frequency: d.coverage }} />
                        </td>
                        <td data-label="Usable for trends">
                          {d.is_usable_for_trends
                            ? <Badge status="verified" variant="plain">Yes</Badge>
                            : <Badge status="disputed" title={d.notes}>Excluded</Badge>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Panel>
          )
        }}
      </Async>
    </>
  )
}
