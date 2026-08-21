/**
 * Formatting.
 *
 * One rule runs through every function here: **null is unknown, and unknown
 * renders as a dash, never as zero.** A token with no market cap reading is
 * not a $0 token. In a system whose whole purpose is distinguishing what it
 * knows from what it does not, silently coercing missing data to zero would
 * put fabricated values into the operator's field of view — and eventually
 * into a screenshot, a note, or a decision.
 *
 * Money arrives from the API as a decimal *string* to avoid float rounding.
 * These functions parse for display only. Nothing here does arithmetic that
 * feeds back into stored data.
 */

export const UNKNOWN = '—'

function toNumber(value) {
  if (value === null || value === undefined || value === '') return null
  const n = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(n) ? n : null
}

/** Compact USD: $124k, $1.2M, $980. */
export function usd(value, { precise = false } = {}) {
  const n = toNumber(value)
  if (n === null) return UNKNOWN
  if (precise) {
    return `$${n.toLocaleString('en-US', { maximumFractionDigits: 0 })}`
  }
  const abs = Math.abs(n)
  if (abs >= 1_000_000_000) return `$${trim(n / 1_000_000_000)}B`
  if (abs >= 1_000_000) return `$${trim(n / 1_000_000)}M`
  if (abs >= 1_000) return `$${trim(n / 1_000)}k`
  return `$${n.toLocaleString('en-US', { maximumFractionDigits: 2 })}`
}

function trim(n) {
  // One decimal below 100, none above — keeps column widths stable without
  // showing precision the underlying figure does not have.
  return Math.abs(n) < 100 ? String(Math.round(n * 10) / 10) : String(Math.round(n))
}

/** A proportion in [0,1] as a percentage. Never accepts a pre-multiplied value. */
export function percent(value, { decimals = 1 } = {}) {
  const n = toNumber(value)
  if (n === null) return UNKNOWN
  return `${(n * 100).toFixed(decimals)}%`
}

/** Signed change in percentage *points*, for comparing two proportions. */
export function points(value, { decimals = 1 } = {}) {
  const n = toNumber(value)
  if (n === null) return UNKNOWN
  const sign = n > 0 ? '+' : ''
  return `${sign}${(n * 100).toFixed(decimals)}pp`
}

export function count(value) {
  const n = toNumber(value)
  if (n === null) return UNKNOWN
  return n.toLocaleString('en-US')
}

/** Multiplier, e.g. 2.4× baseline. */
export function multiple(value, { decimals = 1 } = {}) {
  const n = toNumber(value)
  if (n === null) return UNKNOWN
  return `${n.toFixed(decimals)}×`
}

/** Solana addresses are 44 chars; the middle carries no recognition value. */
export function address(value, { head = 4, tail = 4 } = {}) {
  if (!value) return UNKNOWN
  if (value.length <= head + tail + 1) return value
  return `${value.slice(0, head)}…${value.slice(-tail)}`
}

export function dateTime(value) {
  if (!value) return UNKNOWN
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return UNKNOWN
  return d.toLocaleString('en-US', {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  })
}

export function date(value) {
  if (!value) return UNKNOWN
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return UNKNOWN
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })
}

/** Relative time. Freshness is a first-class signal in this app (§50). */
export function relative(value) {
  if (!value) return UNKNOWN
  const then = new Date(value).getTime()
  if (Number.isNaN(then)) return UNKNOWN
  const seconds = Math.round((Date.now() - then) / 1000)
  if (seconds < 0) return 'just now'
  if (seconds < 60) return `${seconds}s ago`
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.round(hours / 24)
  if (days < 30) return `${days}d ago`
  return date(value)
}

export function duration(seconds) {
  const n = toNumber(seconds)
  if (n === null) return UNKNOWN
  if (n < 60) return `${Math.round(n)}s`
  if (n < 3600) return `${Math.round(n / 60)}m`
  if (n < 86400) return `${Math.round(n / 3600)}h`
  return `${Math.round(n / 86400)}d`
}

export function minutes(value) {
  const n = toNumber(value)
  if (n === null) return UNKNOWN
  if (n < 60) return `${Math.round(n)}m`
  if (n < 1440) return `${(n / 60).toFixed(1)}h`
  return `${(n / 1440).toFixed(1)}d`
}

/** Tier labels used as object keys by the API: "100000" -> "$100k". */
export function tierLabel(value) {
  const n = toNumber(value)
  if (n === null) return UNKNOWN
  return usd(n)
}

/** Turn a snake_case status into display text. */
export function humanise(value) {
  if (!value) return UNKNOWN
  return value.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}

/**
 * Is a rate too thin to lead with?
 *
 * Mirrors `MIN_RECENT_SAMPLE` in the backend's stats module. Duplicated
 * intentionally — the frontend must be able to visually flag a thin sample
 * even when rendering a payload that predates the current threshold, and a
 * round-trip to ask the server whether to show a warning would be absurd.
 * If the backend threshold changes, change this one too.
 */
export const THIN_SAMPLE = 20

export function isThinSample(total) {
  const n = toNumber(total)
  // Zero is *no* sample, not a thin one. Warning on 0/0 puts a caution marker
  // next to every disabled provider and unmeasured row, which trains the eye
  // to ignore the marker in the places it actually matters.
  return n !== null && n > 0 && n < THIN_SAMPLE
}
