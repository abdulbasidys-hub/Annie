/**
 * Shared display primitives.
 *
 * Two of these encode rules rather than just styles, and should not be
 * bypassed:
 *
 * `<Sample>` is the only sanctioned way to render a rate. It always shows the
 * denominator and flags a thin sample in place. Rendering a bare percentage
 * anywhere else re-opens the exact failure the backend's statistics module
 * exists to prevent.
 *
 * `<Value>` renders unknown as a dash. Anything that might be null goes
 * through it, so a missing market cap can never appear as $0.
 */

import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import {
  UNKNOWN,
  address,
  count,
  humanise,
  isThinSample,
  percent,
  points,
  relative,
} from '../lib/format.js'

/* ---------------------------------------------------------------- Panel -- */

export function Panel({ title, meta, actions, flush, children, className = '' }) {
  return (
    <section className={`panel ${className}`}>
      {(title || meta || actions) && (
        <header className="panel__head">
          {title && <h2 className="panel__title">{title}</h2>}
          {meta && <span className="panel__meta">{meta}</span>}
          {actions && <div className="row gap-2" style={{ marginLeft: 'auto' }}>{actions}</div>}
        </header>
      )}
      <div className={flush ? 'panel__body panel__body--flush' : 'panel__body'}>{children}</div>
    </section>
  )
}

/* ---------------------------------------------------------------- Value -- */

export function Value({ children, unknown = UNKNOWN, className = '' }) {
  const missing =
    children === null || children === undefined || children === '' || children === UNKNOWN
  return (
    <span className={missing ? `faint ${className}` : className}>
      {missing ? unknown : children}
    </span>
  )
}

/* ---------------------------------------------------------------- Badge -- */

export function Badge({ status, children, variant, dot = false, title }) {
  const cls = ['badge', variant === 'outline' && 'badge--outline', variant === 'plain' && 'badge--plain']
    .filter(Boolean)
    .join(' ')
  return (
    <span className={cls} data-status={status} title={title}>
      {dot && <span className="badge__dot" />}
      {children ?? humanise(status)}
    </span>
  )
}

/**
 * Evidential standing, shown as filled pips beside the direction badge.
 *
 * Kept visually separate from status because they answer different questions —
 * "which way is it moving" and "should you believe it". A single combined pill
 * is how a two-day observation ends up looking like a validated finding.
 */
export function Maturity({ value }) {
  const levels = { observation: 1, candidate: 2, validated: 3 }
  const filled = levels[value] ?? 0
  const label =
    value === 'validated'
      ? 'Validated — persistent, statistically supported'
      : value === 'candidate'
        ? 'Candidate — significant, not yet persistent'
        : 'Observation — recorded, not yet supported'
  return (
    <span className="maturity" title={label}>
      <span className="maturity__pips" aria-hidden="true">
        {[1, 2, 3].map((i) => (
          <span key={i} className={`maturity__pip ${i <= filled ? 'is-on' : ''}`} />
        ))}
      </span>
      <span className="sr-only">{label}</span>
      <span aria-hidden="true">{humanise(value)}</span>
    </span>
  )
}

/* --------------------------------------------------------------- Sample -- */

/**
 * A rate with its denominator. Always both.
 *
 * @param {{count:number,total:number,frequency:number|null}} sample
 */
export function Sample({ sample, decimals = 1 }) {
  if (!sample) return <Value>{null}</Value>
  // Nothing measured — render as unknown rather than as "0.0% 0/0", which
  // reads like a real measurement of zero.
  if (!sample.total) return <Value>{null}</Value>
  const thin = isThinSample(sample.total)
  return (
    <span className={`sample ${thin ? 'sample--thin' : ''}`}>
      <span className="sample__value">{percent(sample.frequency, { decimals })}</span>
      <span
        className="sample__counts"
        title={thin ? `Only ${sample.total} tokens in this window — too few to be reliable` : undefined}
      >
        {count(sample.count)}/{count(sample.total)}
        {thin && ' ⚠'}
      </span>
    </span>
  )
}

/* ---------------------------------------------------------------- Delta -- */

export function Delta({ value, format = points }) {
  if (value === null || value === undefined) return <Value>{null}</Value>
  const direction = value > 0.0005 ? 'up' : value < -0.0005 ? 'down' : 'flat'
  return <span className={`delta delta--${direction}`}>{format(value)}</span>
}

/* ----------------------------------------------------------------- Stat -- */

export function Stat({ label, value, foot, unknown }) {
  const missing = value === null || value === undefined
  return (
    <div className="stat">
      <span className="stat__label">{label}</span>
      <span className={`stat__value ${missing ? 'stat__value--unknown' : ''}`}>
        {missing ? (unknown ?? 'Not measured') : value}
      </span>
      {foot && <div className="stat__foot">{foot}</div>}
    </div>
  )
}

