import { useState } from 'react'

import { api } from '../api/client.js'
import { useApi } from '../api/useApi.js'
import { Async, Empty, ErrorState, Panel } from '../components/primitives.jsx'
import { relative } from '../lib/format.js'

/**
 * Personality.
 *
 * Every field here is plain free text — tone, how she engages, how she
 * explains herself — saved one field at a time, PATCH-per-field, the same
 * discipline Settings.jsx uses for research parameters. Unlike Settings.jsx
 * there is no JSON to round-trip; these are strings, edited as strings.
 *
 * What is deliberately absent: the rules that keep Annie honest (every
 * number from a real tool call, every claim labelled fact/inference/
 * hypothesis/speculation, no trading advice) are hard-coded in
 * app/annie/persona.py, not stored in this config, and not editable from
 * here. The panel at the bottom explains that rather than pretending it's a
 * setting.
 */
const IDENTITY_FIELDS = [
  { key: 'name', label: 'Name', multiline: false },
  { key: 'description', label: 'Description', multiline: true },
]

const PERSONALITY_FIELDS = [
  { key: 'tone', label: 'Tone', multiline: true },
  { key: 'communication_style', label: 'Communication style', multiline: true },
  { key: 'skepticism_level', label: 'Skepticism level', multiline: true },
  { key: 'pushback_degree', label: 'Pushback degree', multiline: true },
  { key: 'explanation_style', label: 'Explanation style', multiline: true },
]

export default function Personality() {
  const state = useApi(() => api.personality(), [])

  return (
    <>
      <div className="page-head">
        <h2 className="page-head__title">Personality</h2>
        <p className="page-head__sub">
          How Annie sounds and how hard she pushes back — editable. What keeps her honest is not,
          and is explained below rather than exposed as a setting.
        </p>
      </div>

      <Async state={state} empty={<Panel><Empty title="No personality config" /></Panel>}>
        {(data) => (
          <div className="stack gap-5">
            {(data.updated_at || data.updated_by) && (
              <span className="faint" style={{ fontSize: 'var(--text-2xs)' }}>
                Last changed {data.updated_at ? relative(data.updated_at) : 'at an unknown time'}
                {data.updated_by && ` by ${data.updated_by}`}
              </span>
            )}

            <Panel title="Identity">
              <div className="stack gap-4">
                {IDENTITY_FIELDS.map((f) => (
                  <FieldRow key={f.key} field={f} value={data[f.key]} onSaved={state.reload} />
                ))}
              </div>
            </Panel>

            <Panel title="Personality">
              <div className="stack gap-4">
                {PERSONALITY_FIELDS.map((f) => (
                  <FieldRow key={f.key} field={f} value={data[f.key]} onSaved={state.reload} />
                ))}
              </div>
            </Panel>

            <Panel title="Rules (not editable here)">
              <p className="faint" style={{ fontSize: 'var(--text-xs)', lineHeight: 'var(--leading-relaxed)' }}>
                Annie’s core rules are hard-coded in the backend (<code className="mono">app/annie/persona.py</code>)
                and intentionally not configurable from this page: every number she states must
                come from a real tool call made during that conversation, every claim is labelled
                fact, inference, hypothesis or speculation, every percentage carries its sample
                size, and she never gives trading advice. The fields above change how she sounds —
                they can't change what keeps her honest.
              </p>
            </Panel>
          </div>
        )}
      </Async>
    </>
  )
}

function FieldRow({ field, value, onSaved }) {
  const initial = value || ''
  const [text, setText] = useState(initial)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)

  const dirty = text !== initial

  async function save() {
    setSaving(true)
    setError(null)
    try {
      await api.updatePersonality({ [field.key]: text })
      onSaved()
    } catch (err) {
      setError(err)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="stack gap-2" style={{ paddingBottom: 'var(--space-3)', borderBottom: '1px solid var(--border-subtle)' }}>
      <strong style={{ fontSize: 'var(--text-sm)' }}>{field.label}</strong>
      {field.multiline ? (
        <textarea
          className="input"
          rows={3}
          value={text}
          onChange={(e) => setText(e.target.value)}
          aria-label={field.label}
          style={{ fontSize: 'var(--text-sm)', lineHeight: 'var(--leading-relaxed)', resize: 'vertical', minHeight: 72 }}
        />
      ) : (
        <input
          className="input"
          value={text}
          onChange={(e) => setText(e.target.value)}
          aria-label={field.label}
          style={{ fontSize: 'var(--text-sm)' }}
        />
      )}
      <div className="row gap-2">
        <button className="btn btn--sm btn--primary" onClick={save} disabled={!dirty || saving}>
          {saving ? 'Saving…' : 'Save'}
        </button>
      </div>
      {error && <ErrorState error={error} />}
    </div>
  )
}
