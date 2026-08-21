/**
 * Runtime configuration.
 *
 * There is exactly one environment-specific value in this app and it has no
 * default. A missing `VITE_API_BASE_URL` throws before React mounts, and
 * `main.jsx` renders the error instead of the app.
 *
 * The temptation is to fall back to `http://localhost:8000`. That fallback is
 * why misconfigured frontends ship: everything works on the developer's
 * machine, the build goes out, and the deployed app silently calls an origin
 * that does not exist. Failing at startup costs thirty seconds once; the
 * fallback costs an afternoon at the worst possible time.
 *
 * To work on the interface without the Python backend, point this at the
 * fixture server (`npm run fixtures`) — a separate process implementing the
 * same contract, not a fallback baked into the app.
 */

export class ConfigError extends Error {
  constructor(message, { variable, hint } = {}) {
    super(message)
    this.name = 'ConfigError'
    this.variable = variable
    this.hint = hint
  }
}

function required(name) {
  const value = import.meta.env[name]
  if (value === undefined || value === null || String(value).trim() === '') {
    throw new ConfigError(`Missing required environment variable ${name}.`, {
      variable: name,
      hint:
        'Create frontend/.env with this value set. See .env.example at the ' +
        'repository root. No default is substituted by design.',
    })
  }
  return String(value).trim()
}

function readConfig() {
  const apiBaseUrl = required('VITE_API_BASE_URL').replace(/\/+$/, '')

  // Catch a scheme-less or otherwise malformed URL now rather than at the
  // first fetch, where it surfaces as an opaque "Failed to fetch".
  try {
    // eslint-disable-next-line no-new
    new URL(apiBaseUrl)
  } catch {
    throw new ConfigError(
      `VITE_API_BASE_URL is not a valid absolute URL: "${apiBaseUrl}".`,
      {
        variable: 'VITE_API_BASE_URL',
        hint: 'Include the scheme, e.g. http://localhost:8000',
      }
    )
  }

  return Object.freeze({ apiBaseUrl })
}

/**
 * Resolved config, or the error that prevented resolution.
 * Evaluated once at module load; `main.jsx` decides what to render.
 */
export let config = null
export let configError = null

try {
  config = readConfig()
} catch (error) {
  configError = error
}
