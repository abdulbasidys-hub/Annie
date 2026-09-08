import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { api } from '../api/client.js'
import { useApi } from '../api/useApi.js'
import { Async, Badge, Empty, ErrorState, Markdown, Panel } from '../components/primitives.jsx'
import { relative } from '../lib/format.js'

/**
 * Memory — Annie's notebook.
 *
 * Cards, not a table. Each memory is a piece of writing with a point, and a
 * row of columns hides exactly the part worth seeing: what it actually says.
 * So a card shows its section, age, title and an excerpt of the prose, and
 * opening one gives you the whole thing — readable first, editable in place
 * when you want to correct her.
 *
 * Search sits above everything because it is the fastest route in. Pasting a
 * contract address or a creator wallet resolves through the exact-key index
 * straight to the file about it.
 *
 * Everything here is served from local files and SQLite, so the page is free
 * to open and free to browse.
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

/** Files that are regenerated, so a hand edit will eventually be overwritten. */
const REGENERATED = {
  'core/watchlist.md':
    'Rewritten from the ledger every cycle — anything you change here will be overwritten.',
  daily: 'The counts at the top of a daily log are rewritten at the end of the day.',
}

function regenerationWarning(file) {
  return REGENERATED[file.path] || REGENERATED[file.section] || null
}

export default function Memory() {
  const tree = useApi(() => api.memoryTree(), [])
  const [openPath, setOpenPath] = useState(null)
  const [creating, setCreating] = useState(false)
  const [query, setQuery] = useState('')
  const [hits, setHits] = useState(null)
  const [searching, setSearching] = useState(false)
  const [searchError, setSearchError] = useState(null)
  const [section, setSection] = useState('all')

  const sections = useMemo(() => {
    const found = tree.data?.sections ?? []
    const byName = Object.fromEntries(found.map((s) => [s.name, s]))
    return SECTION_ORDER.map((name) => byName[name]).filter(Boolean)
  }, [tree.data])

  const visible = useMemo(() => {
    const chosen = section === 'all' ? sections : sections.filter((s) => s.name === section)
    return chosen
      .flatMap((s) => s.files.map((f) => ({ ...f, section: s.name })))
      .sort((a, b) => (b.updated || '').localeCompare(a.updated || ''))
  }, [sections, section])

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
        setHits(await api.searchMemory(q, { limit: 24 }))
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
          What Annie has learned, in her own words — {tree.data?.total_files ?? '…'} files,
          deliberately a small fraction of what she has seen. Open one to read it, or edit it
          to correct her.
        </p>
      </div>

      {durability && !durability.looks_like_volume && (
        <Panel title="Memory is not on a persistent volume">
          It lives at <code className="mono">{durability.root}</code>, inside the container, so
          a redeploy wipes it. It is mirrored to Firestore and restored on boot, but attaching
          a Railway Volume and pointing <code className="mono">ANNIE_MEMORY_DIR</code> at it is
          the durable setup.
        </Panel>
      )}

      <form className="row memory-toolbar" onSubmit={runSearch}>
        <input
          className="input"
          style={{ flex: 1, minWidth: 0 }}
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
        <button className="btn btn--primary" type="button" onClick={() => setCreating(true)}>
          New memory
        </button>
      </form>

      {searchError && <ErrorState error={searchError} />}

      {!hits && (
        <div className="segmented memory-filter" role="tablist">
          <button
            role="tab"
            aria-selected={section === 'all'}
            className={section === 'all' ? 'is-active' : ''}
            onClick={() => setSection('all')}
          >
            All <span className="faint">{tree.data?.total_files ?? 0}</span>
          </button>
          {sections.map((s) => (
            <button
              key={s.name}
              role="tab"
              aria-selected={section === s.name}
              className={section === s.name ? 'is-active' : ''}
              onClick={() => setSection(s.name)}
              title={s.description}
            >
              {s.name} <span className="faint">{s.count}</span>
            </button>
          ))}
        </div>
      )}

      {hits ? (
        hits.count === 0 ? (
          <Panel>
            <Empty
              title="No memory covers this"
              body="That is a real answer, not a failed search — it means she has not seen it, or has not thought it worth keeping."
            />
          </Panel>
        ) : (
          <>
            <p className="faint memory-resultnote">
              {hits.count} result{hits.count === 1 ? '' : 's'} ·{' '}
              {hits.matched_by === 'key'
                ? 'matched an exact handle — a mint, wallet or ticker these files are about'
                : 'matched on text relevance'}
            </p>
            <div className="memory-grid">
              {hits.hits.map((hit) => (
                <MemoryCard
                  key={hit.path}
                  file={{
                    path: hit.path,
                    title: hit.title,
                    section: hit.section,
                    summary: hit.snippet,
                  }}
                  onOpen={() => setOpenPath(hit.path)}
                />
              ))}
            </div>
          </>
        )
      ) : (
        <Async state={tree}>
          {() =>
            visible.length === 0 ? (
              <Panel>
                <Empty
                  title="Nothing here yet"
                  body="Annie writes to her notebook every cycle. You can also create a file yourself, or ask her to in Telegram or Discord."
                />
              </Panel>
            ) : (
              <div className="memory-grid">
                {visible.map((file) => (
                  <MemoryCard key={file.path} file={file} onOpen={() => setOpenPath(file.path)} />
                ))}
              </div>
            )
          }
        </Async>
      )}

      {index && (
        <p className="faint memory-indexnote">
          {index.files} files · {index.keys} lookup keys ·{' '}
          {index.fts ? 'full-text search on' : 'full-text unavailable, using LIKE matching'}
        </p>
      )}

      {openPath && (
        <MemoryModal
          path={openPath}
          onClose={() => setOpenPath(null)}
          onChanged={() => {
            setOpenPath(null)
            setHits(null)
            tree.reload?.()
          }}
        />
      )}

      {creating && (
        <NewMemoryModal
          sections={sections}
          onClose={() => setCreating(false)}
          onCreated={(path) => {
            setCreating(false)
            tree.reload?.()
            setOpenPath(path)
          }}
        />
      )}
    </>
  )
}

