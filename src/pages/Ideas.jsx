import { useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../api/client.js'
import { useApi } from '../api/useApi.js'
import { Async, Badge, Empty, ErrorState, Panel } from '../components/primitives.jsx'
import { usd } from '../lib/format.js'

/**
 * Ideas — the thing all the watching is for.
 *
 * This is the only page that spends money, and it does so only when asked,
 * which is why there is a button rather than a feed: an idea nobody
 * requested is spend with no reader.
 *
 * The grounding panel below the button is the honest half. Every idea comes
 * back labelled `observed`, `inferred` or `speculative`, and on a deployment
 * with no history that label will be `speculative` across the board — which
 * is the correct answer, not a broken one. Showing what the generation would
 * be grounded in *before* you spend anything is how you tell those apart.
 */

const GROUNDING = {
  observed: {
    status: 'verified',
    note: 'Directly supported by what she is seeing right now.',
  },
  inferred: {
    status: 'stable',
    note: 'A reasonable step from the data, with a gap she is naming.',
  },
  speculative: {
    status: 'new',
    note: 'A hunch. Nothing in memory backs this one.',
  },
}

export default function Ideas() {
  const [brief, setBrief] = useState('')
  const [count, setCount] = useState(3)
  const [result, setResult] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [saved, setSaved] = useState(null)

  const context = useApi(() => api.ideaContext(''), [])

  const generate = async () => {
    setBusy(true)
    setError(null)
    setSaved(null)
    try {
      setResult(await api.generateIdeas({ brief: brief.trim(), count }))
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  const keep = async () => {
    try {
      const response = await api.keepIdeas({ payload: result, note: brief.trim() })
      setSaved(response.path)
    } catch (err) {
      setError(err)
    }
  }

  const thin = (context.data?.movers?.length ?? 0) === 0

  return (
    <>
      <div className="page-head">
        <h2 className="page-head__title">Ideas</h2>
        <p className="page-head__sub">
          A launch idea grounded in what is winning now, what her notebook says has
          worked, and what is already crowded. Generated on request — nothing runs this
          on a schedule.
        </p>
      </div>

      <Panel
        title="Ask for an idea"
        meta="the only page here that costs a model call"
      >
        <div className="stack gap-4">
          <label className="stack" style={{ gap: 4 }}>
            <span className="faint" style={{ fontSize: 'var(--text-2xs)' }}>
              Steer it, optionally
            </span>
            <input
              className="input"
              value={brief}
              onChange={(e) => setBrief(e.target.value)}
              placeholder="something in the AI space · low effort · aimed at $250k not $1M"
              onKeyDown={(e) => e.key === 'Enter' && !busy && generate()}
            />
            <span className="faint" style={{ fontSize: 'var(--text-2xs)' }}>
              A steer is context, not permission to contradict the evidence — an idea
              that matches your brief but is unsupported still comes back marked
              speculative.
            </span>
          </label>

          <div className="row gap-3 wrap" style={{ alignItems: 'center' }}>
            <div className="segmented" role="group" aria-label="How many ideas">
              {[1, 2, 3, 4].map((n) => (
                <button
                  key={n}
                  className={n === count ? 'is-active' : ''}
                  onClick={() => setCount(n)}
                >
                  {n}
                </button>
              ))}
            </div>
            <button className="btn btn--primary" onClick={generate} disabled={busy}>
              {busy ? 'Thinking…' : 'Generate'}
            </button>
          </div>
        </div>
      </Panel>

      {error && <ErrorState error={error} />}

      {thin && !result && (
        <Panel title="There is not much to ground an idea in yet">
          She will still answer, but with nothing in the ledger and an empty notebook
          every idea will be a hunch rather than a read. Worth waiting until something
          has actually cleared a tier — check <Link to="/health">System Health</Link> for
          whether the stream is arriving.
        </Panel>
      )}

      {result && <Result result={result} onKeep={keep} saved={saved} />}

      <GroundedIn context={context} />
    </>
  )
}

function Result({ result, onKeep, saved }) {
  return (
    <>
      <Panel
        title="Her read"
        actions={
          saved ? (
            <span className="faint" style={{ fontSize: 'var(--text-xs)' }}>
              Saved to <code className="mono">{saved}</code>
            </span>
          ) : (
            <button className="btn btn--ghost" onClick={onKeep}>
              Keep these
            </button>
          )
        }
      >
        <p style={{ margin: 0 }}>{result.read_of_the_market}</p>

        {result.avoid?.length > 0 && (
          <div className="stack" style={{ gap: 6, marginTop: 16 }}>
            <span className="faint" style={{ fontSize: 'var(--text-2xs)' }}>
              Saturated or rolling over — she will not propose these
            </span>
            <div className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
              {result.avoid.map((a) => (
                <Badge key={a} status="declining" variant="outline">
                  {a}
                </Badge>
              ))}
            </div>
          </div>
        )}

        <p className="faint" style={{ fontSize: 'var(--text-2xs)', marginTop: 16, marginBottom: 0 }}>
          Grounded in {result.grounded_in?.movers ?? 0} movers,{' '}
          {result.grounded_in?.signals ?? 0} signals and{' '}
          {result.grounded_in?.memories?.length ?? 0} memory file(s). Keeping these writes
          them to the playbook, where later ideas can build on them.
        </p>
      </Panel>

      <div className="idea-grid">
        {(result.ideas ?? []).map((idea, i) => (
          <IdeaCard key={i} idea={idea} />
        ))}
      </div>
    </>
  )
}

function IdeaCard({ idea }) {
  const grounding = GROUNDING[idea.grounding] ?? GROUNDING.speculative
  return (
    <article className="ideacard">
      <header className="ideacard__head">
        <div className="stack" style={{ gap: 2, minWidth: 0 }}>
          <strong className="ideacard__name">{idea.name}</strong>
          <code className="mono ideacard__ticker">${idea.ticker}</code>
        </div>
        <Badge status={grounding.status} variant="outline">
          {idea.grounding}
        </Badge>
      </header>

      <p className="ideacard__angle">{idea.angle}</p>

      <dl className="ideacard__detail">
        <dt>Why now</dt>
        <dd>{idea.why_now}</dd>
        <dt>Evidence</dt>
        <dd>{idea.evidence}</dd>
        <dt>Risk</dt>
        <dd>{idea.risk}</dd>
      </dl>

      <footer className="ideacard__foot faint">{grounding.note}</footer>
    </article>
  )
}

/**
 * What a generation would be built on, shown before you spend anything.
 *
 * On an empty deployment this is the page's most useful panel: it shows the
 * grounding is empty, which explains why the ideas would be hunches.
 */
function GroundedIn({ context }) {
  return (
    <Panel title="What she would build on" meta="free — this costs nothing to look at">
      <Async
        state={context}
        empty={<Empty title="Nothing to ground an idea in yet" />}
      >
        {(c) => (
          <div className="today-split">
            <div className="stack gap-4">
              <div className="stack" style={{ gap: 6 }}>
                <span className="faint" style={{ fontSize: 'var(--text-2xs)' }}>
                  Winning characteristics
                </span>
                {c.winning_characteristics?.length ? (
                  c.winning_characteristics.slice(0, 8).map((s) => (
                    <div
                      key={s.slug}
                      className="row between gap-3"
                      style={{ fontSize: 'var(--text-xs)' }}
                    >
                      <span className="truncate">{s.name}</span>
                      <span className="mono faint" style={{ flexShrink: 0 }}>
                        {s.recent_count}/{s.recent_total}
                      </span>
                    </div>
                  ))
                ) : (
                  <span className="faint" style={{ fontSize: 'var(--text-xs)' }}>
                    None yet — needs a cohort before anything is significant.
                  </span>
                )}
              </div>

              {c.saturated?.length > 0 && (
                <div className="stack" style={{ gap: 6 }}>
                  <span className="faint" style={{ fontSize: 'var(--text-2xs)' }}>
                    Already crowded
                  </span>
                  <div className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
                    {c.saturated.map((s) => (
                      <Badge key={s.slug} status="declining" variant="plain">
                        {s.name}
                      </Badge>
                    ))}
                  </div>
                </div>
              )}
            </div>

            <div className="stack gap-4">
              <div className="stack" style={{ gap: 6 }}>
                <span className="faint" style={{ fontSize: 'var(--text-2xs)' }}>
                  Recent movers
                </span>
                {c.movers?.length ? (
                  c.movers.slice(0, 8).map((m) => (
                    <div
                      key={m.mint}
                      className="row between gap-3"
                      style={{ fontSize: 'var(--text-xs)' }}
                    >
                      <span className="truncate">{m.symbol || m.name || m.mint.slice(0, 8)}</span>
                      <span className="mono faint" style={{ flexShrink: 0 }}>
                        {usd(m.peak_market_cap)}
                      </span>
                    </div>
                  ))
                ) : (
                  <span className="faint" style={{ fontSize: 'var(--text-xs)' }}>
                    Nothing has moved yet.
                  </span>
                )}
              </div>

              <div className="stack" style={{ gap: 6 }}>
                <span className="faint" style={{ fontSize: 'var(--text-2xs)' }}>
                  Memory she would read
                </span>
                {c.memories_that_would_be_used?.length ? (
                  c.memories_that_would_be_used.map((h) => (
                    <Link
                      key={h.path}
                      to="/memory"
                      className="mono truncate"
                      style={{ fontSize: 'var(--text-2xs)' }}
                    >
                      {h.path}
                    </Link>
                  ))
                ) : (
                  <span className="faint" style={{ fontSize: 'var(--text-xs)' }}>
                    Her notebook has nothing relevant yet.
                  </span>
                )}
              </div>
            </div>
          </div>
        )}
      </Async>
    </Panel>
  )
}
