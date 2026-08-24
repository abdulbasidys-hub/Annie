import { useEffect, useState } from 'react'

import { api } from '../api/client.js'
import { useApi } from '../api/useApi.js'
import { Async, Badge, Empty, ErrorState, Panel, Value } from '../components/primitives.jsx'
import { count, dateTime, humanise, relative } from '../lib/format.js'

const STATUSES = ['active', 'uncertain', 'superseded', 'archived']

const TABS = [
  { key: 'long_term', label: 'Long-Term' },
  { key: 'daily_log', label: 'Daily Logs' },
  { key: 'research', label: 'Research' },
  { key: 'dreams', label: 'Dreams' },
]

/**
 * Memory — what Annie has learned and retained.
 *
 * Long-Term and Daily Logs are backed by the memory API directly. Research
 * reuses the existing research-notes endpoint rather than inventing a
 * memory-typed "research" record that does not exist server-side. Dreams has
 * no backing endpoint at all — consolidation runs on a schedule but the
 * backend does not expose a run history — so that tab is an honest empty
 * state rather than fabricated data.
 */
export default function Memory() {
  const [tab, setTab] = useState('long_term')

  return (
    <>
      <div className="page-head">
        <h2 className="page-head__title">Memory</h2>
        <p className="page-head__sub">
          Durable findings, daily summaries, and the research behind them. Long-term memory is
          what consolidation has decided is worth keeping.
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

      {tab === 'long_term' && (
        <MemoryListTab
          type="long_term"
          limit={100}
          editableStatus
          contentAs="text"
          panelLabel="long-term memory"
          emptyTitle="No long-term memories yet"
          emptyBody="Long-term memory is populated when consolidation promotes a finding worth keeping."
        />
      )}
      {tab === 'daily_log' && (
        <MemoryListTab
          type="daily_log"
          limit={60}
          editableStatus={false}
          contentAs="pre"
          panelLabel="daily logs"
          emptyTitle="No daily logs yet"
          emptyBody="A daily log is written at the end of each processing cycle."
        />
      )}
      {tab === 'research' && <ResearchNotesTab />}
      {tab === 'dreams' && (
        <Panel>
          <Empty
            title="No consolidation history yet"
            body="Memory consolidation runs on a daily schedule and promotes findings into Long-Term memory automatically — check the Long-Term tab for what it's produced, filtered by source_type: 'consolidation' if you want to see specifically what consolidation added."
          />
        </Panel>
      )}
    </>
  )
}

/* ---------------------------------------------------------- List + detail -- */

function MemoryListTab({ type, limit, editableStatus, contentAs, panelLabel, emptyTitle, emptyBody }) {
  const [query, setQuery] = useState('')
  const [selectedId, setSelectedId] = useState(null)

  const list = useApi(() => api.memories({ type, limit }), [type, limit])

  // Land on the newest memory rather than an empty panel, same reasoning as
  // Reports — the newest item is the one most likely wanted.
  const newestId = list.data?.items?.[0]?.id ?? null
  useEffect(() => {
    if (selectedId === null && newestId !== null) setSelectedId(newestId)
  }, [selectedId, newestId])

  function handleDeleted() {
    setSelectedId(null)
    list.reload()
  }

  return (
    <div className="stack gap-4">
      <input
        className="input"
        type="search"
        placeholder="Search title or content…"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        aria-label={`Search ${panelLabel}`}
      />

      <div className="detail detail--index">
        <Async state={list} empty={<Panel><Empty title={emptyTitle} body={emptyBody} /></Panel>}>
          {(data) => {
            const q = query.trim().toLowerCase()
            const items = q
              ? data.items.filter(
                  (m) => m.title?.toLowerCase().includes(q) || m.content?.toLowerCase().includes(q)
                )
              : data.items

            return (
              <Panel
                title={q ? `${count(items.length)} of ${count(data.total)}` : `${count(data.total)} memories`}
                flush
              >
                {items.length === 0 ? (
                  <div style={{ padding: 'var(--space-4)' }}>
                    <Empty title="No matches" body={`Nothing matched “${query}”.`} />
                  </div>
                ) : (
                  <div className="stack">
                    {items.map((m) => {
                      const active = m.id === selectedId
                      return (
                        <button
                          key={m.id}
                          onClick={() => setSelectedId(m.id)}
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
                          <div className="row gap-2 wrap">
                            <Badge status={m.status} dot />
                            <span className="faint" style={{ fontSize: 'var(--text-2xs)', marginLeft: 'auto' }}>
                              {relative(m.created_at)}
                            </span>
                          </div>
                          <strong className="truncate" style={{ fontSize: 'var(--text-sm)' }}>{m.title}</strong>
                          <div className="row gap-3 wrap faint" style={{ fontSize: 'var(--text-2xs)' }}>
                            <span>confidence {m.confidence}</span>
                            <ImportanceMeter value={m.importance} />
                          </div>
                        </button>
                      )
                    })}
                  </div>
                )}
              </Panel>
            )
          }}
        </Async>

        <MemoryDetail
          id={selectedId}
          listLoading={list.loading}
          editableStatus={editableStatus}
          contentAs={contentAs}
          onStatusSaved={list.reload}
          onDeleted={handleDeleted}
        />
      </div>
    </div>
  )
}

