import { Link } from 'react-router-dom'

import { api } from '../api/client.js'
import { useApi } from '../api/useApi.js'
import { Async, Badge, Empty, Markdown, Panel } from '../components/primitives.jsx'
import { count, relative, usd } from '../lib/format.js'

/**
 * Today — what Annie makes of the market right now.
 *
 * This replaced a dashboard that led with counts: tokens collected, qualified
 * by tier, this period against the last. That was the right front page for a
 * system whose product was a database. It is the wrong one now — the product
 * is her judgement, and a count of qualified tokens measures how much raw
 * material passed through, not what anyone learned from it.
 *
 * Ordered by what should change a decision:
 *
 * 1. Whether the pipeline is working at all. Every judgement below is
 *    worthless while nothing is arriving, so it cannot sit at the bottom.
 * 2. Her latest read, in her words.
 * 3. What she thinks is working, verbatim from her notebook.
 * 4. What moved, and who she is watching.
 * 5. The statistical backing, so a claim can be checked rather than taken.
 *
 * Scale — 16,000 launches a day, almost all discarded — is one quiet line at
 * the bottom. It is context for how much filtering happened, not a headline.
 */
export default function Today() {
  const state = useApi(() => api.today(24), [])

  return (
    <Async state={state} rows={8}>
      {(d) => (
        <>
          <div className="page-head">
            <h2 className="page-head__title">Today</h2>
            <p className="page-head__sub">
              What Annie makes of the market right now — her read, not a count of what
              went past.
            </p>
          </div>

          {d.pipeline.state !== 'healthy' && <PipelineWarning pipeline={d.pipeline} />}

          <LatestRead read={d.latest_read} pipelineState={d.pipeline.state} />

          <div className="today-split">
            <div className="stack gap-5">
              <CoreFile
                title="What's working right now"
                file={d.whats_working}
                empty="She has not found an edge yet. This fills in once enough tokens have cleared a tier for a pattern to show."
              />
              <Movers movers={d.movers} hours={d.window_hours} />
            </div>

            <div className="stack gap-5">
              <Watching watching={d.watching} />
              <Signals signals={d.signals} />
              <RecentThinking items={d.recent_thinking} />
            </div>
          </div>

          <div className="today-split" style={{ marginTop: 'var(--space-5)' }}>
            <CoreFile
              title="Her market model"
              file={d.market_model}
              empty="Nothing observed yet — the model fills in after the first full cycles."
            />
            <CoreFile
              title="Open questions"
              file={d.open_questions}
              empty="Nothing open. She records questions here rather than letting an unsure pattern drift into the market model."
            />
          </div>

          <Scale scale={d.scale} />
        </>
      )}
    </Async>
  )
}

/** Nothing below is trustworthy while nothing is arriving. */
function PipelineWarning({ pipeline }) {
  return (
    <Panel
      title="Nothing is coming through"
      meta={<Badge status={pipeline.state === 'warming_up' ? 'new' : 'alert'}>
        {pipeline.state.replace(/_/g, ' ')}
      </Badge>}
    >
      <p style={{ margin: '0 0 10px' }}>{pipeline.headline}</p>
      {pipeline.what_to_check?.length > 0 && (
        <div className="stack" style={{ gap: 4 }}>
          {pipeline.what_to_check.map((action, i) => (
            <div key={i} className="row" style={{ gap: 8, alignItems: 'flex-start' }}>
              <span className="faint">·</span>
              <span style={{ fontSize: 'var(--text-sm)' }}>{action}</span>
            </div>
          ))}
        </div>
      )}
      <p className="faint" style={{ fontSize: 'var(--text-2xs)', marginTop: 12, marginBottom: 0 }}>
        More detail on <Link to="/health">System Health</Link>.
      </p>
    </Panel>
  )
}

/**
 * The headline from her last cycle — the single most important thing on the
 * page, so it gets the most room and the largest type.
 */
