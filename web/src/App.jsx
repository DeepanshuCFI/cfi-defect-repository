import { useEffect, useMemo, useState } from 'react'
import { List, Search, SlidersHorizontal, X } from 'lucide-react'
import MapView from './MapView.jsx'
import Dossier from './Dossier.jsx'
import Rankings from './Rankings.jsx'
import Sheet from './Sheet.jsx'
import { EVIDENCE_HINT, EVIDENCE_LABEL, ROAD_TYPE_LABEL, TIER_COLOR, TIER_HINT, TIER_LABEL, fmtDate, plural, isWaterloggingOnly } from './vocab.js'

// Filters, tab, heat and selected location live in the URL so any view is a
// citable link (?state=Rajasthan&tier=high, ?h=42, …).
const initQ = new URLSearchParams(window.location.search)
const EMPTY = { state: '', district: '', road: '', defect: '', tier: '', repeatOnly: false, hideWaterOnly: false }

function useMedia(query) {
  const [m, setM] = useState(() => window.matchMedia(query).matches)
  useEffect(() => {
    const mq = window.matchMedia(query)
    const h = e => setM(e.matches)
    mq.addEventListener('change', h)
    return () => mq.removeEventListener('change', h)
  }, [query])
  return m
}

export default function App() {
  // Under md the desktop two-pane layout is replaced by a full-bleed map with a
  // bottom sheet (launch review, 16 Oct 2026: the panel used to leave the map 87px).
  const mobile = !useMedia('(min-width: 768px)')
  const [data, setData] = useState(null)
  const [tab, setTab] = useState(['map', 'rankings', 'method'].includes(initQ.get('tab')) ? initQ.get('tab') : 'map')
  const [selected, setSelected] = useState(null)          // location feature
  const [focusTarget, setFocusTarget] = useState(null)    // location the map should fly to (not map clicks)
  const [filters, setFilters] = useState({
    state: initQ.get('state') || '', district: initQ.get('district') || '',
    road: initQ.get('road') || '', defect: initQ.get('defect') || '',
    tier: initQ.get('tier') || '', repeatOnly: initQ.get('repeat') === '1',
    hideWaterOnly: initQ.get('nowl') === '1',
  })
  const [heat, setHeat] = useState(initQ.get('heat') === '1')
  const [view, setView] = useState(null)
  const [sheet, setSheet] = useState('peek')
  const [sheetTab, setSheetTab] = useState('list')
  const [query, setQuery] = useState('')

  // The page loads locations and meta only; a dossier fetches its own reports.
  useEffect(() => {
    Promise.all([
      fetch('data/hotspots.geojson').then(r => r.json()),
      fetch('data/meta.json').then(r => r.json()),
    ]).then(([hs, meta]) => setData({ hs, meta }))
  }, [])

  useEffect(() => {
    if (!data) return
    const h = initQ.get('h')
    if (h) {
      const f = data.hs.features.find(f => String(f.properties.id) === h)
      if (f) { setSelected(f); setFocusTarget(f) }
    }
  }, [data])

  useEffect(() => {
    const p = new URLSearchParams()
    if (tab !== 'map') p.set('tab', tab)
    for (const k of ['state', 'district', 'road', 'defect', 'tier']) if (filters[k]) p.set(k, filters[k])
    if (filters.repeatOnly) p.set('repeat', '1')
    if (filters.hideWaterOnly) p.set('nowl', '1')
    if (heat) p.set('heat', '1')
    if (selected) p.set('h', selected.properties.id)
    const qs = p.toString()
    window.history.replaceState(null, '', qs ? `?${qs}` : window.location.pathname)
  }, [tab, filters, heat, selected])

  const filtered = useMemo(() => {
    if (!data) return []
    return data.hs.features.filter(f => {
      const p = f.properties
      if (filters.state && p.state !== filters.state) return false
      if (filters.district && p.district !== filters.district) return false
      if (filters.defect && !(p.defects || []).includes(filters.defect)) return false
      if (filters.tier && p.tier !== filters.tier) return false
      if (filters.repeatOnly && p.incidents < 2) return false
      if (filters.hideWaterOnly && isWaterloggingOnly(p.defects)) return false
      if (filters.road && !(p.road_types || []).includes(filters.road)) return false
      return true
    })
  }, [data, filters])

  // What the phone's list shows: the filtered locations inside the current map view.
  const inView = useMemo(() => {
    if (!view) return filtered.slice(0, 60)
    return filtered
      .filter(f => { const [x, y] = f.geometry.coordinates; return x >= view.w && x <= view.e && y >= view.s && y <= view.n })
      .sort((a, b) => (b.properties.score || 0) - (a.properties.score || 0))
      .slice(0, 60)
  }, [filtered, view])

  const results = useMemo(() => {
    if (!data || query.trim().length < 2) return []
    const q = query.trim().toLowerCase()
    return data.hs.features
      .filter(f => { const p = f.properties; return [p.road_name, p.city, p.district, p.state].some(v => v && v.toLowerCase().includes(q)) })
      .sort((a, b) => (b.properties.score || 0) - (a.properties.score || 0))
      .slice(0, 8)
  }, [data, query])

  if (!data) return (
    <div className="h-full grid place-items-center text-muted-foreground">Loading the registry…</div>
  )

  const { meta } = data
  const districts = [...new Set(data.hs.features
    .filter(f => !filters.state || f.properties.state === filters.state)
    .map(f => f.properties.district).filter(Boolean))].sort()
  const defectsInData = [...new Set(data.hs.features.flatMap(f => f.properties.defects || []))].sort()
  const roadTypes = meta.road_types || [...new Set(data.hs.features.flatMap(f => f.properties.road_types || []))].sort()
  const coverage = meta.coverage || { states: meta.states, districts: null }
  const covered = coverage.states?.length ? data.hs.features.filter(f => coverage.states.includes(f.properties.state)) : data.hs.features

  const sel = (k, v) => setFilters(f => ({ ...f, [k]: v, ...(k === 'state' ? { district: '' } : {}) }))
  const activeFilters = ['state', 'district', 'road', 'defect', 'tier'].filter(k => filters[k]).length + (filters.repeatOnly ? 1 : 0) + (filters.hideWaterOnly ? 1 : 0)
  const clearFilters = () => setFilters(EMPTY)
  const pick = f => { setSelected(f); setFocusTarget(f); setQuery(''); if (mobile) setSheet('peek') }
  const openFromRankings = f => { setSelected(f); setFocusTarget(f); setTab('map'); setSheet('peek') }

  const filterPanel = (
    <FilterPanel filters={filters} sel={sel} meta={meta} districts={districts} roadTypes={roadTypes}
      defectsInData={defectsInData} heat={heat} setHeat={setHeat} activeFilters={activeFilters}
      clearFilters={clearFilters} filtered={filtered} />
  )

  return (
    <div className="h-full flex flex-col">
      {/* top bar */}
      <header className="border-b border-border bg-white z-20 shrink-0">
        <div className="px-4 sm:px-5 h-14 flex items-center justify-between gap-3">
          <div className="flex items-center gap-3 min-w-0">
            {/* brand rule: logo min-width 180px — below sm we show the text title only */}
            <img src="cfi-logo.svg" alt="Crashfree India" className="h-6 w-auto shrink-0 hidden sm:block" />
            <span className="h-6 w-px bg-border shrink-0 hidden sm:block" aria-hidden="true" />
            <span className="font-heading font-bold text-brand text-[14px] sm:text-[15px] truncate">
              <span className="sm:hidden">Hazards map</span>
              <span className="hidden sm:inline">Road Infrastructure Defect Repository</span>
            </span>
          </div>
          <nav className="flex gap-0.5 no-print shrink-0" aria-label="Sections">
            {['map', 'rankings', 'method'].map(t => (
              <button key={t} onClick={() => setTab(t)} aria-current={tab === t ? 'page' : undefined}
                className={`h-10 px-3 sm:px-4 rounded-full text-[13px] capitalize cursor-pointer
                  ${tab === t ? 'bg-brand text-white' : 'text-muted-foreground hover:text-foreground'}`}>
                {t}
              </button>
            ))}
          </nav>
        </div>
      </header>

      {/* one-line disclaimer; the full text lives on Method (and in every dossier's citation line) */}
      <div className="bg-brand-soft border-b border-border px-4 sm:px-5 py-1.5 text-[12px] text-muted-foreground flex gap-2 items-center shrink-0">
        <b className="text-warn font-heading text-[10.5px] tracking-widest shrink-0">AS REPORTED</b>
        <span className="truncate">Reports as published in news media · locations indicative · absence of data is absence of coverage</span>
        <button onClick={() => setTab('method')} className="text-brand hover:underline cursor-pointer shrink-0 ml-auto">How we work</button>
      </div>

      {tab === 'map' && !mobile && (
        <div className="flex-1 flex min-h-0">
          <aside className="w-80 border-r border-border bg-white flex flex-col no-print">
            <div className="p-4 border-b border-border">
              <SearchBox query={query} setQuery={setQuery} results={results} onPick={pick} />
            </div>
            <div className="p-4 border-b border-border">
              <StatsGrid meta={meta} shown={filtered.length} />
              <CoverageChip coverage={coverage} className="mt-2.5" />
            </div>
            <div className="p-4 overflow-y-auto">{filterPanel}</div>
            <div className="mt-auto p-4 text-[11px] text-muted-foreground border-t border-border">
              Updated {fmtDate(meta.generated_at)} · {plural(meta.hotspots, 'location')} on record
            </div>
          </aside>
          <main className="flex-1 relative min-w-0">
            <MapView features={filtered} frame={covered} heat={heat} onSelect={f => setSelected(f)} selectedId={selected?.properties?.id}
              focus={focusTarget} fitKey={JSON.stringify(filters)} onViewChange={setView} />
            {filtered.length === 0 && <NoMatch clearFilters={clearFilters} />}
            {selected && <Dossier feature={selected} meta={meta} onClose={() => setSelected(null)} />}
          </main>
        </div>
      )}

      {tab === 'map' && mobile && (
        <main className="flex-1 relative min-h-0">
          <MapView features={filtered} frame={covered} heat={heat} onSelect={f => { setSelected(f); setSheet('peek') }}
            selectedId={selected?.properties?.id} focus={focusTarget} fitKey={JSON.stringify(filters)}
            onViewChange={setView} padding={{ top: 90, bottom: 120, left: 24, right: 24 }} />
          <div className="absolute top-3 left-3 right-14 z-10 flex flex-col gap-2">
            <SearchBox query={query} setQuery={setQuery} results={results} onPick={pick} floating />
            <CoverageChip coverage={coverage} />
          </div>
          {filtered.length === 0 && <NoMatch clearFilters={clearFilters} />}
          {!selected && (
            <Sheet state={sheet} onState={setSheet}
              title={sheet === 'peek' ? `${plural(inView.length, 'location')} in view` : undefined}>
              <div className="px-4 pb-2 flex items-center gap-2 sticky top-0 bg-white">
                <button onClick={() => { setSheetTab('list'); if (sheet === 'peek') setSheet('half') }}
                  className={`inline-flex items-center gap-1.5 h-9 px-3.5 rounded-full text-[12.5px] cursor-pointer border
                    ${sheetTab === 'list' ? 'bg-brand text-white border-brand' : 'border-border text-muted-foreground'}`}>
                  <List className="h-4 w-4" /> List
                </button>
                <button onClick={() => { setSheetTab('filters'); if (sheet === 'peek') setSheet('half') }}
                  className={`inline-flex items-center gap-1.5 h-9 px-3.5 rounded-full text-[12.5px] cursor-pointer border
                    ${sheetTab === 'filters' ? 'bg-brand text-white border-brand' : 'border-border text-muted-foreground'}`}>
                  <SlidersHorizontal className="h-4 w-4" /> Filters{activeFilters ? ` (${activeFilters})` : ''}
                </button>
                <span className="ml-auto text-[11.5px] text-muted-foreground tabular-nums">{plural(filtered.length, 'location')}</span>
              </div>
              {sheetTab === 'list' ? (
                <div className="px-4 pb-4">
                  <StatsGrid meta={meta} shown={filtered.length} compact />
                  <div className="font-heading text-[10px] tracking-[0.2em] text-muted-foreground mt-3 mb-1.5">
                    {plural(inView.length, 'LOCATION').toUpperCase()} IN VIEW · HIGHEST PRIORITY FIRST
                  </div>
                  {inView.length === 0 && <div className="text-[12.5px] text-muted-foreground py-4">Pan or zoom the map to find locations, or search above.</div>}
                  {inView.map(f => <LocationRow key={f.properties.id} f={f} meta={meta} onPick={pick} />)}
                </div>
              ) : (
                <div className="px-4 pb-4">{filterPanel}</div>
              )}
            </Sheet>
          )}
          {selected && <Dossier feature={selected} meta={meta} onClose={() => setSelected(null)} mobile />}
        </main>
      )}

      {tab === 'rankings' && (
        <Rankings features={data.hs.features} meta={meta} onOpen={openFromRankings} />
      )}

      {tab === 'method' && <Method meta={meta} coverage={coverage} />}
    </div>
  )
}

