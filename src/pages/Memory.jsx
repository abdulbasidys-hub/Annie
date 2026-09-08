import { useCallback, useEffect, useMemo, useState } from 'react'

import { api } from '../api/client.js'
import { useApi } from '../api/useApi.js'
import { Async, Badge, Empty, ErrorState, Markdown, Panel } from '../components/primitives.jsx'
import { relative } from '../lib/format.js'

/**
 * Memory — Annie's notebook, as a notebook.
 *
 * The 2026-09-08 rewrite made memory a folder of markdown files rather than a
 * collection of typed records, so this page is a file browser, not a table:
 * sections on the left, the file itself on the right, rendered as prose. That
 * is the whole point — you should be able to read what she thinks without an
 * interface interpreting it for you.
 *
 * Search sits above everything because it is the fastest route in. Pasting a
 * contract address or a creator wallet resolves through the exact-key index
 * to the file about it, which is what makes "what do you know about this CA"
 * a one-step question here as well as in chat.
 *
 * Everything on this page is served from local files and SQLite, so it is
 * free to open and free to poll.
 */

const SECTION_ORDER = [
  'core',
  'playbook',
  'narratives',
  'creators',
  'tokens',
  'daily',
  'weekly',
  'monthly',
  'notes',
]

export default function Memory() {
  const tree = useApi(() => api.memoryTree(), [])
  const [selected, setSelected] = useState(null)
  const [query, setQuery] = useState('')
  const [hits, setHits] = useState(null)
  const [searching, setSearching] = useState(false)
  const [searchError, setSearchError] = useState(null)

  const sections = useMemo(() => {
    const found = tree.data?.sections ?? []
    const byName = Object.fromEntries(found.map((s) => [s.name, s]))
    return SECTION_ORDER.map((name) => byName[name]).filter(Boolean)
  }, [tree.data])

  // Open something sensible on first load rather than an empty right pane.
  useEffect(() => {
    if (selected || !sections.length) return
    const core = sections.find((s) => s.name === 'core')
    const first = (core ?? sections.find((s) => s.files.length))?.files?.[0]
    if (first) setSelected(first.path)
  }, [sections, selected])

  const runSearch = useCallback(
    async (event) => {
      event?.preventDefault()
      const q = query.trim()
      if (!q) {
        setHits(null)
        setSearchError(null)
        return
      }
      setSearching(true)
      setSearchError(null)
      try {
        setHits(await api.searchMemory(q, { limit: 12 }))
      } catch (error) {
        setSearchError(error)
        setHits(null)
      } finally {
        setSearching(false)
      }
    },
    [query]
  )

  const durability = tree.data?.durability
  const index = tree.data?.index

  return (
    <>
      <div className="page-head">
        <h2 className="page-head__title">Memory</h2>
        <p className="page-head__sub">
          What Annie has learned, in her own words. Plain markdown — {tree.data?.total_files ?? '…'}{' '}
          files, deliberately a small fraction of what she has seen.
        </p>
      </div>

      {durability && !durability.looks_like_volume && (
        <Panel title="Memory is not on a persistent volume">
          It lives at <code className="mono">{durability.root}</code>, inside the container, so a
          redeploy wipes it. It is mirrored to Firestore and restored on boot, but attaching a
          Railway Volume and pointing <code className="mono">ANNIE_MEMORY_DIR</code> at it is the
          durable setup.
        </Panel>
      )}

      <form className="row" onSubmit={runSearch} style={{ gap: 8, marginBottom: 16 }}>
        <input
          className="input"
          style={{ flex: 1 }}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search memory — or paste a contract address or creator wallet"
          aria-label="Search memory"
        />
        <button className="btn" type="submit" disabled={searching}>
          {searching ? 'Searching…' : 'Search'}
        </button>
        {hits && (
          <button
            className="btn btn--ghost"
            type="button"
            onClick={() => {
              setHits(null)
              setQuery('')
            }}
          >
            Clear
          </button>
        )}
      </form>

      {searchError && <ErrorState error={searchError} />}

      {hits && (
        <Panel
          title={`${hits.count} result${hits.count === 1 ? '' : 's'}`}
          meta={
            hits.count
              ? hits.matched_by === 'key'
                ? 'Matched an exact handle — a mint, wallet or ticker this file is about'
                : 'Matched on text relevance'
              : null
          }
        >
          {hits.count === 0 ? (
            <Empty
              title="No memory covers this"
              body="That is a real answer, not a failed search — it means she has not seen it or has not thought it worth keeping."
            />
          ) : (
            <div className="stack">
              {hits.hits.map((hit) => (
                <button
                  key={hit.path}
                  className="listrow"
                  onClick={() => {
                    setSelected(hit.path)
                    setHits(null)
                  }}
                >
                  <div className="stack" style={{ gap: 2, textAlign: 'left' }}>
                    <span className="row" style={{ gap: 8 }}>
                      <strong>{hit.title}</strong>
                      <Badge>{hit.section}</Badge>
                    </span>
                    <span className="faint mono" style={{ fontSize: 'var(--text-2xs)' }}>
                      {hit.path}
                    </span>
                    <span className="faint">{hit.snippet}</span>
                  </div>
                </button>
              ))}
            </div>
          )}
        </Panel>
      )}

      <Async state={tree}>
        {() => (
          <div className="memory-layout">
            <nav className="memory-tree">
              {sections.map((section) => (
                <div key={section.name} className="stack" style={{ gap: 2 }}>
                  <span className="memory-tree__section" title={section.description}>
                    {section.name} <span className="faint">({section.count})</span>
                  </span>
                  {section.files.length === 0 ? (
                    <span className="faint memory-tree__empty">nothing yet</span>
                  ) : (
                    section.files.slice(0, 40).map((file) => (
                      <button
                        key={file.path}
                        className={`memory-tree__file${selected === file.path ? ' is-active' : ''}`}
                        onClick={() => setSelected(file.path)}
                        title={file.summary}
                      >
                        <span className="truncate">{file.title}</span>
                      </button>
                    ))
                  )}
                  {section.files.length > 40 && (
                    <span className="faint memory-tree__empty">
                      +{section.files.length - 40} more — use search
                    </span>
                  )}
                </div>
              ))}

              {index && (
                <div className="memory-tree__foot faint">
                  {index.files} files · {index.keys} lookup keys ·{' '}
                  {index.fts ? 'full-text on' : 'full-text unavailable'}
                </div>
              )}
            </nav>

            <div className="memory-reader">
              {selected ? (
                <MemoryFile path={selected} onDeleted={() => {
                  setSelected(null)
                  tree.reload?.()
                }} />
              ) : (
                <Empty title="Pick a file" body="Or search above for a contract address, a wallet, or an idea." />
              )}
            </div>
          </div>
        )}
      </Async>
    </>
  )
}

