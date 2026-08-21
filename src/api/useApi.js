import { useCallback, useEffect, useRef, useState } from 'react'

/**
 * Minimal data-fetching hook.
 *
 * Deliberately not a caching library. The data this app displays changes on a
 * daily ingestion cycle and every screen shows its own freshness, so a stale
 * cache would actively mislead — a dashboard that quietly serves yesterday's
 * trend counts is worse than one that takes 200ms to load today's.
 *
 * Returns `{ data, error, loading, reload }`. Errors are surfaced as values,
 * never thrown into render, so every screen can show the structured
 * capability/network error rather than blanking behind an error boundary.
 */
export function useApi(fetcher, deps = [], { enabled = true } = {}) {
  const [state, setState] = useState({ data: null, error: null, loading: enabled })
  const requestId = useRef(0)

  const run = useCallback(async () => {
    if (!enabled) {
      setState({ data: null, error: null, loading: false })
      return
    }
    const id = ++requestId.current
    setState((prev) => ({ ...prev, loading: true, error: null }))
    try {
      const data = await fetcher()
      // Ignore responses from superseded requests — otherwise fast-typing in a
      // search box lands results out of order.
      if (id !== requestId.current) return
      setState({ data, error: null, loading: false })
    } catch (error) {
      if (error.name === 'AbortError') return
      if (id !== requestId.current) return
      setState({ data: null, error, loading: false })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, ...deps])

  useEffect(() => {
    run()
  }, [run])

  return { ...state, reload: run }
}

/** Debounce a rapidly changing value — used for search inputs. */
export function useDebounced(value, delay = 250) {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delay)
    return () => clearTimeout(timer)
  }, [value, delay])
  return debounced
}