/* ------------------------------------------------------------- Evidence -- */

export function Evidence({ evidence }) {
  if (!evidence) return null
  const status = evidence.verification_status || 'pending'
  const parts = [
    evidence.source && `Source: ${evidence.source}`,
    evidence.source_type && `Type: ${evidence.source_type}`,
    evidence.observed_at && `Observed: ${relative(evidence.observed_at)}`,
    `Status: ${humanise(status)}`,
  ].filter(Boolean)
  return (
    <span className="evidence" data-status={status} title={parts.join('\n')}>
      <span className="evidence__dot" />
      {evidence.source || humanise(status)}
    </span>
  )
}

export function EvidenceRows({ data }) {
  const entries = Object.entries(data || {}).filter(([, v]) => v !== null && v !== undefined)
  if (!entries.length) return <Empty title="No evidence recorded" />
  return (
    <div className="evidence-list">
      {entries.map(([key, value]) => (
        <div className="evidence-row" key={key}>
          <span className="evidence-row__key">{humanise(key)}</span>
          <span className="evidence-row__value">
            {typeof value === 'object' ? JSON.stringify(value) : String(value)}
          </span>
        </div>
      ))}
    </div>
  )
}

/**
 * Caveats are rendered inline and never behind a disclosure control.
 *
 * If a finding rests on 14 tokens, that is part of the finding. Hiding it
 * behind "show details" makes the default reading of every result more
 * confident than the data supports.
 */
export function Caveats({ items, label = 'Limitations' }) {
  if (!items || !items.length) return null
  return (
    <div className="caveats">
      <span className="caveats__label">{label}</span>
      {items.map((item, i) => (
        <span key={i}>{item}</span>
      ))}
    </div>
  )
}

/* ------------------------------------------------------------ Sparkline -- */

/**
 * Frequency history for a trend.
 *
 * Renders the baseline as a dashed rule so the line is read against the thing
 * it is being compared to, not against zero. A sparkline without its baseline
 * makes every series look dramatic.
 */