/** One memory file, rendered. */
function MemoryFile({ path, onDeleted }) {
  const file = useApi(() => api.memoryFile(path), [path])
  const [busy, setBusy] = useState(false)

  const remove = async () => {
    if (!window.confirm(`Delete ${path}? Annie will no longer know this.`)) return
    setBusy(true)
    try {
      await api.deleteMemoryFile(path)
      onDeleted?.()
    } finally {
      setBusy(false)
    }
  }

  return (
    <Async state={file}>
      {(data) => (
        <Panel
          title={data.title}
          meta={
            <span className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
              <code className="mono">{data.path}</code>
              {data.updated && <span className="faint">updated {relative(data.updated)}</span>}
              {data.tags?.map((tag) => (
                <Badge key={tag}>{tag}</Badge>
              ))}
            </span>
          }
          actions={
            <button className="btn btn--ghost" onClick={remove} disabled={busy}>
              {busy ? 'Deleting…' : 'Delete'}
            </button>
          }
        >
          <Markdown text={data.body} />

          {data.keys?.length > 0 && (
            <div className="stack" style={{ gap: 4, marginTop: 16 }}>
              <span className="faint" style={{ fontSize: 'var(--text-2xs)' }}>
                Findable by — paste any of these into search or ask Annie about them:
              </span>
              <div className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
                {data.keys.map((key) => (
                  <code key={key} className="mono chip">
                    {key}
                  </code>
                ))}
              </div>
            </div>
          )}
        </Panel>
      )}
    </Async>
  )
}
