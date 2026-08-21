import { useState } from 'react'

import { api } from '../api/client.js'

/**
 * Single-operator login (§66).
 *
 * There is no signup, no password reset, no "remember me" — one operator,
 * one set of credentials, set via AUTH_USERNAME/AUTH_PASSWORD on the backend.
 * A successful login sets an httpOnly session cookie; this component just
 * asks for credentials and tells the app when to stop showing it.
 */
export default function Login({ onAuthenticated }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState(null)
  const [submitting, setSubmitting] = useState(false)

  async function submit(e) {
    e.preventDefault()
    setSubmitting(true)
    setError(null)
    try {
      await api.login(username, password)
      onAuthenticated()
    } catch (err) {
      setError(err.status === 401 ? 'Incorrect username or password.' : err.message)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div
      style={{
        minHeight: '100dvh',
        display: 'grid',
        placeItems: 'center',
        padding: '2rem',
        background: 'var(--bg)',
      }}
    >
      <form onSubmit={submit} className="panel" style={{ width: '100%', maxWidth: 360 }}>
        <div className="panel__body stack gap-4">
          <div className="stack gap-1">
            <span className="sidebar__mark" style={{ width: 36, height: 36, fontSize: '1rem' }}>
              An
            </span>
            <h1 style={{ fontSize: 'var(--text-lg)', fontWeight: 600, marginTop: 'var(--space-2)' }}>
              Sign in to Annie
            </h1>
            <p className="secondary" style={{ fontSize: 'var(--text-xs)' }}>
              Private research system. This session's credentials only.
            </p>
          </div>

          <div className="stack gap-2">
            <label className="stack" style={{ gap: 4 }}>
              <span className="faint" style={{ fontSize: 'var(--text-2xs)' }}>Username</span>
              <input
                className="input"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                autoComplete="username"
                autoFocus
                required
              />
            </label>
            <label className="stack" style={{ gap: 4 }}>
              <span className="faint" style={{ fontSize: 'var(--text-2xs)' }}>Password</span>
              <input
                className="input"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
                required
              />
            </label>
          </div>

          {error && (
            <div className="error-panel">
              <p className="error-panel__detail">{error}</p>
            </div>
          )}

          <button className="btn btn--primary" type="submit" disabled={submitting}>
            {submitting ? 'Signing in…' : 'Sign in'}
          </button>
        </div>
      </form>
    </div>
  )
}
