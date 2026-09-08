import { useCallback, useEffect, useState } from 'react'
import { NavLink, Navigate, Route, Routes, useLocation } from 'react-router-dom'

import { api } from './api/client.js'
import { useApi } from './api/useApi.js'
import { Freshness, Loading } from './components/primitives.jsx'

import Login from './pages/Login.jsx'
import Today from './pages/Today.jsx'
import Ideas from './pages/Ideas.jsx'
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
import Memory from './pages/Memory.jsx'
import AnniePage from './pages/Annie.jsx'
import DataSources from './pages/DataSources.jsx'
import SystemHealth from './pages/SystemHealth.jsx'
import Settings from './pages/Settings.jsx'
import Personality from './pages/Personality.jsx'

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
/**
 * Navigation.
 *
 * Restructured when the system stopped being a database and started being a
 * notebook. The old grouping was Intelligence / **Catalogue** / System —
 * "catalogue" being, literally, the raw material. That was honest about what
 * the product was then: a collection of tokens you could browse.
 *
 * It is the wrong shape now. What Annie *thinks* is the product; the ledger
 * is disposable evidence pruned within 48 hours. So the four things she
 * actually produces sit at the top with no group heading at all, and
 * everything that backs them up is demoted to "Evidence" — the material you
 * consult to check a claim, not the reason to open the site.
 *
 * `mobile` marks the destinations that survive into the phone tab bar, and
 * all four top-level ones do.
 */
const NAV = [
  {
    section: null,
    items: [
      { to: '/', label: 'Today', icon: '◐', end: true, mobile: true },
      { to: '/annie', label: 'Annie', icon: '✳', annie: true, mobile: true },
      { to: '/memory', label: 'Memory', icon: '◒', mobile: true },
      { to: '/ideas', label: 'Ideas', icon: '✦', mobile: true },
    ],
  },
  {
    section: 'Evidence',
    items: [
      { to: '/movers', label: 'Movers', icon: '◇', mobile: true },
      { to: '/creators', label: 'Creators', icon: '◔' },
      { to: '/signals', label: 'Signals', icon: '◈', mobile: true },
      { to: '/narratives', label: 'Narratives', icon: '◑' },
      { to: '/launchpads', label: 'Launchpads', icon: '◐' },
      { to: '/research', label: 'Research', icon: '◎' },
      { to: '/reports', label: 'Reports', icon: '▣' },
    ],
  },
  {
    section: 'System',
    items: [
      { to: '/health', label: 'System Health', icon: '⊙' },
      { to: '/sources', label: 'Data Sources', icon: '⊞' },
      { to: '/settings', label: 'Settings', icon: '⚙' },
      { to: '/personality', label: 'Personality', icon: '⚑' },
    ],
  },
]

const TITLES = {
  '/': 'Today',
  '/annie': 'Annie',
  '/memory': 'Memory',
  '/ideas': 'Ideas',
  '/movers': 'Movers',
  '/tokens': 'Movers',
  '/creators': 'Creators',
  '/signals': 'Signals',
  '/trends': 'Signals',
  '/narratives': 'Narratives',
  '/launchpads': 'Launchpads',
  '/research': 'Research',
  '/reports': 'Reports',
  '/health': 'System Health',
  '/sources': 'Data Sources',
  '/settings': 'Settings',
  '/personality': 'Personality',
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
        <img src="/Annie.jpg" alt="" className="sidebar__mark" />
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
 * Checks once on mount. `authed === null` means "still checking" — rendered
 * as a blank loading state rather than flashing the login form for every
 * returning visitor. No stored token means no session, full stop — skips
 * the network round trip and goes straight to the login screen. A stored
 * token still gets verified against `/api/auth/me`, since it may have
 * expired since the last visit.
 */
export default function AuthGate() {
  const [authed, setAuthed] = useState(null)

  const check = useCallback(() => {
    if (!api.hasToken()) {
      setAuthed(false)
      return
    }
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

  // The front page's payload doubles as the shell's status source. It used
  // to call /api/dashboard here, which issued five Firestore queries on
  // every page load to render one sidebar number; /api/today is local reads
  // plus a single query for that count.
  const shell = useApi(() => api.today(24), [])

  useEffect(() => {
    const base = TITLES[`/${location.pathname.split('/')[1]}`] || TITLES['/']
    document.title = base === 'Today' ? 'Annie' : `${base} · Annie`
  }, [location.pathname])

  const title =
    TITLES[location.pathname] ||
    TITLES[`/${location.pathname.split('/')[1]}`] ||
    'Annie'

  return (
    <div className="app">
      <Sidebar pendingTasks={shell.data?.research_pending ?? 0} />

      <div className="main">
        <header className="topbar">
          <h1 className="topbar__title">{title}</h1>
          <div className="topbar__actions">
            <Freshness at={shell.data?.data_freshness} />
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
            <Route path="/" element={<Today />} />
            <Route path="/annie" element={<AnniePage />} />
            <Route path="/memory" element={<Memory />} />
            <Route path="/ideas" element={<Ideas />} />

            {/* "Signals" is the current name; /trends stays as an alias so an
                existing bookmark or a link Annie wrote into memory before the
                rename still resolves. */}
            <Route path="/signals" element={<Trends />} />
            <Route path="/signals/:slug" element={<TrendDetail />} />
            <Route path="/trends" element={<Navigate to="/signals" replace />} />
            <Route path="/trends/:slug" element={<TrendDetail />} />

            {/* Same for tokens -> movers: the ledger holds what moved, not a
                catalogue of everything seen, and the label should say so. */}
            <Route path="/movers" element={<Tokens />} />
            <Route path="/tokens" element={<Navigate to="/movers" replace />} />
            <Route path="/tokens/:mint" element={<TokenDetail />} />

            <Route path="/creators" element={<Creators />} />
            <Route path="/creators/:wallet" element={<CreatorDetail />} />
            <Route path="/launchpads" element={<Launchpads />} />
            <Route path="/launchpads/:slug" element={<LaunchpadDetail />} />
            <Route path="/narratives" element={<Narratives />} />
            <Route path="/research" element={<Research />} />
            <Route path="/reports" element={<Reports />} />
            <Route path="/reports/:id" element={<Reports />} />

            <Route path="/sources" element={<DataSources />} />
            <Route path="/health" element={<SystemHealth />} />
            <Route path="/settings" element={<Settings />} />
            <Route path="/personality" element={<Personality />} />

            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </main>
      </div>
    </div>
  )
}