function SearchBox({ query, setQuery, results, onPick, floating }) {
  return (
    <div className="relative">
      <label className="relative block">
        <Search className="h-4 w-4 text-muted-foreground absolute left-3.5 top-1/2 -translate-y-1/2" aria-hidden="true" />
        <input value={query} onChange={e => setQuery(e.target.value)} type="search" inputMode="search"
          placeholder="Search a district, road or city" aria-label="Search a district, road or city"
          className={`w-full h-11 pl-10 pr-9 rounded-full border border-border bg-white text-[13.5px] outline-none focus:border-brand ${floating ? 'shadow-lg' : ''}`} />
        {query && (
          <button onClick={() => setQuery('')} aria-label="Clear search"
            className="absolute right-2.5 top-1/2 -translate-y-1/2 p-1 text-muted-foreground cursor-pointer"><X className="h-4 w-4" /></button>
        )}
      </label>
      {results.length > 0 && (
        <ul className="absolute left-0 right-0 mt-1.5 bg-white border border-border rounded-2xl shadow-xl overflow-hidden z-30" role="listbox">
          {results.map(f => {
            const p = f.properties
            return (
              <li key={p.id}>
                <button onClick={() => onPick(f)} role="option" aria-selected="false"
                  className="w-full text-left px-4 py-2.5 hover:bg-brand-soft cursor-pointer border-b border-border last:border-0">
                  <span className="font-heading font-semibold text-[13px] block truncate">{p.road_name || 'Unnamed stretch'}</span>
                  <span className="text-[11.5px] text-muted-foreground block truncate">
                    {[p.city, p.district, p.state].filter(Boolean).join(', ')} · {plural(p.incidents, 'report')}
                  </span>
                </button>
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}

function StatsGrid({ meta, shown, compact }) {
  const Stat = ({ n, l, red }) => (
    <div className={`rounded-xl border border-border ${compact ? 'p-2' : 'p-2.5'}`}>
      <div className={`font-heading font-bold ${compact ? 'text-lg' : 'text-xl'} tabular-nums ${red ? 'text-danger' : 'text-brand'}`}>{n.toLocaleString('en-IN')}</div>
      <div className="text-[11px] text-muted-foreground leading-tight mt-0.5">{l}</div>
    </div>
  )
  // Vocabulary: reports and locations lead; deaths keep their true base (launch review)
  return (
    <div className={`grid gap-2 ${compact ? 'grid-cols-4' : 'grid-cols-2'}`}>
      <Stat n={meta.incidents} l="public reports" />
      <Stat n={shown} l="locations shown" />
      <Stat n={meta.with_casualties ?? 0} l="reports with casualties" />
      <Stat n={meta.fatalities} l="deaths on record" red />
    </div>
  )
}

function CoverageChip({ coverage, className = '' }) {
  if (!coverage?.states?.length) return null
  return (
    <div className={`inline-flex max-w-full items-center gap-1.5 rounded-full bg-white/95 border border-border px-3 py-1 text-[11px] text-muted-foreground shadow-sm ${className}`}>
      <span className="w-1.5 h-1.5 rounded-full bg-brand shrink-0" aria-hidden="true" />
      <span className="truncate">Covers {coverage.states.join(', ')}{coverage.districts ? ` · ${coverage.districts} districts` : ''} · expanding</span>
    </div>
  )
}

function LocationRow({ f, meta, onPick }) {
  const p = f.properties
  return (
    <button onClick={() => onPick(f)}
      className="w-full text-left rounded-xl border border-border px-3 py-2.5 mb-2 hover:bg-brand-soft cursor-pointer">
      <div className="flex items-center gap-2">
        <span className="w-2.5 h-2.5 rounded-full shrink-0" style={{ background: TIER_COLOR[p.tier] }} aria-hidden="true" />
        <span className="font-heading font-semibold text-[13px] truncate">{p.road_name || 'Unnamed stretch'}</span>
        <span className="ml-auto text-[11px] text-muted-foreground shrink-0">{TIER_LABEL[p.tier]}</span>
      </div>
      <div className="text-[11.5px] text-muted-foreground mt-0.5 truncate">
        {[p.city, p.district].filter(Boolean).join(', ')} · {plural(p.incidents, 'report')}
        {p.fatalities > 0 ? ` · ${plural(p.fatalities, 'death')}` : ''}
        {p.escalation ? ` · ${EVIDENCE_LABEL.escalation.toLowerCase()}` : ''}
        {(p.defects || []).length ? ` · ${(meta.defect_labels[p.defects[0]] || p.defects[0])}` : ''}
      </div>
    </button>
  )
}

function FilterPanel({ filters, sel, meta, districts, roadTypes, defectsInData, heat, setHeat, activeFilters, clearFilters, filtered }) {
  return (
    <div className="flex flex-col gap-2.5 text-[13px]">
      <Select label="State" value={filters.state} onChange={v => sel('state', v)} options={meta.states} />
      <Select label="District" value={filters.district} onChange={v => sel('district', v)} options={districts} />
      <Select label="Road type" value={filters.road} onChange={v => sel('road', v)} options={roadTypes} labels={ROAD_TYPE_LABEL} />
      <Select label="Defect" value={filters.defect} onChange={v => sel('defect', v)} options={defectsInData} labels={meta.defect_labels} />
      <Select label="Priority tier" value={filters.tier} onChange={v => sel('tier', v)}
        options={['critical', 'high', 'medium', 'watch']} labels={Object.fromEntries(Object.keys(TIER_LABEL).map(t => [t, `${TIER_LABEL[t]} — ${TIER_HINT[t]}`]))} />
      <label className="flex items-center gap-2 mt-1 cursor-pointer text-muted-foreground">
        <input type="checkbox" checked={filters.repeatOnly} onChange={e => sel('repeatOnly', e.target.checked)} className="accent-danger h-4 w-4" />
        Repeated reports only (2+)
      </label>
      <label className="flex items-start gap-2 cursor-pointer text-muted-foreground">
        <input type="checkbox" checked={filters.hideWaterOnly} onChange={e => sel('hideWaterOnly', e.target.checked)} className="accent-danger h-4 w-4 mt-0.5" />
        <span>Hide waterlogging-only locations
          <span className="block text-[11.5px] leading-snug">Seasonal: drainage reports with no other defect named</span>
        </span>
      </label>
      <label className="flex items-center gap-2 cursor-pointer text-muted-foreground">
        <input type="checkbox" checked={heat} onChange={e => setHeat(e.target.checked)} className="accent-brand h-4 w-4" />
        Heat layer
      </label>
      {activeFilters > 0 && (
        <button onClick={clearFilters}
          className="mt-1 h-10 rounded-full border border-border text-[12.5px] text-brand hover:bg-brand-soft cursor-pointer">
          Clear all filters ({activeFilters})
        </button>
      )}
      <div className="mt-3 pt-3 border-t border-border">
        <div className="font-heading text-[10px] tracking-[0.2em] text-muted-foreground mb-2">PRIORITY TIERS</div>
        {Object.entries(TIER_COLOR).map(([t, c]) => (
          <div key={t} className="flex items-center gap-2 text-[12px] text-muted-foreground py-0.5">
            <span className="w-3 h-3 rounded-full shrink-0" style={{ background: c }} />
            {TIER_LABEL[t]} <span className="text-[11px]">· {TIER_HINT[t]}</span>
            <span className="ml-auto tabular-nums text-[11px]">{filtered.filter(f => f.properties.tier === t).length}</span>
          </div>
        ))}
        <div className="flex items-center gap-2 text-[12px] text-muted-foreground pt-2 mt-1 border-t border-border">
          <span className="w-5 h-5 rounded-full grid place-items-center text-[9px] font-bold text-white shrink-0 bg-brand">12</span>
          A blue bubble is a count of locations — tap to zoom in. Colour shows priority only on single locations.
        </div>
        <div className="text-[12px] text-muted-foreground pt-2 mt-1 border-t border-border">
          {Object.entries(EVIDENCE_LABEL).map(([k, v]) => <div key={k}><b className="text-foreground font-medium">{v}</b> · {EVIDENCE_HINT[k]}</div>)}
        </div>
      </div>
    </div>
  )
}

function NoMatch({ clearFilters }) {
  return (
    <div className="absolute inset-0 z-10 grid place-items-center pointer-events-none px-6">
      <div className="pointer-events-auto bg-white border border-border rounded-2xl px-6 py-5 max-w-sm text-center shadow-lg">
        <div className="font-heading font-bold text-[14px]">No locations match these filters</div>
        <p className="text-[12.5px] text-muted-foreground mt-1.5">Absence here is absence of coverage — never a safety clearance.</p>
        <button onClick={clearFilters} className="mt-3 h-10 px-5 rounded-full bg-brand text-white text-[13px] cursor-pointer">Clear all filters</button>
      </div>
    </div>
  )
}

function Select({ label, value, onChange, options, labels }) {
  const id = `f-${label.toLowerCase().replace(/\s+/g, '-')}`
  return (
    <label className="block" htmlFor={id}>
      <span className="font-heading text-[10px] tracking-[0.2em] text-muted-foreground">{label.toUpperCase()}</span>
      <select id={id} value={value} onChange={e => onChange(e.target.value)}
        className="mt-1 w-full h-10 border border-border rounded-lg px-2.5 bg-white text-[13px]">
        <option value="">All</option>
        {options.map(o => <option key={o} value={o}>{labels?.[o] || o}</option>)}
      </select>
    </label>
  )
}

function Method({ meta, coverage }) {
  const Item = ({ k, t, children }) => (
    <div className="rounded-2xl border border-border p-5 sm:p-6">
      <div className="font-heading text-[10.5px] tracking-[0.2em] text-brand">{k}</div>
      <h3 className="font-heading font-bold mt-1.5">{t}</h3>
      <p className="text-[13.5px] text-muted-foreground mt-2 leading-relaxed">{children}</p>
    </div>
  )
  return (
    <div className="flex-1 overflow-y-auto">
      <div className="max-w-4xl mx-auto px-4 sm:px-6 py-8 sm:py-12">
        <span className="font-heading text-[11px] tracking-[0.25em] text-brand">METHOD & HONESTY</span>
        <h2 className="font-heading font-bold text-2xl sm:text-3xl mt-4 leading-tight">
          Built to be quoted.<br /><span className="text-brand">So it holds itself to evidence.</span>
        </h2>
        <div className="grid sm:grid-cols-2 gap-4 mt-8">
          <Item k="WHAT IS HERE" t="Reports of road defects, as published">
            Every public entry is a news report that names a road-infrastructure defect, in the words of the
            outlet that reported it. Most carry no casualties: they are reports of potholes, waterlogging, missing
            lighting or signage. Some are crash reports that attribute the crash to a defect. The map says which.
          </Item>
          <Item k="COVERAGE" t={`${coverage.states?.length || 0} states today, expanding`}>
            The collector runs daily in {coverage.states?.join(', ')}{coverage.districts ? `, ${coverage.districts} districts` : ''}.
            Other states appear only when their stories surface incidentally. An empty state on this map is an
            uncovered state, not a safe one.
          </Item>
          <Item k="SOURCE" t="Everything traces to published articles">
            Every report links every article behind it — outlet, date, URL — and carries its evidence quotes
            verbatim, in the original language. No source, no record.
          </Item>
          <Item k="CONFIDENCE GATE" t="Only high-confidence entries publish">
            An entry appears only if extraction confidence is at least 0.7, location confidence at least 0.6,
            the coverage itself implicates infrastructure, and a second automated review agrees — or a human
            reviewer approved it. Everything else waits in an internal review queue, and what no human will act
            on is archived rather than left to imply a backlog.
          </Item>
          <Item k="NO INFERRED BLAME" t="Defect tags only when coverage claims one">
            A crash report with no infrastructure claim is stored without a defect and never published. The
            repository never invents a pothole to explain a death, and it reports attribution, not accusation.
          </Item>
          <Item k="LOCATION" t="Road-level unless the reports say otherwise">
            Most reports name a road or a landmark, not a point. A location marked road-level is that named
            feature; a point location has three or more distinct coordinates behind it. Both are indicative
            pending a physical audit.
          </Item>
          <Item k="REPEAT CRASHES" t="Three or more casualty crashes in six months">
            A location is flagged for repeat crashes when at least three crashes with deaths or injuries were
            reported there in the last six months on precisely located pins. Crash reports that make no
            infrastructure claim count toward this flag and nothing else; the dossier says how many there are.
          </Item>
          <Item k="PRIORITY" t="A transparent score over public evidence only">
            Each location's score (0–100) weighs casualties, report frequency, recency, vulnerable road users,
            defect severity and evidence strength — computed only over the reports you can see, with the full
            breakdown in every dossier. Tiers are relative: Critical is the top 5 percent of locations, High the
            top 15, Medium the top 40.
          </Item>
          <Item k="POSITIVE EVIDENCE ONLY" t="Confirms danger, never certifies safety">
            News coverage is incomplete by nature. This map can show a stretch is dangerous; it can never show
            one is safe. Better-covered districts are not more dangerous ones.
          </Item>
          <Item k="CORRECTIONS" t="Disputable by design">
            Every dossier carries a “Report a correction” action. Corrections route to the review queue and
            disputed entries are withdrawn from the public view while checked.
          </Item>
        </div>
        <p className="text-[12px] text-muted-foreground mt-8">
          Registry generated {new Date(meta.generated_at).toLocaleString('en-IN')} ·
          {' '}{plural(meta.hotspots, 'location')} · {plural(meta.incidents, 'public report')} · Crashfree India (Vision Zero Trust).
          Cite as: Crashfree India, National Road-Infrastructure Defect Repository, as of {fmtDate(meta.generated_at)}.
        </p>
      </div>
    </div>
  )
}
