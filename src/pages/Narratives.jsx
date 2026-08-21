import { api } from '../api/client.js'
import { useApi } from '../api/useApi.js'
import { Async, Badge, Delta, Empty, Panel, Sample } from '../components/primitives.jsx'
import { count, percent, relative } from '../lib/format.js'

/**
 * Narratives (§16).
 *
 * `is_emergent` marks themes the analysis discovered rather than ones seeded
 * in code. That flag is surfaced prominently: a seeded category appearing
 * often is unsurprising, while an emergent one is the system doing the job it
 * was built for.
 */
export default function Narratives() {
  const state = useApi(() => api.narratives({ limit: 100 }), [])

  return (
    <>
      <div className="page-head">
        <h2 className="page-head__title">Narratives</h2>
        <p className="page-head__sub">
          Themes recurring across names, tickers and descriptions. Emergent themes were
          discovered from the data, not defined in advance.
        </p>
      </div>

      <Async
        state={state}
        empty={
          <Panel>
            <Empty
              title="No narratives yet"
              body="Narratives are extracted during analysis, once enough qualified tokens exist to find recurring language."
            />
          </Panel>
        }
      >
        {(data) => (
          <Panel title={`${count(data.total)} narratives`} flush>
            <div className="table-wrap">
              <table className="table table--responsive">
                <thead>
                  <tr>
                    <th>Narrative</th>
                    <th>Origin</th>
                    <th className="num">Share of qualified</th>
                    <th className="num">Baseline</th>
                    <th className="num">Change</th>
                    <th>Last seen</th>
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((n) => {
                    const change =
                      n.share_of_qualified !== null && n.baseline_share !== null
                        ? n.share_of_qualified - n.baseline_share
                        : null
                    return (
                      <tr key={n.slug}>
                        <td className="primary" data-label="Narrative">
                          <div className="stack" style={{ gap: 0 }}>
                            <strong>{n.label}</strong>
                            {n.category && (
                              <span className="faint" style={{ fontSize: 'var(--text-2xs)' }}>{n.category}</span>
                            )}
                          </div>
                        </td>
                        <td data-label="Origin">
                          {n.is_emergent
                            ? <Badge status="new" variant="outline">Emergent</Badge>
                            : <Badge status="stable" variant="plain">Seeded</Badge>}
                        </td>
                        <td className="num" data-label="Share of qualified">
                          <Sample
                            sample={{ count: n.qualified_count, total: n.token_count, frequency: n.share_of_qualified }}
                          />
                        </td>
                        <td className="num" data-label="Baseline">{percent(n.baseline_share)}</td>
                        <td className="num" data-label="Change"><Delta value={change} /></td>
                        <td data-label="Last seen" className="faint">{relative(n.last_seen_at)}</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          </Panel>
        )}
      </Async>
    </>
  )
}
