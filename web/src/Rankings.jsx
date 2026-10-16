import { useMemo, useState } from 'react'
import { Download, Flag } from 'lucide-react'
import { EVIDENCE_HINT, TIER_COLOR, TIER_LABEL, TIER_TEXT } from './vocab.js'

export default function Rankings({ features, meta, onOpen }) {
  const [state, setState] = useState('')
  const rows = useMemo(() =>
    features
      .filter(f => !state || f.properties.state === state)
      .sort((a, b) => (b.properties.score || 0) - (a.properties.score || 0)),
    [features, state])

  const csv = () => {
    const head = ['rank', 'score', 'tier', 'repeat_crashes', 'location_precision', 'road_name', 'city',
      'district', 'state', 'public_reports', 'records_not_shown', 'fatalities', 'injuries', 'first_report',
      'last_report', 'dominant_defects', 'lat', 'lon']
    const lines = rows.map((f, ix) => {
      const p = f.properties
      const [lon, lat] = f.geometry.coordinates
      return [ix + 1, p.score, p.tier, p.escalation, p.location_precision || 'feature', q(p.road_name), q(p.city),
        q(p.district), q(p.state), p.incidents, p.records_hidden ?? 0, p.fatalities, p.injuries, p.first, p.last,
        q((p.defects || []).map(d => meta.defect_labels[d] || d).join('; ')), lat, lon].join(',')
    })
    const blob = new Blob([head.join(',') + '\n' + lines.join('\n')], { type: 'text/csv' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `cfi-defect-locations${state ? '-' + state : ''}-${new Date().toISOString().slice(0, 10)}.csv`
    a.click()
  }

  return (
    <div className="flex-1 overflow-y-auto bg-brand-soft">
      <div className="max-w-5xl mx-auto px-4 sm:px-6 py-6 sm:py-8">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <span className="font-heading text-[11px] tracking-[0.25em] text-brand">PRIORITY RANKINGS</span>
            <h2 className="font-heading font-bold text-xl sm:text-2xl mt-1">
              {state || 'All covered states'} — highest-priority locations
            </h2>
            <p className="text-[12.5px] text-muted-foreground mt-1 max-w-2xl">
              Ranked by a transparent score over public evidence only. Tiers are quantiles: Critical is the
              top 5 percent of locations, High the top 15, Medium the top 40.
            </p>
          </div>
          <div className="flex flex-wrap gap-2 no-print">
            <select value={state} onChange={e => setState(e.target.value)} aria-label="State"
              className="h-11 border border-border rounded-full px-4 bg-white text-[13px]">
              <option value="">All covered states</option>
              {meta.states.map(s => <option key={s}>{s}</option>)}
            </select>
            <button onClick={csv}
              className="inline-flex items-center gap-1.5 h-11 px-5 rounded-full bg-brand text-white text-[13px] cursor-pointer shadow-lg shadow-brand/30 hover:bg-brand/90 whitespace-nowrap">
              <Download className="h-4 w-4" /> Export CSV
            </button>
          </div>
        </div>

        <div className="mt-5 bg-white border border-border rounded-2xl overflow-hidden">
          <div className="hidden md:grid grid-cols-[3rem_1fr_7rem_5rem_5rem_5rem] gap-2 px-5 py-2.5 bg-brand-soft
                          border-b border-border font-heading text-[10px] tracking-[0.15em] text-muted-foreground">
            <span>#</span><span>LOCATION</span><span>PRIORITY</span>
            <span>REPORTS</span><span>DEATHS</span><span>INJURIES</span>
          </div>
          {rows.slice(0, 500).map((f, ix) => {
            const p = f.properties
            return (
              <button key={p.id} onClick={() => onOpen(f)}
                className="w-full text-left border-b border-border last:border-0 hover:bg-brand-soft cursor-pointer
                           grid md:grid-cols-[3rem_1fr_7rem_5rem_5rem_5rem] grid-cols-[2.5rem_1fr] gap-x-2 gap-y-1 px-4 md:px-5 py-3 items-center">
                <span className="font-heading font-bold text-muted-foreground">{ix + 1}</span>
                <span className="min-w-0">
                  <span className="font-heading font-semibold text-[13.5px] block truncate">
                    {p.road_name || 'Unnamed stretch'}
                    {p.escalation && <Flag className="inline h-3.5 w-3.5 text-danger ml-1 align-[-2px]" aria-label="Repeat crashes" />}
                  </span>
                  <span className="text-[12px] text-muted-foreground block truncate">
                    {[p.district, p.state].filter(Boolean).join(', ')}
                    {(p.defects || []).length > 0 &&
                      ' · ' + p.defects.map(d => meta.defect_labels[d] || d).join(', ')}
                  </span>
                </span>
                {/* phone: one summary line under the name; desktop: the columns */}
                <span className="md:hidden col-start-2 text-[12px] text-muted-foreground flex flex-wrap gap-x-3 gap-y-1 items-center">
                  <span className="font-heading font-bold text-[11px] px-2 py-0.5 rounded-full"
                    style={{ background: TIER_COLOR[p.tier], color: TIER_TEXT?.[p.tier] || '#fff' }}>
                    {TIER_LABEL[p.tier]} {Number(p.score).toFixed(0)}
                  </span>
                  <span>{p.incidents} report{p.incidents === 1 ? '' : 's'}</span>
                  {p.fatalities > 0 && <span className="text-danger font-semibold">{p.fatalities} death{p.fatalities === 1 ? '' : 's'}</span>}
                  {p.injuries > 0 && <span>{p.injuries} injured</span>}
                </span>
                <span className="hidden md:block">
                  <span className="font-heading font-bold text-[12px] px-2.5 py-1 rounded-full"
                    style={{ background: TIER_COLOR[p.tier], color: TIER_TEXT?.[p.tier] || '#fff' }}>
                    {Number(p.score).toFixed(1)}
                  </span>
                </span>
                <span className="hidden md:block font-heading font-bold tabular-nums">{p.incidents}</span>
                <span className="hidden md:block font-heading font-bold text-danger tabular-nums">{p.fatalities}</span>
                <span className="hidden md:block font-heading font-bold tabular-nums">{p.injuries}</span>
              </button>
            )
          })}
        </div>
        <p className="text-[11.5px] text-muted-foreground mt-4">
          Score weighs casualties, report frequency, recency, vulnerable road users, defect severity and
          evidence strength — public reports only. <Flag className="inline h-3 w-3 text-danger align-[-1px]" /> = {EVIDENCE_HINT.escalation}.
          {rows.length > 500 && ` Showing the top 500 of ${rows.length}; the CSV has every row.`} CSV includes coordinates for GIS use.
        </p>
      </div>
    </div>
  )
}

const q = s => `"${String(s ?? '').replaceAll('"', '""')}"`