export function Sparkline({ values, baseline, status, height = 28 }) {
  const points_ = (values || []).filter((v) => v !== null && v !== undefined)
  if (points_.length < 2) {
    return <div className="faint" style={{ fontSize: 'var(--text-2xs)' }}>Not enough history</div>
  }

  const width = 100
  const max = Math.max(...points_, baseline ?? 0) || 1
  const min = Math.min(...points_, baseline ?? 0)
  const range = max - min || 1
  const pad = 2

  const x = (i) => (i / (points_.length - 1)) * width
  const y = (v) => height - pad - ((v - min) / range) * (height - pad * 2)

  const line = points_.map((v, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(2)},${y(v).toFixed(2)}`).join(' ')
  const area = `${line} L${width},${height} L0,${height} Z`

  return (
    <svg
      className="spark"
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="none"
      data-status={status}
      role="img"
      aria-label={`Frequency history, ${points_.length} observations`}
    >
      <path className="spark__area" d={area} />
      {baseline !== null && baseline !== undefined && (
        <line
          className="spark__baseline"
          x1="0"
          x2={width}
          y1={y(baseline)}
          y2={y(baseline)}
          vectorEffect="non-scaling-stroke"
        />
      )}
      <path className="spark__line" d={line} vectorEffect="non-scaling-stroke" />
    </svg>
  )
}

/* --------------------------------------------------------------- States -- */

export function Loading({ rows = 4 }) {
  return (
    <div className="stack gap-2" aria-busy="true" aria-live="polite">
      <span className="sr-only">Loading</span>
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="skeleton" style={{ height: 34, opacity: 1 - i * 0.12 }} />
      ))}
    </div>
  )
}

export function Empty({ title = 'Nothing here yet', body, action }) {
  return (
    <div className="state">
      <span className="state__title">{title}</span>
      {body && <p className="state__body">{body}</p>}
      {action}
    </div>
  )
}

/**
 * Error display.
 *
 * A capability error names the missing environment variables, because that is
 * the whole point of the backend's tiered config: the operator should be told
 * what to set, not that something went wrong.
 */
export function ErrorState({ error, onRetry }) {
  if (!error) return null

  if (error.isCapabilityError) {
    return (
      <div className="error-panel">
        <span className="error-panel__title">This feature is not configured</span>
        <p className="error-panel__detail">
          {error.message} Set{' '}
          {error.missingEnvVars.map((v, i) => (
            <span key={v}>
              {i > 0 && ', '}
              <code>{v}</code>
            </span>
          ))}{' '}
          and restart the API.
        </p>
      </div>
    )
  }

  if (error.isNetworkError) {
    return (
      <div className="error-panel">
        <span className="error-panel__title">Can’t reach the API</span>
        <p className="error-panel__detail">
          {error.message} Check the backend is running, and that{' '}
          <code>VITE_API_BASE_URL</code> points at it.
          {error.detail && <> ({error.detail})</>}
        </p>
        {onRetry && (
          <button className="btn btn--sm" onClick={onRetry} style={{ alignSelf: 'flex-start' }}>
            Retry
          </button>
        )}
      </div>
    )
  }

  return (
    <div className="error-panel">
      <span className="error-panel__title">Something went wrong</span>
      <p className="error-panel__detail">
        {error.message}
        {error.detail && <> — {error.detail}</>}
      </p>
      {onRetry && (
        <button className="btn btn--sm" onClick={onRetry} style={{ alignSelf: 'flex-start' }}>
          Retry
        </button>
      )}
    </div>
  )
}

/**
 * Wraps the common loading / error / empty / content decision.
 *
 * `children` is never called with null. A render function that receives no
 * data will dereference it and take the whole page down — so an absent payload
 * resolves to the caller's empty state, or a generic one if they gave none.
 */
export function Async({ state, empty, children, rows }) {
  if (state.loading && !state.data) return <Loading rows={rows} />
  if (state.error) return <ErrorState error={state.error} onRetry={state.reload} />

  const noData = state.data === null || state.data === undefined
  const emptyList = Array.isArray(state.data?.items) && state.data.items.length === 0

  if (noData || emptyList) return empty ?? <Empty />
  return children(state.data)
}

/* ----------------------------------------------------------------- Misc -- */

export function TokenLink({ token }) {
  if (!token) return <Value>{null}</Value>
  return (
    <Link to={`/tokens/${token.mint}`} className="row gap-2" style={{ minWidth: 0 }}>
      <strong className="truncate">{token.symbol || token.name || 'Unnamed'}</strong>
      <span className="mono faint">{token.mint.slice(0, 4)}…</span>
    </Link>
  )
}

/**
 * A wallet or mint address that copies itself to the clipboard on click,
 * rather than navigating — for use inside a row that's clickable as a whole
 * (see `ClickableRow` below). Someone reaching for an address almost always
 * wants to paste it into a block explorer, not open a page they're already
 * one click away from via the rest of the row.
 */
export function CopyableAddress({ value, head = 4, tail = 4, full = false, className = 'mono' }) {
  const [copied, setCopied] = useState(false)
  if (!value) return <Value>{null}</Value>

  async function onClick(e) {
    e.stopPropagation()
    e.preventDefault()
    try {
      await navigator.clipboard.writeText(value)
      setCopied(true)
      setTimeout(() => setCopied(false), 1200)
    } catch {
      // Clipboard access denied (permissions, non-HTTPS) — nothing to
      // recover into, the address is still visible to select manually.
    }
  }

  return (
    <button
      type="button"
      onClick={onClick}
      className={className}
      title={copied ? 'Copied' : `Copy ${value}`}
      style={{
        background: 'none', border: 'none', padding: 0, cursor: 'pointer',
        color: copied ? 'var(--rise)' : 'inherit', font: 'inherit',
      }}
    >
      {copied ? 'Copied' : full ? value : address(value, { head, tail })}
    </button>
  )
}

/**
 * Makes an entire `<tr>` navigate on click, while anything inside it that
 * calls `e.stopPropagation()` (a `CopyableAddress`, a nested `<Link>`) keeps
 * its own behaviour instead of also triggering the row navigation.
 */
export function ClickableRow({ to, children, ...props }) {
  const navigate = useNavigate()
  return (
    <tr
      onClick={() => navigate(to)}
      style={{ cursor: 'pointer' }}
      {...props}
    >
      {children}
    </tr>
  )
}

export function Freshness({ seconds, at }) {
  if (seconds === null || seconds === undefined) {
    return <span className="faint" style={{ fontSize: 'var(--text-xs)' }}>Freshness unknown</span>
  }
  // Anything older than a day means the daily cycle did not complete, which is
  // an operational fact the operator needs before reading any number below it.
  const stale = seconds > 86_400
  return (
    <span
      className="row gap-1"
      style={{ fontSize: 'var(--text-xs)', color: stale ? 'var(--fall)' : 'var(--text-muted)' }}
      title={at ? `Last update ${at}` : undefined}
    >
      <span className="badge__dot" style={{ background: 'currentColor' }} />
      Data {relative(Date.now() - seconds * 1000)}
      {stale && ' — stale'}
    </span>
  )
}
