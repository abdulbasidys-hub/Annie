/**
 * API client.
 *
 * Thin on purpose. The one thing it does beyond `fetch` is preserve the
 * backend's structured error envelope, because a capability failure carries
 * information the UI needs to be useful: which capability, and which
 * environment variables are missing. Collapsing that into "Request failed"
 * would turn a fixable configuration problem into a mystery.
 */

import { config } from '../config.js'

export class ApiError extends Error {
  constructor(message, { status, capability, missingEnvVars, detail } = {}) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.capability = capability ?? null
    this.missingEnvVars = missingEnvVars ?? []
    this.detail = detail ?? null
  }

  /** True when the backend is running but not configured for this feature. */
  get isCapabilityError() {
    return this.status === 503 && this.missingEnvVars.length > 0
  }

  /** True when the backend could not be reached at all. */
  get isNetworkError() {
    return this.status === 0
  }
}

function buildUrl(path, params) {
  const url = new URL(`${config.apiBaseUrl}${path}`)
  if (params) {
    for (const [key, value] of Object.entries(params)) {
      if (value === undefined || value === null || value === '') continue
      if (Array.isArray(value)) {
        value.forEach((v) => url.searchParams.append(key, String(v)))
      } else {
        url.searchParams.set(key, String(value))
      }
    }
  }
  return url.toString()
}

async function request(method, path, { params, body, signal } = {}) {
  let response
  try {
    response = await fetch(buildUrl(path, params), {
      method,
      signal,
      credentials: 'include',
      headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    })
  } catch (error) {
    if (error.name === 'AbortError') throw error
    throw new ApiError(`Could not reach the API at ${config.apiBaseUrl}.`, {
      status: 0,
      detail: error.message,
    })
  }

  if (response.status === 204) return null

  let payload = null
  const contentType = response.headers.get('content-type') || ''
  if (contentType.includes('application/json')) {
    try {
      payload = await response.json()
    } catch {
      payload = null
    }
  }

  if (!response.ok) {
    throw new ApiError(payload?.error || `Request failed (${response.status}).`, {
      status: response.status,
      capability: payload?.capability,
      missingEnvVars: payload?.missing_env_vars,
      detail: payload?.detail,
    })
  }

  return payload
}

export const api = {
  get: (path, options) => request('GET', path, options),
  post: (path, body, options) => request('POST', path, { ...options, body }),
  patch: (path, body, options) => request('PATCH', path, { ...options, body }),

  // -- Endpoints ------------------------------------------------------------
  // Named methods rather than raw paths at call sites, so the surface the
  // frontend depends on is enumerable in one place. When the backend contract
  // changes, this list is the checklist.

  dashboard: (windowDays) => request('GET', '/api/dashboard', { params: { window_days: windowDays } }),

  tokens: (params) => request('GET', '/api/tokens', { params }),
  token: (mint) => request('GET', `/api/tokens/${mint}`),

  creators: (params) => request('GET', '/api/creators', { params }),
  creator: (wallet) => request('GET', `/api/creators/${wallet}`),

  launchpads: (params) => request('GET', '/api/launchpads', { params }),
  launchpad: (slug) => request('GET', `/api/launchpads/${slug}`),

  narratives: (params) => request('GET', '/api/narratives', { params }),
  narrative: (slug) => request('GET', `/api/narratives/${slug}`),

  trends: (params) => request('GET', '/api/trends', { params }),
  trend: (slug) => request('GET', `/api/trends/${slug}`),

  researchTasks: (params) => request('GET', '/api/research/tasks', { params }),
  researchTask: (id) => request('GET', `/api/research/tasks/${id}`),
  createResearchTask: (body) => request('POST', '/api/research/tasks', { body }),
  researchNotes: (params) => request('GET', '/api/research/notes', { params }),
  hypotheses: (params) => request('GET', '/api/research/hypotheses', { params }),
  anomalies: (params) => request('GET', '/api/research/anomalies', { params }),

  reports: (params) => request('GET', '/api/reports', { params }),
  report: (id) => request('GET', `/api/reports/${id}`),

  conversations: () => request('GET', '/api/annie/conversations'),
  conversation: (id) => request('GET', `/api/annie/conversations/${id}`),
  chat: (body) => request('POST', '/api/annie/chat', { body }),

  health: () => request('GET', '/api/system/health'),
  capabilities: () => request('GET', '/api/system/capabilities'),
  dataQuality: (params) => request('GET', '/api/system/data-quality', { params }),
  settings: () => request('GET', '/api/system/settings'),
  updateSetting: (key, value) => request('PATCH', `/api/system/settings/${key}`, { body: { value } }),

  login: (username, password) => request('POST', '/api/auth/login', { body: { username, password } }),
  logout: () => request('POST', '/api/auth/logout'),
  me: () => request('GET', '/api/auth/me'),
}