function LatestRead({ read, pipelineState }) {
  if (!read.headline) {
    return (
      <Panel title="Her latest read">
        <Empty
          title={pipelineState === 'healthy' ? 'No cycle has run yet' : 'Nothing to read yet'}
          body={
            pipelineState === 'healthy'
              ? 'The cycle runs at 00:00, 06:00, 12:00 and 18:00 WAT. Trigger one now from System Health if you do not want to wait.'
              : 'She writes this at the end of each cycle, once there is something to write about.'
          }
        />
      </Panel>
    )
  }

  return (
    <Panel
      title="Her latest read"
      meta={read.at ? `written ${relative(read.at)}` : null}
      actions={
        <Link to="/annie" className="btn btn--annie">
          Ask her about it
        </Link>
      }
    >
      <p className="today-headline">{read.headline}</p>

      {read.edits?.length > 0 && (
        <div className="stack" style={{ gap: 4, marginTop: 16 }}>
          <span className="faint" style={{ fontSize: 'var(--text-2xs)' }}>
            What she changed her mind about
          </span>
          <div className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
            {read.edits.map((edit) => (
              <Link key={edit.path} to="/memory" className="chip mono">
                {edit.path} <span className="faint">{edit.op}</span>
              </Link>
            ))}
          </div>
        </div>
      )}
    </Panel>
  )
}

/** One core memory file, rendered as the prose it is. */
function CoreFile({ title, file, empty }) {
  return (
    <Panel
      title={title}
      meta={
        file ? (
          <Link to="/memory" className="mono" style={{ fontSize: 'var(--text-2xs)' }}>
            {file.path}
          </Link>
        ) : null
      }
    >
      {file && file.body ? (
        <>
          <Markdown text={file.body} />
          {file.truncated && (
            <p style={{ marginTop: 12, marginBottom: 0 }}>
              <Link to="/memory" style={{ fontSize: 'var(--text-xs)' }}>
                Read the rest →
              </Link>
            </p>
          )}
        </>
      ) : (
        <Empty title="Nothing written yet" body={empty} />
      )}
    </Panel>
  )
}

