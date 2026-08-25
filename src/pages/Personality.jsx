import { useState } from 'react'

import { api } from '../api/client.js'
import { useApi } from '../api/useApi.js'
import { Async, Empty, ErrorState, Panel } from '../components/primitives.jsx'
import { relative } from '../lib/format.js'

/**
 * Personality.
 *
 * One paragraph in, five voice fields out (§ 2026-08-25 — "I cannot fill
 * that one by one"). The operator writes a free-form description of how
 * they want Annie to sound; an LLM extracts tone/communication_style/
 * skepticism_level/pushback_degree/explanation_style from it. Those five
 * fields are shown below as a read-only preview of what got extracted, not
 * as editable inputs — the paragraph is the single source of truth, so
 * hand-editing an extracted field would just get silently overwritten the
 * next time the paragraph is re-submitted.
 *
 * What is deliberately absent: the rules that keep Annie honest (every
 * number from a real tool call, every claim labelled fact/inference/
 * hypothesis/speculation, no trading advice) are hard-coded in
 * app/annie/persona.py, not stored in this config, and not editable from
 * here. The panel at the bottom explains that rather than pretending it's a
 * setting.
 */
const EXTRACTED_FIELDS = [
  { key: 'description', label: 'Overall description' },
  { key: 'tone', label: 'Tone' },
  { key: 'communication_style', label: 'Communication style' },
  { key: 'skepticism_level', label: 'Skepticism level' },
  { key: 'pushback_degree', label: 'Pushback degree' },
  { key: 'explanation_style', label: 'Explanation style' },
]

export default function Personality() {
  const state = useApi(() => api.personality(), [])

  return (
    <>
      <div className="page-head">
        <h2 className="page-head__title">Personality</h2>
        <p className="page-head__sub">
          Describe how you want Annie to sound in your own words. What keeps her honest is not
          editable here, and is explained below rather than exposed as a setting.
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

            <ParagraphEditor sourceText={data.source_text} onSaved={state.reload} />

            <Panel title="What Annie extracted from that" meta="read-only — edit the paragraph above to change these">
              <div className="stack gap-4">
                {EXTRACTED_FIELDS.map((f) => (
                  <div key={f.key} className="stack gap-1">
                    <strong style={{ fontSize: 'var(--text-sm)' }}>{f.label}</strong>
                    {data[f.key] ? (
                      <p className="secondary" style={{ fontSize: 'var(--text-sm)', lineHeight: 'var(--leading-relaxed)' }}>
                        {data[f.key]}
                      </p>
                    ) : (
                      <span className="faint" style={{ fontSize: 'var(--text-xs)' }}>
                        Nothing extracted yet — write a paragraph above and hit Update.
                      </span>
                    )}
                  </div>
                ))}
              </div>
            </Panel>

            <Panel title="Rules (not editable here)">
              <p className="faint" style={{ fontSize: 'var(--text-xs)', lineHeight: 'var(--leading-relaxed)' }}>
                Annie’s core rules are hard-coded in the backend (<code className="mono">app/annie/persona.py</code>)
                and intentionally not configurable from this page: every number she states must
                come from a real tool call made during that conversation, every claim is labelled
                fact, inference, hypothesis or speculation, every percentage carries its sample
                size, and she never gives trading advice. The paragraph above changes how she
                sounds — it can't change what keeps her honest.
              </p>
            </Panel>
          </div>
        )}
      </Async>
    </>
  )
}

function ParagraphEditor({ sourceText, onSaved }) {
  const initial = sourceText || ''
  const [text, setText] = useState(initial)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)

  const dirty = text !== initial

  async function update() {
    setSaving(true)
    setError(null)
    try {
      await api.extractPersonality(text)
      onSaved()
    } catch (err) {
      setError(err)
    } finally {
      setSaving(false)
    }
  }

  return (
    <Panel title="Describe Annie" meta="one paragraph, plain English">
      <div className="stack gap-3">
        <textarea
          className="input"
          rows={6}
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="e.g. I want her blunt and fast — no hedging, no summarising my question back to me. She should push back hard when the data disagrees with me, but never be condescending about it. Explain things the way you'd explain them to a sharp trader who doesn't know the codebase, not an engineer."
          aria-label="Describe Annie's personality"
          style={{ fontSize: 'var(--text-sm)', lineHeight: 'var(--leading-relaxed)', resize: 'vertical', minHeight: 140 }}
        />
        <div className="row gap-2">
          <button className="btn btn--sm btn--primary" onClick={update} disabled={!dirty || !text.trim() || saving}>
            {saving ? 'Updating…' : 'Update'}
          </button>
          {saving && (
            <span className="faint" style={{ fontSize: 'var(--text-xs)' }}>Extracting tone, style, and how she pushes back…</span>
          )}
        </div>
        {error && <ErrorState error={error} />}
      </div>
    </Panel>
  )
}
