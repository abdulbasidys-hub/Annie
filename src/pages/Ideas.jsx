import { useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../api/client.js'
import { useApi } from '../api/useApi.js'
import { Async, Badge, Empty, ErrorState, Panel } from '../components/primitives.jsx'
import { relative, usd } from '../lib/format.js'

/**
 * Ideas — the thing all the watching is for.
 *
 * Two ways an idea set arrives. Once a day, generated after the brief from
 * what moved over the preceding 24 hours, which is what this page shows on
 * arrival — "what should I launch" is worth answering before someone thinks
 * to ask. And on request, when you want to steer it.
 *
 * Every idea is launch-ready: name, ticker, the description copy and the
 * image, all fillable straight into a launchpad form. And every one carries
 * `observed` / `inferred` / `speculative`, so a hunch never arrives looking
 * like a finding. On a deployment with no history they will all be
 * speculative — which is correct, not broken, and the grounding panel at the
 * bottom is how you tell those apart before spending anything.
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
  const [requested, setRequested] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  const daily = useApi(() => api.latestIdeas('daily'), [])
  const context = useApi(() => api.ideaContext(''), [])

  const generate = async () => {
    setBusy(true)
    setError(null)
    try {
      setRequested(await api.generateIdeas({ brief: brief.trim(), count }))
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  const thin = (context.data?.movers?.length ?? 0) === 0
  const todays = daily.data?.ideas

  return (
    <>
      <div className="page-head">
        <h2 className="page-head__title">Ideas</h2>
        <p className="page-head__sub">
          Launch ideas grounded in what is winning now, what her notebook says has
          worked, and what is already crowded. Three arrive with the daily brief; ask
          any time for more.
        </p>
      </div>

      {/* Today's set first — it arrived on its own and is most likely what
          someone opening this page came for. */}
      <Async state={daily} rows={3}>
        {(d) =>
          d.ideas ? (
            <IdeaSet
              set={d.ideas}
              title="Today's ideas"
              meta={`from what moved yesterday · ${relative(d.ideas.generated_at)}`}
            />
          ) : (
            <Panel title="No daily ideas yet">
              <Empty
                title="Nothing generated yet"
                body="Three ideas are written after the daily brief at 00:00 WAT, grounded in what moved over the preceding 24 hours. They are skipped on a day when nothing moved — three ideas from an empty ledger would arrive looking exactly like grounded ones."
              />
            </Panel>
          )
        }
      </Async>

      <Panel title="Ask for more" meta="the only thing here that costs a model call">
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

      {thin && !requested && !todays && (
        <Panel title="There is not much to ground an idea in yet">
          She will still answer, but with nothing in the ledger and an empty notebook
          every idea will be a hunch rather than a read. Worth waiting until something
          has actually cleared a tier — check <Link to="/health">System Health</Link> for
          whether the stream is arriving.
        </Panel>
      )}

      {requested && <IdeaSet set={requested} title="What you asked for" meta="just now" />}

      <GroundedIn context={context} />
    </>
  )
}

function IdeaSet({ set, title, meta }) {
  return (
    <>
      <Panel
        title={title}
        meta={meta}
        actions={
          set.memory_path ? (
            <Link to="/memory" className="mono" style={{ fontSize: 'var(--text-2xs)' }}>
              {set.memory_path}
            </Link>
          ) : null
        }
      >
        <p style={{ margin: 0 }}>{set.read_of_the_market}</p>

        {set.avoid?.length > 0 && (
          <div className="stack" style={{ gap: 6, marginTop: 16 }}>
            <span className="faint" style={{ fontSize: 'var(--text-2xs)' }}>
              Saturated or rolling over — she will not propose these
            </span>
            <div className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
              {set.avoid.map((a) => (
                <Badge key={a} status="declining" variant="outline">
                  {a}
                </Badge>
              ))}
            </div>
          </div>
        )}

        {set.grounded_in && (
          <p
            className="faint"
            style={{ fontSize: 'var(--text-2xs)', marginTop: 16, marginBottom: 0 }}
          >
            Grounded in {set.grounded_in.movers ?? 0} movers, {set.grounded_in.signals ?? 0}{' '}
            signals and {set.grounded_in.memories?.length ?? 0} memory file(s). Written to
            the playbook automatically, so later ideas can build on it.
          </p>
        )}
      </Panel>

      <div className="idea-grid">
        {(set.ideas ?? []).map((idea, i) => (
          <IdeaCard key={`${idea.ticker}-${i}`} idea={idea} />
        ))}
      </div>
    </>
  )
}

/**
 * One idea, in the shape of the form you would fill in.
 *
 * Name, ticker, description and image sit at the top as copyable fields
 * because those four are what a launchpad actually asks for; the reasoning
 * is below them, where you read it once and then stop.
 */
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

      {idea.description && (
        <Field label="Description" hint="paste into the launchpad as-is">
          {idea.description}
        </Field>
      )}
      {idea.image && <Field label="Image">{idea.image}</Field>}

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

/** A launch-form field, with a copy button — this is meant to be used, not read. */
function Field({ label, hint, children }) {
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(String(children))
      setCopied(true)
      setTimeout(() => setCopied(false), 1400)
    } catch {
      /* clipboard blocked (insecure context, denied permission) — the text is
         selectable either way, so this is not worth surfacing as an error */
    }
  }
  return (
    <div className="ideafield">
      <div className="ideafield__head">
        <span className="ideafield__label">{label}</span>
        {hint && <span className="faint ideafield__hint">{hint}</span>}
        <button className="ideafield__copy" onClick={copy} type="button">
          {copied ? 'copied' : 'copy'}
        </button>
      </div>
      <p className="ideafield__value">{children}</p>
    </div>
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
      <Async state={context} empty={<Empty title="Nothing to ground an idea in yet" />}>
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
                      <span className="truncate">
                        {m.symbol || m.name || m.mint.slice(0, 8)}
                      </span>
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
