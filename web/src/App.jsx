import { useEffect, useMemo, useState } from 'react'
import MapView from './MapView.jsx'
import Drawer from './Drawer.jsx'
import Rankings from './Rankings.jsx'

const TIER_COLOR = { critical: '#F10015', high: '#F57C00', medium: '#4A35FF', watch: '#777589' }
const TIER_LABEL = { critical: 'Critical', high: 'High', medium: 'Medium', watch: 'Watch' }

export default function App() {
  const [data, setData] = useState(null)
  const [tab, setTab] = useState('map')
  const [selected, setSelected] = useState(null)          // hotspot feature
  const [filters, setFilters] = useState({ state: '', district: '', road: '', defect: '', tier: '', repeatOnly: false })
  const [heat, setHeat] = useState(false)

  useEffect(() => {
    Promise.all([
      fetch('data/hotspots.geojson').then(r => r.json()),
      fetch('data/incidents.json').then(r => r.json()),
      fetch('data/meta.json').then(r => r.json()),
    ]).then(([hs, inc, meta]) => setData({ hs, inc, meta }))
  }, [])

  const filtered = useMemo(() => {
    if (!data) return []
    return data.hs.features.filter(f => {
      const p = f.properties
      if (filters.state && p.state !== filters.state) return false
      if (filters.district && p.district !== filters.district) return false
      if (filters.defect && !(p.defects || []).includes(filters.defect)) return false
      if (filters.tier && p.tier !== filters.tier) return false
      if (filters.repeatOnly && p.incidents < 2) return false
      if (filters.road) {
        const members = data.inc.filter(i => i.hotspot_id === p.id)
        if (!members.some(i => i.road_type === filters.road)) return false
      }
      return true
    })
  }, [data, filters])

  if (!data) return (
    <div className="h-screen grid place-items-center text-muted">Loading registry…</div>
  )

  const { meta } = data
  const districts = [...new Set(data.hs.features
    .filter(f => !filters.state || f.properties.state === filters.state)
    .map(f => f.properties.district).filter(Boolean))].sort()
  const defectsInData = [...new Set(data.hs.features.flatMap(f => f.properties.defects || []))].sort()
  const roadTypes = [...new Set(data.inc.map(i => i.road_type).filter(Boolean))].sort()

  const sel = (k, v) => setFilters(f => ({ ...f, [k]: v, ...(k === 'state' ? { district: '' } : {}) }))

  return (
    <div className="h-screen flex flex-col">
      {/* top bar */}
      <header className="border-b border-line bg-white z-20">
        <div className="px-5 h-14 flex items-center justify-between gap-4">
          <div className="flex items-baseline gap-3 min-w-0">
            <span className="font-heading font-bold text-brand text-[16px] whitespace-nowrap">
              Road Infrastructure Defect Repository
            </span>
            <span className="text-muted text-xs whitespace-nowrap hidden sm:inline">by Crashfree India</span>
          </div>
          <nav className="flex gap-1 no-print">
            {['map', 'rankings', 'method'].map(t => (
              <button key={t} onClick={() => setTab(t)}
                className={`px-4 py-1.5 rounded-full text-[13px] capitalize cursor-pointer
                  ${tab === t ? 'bg-brand text-white' : 'text-muted hover:text-ink'}`}>
                {t}
              </button>
            ))}
          </nav>
        </div>
      </header>

      {/* disclaimer ribbon */}
      <div className="bg-brand-soft border-b border-line px-5 py-1.5 text-[12px] text-muted">
        <b className="text-warn font-heading text-[10.5px] tracking-widest">AS REPORTED&nbsp;·&nbsp;</b>
        Defects are as reported in news media; locations are indicative pending physical audit.
        Every entry links its sources. Absence of data is absence of coverage — never a safety clearance.
      </div>

      {tab === 'map' && (
        <div className="flex-1 flex min-h-0">
          {/* left panel */}
          <aside className="w-72 border-r border-line bg-white flex flex-col no-print">
            <div className="p-4 border-b border-line grid grid-cols-2 gap-2">
              <Stat n={filtered.length} l="hotspots shown" />
              <Stat n={meta.incidents} l="public incidents" />
              <Stat n={meta.fatalities} l="deaths on record" red />
              <Stat n={meta.injuries} l="injuries on record" />
            </div>
            <div className="p-4 flex flex-col gap-2.5 text-[13px] overflow-y-auto">
              <Select label="State" value={filters.state} onChange={v => sel('state', v)}
                options={meta.states} />
              <Select label="District" value={filters.district} onChange={v => sel('district', v)}
                options={districts} />
              <Select label="Road type" value={filters.road} onChange={v => sel('road', v)}
                options={roadTypes} />
              <Select label="Defect" value={filters.defect} onChange={v => sel('defect', v)}
                options={defectsInData} labels={meta.defect_labels} />
              <Select label="Priority tier" value={filters.tier} onChange={v => sel('tier', v)}
                options={['critical', 'high', 'medium', 'watch']} labels={TIER_LABEL} />
              <label className="flex items-center gap-2 mt-1 cursor-pointer text-muted">
                <input type="checkbox" checked={filters.repeatOnly}
                  onChange={e => sel('repeatOnly', e.target.checked)} className="accent-[#F10015]" />
                Repeat hotspots only (2+ crashes)
              </label>
              <label className="flex items-center gap-2 cursor-pointer text-muted">
                <input type="checkbox" checked={heat} onChange={e => setHeat(e.target.checked)}
                  className="accent-[#4A35FF]" />
                Heat layer
              </label>
              <div className="mt-3 pt-3 border-t border-line">
                <div className="font-heading text-[10px] tracking-[0.2em] text-muted mb-2">PRIORITY TIERS</div>
                {Object.entries(TIER_COLOR).map(([t, c]) => (
                  <div key={t} className="flex items-center gap-2 text-[12px] text-muted py-0.5">
                    <span className="w-3 h-3 rounded-full" style={{ background: c }} />
                    {TIER_LABEL[t]}
                  </div>
                ))}
              </div>
            </div>
            <div className="mt-auto p-4 text-[11px] text-muted border-t border-line">
              Updated {new Date(meta.generated_at).toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric' })} · {meta.hotspots} hotspots on record
            </div>
          </aside>

          {/* map */}
          <main className="flex-1 relative min-w-0">
            <MapView features={filtered} heat={heat} tierColor={TIER_COLOR}
              onSelect={f => setSelected(f)} selectedId={selected?.properties?.id} />
            {selected && (
              <Drawer feature={selected} incidents={data.inc} meta={meta}
                tierColor={TIER_COLOR} tierLabel={TIER_LABEL}
                onClose={() => setSelected(null)} />
            )}
          </main>
        </div>
      )}

      {tab === 'rankings' && (
        <Rankings features={data.hs.features} meta={meta}
          tierColor={TIER_COLOR} tierLabel={TIER_LABEL}
          onOpen={f => { setSelected(f); setTab('map') }} />
      )}

      {tab === 'method' && <Method meta={meta} />}
    </div>
  )
}

function Stat({ n, l, red }) {
  return (
    <div className="rounded-xl border border-line p-2.5">
      <div className={`font-heading font-bold text-xl ${red ? 'text-danger' : 'text-brand'}`}>{n}</div>
      <div className="text-[11px] text-muted leading-tight mt-0.5">{l}</div>
    </div>
  )
}

function Select({ label, value, onChange, options, labels }) {
  return (
    <label className="block">
      <span className="font-heading text-[10px] tracking-[0.2em] text-muted">{label.toUpperCase()}</span>
      <select value={value} onChange={e => onChange(e.target.value)}
        className="mt-1 w-full border border-line rounded-lg px-2.5 py-1.5 bg-white text-[13px]">
        <option value="">All</option>
        {options.map(o => <option key={o} value={o}>{labels?.[o] || o}</option>)}
      </select>
    </label>
  )
}

function Method({ meta }) {
  const Item = ({ k, t, children }) => (
    <div className="rounded-2xl border border-line p-6">
      <div className="font-heading text-[10.5px] tracking-[0.2em] text-brand">{k}</div>
      <h3 className="font-heading font-bold mt-1.5">{t}</h3>
      <p className="text-[13.5px] text-muted mt-2 leading-relaxed">{children}</p>
    </div>
  )
  return (
    <div className="flex-1 overflow-y-auto">
      <div className="max-w-4xl mx-auto px-6 py-12">
        <span className="font-heading text-[11px] tracking-[0.25em] text-brand">METHOD & HONESTY</span>
        <h2 className="font-heading font-bold text-3xl mt-4 leading-tight">
          Built to be quoted.<br /><span className="text-brand">So it holds itself to evidence.</span>
        </h2>
        <div className="grid sm:grid-cols-2 gap-4 mt-8">
          <Item k="01 · SOURCE" t="Everything traces to published articles">
            Every incident links every article that reported it — outlet, date, URL. No source, no record.
            Median public entry carries its evidence quotes verbatim, in the original language.
          </Item>
          <Item k="02 · CONFIDENCE GATE" t="Only high-confidence entries publish">
            An entry appears here only if extraction confidence ≥ 0.7, location confidence ≥ 0.6, and the
            coverage itself implicates infrastructure — or a human reviewer approved it. Everything else
            waits in an internal review queue.
          </Item>
          <Item k="03 · NO INFERRED BLAME" t="Defect tags only when coverage claims one">
            A crash report with no infrastructure claim is stored with no defect. The repository never
            invents a pothole to explain a death, and it reports attribution, not accusation.
          </Item>
          <Item k="04 · POSITIVE EVIDENCE ONLY" t="Confirms danger, never certifies safety">
            News coverage is incomplete by nature. This map can prove a stretch is dangerous; it can never
            prove one is safe. Coverage bias is real: better-covered districts are not more dangerous ones.
          </Item>
          <Item k="05 · TRANSPARENT PRIORITY" t="Every score shows its work">
            Hotspot priority (0–100) weighs casualties, crash frequency, recency, vulnerable road users,
            defect severity, and evidence strength — the full breakdown ships with every hotspot.
            Locations with ≥3 crashes in 6 months auto-flag for escalation.
          </Item>
          <Item k="06 · CORRECTIONS" t="Disputable by design">
            Every entry carries a “Report a correction” action. Corrections route to the review queue and
            disputed entries are withdrawn from the public view while checked.
          </Item>
        </div>
        <p className="text-[12px] text-muted mt-8">
          Registry generated {new Date(meta.generated_at).toLocaleString('en-IN')} ·
          {' '}{meta.hotspots} hotspots · {meta.incidents} incidents · Crashfree India (Vision Zero Trust)
        </p>
      </div>
    </div>
  )
}
