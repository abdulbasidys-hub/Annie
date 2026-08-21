import { useCallback, useEffect, useState } from 'react'
import { NavLink, Navigate, Route, Routes, useLocation } from 'react-router-dom'

import { api } from './api/client.js'
import { useApi } from './api/useApi.js'
import { Freshness, Loading } from './components/primitives.jsx'

import Login from './pages/Login.jsx'
import Dashboard from './pages/Dashboard.jsx'
import Tokens from './pages/Tokens.jsx'
import TokenDetail from './pages/TokenDetail.jsx'
import Trends from './pages/Trends.jsx'
import TrendDetail from './pages/TrendDetail.jsx'
import Creators from './pages/Creators.jsx'
import CreatorDetail from './pages/CreatorDetail.jsx'
import Launchpads from './pages/Launchpads.jsx'
import LaunchpadDetail from './pages/LaunchpadDetail.jsx'
import Narratives from './pages/Narratives.jsx'
import Research from './pages/Research.jsx'
import Reports from './pages/Reports.jsx'
import AnniePage from './pages/Annie.jsx'
import DataSources from './pages/DataSources.jsx'
import SystemHealth from './pages/SystemHealth.jsx'
import Settings from './pages/Settings.jsx'

/**
 * Navigation.
 *
 * Grouped by what the user is doing, not by data model. "Intelligence" is the
 * output of the system, "Catalogue" is its raw material, "System" is its
 * plumbing — an operator reaching for a trend should never have to scan past
 * provider health to find it.
 *
 * `mobile` marks the six destinations that survive into the phone tab bar.
 */
const NAV = [
  {
    section: null,
    items: [
      { to: '/', label: 'Dashboard', icon: '▤', end: true, mobile: true },
      { to: '/annie', label: 'Annie', icon: '✳', annie: true, mobile: true },
    ],
  },
  {
    section: 'Intelligence',
    items: [
      { to: '/trends', label: 'Trends', icon: '◈', mobile: true },
      { to: '/research', label: 'Research', icon: '◎', mobile: true },
      { to: '/reports', label: 'Reports', icon: '▣' },
    ],
  },
  {
    section: 'Catalogue',
    items: [
      { to: '/tokens', label: 'Tokens', icon: '◇', mobile: true },
      { to: '/launchpads', label: 'Launchpads', icon: '◐', mobile: true },
      { to: '/creators', label: 'Creators', icon: '◔' },
      { to: '/narratives', label: 'Narratives', icon: '◑' },
    ],
  },
  {
    section: 'System',
    items: [
      { to: '/sources', label: 'Data Sources', icon: '⊞' },
      { to: '/health', label: 'System Health', icon: '⊙' },
      { to: '/settings', label: 'Settings', icon: '⚙' },
    ],
  },
]

const TITLES = {
  '/': 'Dashboard',
  '/annie': 'Annie',
  '/trends': 'Trends',
  '/research': 'Research',
  '/reports': 'Reports',
  '/tokens': 'Tokens',
  '/launchpads': 'Launchpads',
  '/creators': 'Creators',
  '/narratives': 'Narratives',
  '/sources': 'Data Sources',
  '/health': 'System Health',
  '/settings': 'Settings',
}

function useTheme() {
  const [theme, setTheme] = useState(
    () => document.documentElement.getAttribute('data-theme') || 'dark'
  )
  const toggle = useCallback(() => {
    setTheme((current) => {
      const next = current === 'dark' ? 'light' : 'dark'
      document.documentElement.setAttribute('data-theme', next)
      try {
        localStorage.setItem('annie.theme', next)
      } catch {
        /* private mode — the theme simply will not persist */
      }
      return next
    })
  }, [])
  return { theme, toggle }
}

