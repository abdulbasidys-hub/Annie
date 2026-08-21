import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'

import App from './App.jsx'
import { config, configError } from './config.js'
import './styles/global.css'

/**
 * Entry point.
 *
 * If configuration failed, the app does not mount. The user gets a page that
 * names the missing variable and how to fix it — the frontend counterpart to
 * the backend refusing to start. A half-mounted app throwing "Failed to fetch"
 * on every screen would be strictly worse than this.
 */
function ConfigErrorScreen({ error }) {
  return (
    <div
      style={{
        minHeight: '100dvh',
        display: 'grid',
        placeItems: 'center',
        padding: '2rem',
        background: 'var(--bg)',
        color: 'var(--text)',
        fontFamily: 'var(--font-ui)',
      }}
    >
      <div style={{ maxWidth: '46ch', display: 'flex', flexDirection: 'column', gap: '0.75rem' }}>
        <span style={{ fontSize: '0.75rem', letterSpacing: '0.08em', textTransform: 'uppercase', color: 'var(--alert)' }}>
          Configuration error
        </span>
        <h1 style={{ fontSize: '1.375rem', fontWeight: 600, letterSpacing: '-0.02em' }}>
          Annie can’t start
        </h1>
        <p style={{ color: 'var(--text-secondary)', lineHeight: 1.6, fontSize: '0.875rem' }}>
          {error.message}
        </p>
        {error.hint && (
          <p style={{ color: 'var(--text-muted)', lineHeight: 1.6, fontSize: '0.8125rem' }}>
            {error.hint}
          </p>
        )}
        <pre
          style={{
            background: 'var(--bg-inset)',
            border: '1px solid var(--border)',
            borderRadius: 6,
            padding: '0.75rem',
            fontSize: '0.75rem',
            fontFamily: 'var(--font-mono)',
            overflowX: 'auto',
            color: 'var(--text-secondary)',
          }}
        >
{`# frontend/.env
VITE_API_BASE_URL=http://localhost:8000`}
        </pre>
      </div>
    </div>
  )
}

const root = createRoot(document.getElementById('root'))

if (configError || !config) {
  root.render(<ConfigErrorScreen error={configError} />)
} else {
  root.render(
    <StrictMode>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </StrictMode>
  )
}
