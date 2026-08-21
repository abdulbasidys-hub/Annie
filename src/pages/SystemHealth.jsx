import { api } from '../api/client.js'
import { useApi } from '../api/useApi.js'
import { Async, Badge, Empty, Panel, Sample, Stat } from '../components/primitives.jsx'
import { count, duration, humanise, percent, relative, usd } from '../lib/format.js'

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
