import { useEffect, useMemo, useState } from 'react'
import Report from './Report.jsx'
import { plural } from './vocab.js'

// Reports that are published but have no marker (16 Nov 2026).
//
// A marker needs the map provider to know the place the article names. For about one
// report in five it does not — a village road, a chauraha, a new colony — and until
// then such reports were pinned on whatever street the provider offered instead,
// usually one called "Road" in the district town. They are real reports of real
// defects, so they stay public: listed by district here, never at a guessed point.
export default function Unmapped({ meta, initial, onPlace }) {
  const [index, setIndex] = useState(null)
  const [state, setState] = useState(initial?.state || '')
  const [district, setDistrict] = useState(initial?.district || '')
  const [items, setItems] = useState(null)

  useEffect(() => {
    fetch('data/unmapped/index.json').then(r => (r.ok ? r.json() : [])).catch(() => []).then(setIndex)
  }, [])

  const states = useMemo(() => [...new Set((index || []).map(g => g.state))].sort(), [index])
  const groups = useMemo(() =>
    (index || []).filter(g => !state || g.state === state).sort((a, b) => b.count - a.count), [index, state])
  const group = useMemo(() =>
    (index || []).find(g => g.district === district && (!state || g.state === state)), [index, state, district])

  useEffect(() => {
    if (!group) { setItems(null); return }
    let live = true
    setItems(null)
    fetch(`data/unmapped/${group.file}`).then(r => (r.ok ? r.json() : [])).catch(() => [])
      .then(rows => { if (live) setItems([...rows].sort((a, b) => dateOf(b).localeCompare(dateOf(a)))) })
    return () => { live = false }
  }, [group])

  useEffect(() => { onPlace?.({ state, district }) }, [state, district, onPlace])

  const total = (index || []).reduce((n, g) => n + g.count, 0)
  const highway = (index || []).reduce((n, g) => n + (g.highway || 0), 0)

  return (
    <div className="flex-1 overflow-y-auto bg-brand-soft">
      <div className="max-w-3xl mx-auto px-4 sm:px-6 py-6 sm:py-8">
        <span className="font-heading text-[11px] tracking-[0.25em] text-brand">REPORTS WITHOUT A MARKER</span>
        <h2 className="font-heading font-bold text-xl sm:text-2xl mt-1">
          {index === null ? 'Loading…' : `${plural(total, 'report')} listed by district`}
        </h2>
        <p className="text-[13px] text-muted-foreground mt-2 max-w-2xl leading-relaxed">
          These reports are published, but the place they name could not be found on the map
          {highway > 0 ? `, or only a numbered highway could be (${highway.toLocaleString('en-IN')} of them)` : ''}.
          They are listed by district rather than pinned at a guess. Each one links to the article behind it.
        </p>

        <div className="flex flex-wrap gap-2 mt-5 no-print">
          <select value={state} onChange={e => { setState(e.target.value); setDistrict('') }} aria-label="State"
            className="h-11 border border-border rounded-full px-4 bg-white text-[13px]">
            <option value="">All covered states</option>
            {states.map(s => <option key={s}>{s}</option>)}
          </select>
          <select value={district} onChange={e => setDistrict(e.target.value)} aria-label="District"
            className="h-11 border border-border rounded-full px-4 bg-white text-[13px] max-w-full">
            <option value="">Choose a district</option>
            {groups.map(g => <option key={g.state + g.district} value={g.district}>{g.district} ({g.count})</option>)}
          </select>
        </div>

        {index !== null && total === 0 && (
          <div className="mt-6 bg-white border border-border rounded-2xl px-5 py-6 text-[13px] text-muted-foreground">
            Every public report currently has a marker on the map.
          </div>
        )}

        {!district && groups.length > 0 && (
          <div className="mt-5 bg-white border border-border rounded-2xl overflow-hidden">
            {groups.slice(0, 200).map(g => (
              <button key={g.state + g.district} onClick={() => { setState(g.state); setDistrict(g.district) }}
                className="w-full text-left px-5 py-3 border-b border-border last:border-0 hover:bg-brand-soft cursor-pointer flex items-center gap-3">
                <span className="min-w-0">
                  <span className="font-heading font-semibold text-[13.5px] block truncate">{g.district}</span>
                  <span className="text-[12px] text-muted-foreground">{g.state}</span>
                </span>
                <span className="ml-auto font-heading font-bold tabular-nums">{g.count}</span>
              </button>
            ))}
          </div>
        )}

        {district && (
          <div className="mt-5">
            <div className="font-heading text-[10px] tracking-[0.2em] text-muted-foreground mb-2">
              {items === null ? 'LOADING REPORTS' : `${plural(items.length, 'REPORT').toUpperCase()} · ${district.toUpperCase()} · NEWEST FIRST`}
            </div>
            {(items || []).map(i => <Report key={i.id} i={i} showPlace />)}
          </div>
        )}

        <p className="text-[11.5px] text-muted-foreground mt-6">
          {meta.mapped != null && `${meta.mapped.toLocaleString('en-IN')} other reports are on the map. `}
          A report moves to the map when its place can be named there.
        </p>
      </div>
    </div>
  )
}

const dateOf = i => i.date || i.sources?.[0]?.date || ''