function Sidebar({ pendingTasks }) {
  return (
    <aside className="sidebar">
      <div className="sidebar__brand">
        <span className="sidebar__mark">An</span>
        <span className="sidebar__name">Annie</span>
      </div>

      <nav className="sidebar__nav">
        {NAV.map((group, gi) => (
          <div key={gi} className="stack" style={{ gap: 1 }}>
            {group.section && (
              <span className="sidebar__section desktop-only">{group.section}</span>
            )}
            {group.items.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) =>
                  [
                    'navlink',
                    item.annie && 'navlink--annie',
                    !item.mobile && 'navlink--secondary',
                    isActive && 'is-active',
                  ]
                    .filter(Boolean)
                    .join(' ')
                }
              >
                <span className="navlink__icon" aria-hidden="true">
                  {item.icon}
                </span>
                <span className="truncate">{item.label}</span>
                {item.to === '/research' && pendingTasks > 0 && (
                  <span className="navlink__count desktop-only">{pendingTasks}</span>
                )}
              </NavLink>
            ))}
          </div>
        ))}
      </nav>

      <div className="sidebar__foot desktop-only">
        <span className="faint" style={{ fontSize: 'var(--text-2xs)' }}>
          Research system. Not a trading bot.
        </span>
      </div>
    </aside>
  )
}

/**
 * Auth gate (§66).
 *
 * Checks the session cookie once on mount. `authed === null` means "still
 * checking" — rendered as a blank loading state rather than flashing the
 * login form for every returning visitor while `/api/auth/me` resolves.
 */
export default function AuthGate() {
  const [authed, setAuthed] = useState(null)

  const check = useCallback(() => {
    api
      .me()
      .then(() => setAuthed(true))
      .catch(() => setAuthed(false))
  }, [])

  useEffect(check, [check])

  if (authed === null) {
    return (
      <div style={{ minHeight: '100dvh', display: 'grid', placeItems: 'center' }}>
        <Loading rows={1} />
      </div>
    )
  }
  if (!authed) {
    return <Login onAuthenticated={() => setAuthed(true)} />
  }
  return <App onLogout={() => setAuthed(false)} />
}

function App({ onLogout }) {
  const location = useLocation()
  const { theme, toggle } = useTheme()

  // The dashboard payload doubles as the shell's status source — freshness and
  // the pending-task count. One request, not three.
  const dashboard = useApi(() => api.dashboard(7), [])

  useEffect(() => {
    const base = TITLES[`/${location.pathname.split('/')[1]}`] || TITLES['/']
    document.title = base === 'Dashboard' ? 'Annie' : `${base} · Annie`
  }, [location.pathname])

  const title =
    TITLES[location.pathname] ||
    TITLES[`/${location.pathname.split('/')[1]}`] ||
    'Annie'

  return (
    <div className="app">
      <Sidebar pendingTasks={dashboard.data?.pending_tasks?.length ?? 0} />

      <div className="main">
        <header className="topbar">
          <h1 className="topbar__title">{title}</h1>
          <div className="topbar__actions">
            <Freshness
              seconds={dashboard.data?.data_freshness_seconds}
              at={dashboard.data?.last_ingestion_at}
            />
            <button
              className="btn btn--ghost btn--icon"
              onClick={toggle}
              aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`}
              title={`Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`}
            >
              {theme === 'dark' ? '☾' : '☀'}
            </button>
            <button
              className="btn btn--ghost btn--icon"
              onClick={() => api.logout().finally(onLogout)}
              aria-label="Sign out"
              title="Sign out"
            >
              ⏻
            </button>
          </div>
        </header>

        <main className="content">
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/annie" element={<AnniePage />} />

            <Route path="/trends" element={<Trends />} />
            <Route path="/trends/:slug" element={<TrendDetail />} />
            <Route path="/research" element={<Research />} />
            <Route path="/reports" element={<Reports />} />
            <Route path="/reports/:id" element={<Reports />} />

            <Route path="/tokens" element={<Tokens />} />
            <Route path="/tokens/:mint" element={<TokenDetail />} />
            <Route path="/launchpads" element={<Launchpads />} />
            <Route path="/launchpads/:slug" element={<LaunchpadDetail />} />
            <Route path="/creators" element={<Creators />} />
            <Route path="/creators/:wallet" element={<CreatorDetail />} />
            <Route path="/narratives" element={<Narratives />} />

            <Route path="/sources" element={<DataSources />} />
            <Route path="/health" element={<SystemHealth />} />
            <Route path="/settings" element={<Settings />} />

            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </main>
      </div>
    </div>
  )
}