function MemoryDetail({ id, listLoading, editableStatus, contentAs, onStatusSaved, onDeleted }) {
  const detail = useApi(() => api.memory(id), [id], { enabled: id !== null })
  const [deleting, setDeleting] = useState(false)
  const [deleteError, setDeleteError] = useState(null)

  async function handleDelete(memoryId) {
    if (!window.confirm('Delete this memory? This cannot be undone.')) return
    setDeleting(true)
    setDeleteError(null)
    try {
      await api.deleteMemory(memoryId)
      onDeleted()
    } catch (err) {
      setDeleteError(err)
      setDeleting(false)
    }
  }

  if (id === null) {
    return (
      <Panel>
        <Empty title={listLoading ? 'Loading memories…' : 'No memory selected'} />
      </Panel>
    )
  }

  return (
    <Async state={detail} rows={8} empty={<Panel><Empty title="Memory not found" /></Panel>}>
      {(m) => (
        <div className="stack gap-5">
          <div className="page-head">
            <div className="row between wrap gap-3">
              <h3 className="page-head__title" style={{ fontSize: 'var(--text-lg)' }}>{m.title}</h3>
              <div className="row gap-2">
                <Badge status={m.status} dot />
                <button
                  className="btn btn--sm btn--ghost"
                  style={{ color: 'var(--alert)' }}
                  onClick={() => handleDelete(m.id)}
                  disabled={deleting}
                >
                  {deleting ? 'Deleting…' : 'Delete'}
                </button>
              </div>
            </div>
            <div className="row gap-3 wrap faint" style={{ fontSize: 'var(--text-2xs)' }}>
              <span>confidence {m.confidence}</span>
              <span>created {relative(m.created_at)}</span>
              {m.updated_at && <span>updated {relative(m.updated_at)}</span>}
              {m.last_used_at && <span>last used {relative(m.last_used_at)}</span>}
            </div>
            {deleteError && <ErrorState error={deleteError} />}
          </div>

          {editableStatus && (
            <Panel title="Status">
              <StatusEditor memory={m} onSaved={() => { detail.reload(); onStatusSaved() }} />
            </Panel>
          )}

          <Panel title="Content">
            {contentAs === 'pre' ? (
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
                {m.content}
              </pre>
            ) : (
              <p style={{ fontSize: 'var(--text-sm)', lineHeight: 'var(--leading-relaxed)', whiteSpace: 'pre-wrap' }}>
                {m.content}
              </p>
            )}
          </Panel>

          {m.tags?.length > 0 && (
            <Panel title="Tags">
              <div className="row gap-1 wrap">
                {m.tags.map((t) => (
                  <Badge key={t} status="stable" variant="plain">{t}</Badge>
                ))}
              </div>
            </Panel>
          )}

          <Panel title="Source">
            <div className="deflist">
              <dt>Source type</dt>
              <dd><Value>{m.source_type}</Value></dd>
              <dt>Source id</dt>
              <dd><Value>{m.source_id}</Value></dd>
              <dt>Importance</dt>
              <dd><Value>{m.importance !== null && m.importance !== undefined ? m.importance.toFixed(2) : null}</Value></dd>
            </div>
          </Panel>

          {(m.related_memory_ids?.length > 0 || m.related_research_ids?.length > 0) && (
            <Panel title="Related">
              <div className="stack gap-3">
                {m.related_memory_ids?.length > 0 && (
                  <div className="stack gap-1">
                    <span className="faint" style={{ fontSize: 'var(--text-2xs)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
                      Memories
                    </span>
                    <div className="mono faint" style={{ fontSize: 'var(--text-2xs)', wordBreak: 'break-all' }}>
                      {m.related_memory_ids.join(', ')}
                    </div>
                  </div>
                )}
                {m.related_research_ids?.length > 0 && (
                  <div className="stack gap-1">
                    <span className="faint" style={{ fontSize: 'var(--text-2xs)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
                      Research
                    </span>
                    <div className="mono faint" style={{ fontSize: 'var(--text-2xs)', wordBreak: 'break-all' }}>
                      {m.related_research_ids.join(', ')}
                    </div>
                  </div>
                )}
              </div>
            </Panel>
          )}
        </div>
      )}
    </Async>
  )
}

function StatusEditor({ memory, onSaved }) {
  const [status, setStatus] = useState(memory.status)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)

  const dirty = status !== memory.status

  async function save() {
    setSaving(true)
    setError(null)
    try {
      await api.updateMemory(memory.id, { status })
      onSaved()
    } catch (err) {
      setError(err)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="stack gap-2">
      <div className="filters">
        <select className="select" value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Status">
          {STATUSES.map((s) => (
            <option key={s} value={s}>{humanise(s)}</option>
          ))}
        </select>
        <button className="btn btn--sm btn--primary" onClick={save} disabled={!dirty || saving}>
          {saving ? 'Saving…' : 'Save'}
        </button>
      </div>
      {error && <ErrorState error={error} />}
    </div>
  )
}

function ImportanceMeter({ value }) {
  if (value === null || value === undefined) return <Value>{null}</Value>
  const pct = Math.round(Math.max(0, Math.min(1, value)) * 100)
  return (
    <span className="row gap-2" title={`Importance ${value.toFixed(2)}`}>
      <span
        style={{
          width: 36,
          height: 4,
          borderRadius: 'var(--radius-full)',
          background: 'var(--bg-inset)',
          overflow: 'hidden',
          display: 'inline-block',
        }}
      >
        <span style={{ display: 'block', height: '100%', width: `${pct}%`, background: 'var(--accent)' }} />
      </span>
      <span className="mono">{value.toFixed(2)}</span>
    </span>
  )
}

/* ---------------------------------------------------------------- Research -- */

/**
 * "Research memory" has no dedicated memory-typed record — it is the existing
 * research notes. Rendered the same way Research.jsx's Findings tab does,
 * including the claim-type styling, so this isn't a second, divergent
 * implementation of the same list.
 */
function ResearchNotesTab() {
  const state = useApi(() => api.researchNotes({ limit: 50 }), [])
  return (
    <Async
      state={state}
      empty={<Panel><Empty title="Research memory is empty" body="Findings are written here when an investigation reaches a conclusion with supporting evidence." /></Panel>}
    >
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