/** What actually moved. The evidence behind everything above. */
function Movers({ movers, hours }) {
  return (
    <Panel
      title="What moved"
      meta={`last ${hours}h`}
      actions={
        <Link to="/movers" className="btn btn--ghost">
          All
        </Link>
      }
      flush={movers.length > 0}
    >
      {movers.length === 0 ? (
        <Empty
          title="Nothing moved"
          body="Launches are sighted continuously; a token appears here once it actually trades."
        />
      ) : (
        <div className="table-wrap">
          <table className="table table--responsive">
            <thead>
              <tr>
                <th>Token</th>
                <th className="num">Peak</th>
                <th className="num">Now</th>
                <th>Launchpad</th>
              </tr>
            </thead>
            <tbody>
              {movers.map((m) => (
                <tr key={m.mint}>
                  <td className="primary" data-label="Token">
                    <Link to={`/tokens/${m.mint}`} className="stack" style={{ gap: 0 }}>
                      <strong>{m.symbol || m.name || m.mint.slice(0, 8)}</strong>
                      {m.tier && (
                        <span className="faint" style={{ fontSize: 'var(--text-2xs)' }}>
                          cleared {usd(m.tier)}
                        </span>
                      )}
                    </Link>
                  </td>
                  <td className="num" data-label="Peak">
                    {usd(m.peak_market_cap)}
                  </td>
                  <td className="num" data-label="Now">
                    {usd(m.market_cap)}
                    {m.round_tripped && (
                      <span className="faint" style={{ fontSize: 'var(--text-2xs)' }}>
                        {' '}
                        round-tripped
                      </span>
                    )}
                  </td>
                  <td data-label="Launchpad">{m.launchpad || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  )
}

/** Who and what she has decided to follow. */
function Watching({ watching }) {
  const nothing = !watching.creators?.length && !watching.narratives?.length
  return (
    <Panel
      title="What she's watching"
      actions={
        <Link to="/creators" className="btn btn--ghost">
          Creators
        </Link>
      }
    >
      {nothing ? (
        <Empty
          title="Nothing tracked yet"
          body="A wallet earns tracking by launching a lot or by producing a winner. Neither has happened yet."
        />
      ) : (
        <div className="stack gap-4">
          {watching.narratives?.length > 0 && (
            <div className="stack" style={{ gap: 6 }}>
              <span className="faint" style={{ fontSize: 'var(--text-2xs)' }}>
                Narratives running
              </span>
              <div className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
                {watching.narratives.map((n) => (
                  <Badge key={n} status="rising" variant="outline">
                    {n}
                  </Badge>
                ))}
              </div>
            </div>
          )}

          {watching.creators?.length > 0 && (
            <div className="stack" style={{ gap: 6 }}>
              <span className="faint" style={{ fontSize: 'var(--text-2xs)' }}>
                Creators tracked
              </span>
              {watching.creators.map((c) => (
                <Link
                  key={c.wallet}
                  to={`/creators/${c.wallet}`}
                  className="row between gap-3"
                  style={{ fontSize: 'var(--text-xs)' }}
                >
                  <code className="mono truncate">{c.wallet.slice(0, 10)}…</code>
                  <span className="faint mono" style={{ flexShrink: 0 }}>
                    {count(c.launches)} launches · {count(c.winners)} winners
                  </span>
                </Link>
              ))}
            </div>
          )}
        </div>
      )}
    </Panel>
  )
}

/** The statistics behind her claims, so they can be checked. */
function Signals({ signals }) {
  return (
    <Panel
      title="What the numbers say"
      meta="over tokens that cleared a tier"
      actions={
        <Link to="/signals" className="btn btn--ghost">
          All
        </Link>
      }
    >
      {signals.length === 0 ? (
        <Empty
          title="Nothing significant yet"
          body="Signals need about 20 qualified tokens in a window before anything clears the statistical bar. Until then there is data but no finding."
        />
      ) : (
        <div className="stack" style={{ gap: 8 }}>
          {signals.map((s) => (
            <Link
              key={s.slug}
              to={`/signals/${s.slug}`}
              className="row between gap-3"
              style={{ fontSize: 'var(--text-sm)' }}
            >
              <span className="truncate">{s.name}</span>
              <span className="row gap-2" style={{ flexShrink: 0 }}>
                <span className="mono faint" style={{ fontSize: 'var(--text-2xs)' }}>
                  {s.recent_count}/{s.recent_total}
                </span>
                <Badge status={s.status} dot />
              </span>
            </Link>
          ))}
        </div>
      )}
    </Panel>
  )
}

/** What she has actually been thinking about lately. */
function RecentThinking({ items }) {
  if (!items?.length) return null
  return (
    <Panel
      title="Recently revised"
      actions={
        <Link to="/memory" className="btn btn--ghost">
          Notebook
        </Link>
      }
    >
      <div className="stack" style={{ gap: 10 }}>
        {items.map((item) => (
          <Link key={item.path} to="/memory" className="stack" style={{ gap: 2 }}>
            <span className="row gap-2">
              <strong style={{ fontSize: 'var(--text-sm)' }}>{item.title}</strong>
              <Badge>{item.section}</Badge>
            </span>
            <span className="faint" style={{ fontSize: 'var(--text-2xs)' }}>
              {item.summary}
            </span>
          </Link>
        ))}
      </div>
    </Panel>
  )
}

/**
 * Scale, deliberately at the bottom and deliberately quiet.
 *
 * The old dashboard led with these. They belong here: the interesting number
 * is not how many launches went past, it is how few survived the filtering.
 */
function Scale({ scale }) {
  return (
    <p className="today-scale faint">
      Seen {count(scale.seen_24h)} launches in 24h · holding {count(scale.held)} ·{' '}
      {count(scale.qualified_24h)} cleared a tier · tracking {count(scale.creators_tracked)}{' '}
      creators · {count(scale.memory_files)} memory files. Almost everything she sees is
      discarded within 48 hours — the filtering is the point.
    </p>
  )
}
