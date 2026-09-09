import { useCallback, useEffect, useRef, useState } from 'react'

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
      meta="also runs on its own schedule (Settings → Bots & scheduler) — these are for an on-demand check in between"
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
          label="2. Watch"
          hint="Re-price the highest-priority slice of the watchlist. Lookups are batched 30 mints per request and results are written locally, so this is cheap — click it freely."
          stage="watch"
          onTrigger={() => api.runWatch(300)}
          onRan={onRan}
          formatResult={(r) =>
            `${r.checked ?? 0} checked, ${r.priced ?? 0} priced, ${r.unpriced ?? 0} with no pair yet, ` +
            `${r.qualified_count ?? 0} newly cleared a tier.` +
            // The filtering is the point, so it is shown rather than implied:
            // clearing $100k makes a token evidence, not something worth
            // writing a page about.
            (r.qualified_count
              ? ` ${r.remembered ?? 0} earned a memory file, ${r.not_remembered ?? 0} stayed in the ledger only.`
              : '') +
            (r.errors?.length ? ` ${r.errors.length} error(s) — see server logs.` : '')
          }
        />
        <PipelineAction
          label="3. Signals"
          hint="Recompute which characteristics are over-represented among tokens that cleared a tier. Pure statistics over local rows — free."
          stage="signals"
          onTrigger={() => api.runSignals()}
          onRan={onRan}
          formatResult={(r) =>
            `${r.cohorts ?? 0} cohort(s), ${r.evaluated ?? 0} characteristic(s) evaluated, ` +
            `${r.created ?? 0} new, ${r.updated ?? 0} updated.` +
            (r.promoted?.length ? ` Rising: ${r.promoted.slice(0, 3).join(', ')}.` : '')
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
        <PipelineAction
          label="5. Full cycle"
          hint="Everything above plus the thinking: build the digest, one model call to update her notebook, refresh dossiers, snapshot. This is the only button here that spends money."
          stage="cycle"
          onTrigger={() => api.runCycle(false)}
          onRan={onRan}
          formatResult={(r) =>
            (r.learning?.headline || 'Quiet window — no model call made.') +
            (r.learning?.applied?.length
              ? ` ${r.learning.applied.length} memory file(s) updated.`
              : '') +
            (r.delivered === false && r.reason ? ` Not delivered: ${r.reason}` : '')
          }
        />
        <PipelineAction
          label="5b. Full cycle, as midnight"
          hint="The same, run as the 00:00 slot — writes the daily log, generates the day's three launch ideas, and sends the full-day brief. Use this to recover a day whose midnight run was missed."
          stage="cycle"
          onTrigger={() => api.runCycle(true)}
          onRan={onRan}
          formatResult={(r) =>
            `Daily log: ${r.daily_log?.path ?? 'not written'}. ` +
            `Ideas: ${r.ideas?.generated ?? r.ideas?.skipped ?? 'none'}. ` +
            (r.delivered ? 'Brief delivered.' : `Not delivered: ${r.reason ?? 'unknown'}`)
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
/**
 * Where the money is going.
 *
 * This panel exists because "too much data" is only fixable if it is visible.
 * It shows this process's Firestore writes against a budget deliberately set
 * well under the Spark plan's 20,000/day cap, what the local ledger is
 * holding, and — the one that bites silently — whether memory is on a
 * persistent volume or will vanish on the next redeploy.
 */
/**
 * Is the pipeline actually working?
 *
 * Sits above everything because it answers the question a page of zeros
 * cannot: a deployment whose webhook is misconfigured and one that started
 * ten minutes ago produce identical counts, and only one of them needs
 * fixing. Silent when healthy — a banner that is always there is one nobody
 * reads.
 */
function PipelineStatus({ state }) {
  const d = state.data
  // An undelivered brief happens on a perfectly healthy pipeline — everything
  // ran and there was nowhere to send it — so it has to keep the panel open
  // even when the stream itself is fine.
  const undelivered = d?.delivery?.status === 'undelivered'
  if (!d || (d.state === 'healthy' && !undelivered)) return null

  const tone = d.state === 'warming_up' ? 'new' : d.state === 'healthy' ? 'new' : 'alert'
  return (
    <Panel
      title={d.state === 'healthy' ? 'The brief is not being delivered' : 'Nothing is coming through'}
      meta={<Badge status={tone}>{d.state.replace(/_/g, ' ')}</Badge>}
    >
      {d.state !== 'healthy' && <p style={{ margin: '0 0 12px' }}>{d.headline}</p>}

      {d.what_to_check?.length > 0 && (
        <div className="stack" style={{ gap: 6 }}>
          <span className="faint" style={{ fontSize: 'var(--text-2xs)' }}>
            What to check
          </span>
          {d.what_to_check.map((action, i) => (
            <div key={i} className="row" style={{ gap: 8, alignItems: 'flex-start' }}>
              <span className="faint">·</span>
              <span style={{ fontSize: 'var(--text-sm)' }}>{action}</span>
            </div>
          ))}
        </div>
      )}

      {d.delivery?.status === 'undelivered' && (
        <p className="modal__warn" style={{ marginTop: 12 }}>
          <strong>The last brief was written but not sent.</strong> {d.delivery.detail}{' '}
          Set a channel under "Where Annie posts" below.
        </p>
      )}

      <dl className="deflist" style={{ marginTop: 'var(--space-4)' }}>
        <dt>Launches last hour</dt>
        <dd>
          {count(d.stream?.sightings_last_hour)}
          <span className="faint"> · {d.stream?.expected_rate}</span>
        </dd>
        <dt>Last launch seen</dt>
        <dd>{d.stream?.last_sighting_at ? relative(d.stream.last_sighting_at) : 'never'}</dd>
        <dt>Held in ledger</dt>
        <dd>{count(d.ledger?.held)}</dd>
        <dt>Memory files</dt>
        <dd>
          {count(d.notebook?.files)}
          {d.notebook?.only_seeded_placeholders && (
            <span className="faint"> · only the seeded placeholders</span>
          )}
        </dd>
        <dt>Last cycle</dt>
        <dd>{d.jobs?.last_cycle_at ? relative(d.jobs.last_cycle_at) : 'never run'}</dd>
      </dl>
    </Panel>
  )
}

/**
 * The webhook registration, checked against Helius rather than inferred.
 *
 * Shown only when the stream has a problem, because when data is arriving the
 * registration is self-evidently fine. Every way this breaks is invisible
 * from the receiving end — auto-disabled after a run of failures, pointing at
 * a previous domain, or an authHeader that no longer matches so every
 * delivery 401s — and all three look identical to a quiet market.
 */
/**
 * Where Annie posts: the daily brief, and the day's launch ideas.
 *
 * Until this existed the only way to set a channel was to ask Annie in
 * Discord to create one, which needs the bot to hold Manage Channels in a
 * guild it shares with you. Without that permission there was no path at all
 * — the brief was written every day and went nowhere, and the only sign was
 * a reason buried in a job result.
 *
 * Two purposes, because they are two kinds of thing: the brief reports what
 * happened, the ideas propose what to do next. The ideas channel is optional
 * and falls back to the brief channel, so one is enough to start.
 *
 * Always rendered, never hidden behind a failed delivery. Gating it on
 * "undelivered" was wrong the same way gating the webhook check on a broken
 * pipeline was: a control you can only reach while something is broken
 * cannot be used to confirm the fix, and it left no way at all to add the
 * second channel once the first one worked.
 */
function DeliveryChannels({ onSet }) {
  const [state, setState] = useState(null)

  const load = useCallback(async () => {
    try {
      setState(await api.briefChannel())
    } catch {
      setState(null) // Not worth an error panel; the row simply stays quiet.
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  if (!state) return null

  const refresh = () => {
    load()
    onSet?.()
  }

  return (
    <Panel
      title="Where Annie posts"
      meta={state.configured ? null : <Badge status="alert">nothing set</Badge>}
    >
      {!state.discord_configured ? (
        <p className="modal__warn" style={{ margin: 0 }}>
          <strong>Discord is not configured.</strong> Set DISCORD_BOT_TOKEN and the brief
          has somewhere it can go.
        </p>
      ) : (
        <div className="stack" style={{ gap: 14 }}>
          <ChannelRow
            purpose="morning_brief"
            label="Daily brief"
            hint="The 00:00 WAT summary of what moved and what she changed her mind about."
            current={state.channel}
            onSet={refresh}
          />
          <ChannelRow
            purpose="launch_ideas"
            label="Launch ideas"
            hint={
              state.ideas_fall_back_to_brief
                ? 'Going to the brief channel. Set one here to split them out.'
                : "The day's three ideas, once a day, each grounded in something that moved."
            }
            current={state.ideas_channel}
            onSet={refresh}
          />
        </div>
      )}
    </Panel>
  )
}

/**
 * One purpose, one channel. The ID is verified by actually posting to it
 * before anything is saved — registering a channel the bot cannot reach
 * would reproduce exactly the failure this fixes: a configuration that looks
 * right in the UI and delivers nothing.
 */
function ChannelRow({ purpose, label, hint, current, onSet }) {
  const [open, setOpen] = useState(!current)
  const [channelId, setChannelId] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  const save = async () => {
    setBusy(true)
    setError(null)
    try {
      await api.setBriefChannel({ channel_id: channelId.trim(), purpose })
      setChannelId('')
      setOpen(false)
      onSet?.()
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="stack" style={{ gap: 6 }}>
      <div className="row gap-2 wrap" style={{ alignItems: 'baseline' }}>
        <strong style={{ fontSize: 'var(--text-sm)' }}>{label}</strong>
        {current ? (
          <span className="faint" style={{ fontSize: 'var(--text-xs)' }}>
            → #{current.name || current.channel_id}
          </span>
        ) : (
          <span className="faint" style={{ fontSize: 'var(--text-xs)' }}>not set</span>
        )}
        {!open && (
          <button className="btn btn--ghost btn--sm" onClick={() => setOpen(true)}>
            {current ? 'Change' : 'Set'}
          </button>
        )}
      </div>
      <span className="faint" style={{ fontSize: 'var(--text-xs)' }}>{hint}</span>

      {open && (
        <>
          <span className="faint" style={{ fontSize: 'var(--text-xs)' }}>
            In Discord: User Settings → Advanced → Developer Mode, then right-click the
            channel → Copy Channel ID. The bot must be in that server with permission to
            view and post in it.
          </span>
          {error && <ErrorState error={error} />}
          <div className="row gap-2 wrap">
            <input
              className="input"
              style={{ flex: '1 1 220px', minWidth: 0 }}
              value={channelId}
              onChange={(e) => setChannelId(e.target.value)}
              placeholder="1234567890123456789"
              inputMode="numeric"
              aria-label={`Discord channel ID for ${label}`}
              onKeyDown={(e) => e.key === 'Enter' && channelId.trim() && !busy && save()}
            />
            <button
              className="btn btn--primary"
              onClick={save}
              disabled={busy || !channelId.trim()}
            >
              {busy ? 'Checking…' : 'Post here'}
            </button>
            {current && (
              <button className="btn btn--ghost" onClick={() => setOpen(false)} disabled={busy}>
                Cancel
              </button>
            )}
          </div>
        </>
      )}
    </div>
  )
}

function WebhookCheck({ pipelineState }) {
  const [report, setReport] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [repaired, setRepaired] = useState(null)

  // Always rendered, never hidden behind a broken pipeline. It was gated on
  // `state !== 'healthy'` and that was wrong twice over: a diagnostic you can
  // only reach while something is already broken cannot be used to confirm a
  // fix worked, and it cannot answer "is the second launchpad registered?"
  // on a deployment that is merely receiving less than it should.
  //
  // When the stream is fine it collapses to one line and a button, because a
  // panel shouting about a problem that does not exist is how people learn to
  // scroll past a page.
  const stalled = pipelineState && pipelineState !== 'healthy' && pipelineState !== 'warming_up'

  const check = async () => {
    setBusy(true)
    setError(null)
    setRepaired(null)
    try {
      setReport(await api.webhookStatus())
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  const repair = async () => {
    setBusy(true)
    setError(null)
    try {
      setRepaired(await api.repairWebhook())
      setReport(await api.webhookStatus())
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Panel
      title="Is the webhook actually registered?"
      meta={
        stalled
          ? 'asks Helius directly — worth checking first, nothing is arriving'
          : 'asks Helius directly'
      }
      actions={
        <>
          <button className="btn btn--ghost" onClick={check} disabled={busy}>
            {busy ? 'Checking…' : 'Check'}
          </button>
          {report && !report.ok && (
            <button className="btn btn--primary" onClick={repair} disabled={busy}>
              Register / repair
            </button>
          )}
        </>
      }
    >
      {error && <ErrorState error={error} />}

      {!report && !error && (
        <p style={{ margin: 0 }}>
          {stalled
            ? 'Nothing is arriving, and the reason is almost always here rather than in this app. Press Check and it will ask Helius what is registered against your key, compare it to what this deployment expects, and name the difference.'
            : 'Press Check to confirm the registration still points here, carries both launchpad program IDs, and uses a secret this app will accept.'}
        </p>
      )}

      {repaired && (
        <p className="modal__warn" style={{ marginBottom: 12 }}>
          <strong>{repaired.action === 'created' ? 'Created' : 'Repaired'}.</strong>{' '}
          Webhook <code className="mono">{repaired.webhook_id}</code> now points at{' '}
          <code className="mono">{repaired.url}</code>. Deliveries should start within
          a minute or two — watch "Launches last hour" above.
        </p>
      )}

      {report && (
        <div className="stack gap-4">
          <div className="row gap-2 wrap">
            <Badge status={report.ok ? 'verified' : 'alert'} variant="outline">
              {report.state.replace(/_/g, ' ')}
            </Badge>
            <span className="faint mono" style={{ fontSize: 'var(--text-2xs)' }}>
              expects {report.expected_url}
            </span>
          </div>

          {report.problems?.length > 0 && (
            <div className="stack" style={{ gap: 6 }}>
              {report.problems.map((problem, i) => (
                <div key={i} className="row" style={{ gap: 8, alignItems: 'flex-start' }}>
                  <span className="faint">·</span>
                  <span style={{ fontSize: 'var(--text-sm)' }}>{problem}</span>
                </div>
              ))}
            </div>
          )}

          {report.webhooks?.length > 0 && (
            <div className="table-wrap">
              <table className="table table--responsive">
                <thead>
                  <tr>
                    <th>Registered URL</th>
                    <th>Types</th>
                    <th>Programs</th>
                    <th>Secret</th>
                  </tr>
                </thead>
                <tbody>
                  {report.webhooks.map((w) => (
                    <tr key={w.id}>
                      <td className="primary" data-label="URL">
                        <code className="mono">{w.url}</code>
                      </td>
                      <td data-label="Types">{w.transaction_types.join(', ') || '—'}</td>
                      <td className="num" data-label="Programs">
                        {count(w.account_addresses?.length)}
                      </td>
                      <td data-label="Secret">
                        {w.auth_header_matches
                          ? 'matches'
                          : w.auth_header_set
                            ? 'set, does not match'
                            : 'not set'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </Panel>
  )
}

function Cost() {
  const state = useApi(() => api.cost(), [])

  return (
    <Async state={state} rows={3}>
      {(c) => {
        const fs = c.firestore
        const overBudget = fs.writes >= fs.write_budget
        return (
          <Panel
            title="Cost & durability"
            meta={`${fs.write_headroom_pct}% of today's Firestore write budget still free`}
          >
            <div className="grid grid--stats">
              <Stat
                label="Firestore writes today"
                value={`${count(fs.writes)} / ${count(fs.write_budget)}`}
                foot={
                  <span className="muted">
                    plan cap is {count(fs.spark_plan_daily_caps.writes)}/day
                    {fs.writes_skipped > 0 && ` · ${count(fs.writes_skipped)} skipped`}
                  </span>
                }
              />
              <Stat
                label="Firestore reads today"
                value={`${count(fs.reads)} / ${count(fs.read_budget)}`}
                foot={<span className="muted">plan cap is {count(fs.spark_plan_daily_caps.reads)}/day</span>}
              />
              <Stat
                label="Held locally"
                value={count(c.ledger.sightings_total)}
                foot={
                  <span className="muted">
                    of {count(c.ledger.sightings_24h)} seen in 24h · free, pruned at 48h
                  </span>
                }
              />
              <Stat
                label="Memory files"
                value={count(c.memory.files)}
                foot={<span className="muted">{count(c.memory.keys)} lookup keys</span>}
              />
            </div>

            {overBudget && (
              <p className="faint" style={{ marginTop: 'var(--space-3)' }}>
                The write budget is exhausted for today. Writes are being skipped and logged,
                not retried — memory on disk is unaffected, only its Firestore mirror is behind.
              </p>
            )}

            <div className="stack gap-2" style={{ marginTop: 'var(--space-4)' }}>
              <div className="row gap-2 wrap">
                <Badge status={c.durability.looks_like_volume ? 'verified' : 'declining'} variant="outline">
                  {c.durability.looks_like_volume ? 'Memory is durable' : 'Memory is not durable'}
                </Badge>
                <span className="mono faint" style={{ fontSize: 'var(--text-2xs)' }}>
                  {c.durability.root}
                </span>
              </div>
              <span className="faint" style={{ fontSize: 'var(--text-2xs)' }}>
                {c.durability.note}
              </span>
            </div>

            <div className="stack gap-2" style={{ marginTop: 'var(--space-4)' }}>
              <span className="faint" style={{ fontSize: 'var(--text-2xs)' }}>
                Launch stream: {count(c.stream.sightings)} sighted in the last hour from{' '}
                {count(c.stream.distinct_creators)} wallets
                {c.stream.last_sighting_at && ` · last ${relative(c.stream.last_sighting_at)}`}
              </span>
              <span className="faint" style={{ fontSize: 'var(--text-2xs)' }}>
                Model calls: {c.model_calls_per_day.scheduled}. {c.model_calls_per_day.note}
              </span>
            </div>
          </Panel>
        )
      }}
    </Async>
  )
}

export default function SystemHealth() {
  const health = useApi(() => api.health(), [])
  const capabilities = useApi(() => api.capabilities(), [])
  const quality = useApi(() => api.dataQuality({ days: 14 }), [])
  // Fetched once here and passed down. The banner and the webhook check both
  // need it, and fetching it in each meant two identical requests on every
  // page load plus a flicker as they resolved at different times.
  const pipeline = useApi(() => api.pipelineStatus(), [])

  return (
    <>
      <div className="page-head">
        <h2 className="page-head__title">System health</h2>
        <p className="page-head__sub">
          What things cost, whether memory will survive a redeploy, provider status, and
          pipeline coverage. Coverage is a research input, not just an ops metric — a window
          with poor coverage is excluded from comparisons rather than averaged over.
        </p>
      </div>

      <PipelineStatus state={pipeline} />

      <WebhookCheck pipelineState={pipeline.data?.state} />

      <DeliveryChannels onSet={pipeline.reload} />

      <Cost />

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
