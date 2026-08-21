import { useEffect, useState } from 'react'

import { api } from '../api/client.js'
import { useApi } from '../api/useApi.js'
import { Async, Badge, Empty, Panel, Stat } from '../components/primitives.jsx'
import { count, date, relative } from '../lib/format.js'

/** Daily and weekly intelligence reports (§41, §42). */
export default function Reports() {
  const [kind, setKind] = useState('daily')
  const [selectedId, setSelectedId] = useState(null)

  const list = useApi(() => api.reports({ kind, limit: 60 }), [kind])
  const detail = useApi(() => api.report(selectedId), [selectedId], { enabled: selectedId !== null })

  // Land on the newest report rather than an empty panel — the thing an
  // operator most likely wants is the cycle that just ran. Done in an effect
  // because selecting during render is a state update inside another
  // component's render pass.
  const newestId = list.data?.items?.[0]?.id ?? null
  useEffect(() => {
    if (selectedId === null && newestId !== null) setSelectedId(newestId)
  }, [selectedId, newestId])

  return (
    <>
      <div className="page-head">
        <div className="row between wrap gap-3">
          <h2 className="page-head__title">Reports</h2>
          <div className="segmented">
            <button className={kind === 'daily' ? 'is-active' : ''} onClick={() => { setKind('daily'); setSelectedId(null) }}>Daily</button>
            <button className={kind === 'weekly' ? 'is-active' : ''} onClick={() => { setKind('weekly'); setSelectedId(null) }}>Weekly</button>
          </div>
        </div>
        <p className="page-head__sub">
          Generated at the end of each cycle. Reports are stored as written — regenerating one
          against a changed template would rewrite history.
        </p>
      </div>

      <div className="detail detail--index">
        <Async state={list} empty={<Panel><Empty title="No reports yet" body="Reports are produced at the end of each daily processing cycle." /></Panel>}>
          {(data) => (
            <Panel title={`${count(data.total)} reports`} flush>
              <div className="stack">
                {data.items.map((r) => {
                  const active = r.id === selectedId
                  return (
                    <button
                      key={r.id}
                      onClick={() => setSelectedId(r.id)}
                      className="stack gap-1"
                      style={{
                        padding: 'var(--space-3) var(--space-4)',
                        borderBottom: '1px solid var(--border-subtle)',
                        background: active ? 'var(--bg-active)' : 'transparent',
                        border: 'none',
                        borderLeft: active ? '2px solid var(--accent)' : '2px solid transparent',
                        textAlign: 'left',
                        cursor: 'pointer',
                        width: '100%',
                      }}
                    >
                      <strong style={{ fontSize: 'var(--text-sm)' }}>{date(r.period_end)}</strong>
                      <span className="faint" style={{ fontSize: 'var(--text-2xs)' }}>
                        {count(r.tokens_qualified)} qualified · {count(r.trends_rising)} rising
                      </span>
                    </button>
                  )
                })}
              </div>
            </Panel>
          )}
        </Async>

        <ReportBody detail={detail} selectedId={selectedId} listLoading={list.loading} />
      </div>
    </>
  )
}

function ReportBody({ detail, selectedId, listLoading }) {
  if (selectedId === null) {
    return (
      <Panel>
        <Empty title={listLoading ? 'Loading reports…' : 'No report selected'} />
      </Panel>
    )
  }

  return (
    <Async state={detail} rows={8} empty={<Panel><Empty title="Report not found" /></Panel>}>
      {(r) => (
        <div className="stack gap-5">
          <div className="page-head">
            <h3 className="page-head__title" style={{ fontSize: 'var(--text-lg)' }}>{r.title}</h3>
            <span className="faint" style={{ fontSize: 'var(--text-xs)' }}>
              {date(r.period_start)} – {date(r.period_end)}
              {r.generated_by_model && ` · ${r.generated_by_model}`}
            </span>
          </div>

          <div className="grid grid--stats">
            <Stat label="Qualified" value={count(r.tokens_qualified)} />
            <Stat label="New trends" value={count(r.trends_new)} />
            <Stat label="Rising" value={count(r.trends_rising)} />
            <Stat label="Declining" value={count(r.trends_declining)} />
          </div>

          {r.headline_finding && (
            <Panel title="Most interesting finding">
              <p style={{ fontSize: 'var(--text-md)', lineHeight: 'var(--leading-relaxed)' }}>
                {r.headline_finding}
              </p>
            </Panel>
          )}

          {r.biggest_change && (
            <Panel title="Biggest change">
              <p style={{ fontSize: 'var(--text-sm)', lineHeight: 'var(--leading-relaxed)' }}>
                {r.biggest_change}
              </p>
            </Panel>
          )}

          {r.markdown && (
            <Panel title="Full report" meta="as generated">
              <pre
                style={{
                  fontFamily: 'var(--font-mono)',
                  fontSize: 'var(--text-xs)',
                  lineHeight: 'var(--leading-relaxed)',
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word',
                  color: 'var(--text-secondary)',
                }}
              >
                {r.markdown}
              </pre>
            </Panel>
          )}

          {r.limitations && (
            <div className="caveats">
              <span className="caveats__label">Confidence and limitations</span>
              <span>{r.limitations}</span>
            </div>
          )}
        </div>
      )}
    </Async>
  )
}
