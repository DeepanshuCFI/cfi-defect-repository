import { useMemo, useState } from 'react'
import { Flag } from 'lucide-react'

export default function Rankings({ features, meta, tierColor, tierText, tierLabel, onOpen }) {
  const [state, setState] = useState('')
  const rows = useMemo(() =>
    features
      .filter(f => !state || f.properties.state === state)
      .sort((a, b) => (b.properties.score || 0) - (a.properties.score || 0)),
    [features, state])

  const csv = () => {
    const head = ['rank', 'score', 'tier', 'escalation_candidate', 'road_name', 'city',
      'district', 'state', 'incidents', 'fatalities', 'injuries', 'first_crash',
      'last_crash', 'dominant_defects', 'lat', 'lon']
    const lines = rows.map((f, ix) => {
      const p = f.properties
      const [lon, lat] = f.geometry.coordinates
      return [ix + 1, p.score, p.tier, p.escalation, q(p.road_name), q(p.city), q(p.district),
        q(p.state), p.incidents, p.fatalities, p.injuries, p.first, p.last,
        q((p.defects || []).map(d => meta.defect_labels[d] || d).join('; ')), lat, lon].join(',')
    })
    const blob = new Blob([head.join(',') + '\n' + lines.join('\n')], { type: 'text/csv' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `cfi-defect-hotspots${state ? '-' + state : ''}-${new Date().toISOString().slice(0, 10)}.csv`
    a.click()
  }

  return (
    <div className="flex-1 overflow-y-auto bg-brand-soft">
      <div className="max-w-5xl mx-auto px-6 py-8">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <span className="font-heading text-[11px] tracking-[0.25em] text-brand">PRIORITY RANKINGS</span>
            <h2 className="font-heading font-bold text-2xl mt-1">
              {state || 'National'} — top priority locations
            </h2>
          </div>
          <div className="flex gap-2 no-print">
            <select value={state} onChange={e => setState(e.target.value)}
              className="h-11 border border-border rounded-full px-4 bg-white text-[13px]">
              <option value="">All India</option>
              {meta.states.map(s => <option key={s}>{s}</option>)}
            </select>
            <button onClick={csv}
              className="h-11 px-6 rounded-full bg-brand text-white text-[13px] cursor-pointer shadow-lg shadow-brand/30 hover:bg-brand/90">
              Export evidence pack (CSV)
            </button>
            <button onClick={() => window.print()}
              className="h-11 px-6 rounded-full border border-border bg-white text-[13px] cursor-pointer">
              Print / PDF
            </button>
          </div>
        </div>

        <div className="mt-6 bg-white border border-border rounded-2xl overflow-hidden">
          <div className="grid grid-cols-[3rem_1fr_7rem_5rem_5rem_5rem] gap-2 px-5 py-2.5 bg-brand-soft
                          border-b border-border font-heading text-[10px] tracking-[0.15em] text-muted-foreground">
            <span>#</span><span>LOCATION</span><span>PRIORITY</span>
            <span>CRASHES</span><span>DEATHS</span><span>INJURIES</span>
          </div>
          {rows.map((f, ix) => {
            const p = f.properties
            return (
              <button key={p.id} onClick={() => onOpen(f)}
                className="grid grid-cols-[3rem_1fr_7rem_5rem_5rem_5rem] gap-2 px-5 py-3 w-full text-left
                           border-b border-border last:border-0 hover:bg-brand-soft cursor-pointer items-center">
                <span className="font-heading font-bold text-muted-foreground">{ix + 1}</span>
                <span className="min-w-0">
                  <span className="font-heading font-semibold text-[13.5px] block truncate">
                    {p.road_name || 'Unnamed stretch'}
                    {p.escalation && <Flag className="inline h-3.5 w-3.5 text-danger ml-1 align-[-2px]" />}
                  </span>
                  <span className="text-[12px] text-muted-foreground block truncate">
                    {[p.district, p.state].filter(Boolean).join(', ')}
                    {(p.defects || []).length > 0 &&
                      ' · ' + p.defects.map(d => meta.defect_labels[d] || d).join(', ')}
                  </span>
                </span>
                <span>
                  <span className="font-heading font-bold text-[12px] px-2.5 py-1 rounded-full"
                    style={{ background: tierColor[p.tier], color: tierText?.[p.tier] || '#fff' }}>
                    {Number(p.score).toFixed(1)}
                  </span>
                </span>
                <span className="font-heading font-bold">{p.incidents}</span>
                <span className="font-heading font-bold text-danger">{p.fatalities}</span>
                <span className="font-heading font-bold">{p.injuries}</span>
              </button>
            )
          })}
        </div>
        <p className="text-[11.5px] text-muted-foreground mt-4">
          Ranked by transparent priority score (casualties · frequency · recency · vulnerable users ·
          defect severity · evidence). <Flag className="inline h-3 w-3 text-danger align-[-1px]" /> = ≥3 crashes
          in 6 months. Click a row for the full dossier with sources. CSV includes coordinates for GIS use.
        </p>
      </div>
    </div>
  )
}

const q = s => `"${String(s ?? '').replaceAll('"', '""')}"`
