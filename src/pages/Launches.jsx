import { useState } from 'react'

import { api } from '../api/client.js'
import { useApi } from '../api/useApi.js'
import { Async, Badge, Empty } from '../components/primitives.jsx'
import { relative, usd } from '../lib/format.js'

/**
 * Our launches — the only tokens here tracked because we said so.
 *
 * Every other page shows what Annie found, filtered by whether it cleared a
 * bar. This shows what we did, and that filter is exactly wrong for it: a
 * launch of ours matters at $4,000 and it matters at zero, because what we
 * want from it is the post-mortem rather than a verdict.
 *
 * Registering a mint is the trigger for everything else — from that point it
 * is never pruned, it is re-priced ahead of the whole watchlist, and it gets
 * a written check-in every cycle. The record underneath accumulates rather
 * than being replaced, so after a few weeks you have the story of the launch
 * instead of its latest number.
 */

export default function Launches() {
  const [open, setOpen] = useState(null)
  const [adding, setAdding] = useState(false)
  const launches = useApi(() => api.launches(), [])

  return (
    <div className="stack gap-4">
      <div className="row between wrap gap-3">
        <div className="page-head">
          <h2 className="page-head__title">Our launches</h2>
          <p className="page-head__sub">
            Registered by hand and then exempt from every filter: never pruned, re-priced
            first, reviewed in writing each cycle whatever it is worth. You can also just
            tell Annie in Discord — “we launched this, &lt;CA&gt;” does the same thing.
          </p>
        </div>
        <button className="btn btn--primary" onClick={() => setAdding(true)}>
          Register a launch
        </button>
      </div>

      <Async state={launches}>
        {(data) =>
          data.items.length === 0 ? (
            <Empty
              title="Nothing launched yet"
              body="Register a contract address once you launch, and Annie tracks it from there — price, whether anyone outside is talking about it, and what she would change next time."
            />
          ) : (
            <div className="memory-grid">
              {data.items.map((launch) => (
                <LaunchCard
                  key={launch.mint}
                  launch={launch}
                  onOpen={() => setOpen(launch.mint)}
                />
              ))}
            </div>
          )
        }
      </Async>

      {adding && (
        <RegisterDialog onClose={() => setAdding(false)} onDone={launches.reload} />
      )}
      {open && <RecordDialog mint={open} onClose={() => setOpen(null)} />}
    </div>
  )
}

function LaunchCard({ launch, onOpen }) {
  const down =
    launch.peak_market_cap && launch.market_cap
      ? 1 - launch.market_cap / launch.peak_market_cap
      : null

  return (
    <button className="memcard" onClick={onOpen}>
      <div className="memcard__head">
        <span className="memcard__title">
          {launch.ticker ? `$${launch.ticker}` : launch.mint.slice(0, 8)}
        </span>
        <Badge status={launch.status === 'live' ? 'verified' : 'retired'}>
          {launch.status}
        </Badge>
        <span className="faint memcard__age">{relative(launch.launched_at)}</span>
      </div>

      {launch.name && <div className="faint">{launch.name}</div>}

      <dl className="deflist">
        <dt>Now</dt>
        <dd>{usd(launch.market_cap)}</dd>
        <dt>Peak</dt>
        <dd>{usd(launch.peak_market_cap)}</dd>
        <dt>Check-ins</dt>
        <dd>{launch.checkins ?? 0}</dd>
      </dl>

      {down !== null && down > 0.3 && (
        <p className="memcard__excerpt">{Math.round(down * 100)}% off its peak</p>
      )}
      {launch.note && <p className="memcard__excerpt">{launch.note}</p>}
      <div className="memcard__foot">
        <code className="mono truncate">{launch.mint}</code>
      </div>
    </button>
  )
}

function RegisterDialog({ onClose, onDone }) {
  const [form, setForm] = useState({ mint: '', ticker: '', name: '', note: '' })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const set = (key) => (event) => setForm({ ...form, [key]: event.target.value })

  async function submit(event) {
    event.preventDefault()
    setBusy(true)
    setError('')
    try {
      await api.registerLaunch(form)
      onDone?.()
      onClose()
    } catch (err) {
      setError(err.message || 'Could not register that.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="modal__backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <form onSubmit={submit}>
          <div className="modal__head">
            <h2 className="modal__title">Register a launch</h2>
          </div>

          <div className="modal__body stack gap-3">
            <label className="stack gap-1">
              <span className="faint">Contract address</span>
              <input
                className="input mono"
                value={form.mint}
                onChange={set('mint')}
                placeholder="the mint you just launched"
                required
              />
            </label>

            <div className="row gap-2">
              <label className="stack gap-1 grow">
                <span className="faint">Ticker</span>
                <input className="input" value={form.ticker} onChange={set('ticker')} />
              </label>
              <label className="stack gap-1 grow">
                <span className="faint">Name</span>
                <input className="input" value={form.name} onChange={set('name')} />
              </label>
            </div>

            <label className="stack gap-1">
              <span className="faint">Why you launched it, what you expect</span>
              <textarea
                className="input"
                rows={3}
                value={form.note}
                onChange={set('note')}
              />
            </label>

            {error && <p className="modal__warn">{error}</p>}
            <p className="faint modal__hint">
              Once registered this is exempt from pruning and gets a written check-in
              every cycle, however small it stays.
            </p>
          </div>

          <div className="modal__actions">
            <button type="button" className="btn btn--ghost" onClick={onClose}>
              Cancel
            </button>
            <button className="btn btn--primary" disabled={busy || !form.mint}>
              {busy ? 'Registering…' : 'Register'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

function RecordDialog({ mint, onClose }) {
  const launch = useApi(() => api.launch(mint), [mint])
  const [busy, setBusy] = useState(false)

  async function reviewNow() {
    setBusy(true)
    try {
      await api.reviewLaunch(mint)
      launch.reload()
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="modal__backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <Async state={launch}>
          {(data) => (
            <>
              <div className="modal__head">
                <h2 className="modal__title">
                  {data.ticker ? `$${data.ticker}` : data.mint.slice(0, 10)}
                </h2>
                <Badge status={data.status === 'live' ? 'verified' : 'retired'}>
                  {data.status}
                </Badge>
              </div>

              <div className="modal__meta">
                <code>{data.mint}</code>
                <span className="faint">
                  {usd(data.market_cap)} now · peak {usd(data.peak_market_cap)} ·{' '}
                  {data.checkins ?? 0} check-ins
                </span>
              </div>

              <div className="modal__body">
                {data.record ? (
                  <pre className="content">{data.record}</pre>
                ) : (
                  <Empty
                    title="No check-ins yet"
                    body="The next cycle writes the first one. Or force it now."
                  />
                )}
              </div>

              <div className="modal__actions">
                <button className="btn btn--ghost" onClick={reviewNow} disabled={busy}>
                  {busy ? 'Checking…' : 'Check in now'}
                </button>
                <button className="btn" onClick={onClose}>
                  Close
                </button>
              </div>
            </>
          )}
        </Async>
      </div>
    </div>
  )
}
