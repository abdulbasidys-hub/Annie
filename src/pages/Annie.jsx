import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'

import { api, ApiError } from '../api/client.js'
import { Badge, ErrorState } from '../components/primitives.jsx'
import { count, relative } from '../lib/format.js'

const SUGGESTIONS = [
  'What changed today?',
  'What are the strongest current trends?',
  'What separates $1M+ tokens from $100k ones?',
  'Which launchpads are gaining momentum?',
  'Find me something interesting.',
]

/**
 * Annie's conversational interface (§62).
 *
 * This is the only warm surface in the app, and the only place the accent
 * colour appears. Everything else stays monochrome so that her presence reads
 * as a distinct thing rather than as more chrome.
 *
 * Two decisions worth defending:
 *
 * Citations render *beneath every message*, always, never behind a toggle.
 * §33 requires important conclusions to show their support; putting that
 * behind a disclosure makes "unsupported" the default appearance of an
 * assertion, which is exactly backwards for a system whose job is to
 * distinguish evidence from speculation.
 *
 * The claim-type badge is on the message, not in a tooltip. If Annie is
 * speculating, the reader should see that at the same instant they read the
 * sentence — not after they think to check.
 */
export default function AnniePage() {
  const [messages, setMessages] = useState([])
  const [draft, setDraft] = useState('')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState(null)
  const [conversationId, setConversationId] = useState(null)

  const logRef = useRef(null)
  const inputRef = useRef(null)

  useEffect(() => {
    const el = logRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages, pending])

  async function send(text) {
    const trimmed = text.trim()
    if (!trimmed || pending) return

    setError(null)
    setDraft('')
    setMessages((prev) => [
      ...prev,
      { id: `local-${Date.now()}`, role: 'user', content: trimmed, created_at: new Date().toISOString() },
    ])
    setPending(true)

    try {
      const reply = await api.chat({ message: trimmed, conversation_id: conversationId })
      if (reply?.conversation_id) setConversationId(reply.conversation_id)
      setMessages((prev) => [...prev, reply.message])
    } catch (err) {
      setError(err instanceof ApiError ? err : new ApiError(String(err), { status: 0 }))
    } finally {
      setPending(false)
      inputRef.current?.focus()
    }
  }

  function onKeyDown(event) {
    // Enter sends, Shift+Enter newlines. Research questions are usually one
    // line; the two-key alternative would tax the common case.
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      send(draft)
    }
  }

  return (
    <div className="chat">
      <header className="chat__head">
        <span className="chat__avatar">An</span>
        <div className="stack">
          <span className="chat__name">Annie</span>
          <span className="chat__tagline">Reads the data. Will argue with you about it.</span>
        </div>
      </header>

      <div className="chat__log" ref={logRef}>
        {messages.length === 0 && !pending && (
          <div className="state" style={{ margin: 'auto 0' }}>
            <span className="state__title">Ask me something</span>
            <p className="state__body">
              I answer from the database and the trend engine — not from memory. If I don’t
              have the numbers, I’ll say so rather than guess.
            </p>
            <div className="chat__suggestions">
              {SUGGESTIONS.map((s) => (
                <button key={s} className="suggestion" onClick={() => send(s)}>
                  {s}
                </button>
              ))}
            </div>
          </div>
        )}

        {messages.map((msg) => (
          <Message key={msg.id} message={msg} />
        ))}

        {pending && (
          <div className="msg msg--annie">
            <div className="msg__bubble">
              <span className="typing" aria-label="Annie is working">
                <span /><span /><span />
              </span>
            </div>
          </div>
        )}

        {error && (
          <div style={{ maxWidth: '68ch' }}>
            <ErrorState error={error} />
          </div>
        )}
      </div>

      <div className="chat__compose">
        <textarea
          ref={inputRef}
          className="chat__input"
          placeholder="Ask about trends, launchpads, creators, narratives…"
          value={draft}
          rows={1}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={onKeyDown}
          disabled={pending}
          aria-label="Message Annie"
        />
        <button
          className="btn btn--annie"
          onClick={() => send(draft)}
          disabled={pending || !draft.trim()}
        >
          Send
        </button>
      </div>
    </div>
  )
}

/* -------------------------------------------------------------------------- */

function Message({ message }) {
  const isUser = message.role === 'user'
  return (
    <article className={`msg ${isUser ? 'msg--user' : 'msg--annie'}`} data-claim={message.claim_type}>
      <div className="msg__bubble">{message.content}</div>

      {!isUser && (message.claim_type || message.confidence) && (
        <div className="msg__meta">
          {message.claim_type && (
            <Badge
              status={message.claim_type === 'fact' ? 'verified' : 'pending'}
              variant="outline"
              title={CLAIM_HELP[message.claim_type]}
            >
              {message.claim_type}
            </Badge>
          )}
          {message.confidence && <span>confidence: {message.confidence}</span>}
          {message.latency_ms && <span className="faint">{Math.round(message.latency_ms / 100) / 10}s</span>}
        </div>
      )}

      {message.citations?.length > 0 && (
        <div className="citations">
          {message.citations.map((c, i) => (
            <Citation key={i} citation={c} />
          ))}
        </div>
      )}

      {message.tool_calls?.length > 0 && (
        <details>
          <summary className="faint" style={{ fontSize: 'var(--text-2xs)', cursor: 'pointer' }}>
            {message.tool_calls.length} tool call{message.tool_calls.length === 1 ? '' : 's'}
          </summary>
          <div className="tool-trace" style={{ marginTop: 'var(--space-1)' }}>
            {message.tool_calls.map((call, i) => (
              <div key={i} className={`tool-trace__row ${call.succeeded ? '' : 'is-failed'}`}>
                <span>{call.succeeded ? '✓' : '✗'}</span>
                <span>{call.tool}</span>
                <span className="faint">
                  {call.result_rows !== null && call.result_rows !== undefined
                    ? `${count(call.result_rows)} rows`
                    : call.error_message || ''}
                </span>
              </div>
            ))}
          </div>
        </details>
      )}
    </article>
  )
}

const CLAIM_HELP = {
  fact: 'Measured and present in the database.',
  inference: 'Follows from the data, with a stated step of reasoning.',
  hypothesis: 'A candidate explanation that has not been tested.',
  speculation: 'Plausible but unsupported by the data.',
}

function Citation({ citation }) {
  const href =
    citation.kind === 'token' ? `/tokens/${citation.id}`
    : citation.kind === 'trend' ? `/trends/${citation.id}`
    : citation.kind === 'launchpad' ? `/launchpads/${citation.id}`
    : citation.kind === 'creator' ? `/creators/${citation.id}`
    : null

  const label = (
    <>
      <span className="citation__label">{citation.label}</span>
      {citation.sample && (
        <span className="faint mono" style={{ fontSize: 'var(--text-2xs)' }}>
          {count(citation.sample.count)}/{count(citation.sample.total)}
        </span>
      )}
    </>
  )

  return (
    <div className="citation">
      <span className="citation__kind">{citation.kind}</span>
      {href ? <Link to={href} className="row gap-2">{label}</Link> : <span className="row gap-2">{label}</span>}
    </div>
  )
}
