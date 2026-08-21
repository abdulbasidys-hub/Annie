import { useState } from 'react'

import { api } from '../api/client.js'
import { useApi } from '../api/useApi.js'
import { Async, Badge, Empty, ErrorState, Panel } from '../components/primitives.jsx'
import { count, dateTime, humanise, relative, usd } from '../lib/format.js'

const TABS = [
  { key: 'tasks', label: 'Tasks' },
  { key: 'notes', label: 'Findings' },
  { key: 'hypotheses', label: 'Hypotheses' },
  { key: 'anomalies', label: 'Anomalies' },
]

/** Research Centre (§61). */
export default function Research() {
  const [tab, setTab] = useState('tasks')

  return (
    <>
      <div className="page-head">
        <h2 className="page-head__title">Research</h2>
        <p className="page-head__sub">
          Open investigations, what they concluded, and what remains unresolved.
        </p>
      </div>

      <div className="segmented" role="tablist">
        {TABS.map((t) => (
          <button
            key={t.key}
            role="tab"
            aria-selected={tab === t.key}
            className={tab === t.key ? 'is-active' : ''}
            onClick={() => setTab(t.key)}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === 'tasks' && <Tasks />}
      {tab === 'notes' && <Notes />}
      {tab === 'hypotheses' && <Hypotheses />}
      {tab === 'anomalies' && <AnomalyList />}
    </>
  )
}

/* ---------------------------------------------------------------- Tasks -- */

function Tasks() {
  const [question, setQuestion] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState(null)
  const state = useApi(() => api.researchTasks({ limit: 100 }), [])

  async function submit(event) {
    event.preventDefault()
    if (!question.trim() || submitting) return
    setSubmitting(true)
    setSubmitError(null)
    try {
      await api.createResearchTask({ question: question.trim(), origin: 'user' })
      setQuestion('')
      state.reload()
    } catch (err) {
      setSubmitError(err)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <>
      <Panel title="Ask Annie to investigate something">
        <form onSubmit={submit} className="stack gap-3">
          <div className="filters">
            <input
              className="input"
              placeholder="e.g. Are creators moving away from Pump.fun?"
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              aria-label="Research question"
            />
            <button className="btn btn--primary" type="submit" disabled={submitting || !question.trim()}>
              {submitting ? 'Queueing…' : 'Queue task'}
            </button>
          </div>
          <p className="faint" style={{ fontSize: 'var(--text-2xs)' }}>
            Tasks run under a fixed budget — iterations, tool calls, spend and wall-clock time.
            When a budget is exhausted Annie writes up what she established and states what is
            still open.
          </p>
          {submitError && <ErrorState error={submitError} />}
        </form>
      </Panel>

      <Async state={state} empty={<Panel><Empty title="No tasks yet" body="Queue a question above, or let Annie generate her own from anomalies and trend changes." /></Panel>}>
        {(data) => (
          <Panel title={`${count(data.total)} tasks`} flush>
            <div className="stack">
              {data.items.map((task) => (
                <article
                  key={task.id}
                  className="stack gap-2"
                  style={{ padding: 'var(--space-4)', borderBottom: '1px solid var(--border-subtle)' }}
                >
                  <div className="row gap-2 wrap">
                    <Badge status={task.status} dot />
                    <Badge status="stable" variant="plain">{humanise(task.origin)}</Badge>
                    {task.priority !== null && (
                      <span className="mono faint" style={{ fontSize: 'var(--text-2xs)' }}>
                        priority {task.priority.toFixed(2)}
                      </span>
                    )}
                    <span className="faint" style={{ fontSize: 'var(--text-2xs)', marginLeft: 'auto' }}>
                      {relative(task.created_at)}
                    </span>
                  </div>

                  <strong style={{ fontSize: 'var(--text-base)' }}>{task.question}</strong>
                  {task.reason && (
                    <span className="secondary" style={{ fontSize: 'var(--text-sm)' }}>{task.reason}</span>
                  )}

                  {task.status === 'completed' && task.result && (
                    <div className="stack gap-1" data-claim={task.result_claim_type}>
                      <p
                        style={{
                          fontSize: 'var(--text-sm)',
                          lineHeight: 'var(--leading-relaxed)',
                          color: 'var(--claim-fg)',
                          fontStyle: 'var(--claim-style)',
                        }}
                      >
                        {task.result}
                      </p>
                      <div className="row gap-2 wrap faint" style={{ fontSize: 'var(--text-2xs)' }}>
                        {task.result_claim_type && <span>{task.result_claim_type}</span>}
                        {task.confidence && <span>· confidence {task.confidence}</span>}
                        {task.cost_usd && <span>· {usd(task.cost_usd, { precise: true })}</span>}
                      </div>
                    </div>
                  )}

                  {task.status === 'failed' && task.failure_reason && (
                    <span style={{ fontSize: 'var(--text-sm)', color: 'var(--alert)' }}>
                      {task.failure_reason}
                    </span>
                  )}
                </article>
              ))}
            </div>
          </Panel>
        )}
      </Async>
    </>
  )
}

/* ---------------------------------------------------------------- Notes -- */

function Notes() {
  const state = useApi(() => api.researchNotes({ limit: 100 }), [])
  return (
    <Async state={state} empty={<Panel><Empty title="Research memory is empty" body="Findings are written here when an investigation reaches a conclusion with supporting evidence." /></Panel>}>
      {(data) => (
        <Panel title={`${count(data.total)} findings`} flush>
          <div className="stack">
            {data.items.map((note) => (
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
                  <strong>{note.title}</strong>
                  {!note.is_current && <Badge status="dead">Superseded</Badge>}
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
                <span className="faint mono" style={{ fontSize: 'var(--text-2xs)' }}>
                  {note.sample_size !== null && `n = ${count(note.sample_size)} · `}
                  confidence {note.confidence}
                  {note.period_start && ` · ${dateTime(note.period_start)}–${dateTime(note.period_end)}`}
                </span>
              </article>
            ))}
          </div>
        </Panel>
      )}
    </Async>
  )
}

/* ----------------------------------------------------------- Hypotheses -- */

function Hypotheses() {
  const state = useApi(() => api.hypotheses({ limit: 100 }), [])
  return (
    <Async state={state} empty={<Panel><Empty title="No hypotheses" body="Hypotheses are continuously re-tested as new data arrives (§44)." /></Panel>}>
      {(data) => (
        <Panel title={`${count(data.total)} hypotheses`} flush>
          <div className="stack">
            {data.items.map((h) => (
              <article
                key={h.id}
                className="stack gap-2"
                style={{ padding: 'var(--space-4)', borderBottom: '1px solid var(--border-subtle)' }}
              >
                <div className="row gap-2 wrap">
                  <Badge status={statusFor(h.status)} dot>{h.status}</Badge>
                  <span className="faint" style={{ fontSize: 'var(--text-2xs)', marginLeft: 'auto' }}>
                    last tested {relative(h.last_tested_at)}
                  </span>
                </div>
                <strong style={{ fontSize: 'var(--text-base)' }}>{h.statement}</strong>
                {h.rationale && <span className="secondary" style={{ fontSize: 'var(--text-sm)' }}>{h.rationale}</span>}
                <div className="row gap-4 wrap faint mono" style={{ fontSize: 'var(--text-2xs)' }}>
                  <span>n = {count(h.sample_size)}</span>
                  <span>supporting {count(h.supporting_observations)}</span>
                  <span>contradicting {count(h.contradicting_observations)}</span>
                  {h.p_value !== null && <span>p = {h.p_value.toFixed(4)}</span>}
                  <span>confidence {h.confidence}</span>
                </div>
              </article>
            ))}
          </div>
        </Panel>
      )}
    </Async>
  )
}

function statusFor(status) {
  if (status === 'validated' || status === 'supported') return 'verified'
  if (status === 'rejected') return 'disputed'
  if (status === 'weakening') return 'declining'
  return 'pending'
}

/* ------------------------------------------------------------ Anomalies -- */

function AnomalyList() {
  const state = useApi(() => api.anomalies({ limit: 100 }), [])
  return (
    <Async state={state} empty={<Panel><Empty title="No anomalies detected" /></Panel>}>
      {(data) => (
        <Panel title={`${count(data.total)} anomalies`} flush>
          <div className="stack">
            {data.items.map((a) => (
              <article
                key={a.id}
                className="stack gap-2"
                style={{ padding: 'var(--space-4)', borderBottom: '1px solid var(--border-subtle)' }}
              >
                <div className="row gap-2 wrap">
                  <Badge status="new" variant="outline">{humanise(a.kind)}</Badge>
                  {a.acknowledged && <Badge status="stable">Acknowledged</Badge>}
                  <span className="faint" style={{ fontSize: 'var(--text-2xs)', marginLeft: 'auto' }}>
                    {relative(a.detected_at)}
                  </span>
                </div>
                <strong>{a.title}</strong>
                {a.description && (
                  <span className="secondary" style={{ fontSize: 'var(--text-sm)' }}>{a.description}</span>
                )}
                <span className="faint mono" style={{ fontSize: 'var(--text-2xs)' }}>
                  severity {a.severity?.toFixed(2) ?? '—'} · n = {count(a.sample_size)}
                  {a.research_task_id && ' · research task created'}
                </span>
              </article>
            ))}
          </div>
        </Panel>
      )}
    </Async>
  )
}