/** One memory, as a card. The excerpt is the point — a title alone says nothing. */
function MemoryCard({ file, onOpen }) {
  const warning = regenerationWarning(file)
  return (
    <button className="memcard" onClick={onOpen} aria-label={`Open ${file.title}`}>
      <div className="memcard__head">
        <Badge>{file.section}</Badge>
        {file.updated && <span className="faint memcard__age">{relative(file.updated)}</span>}
      </div>
      <strong className="memcard__title">{file.title}</strong>
      <p className="memcard__excerpt">{file.summary || 'No content yet.'}</p>
      <div className="memcard__foot">
        <code className="mono truncate">{file.path}</code>
        {warning && (
          <span className="memcard__flag" title={warning}>
            auto
          </span>
        )}
      </div>
    </button>
  )
}

/**
 * The full memory, in a dialog. Read mode first, edit mode on request.
 *
 * Editing defaults to off because the common action is reading, and a
 * textarea sitting open over Annie's prose invites accidental changes to a
 * file the scheduled cycle is also writing to.
 */
function MemoryModal({ path, onClose, onChanged }) {
  const file = useApi(() => api.memoryFile(path), [path])
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    if (file.data && !editing) setDraft(file.data.body ?? '')
  }, [file.data, editing])

  const save = async () => {
    setBusy(true)
    setError(null)
    try {
      await api.writeMemoryFile({
        path,
        body: draft,
        title: file.data?.title,
        tags: file.data?.tags,
        keys: file.data?.keys,
        importance: file.data?.importance,
      })
      onChanged()
    } catch (err) {
      setError(err)
      setBusy(false)
    }
  }

  const remove = async () => {
    if (!window.confirm(`Delete ${path}? Annie will no longer know this.`)) return
    setBusy(true)
    setError(null)
    try {
      await api.deleteMemoryFile(path)
      onChanged()
    } catch (err) {
      setError(err)
      setBusy(false)
    }
  }

  const dirty = editing && draft !== (file.data?.body ?? '')
  const warning = file.data ? regenerationWarning(file.data) : null

  return (
    <Modal
      onClose={onClose}
      confirmClose={dirty ? 'Discard your unsaved changes?' : null}
      title={file.data?.title || 'Memory'}
      meta={
        <span className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
          <code className="mono">{path}</code>
          {file.data?.updated && (
            <span className="faint">updated {relative(file.data.updated)}</span>
          )}
          {file.data?.tags?.map((tag) => (
            <Badge key={tag}>{tag}</Badge>
          ))}
        </span>
      }
      actions={
        editing ? (
          <>
            <button
              className="btn btn--ghost"
              onClick={() => {
                setDraft(file.data?.body ?? '')
                setEditing(false)
              }}
              disabled={busy}
            >
              Cancel
            </button>
            <button className="btn btn--primary" onClick={save} disabled={busy || !dirty}>
              {busy ? 'Saving…' : 'Save'}
            </button>
          </>
        ) : (
          <>
            <button className="btn btn--ghost" onClick={remove} disabled={busy}>
              Delete
            </button>
            <button className="btn" onClick={() => setEditing(true)} disabled={busy}>
              Edit
            </button>
          </>
        )
      }
    >
      {error && <ErrorState error={error} />}
      <Async state={file}>
        {(data) => (
          <>
            {warning && <p className="modal__warn">{warning}</p>}

            {editing ? (
              <>
                <textarea
                  className="textarea modal__editor"
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  spellCheck={false}
                  aria-label="Memory content"
                />
                <p className="faint modal__hint">
                  Markdown. Headings with <code className="mono">##</code>, bullets with{' '}
                  <code className="mono">-</code>. Any contract address or wallet you write
                  here becomes searchable by itself.
                </p>
              </>
            ) : (
              <Markdown text={data.body} />
            )}

            {!editing && data.keys?.length > 0 && (
              <div className="stack" style={{ gap: 4, marginTop: 20 }}>
                <span className="faint" style={{ fontSize: 'var(--text-2xs)' }}>
                  Findable by — paste any of these into search, or ask Annie about them:
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
          </>
        )}
      </Async>
    </Modal>
  )
}

/** Create a memory by hand — the same thing asking Annie in chat does. */
function NewMemoryModal({ sections, onClose, onCreated }) {
  const [name, setName] = useState('')
  const [section, setSection] = useState('notes')
  const [body, setBody] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  const slug = name
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-|-$/g, '')
  const path = slug ? `${section}/${slug}.md` : ''

  const create = async () => {
    if (!path) return
    setBusy(true)
    setError(null)
    try {
      await api.writeMemoryFile({
        path,
        body: body.trim() || '_Created by hand. Nothing written yet._',
        title: name.trim(),
        importance: 0.6,
      })
      onCreated(path)
    } catch (err) {
      setError(err)
      setBusy(false)
    }
  }

  return (
    <Modal
      onClose={onClose}
      confirmClose={name || body ? 'Discard this new memory?' : null}
      title="New memory"
      meta={
        path ? <code className="mono">{path}</code> : <span className="faint">Give it a name</span>
      }
      actions={
        <>
          <button className="btn btn--ghost" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button className="btn btn--primary" onClick={create} disabled={busy || !path}>
            {busy ? 'Creating…' : 'Create'}
          </button>
        </>
      }
    >
      {error && <ErrorState error={error} />}
      <div className="stack" style={{ gap: 14 }}>
        <label className="stack" style={{ gap: 4 }}>
          <span className="faint" style={{ fontSize: 'var(--text-2xs)' }}>
            Name
          </span>
          <input
            className="input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Crowded narratives"
            autoFocus
          />
        </label>

        <label className="stack" style={{ gap: 4 }}>
          <span className="faint" style={{ fontSize: 'var(--text-2xs)' }}>
            Section
          </span>
          <select className="select" value={section} onChange={(e) => setSection(e.target.value)}>
            {sections.map((s) => (
              <option key={s.name} value={s.name}>
                {s.name}
              </option>
            ))}
          </select>
          <span className="faint" style={{ fontSize: 'var(--text-2xs)' }}>
            {sections.find((s) => s.name === section)?.description}
          </span>
        </label>

        <label className="stack" style={{ gap: 4 }}>
          <span className="faint" style={{ fontSize: 'var(--text-2xs)' }}>
            Content — optional, you can fill it in later
          </span>
          <textarea
            className="textarea"
            style={{ minHeight: 220 }}
            value={body}
            onChange={(e) => setBody(e.target.value)}
            placeholder="Markdown. Anything you write here, Annie reads as established."
            spellCheck={false}
          />
        </label>
      </div>
    </Modal>
  )
}

