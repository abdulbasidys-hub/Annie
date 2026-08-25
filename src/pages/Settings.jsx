import { useState } from 'react'

import { api } from '../api/client.js'
import { useApi } from '../api/useApi.js'
import { Async, Empty, ErrorState, Panel } from '../components/primitives.jsx'
import { humanise, relative } from '../lib/format.js'

/**
 * Settings (§4, §38, §63).
 *
 * Only operational knobs an operator actually touches day-to-day — who can
 * reach Annie, and when the background jobs run. Research parameters
 * (qualification thresholds, trend significance floors, autonomous budgets)
 * used to have their own raw-JSON-editable groups here too, but an operator
 * asked to change one exactly once in this project's life, by asking Annie
 * to change it directly rather than opening this page (2026-08-25) — hidden
 * rather than removed: they still live in the same `settings` collection at
 * the same keys, editable via PATCH /api/system/settings/{key} same as
 * always, just not surfaced as a wall of raw JSON inputs nobody wants to
 * hand-edit. Environment variables are deployment facts and are deliberately
 * not editable here either; the System Health page names them instead.
 */
const GROUPS = {
  bots: {
    title: 'Bots & scheduler',
    note: 'Who can reach Annie on Telegram/Discord, and when the background jobs run. Editing a job’s time/timezone/interval/enabled here takes effect on its next check — no redeploy.',
  },
}

function groupFor(key) {
  if (key.endsWith('_allowlist') || key.startsWith('scheduler_')) return 'bots'
  return null
}

export default function Settings() {
  const state = useApi(() => api.settings(), [])

  return (
    <>
      <div className="page-head">
        <h2 className="page-head__title">Settings</h2>
        <p className="page-head__sub">
          Research parameters. API keys and endpoints are injected from the environment and are
          not editable here — see System Health for what is missing.
        </p>
      </div>

      <Async state={state} empty={<Panel><Empty title="No settings recorded" body="Defaults are in use. Settings rows are created the first time a value is changed." /></Panel>}>
        {(data) => {
          const items = data.items || data
          const grouped = {}
          for (const setting of items) {
            const g = groupFor(setting.key)
            ;(grouped[g] ||= []).push(setting)
          }
          return (
            <div className="stack gap-5">
              {Object.entries(GROUPS).map(([key, group]) => {
                const rows = grouped[key]
                if (!rows?.length) return null
                return (
                  <Panel key={key} title={group.title}>
                    <div className="stack gap-4">
                      {group.note && (
                        <p className="secondary" style={{ fontSize: 'var(--text-xs)', lineHeight: 'var(--leading-relaxed)' }}>
                          {group.note}
                        </p>
                      )}
                      {rows.map((setting) => (
                        <SettingRow key={setting.key} setting={setting} onSaved={state.reload} />
                      ))}
                    </div>
                  </Panel>
                )
              })}
            </div>
          )
        }}
      </Async>
    </>
  )
}

function SettingRow({ setting, onSaved }) {
  const isBool = typeof setting.value === 'boolean'
  const [value, setValue] = useState(isBool ? setting.value : JSON.stringify(setting.value))
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)

  const dirty = isBool ? value !== setting.value : value !== JSON.stringify(setting.value)

  async function save() {
    setSaving(true)
    setError(null)
    try {
      const parsed = isBool ? value : JSON.parse(value)
      await api.updateSetting(setting.key, parsed)
      onSaved()
    } catch (err) {
      setError(err instanceof SyntaxError ? { message: 'Not valid JSON.' } : err)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="stack gap-2" style={{ paddingBottom: 'var(--space-3)', borderBottom: '1px solid var(--border-subtle)' }}>
      <div className="row between wrap gap-2">
        <div className="stack" style={{ gap: 0 }}>
          <strong style={{ fontSize: 'var(--text-sm)' }}>{humanise(setting.key)}</strong>
          <code className="mono faint" style={{ fontSize: 'var(--text-2xs)' }}>{setting.key}</code>
        </div>
        {setting.updated_at && (
          <span className="faint" style={{ fontSize: 'var(--text-2xs)' }}>
            changed {relative(setting.updated_at)}
          </span>
        )}
      </div>

      {setting.description && (
        <span className="secondary" style={{ fontSize: 'var(--text-xs)' }}>{setting.description}</span>
      )}

      <div className="filters">
        {isBool ? (
          <div className="segmented">
            <button className={value ? 'is-active' : ''} onClick={() => setValue(true)}>On</button>
            <button className={!value ? 'is-active' : ''} onClick={() => setValue(false)}>Off</button>
          </div>
        ) : (
          <input
            className="input mono"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            aria-label={setting.key}
            style={{ fontSize: 'var(--text-xs)' }}
          />
        )}
        <button className="btn btn--sm btn--primary" onClick={save} disabled={!dirty || saving}>
          {saving ? 'Saving…' : 'Save'}
        </button>
      </div>

      {error && <ErrorState error={error} />}
    </div>
  )
}