/**
 * A dialog.
 *
 * Escape and a backdrop click both close, and both route through the same
 * guard so neither can discard an unsaved edit by accident. Focus moves into
 * the dialog on open and body scroll is locked behind it — without the lock,
 * scrolling to the end of a long memory starts scrolling the page underneath
 * instead, which feels broken.
 */
function Modal({ title, meta, actions, children, onClose, confirmClose }) {
  const ref = useRef(null)

  const attemptClose = useCallback(() => {
    if (confirmClose && !window.confirm(confirmClose)) return
    onClose()
  }, [confirmClose, onClose])

  useEffect(() => {
    const onKey = (e) => {
      if (e.key === 'Escape') attemptClose()
    }
    document.addEventListener('keydown', onKey)
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    ref.current?.focus()
    return () => {
      document.removeEventListener('keydown', onKey)
      document.body.style.overflow = previousOverflow
    }
  }, [attemptClose])

  return (
    <div
      className="modal__backdrop"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) attemptClose()
      }}
    >
      <div className="modal" role="dialog" aria-modal="true" aria-label={title} tabIndex={-1} ref={ref}>
        <header className="modal__head">
          <div className="stack" style={{ gap: 2, minWidth: 0 }}>
            <h2 className="modal__title">{title}</h2>
            {meta && <span className="modal__meta">{meta}</span>}
          </div>
          <div className="modal__actions">
            {actions}
            <button
              className="btn btn--ghost btn--icon"
              onClick={attemptClose}
              aria-label="Close"
              title="Close"
            >
              ✕
            </button>
          </div>
        </header>
        <div className="modal__body">{children}</div>
      </div>
    </div>
  )
}
