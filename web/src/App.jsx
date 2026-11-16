import { useCallback, useEffect, useMemo, useState } from 'react'
import { ArrowRight, List, Search, SlidersHorizontal, X } from 'lucide-react'
import MapView from './MapView.jsx'
import Dossier from './Dossier.jsx'
import Rankings from './Rankings.jsx'
import Sheet from './Sheet.jsx'
import Unmapped from './Unmapped.jsx'
import { EVIDENCE_HINT, EVIDENCE_LABEL, PRECISION_LABEL, PRECISION_ORDER, ROAD_TYPE_LABEL, TIER_COLOR, TIER_HINT,
         TIER_LABEL, fmtDate, isArea, plural } from './vocab.js'
import { EMPTY, FACETS, FILTER_LABEL, applyFilters, countWith, facetCounts, placeOnly, relaxations } from './filters.js'

// Filters, tab, heat and selected location live in the URL so any view is a
// citable link (?state=Rajasthan&tier=high, ?h=42, …).
const initQ = new URLSearchParams(window.location.search)
const TABS = ['map', 'rankings', 'unmapped', 'method']
const TAB_LABEL = { map: 'Map', rankings: 'Rankings', unmapped: 'Not mapped', method: 'Method' }
// What a location is called: an area is the place it is pinned to, a road is the road.
const nameOf = p => (isArea(p.precision) && p.place ? p.place : (p.road_name || p.place || 'Unnamed stretch'))

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
  const [tab, setTab] = useState(TABS.includes(initQ.get('tab')) ? initQ.get('tab') : 'map')
  const [listPlace, setListPlace] = useState({ state: initQ.get('us') || '', district: initQ.get('ud') || '' })
  const onListPlace = useCallback(p => setListPlace(p), [])
  const [selected, setSelected] = useState(null)          // location feature
  const [focusTarget, setFocusTarget] = useState(null)    // location the map should fly to (not map clicks)
  const [filters, setFilters] = useState({
    state: initQ.get('state') || '', district: initQ.get('district') || '',
    road: initQ.get('road') || '', defect: initQ.get('defect') || '',
    tier: initQ.get('tier') || '', precision: initQ.get('prec') || '', repeatOnly: initQ.get('repeat') === '1',
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
    if (filters.precision) p.set('prec', filters.precision)
    if (tab === 'unmapped' && listPlace.state) p.set('us', listPlace.state)
    if (tab === 'unmapped' && listPlace.district) p.set('ud', listPlace.district)
    if (filters.repeatOnly) p.set('repeat', '1')
    if (filters.hideWaterOnly) p.set('nowl', '1')
    if (heat) p.set('heat', '1')
    if (selected) p.set('h', selected.properties.id)
    const qs = p.toString()
    window.history.replaceState(null, '', qs ? `?${qs}` : window.location.pathname)
  }, [tab, filters, heat, selected, listPlace])

  const filtered = useMemo(() => (data ? applyFilters(data.hs.features, filters) : []), [data, filters])

  // Facets: for every dropdown, how many locations each option would leave given the
  // OTHER active filters. Options that would leave none are not offered, so a stack of
  // filters can no longer land on an empty map (17 Oct 2026: Gaya + NH + Critical + ...).
  const facets = useMemo(() => {
    if (!data) return null
    const all = data.hs.features
    const counts = Object.fromEntries(FACETS.map(k => [k, facetCounts(all, filters, k)]))
    return {
      counts,
      repeat: countWith(all, filters, { repeatOnly: true }),
      noWater: countWith(all, filters, { hideWaterOnly: true }),
      relax: filtered.length ? [] : relaxations(all, filters),
      placeCount: applyFilters(all, placeOnly(filters)).length,
    }
  }, [data, filters, filtered])

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
      .filter(f => { const p = f.properties; return [p.road_name, p.place, p.city, p.district, p.state].some(v => v && v.toLowerCase().includes(q)) })
      .sort((a, b) => (b.properties.score || 0) - (a.properties.score || 0))
      .slice(0, 8)
  }, [data, query])

  if (!data) return (
    <div className="h-full grid place-items-center text-muted-foreground">Loading the registry…</div>
  )

  const { meta } = data
  const options = {
    state: meta.states,
    district: Object.keys(facets.counts.district).sort(),
    road: meta.road_types || Object.keys(facets.counts.road).sort(),
    defect: Object.keys(facets.counts.defect).sort(),
    tier: ['critical', 'high', 'medium', 'watch'],
    precision: PRECISION_ORDER,
  }
  const valueLabels = {
    state: {}, district: {}, road: ROAD_TYPE_LABEL, defect: meta.defect_labels, precision: PRECISION_LABEL,
    tier: Object.fromEntries(Object.keys(TIER_LABEL).map(t => [t, `${TIER_LABEL[t]} — ${TIER_HINT[t]}`])),
  }
  const coverage = meta.coverage || { states: meta.states, districts: null }
  const covered = coverage.states?.length ? data.hs.features.filter(f => coverage.states.includes(f.properties.state)) : data.hs.features

  const sel = (k, v) => setFilters(f => ({ ...f, [k]: v, ...(k === 'state' ? { district: '' } : {}) }))
  const activeFilters = FACETS.filter(k => filters[k]).length + (filters.repeatOnly ? 1 : 0) + (filters.hideWaterOnly ? 1 : 0)
  const clearFilters = () => setFilters(EMPTY)
  const relaxFilter = k => setFilters(f => ({ ...f, [k]: typeof f[k] === 'boolean' ? false : '' }))
  const showPlace = () => setFilters(f => placeOnly(f))
  const pick = f => { setSelected(f); setFocusTarget(f); setQuery(''); if (mobile) setSheet('peek') }
  const openFromRankings = f => { setSelected(f); setFocusTarget(f); setTab('map'); setSheet('peek') }

  const filterPanel = (
    <FilterPanel filters={filters} sel={sel} options={options} counts={facets.counts} valueLabels={valueLabels}
      previews={{ repeat: facets.repeat, noWater: facets.noWater }} heat={heat} setHeat={setHeat}
      activeFilters={activeFilters} clearFilters={clearFilters} filtered={filtered} flagOn={!!meta.site?.repeat_crash_flag} />
  )
  const unmappedLink = (meta.unmapped || 0) > 0 && (
    <button onClick={() => setTab('unmapped')}
      className="mt-2.5 w-full text-left rounded-xl border border-border px-3 py-2 text-[12px] text-muted-foreground hover:bg-brand-soft cursor-pointer flex items-center gap-2">
      <span><b className="text-foreground font-medium">{meta.unmapped.toLocaleString('en-IN')} more reports</b> have no marker: the place they name is not on the map. Listed by district</span>
      <ArrowRight className="h-4 w-4 shrink-0 text-brand" />
    </button>
  )
  const noMatch = (
    <NoMatch place={filters.district || filters.state} placeCount={facets.placeCount} relax={facets.relax}
      describe={k => {
        const v = filters[k]
        const label = k === 'road' ? ROAD_TYPE_LABEL[v] : k === 'defect' ? (meta.defect_labels[v] || v) : k === 'tier' ? TIER_LABEL[v]
          : k === 'precision' ? PRECISION_LABEL[v] : null
        return label ? `${FILTER_LABEL[k]}: ${label}` : FILTER_LABEL[k]
      }}
      onShowPlace={showPlace} onRelax={relaxFilter} clearFilters={clearFilters} />
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
            {/* "Not mapped" sits in the bar from sm up; on a phone it is reached from the
                list sheet and the Method page, where there is room to say what it is */}
            {TABS.map(t => (
              <button key={t} onClick={() => setTab(t)} aria-current={tab === t ? 'page' : undefined}
                className={`h-10 px-3 sm:px-4 rounded-full text-[13px] cursor-pointer whitespace-nowrap
                  ${t === 'unmapped' && tab !== t ? 'hidden sm:inline-block' : ''}
                  ${tab === t ? 'bg-brand text-white' : 'text-muted-foreground hover:text-foreground'}`}>
                {TAB_LABEL[t]}
              </button>
            ))}
          </nav>
        </div>
      </header>

      {/* one-line disclaimer; the full text lives on Method (and in every dossier's citation line) */}
      <div className="bg-brand-soft border-b border-border px-4 sm:px-5 py-1.5 text-[12px] text-muted-foreground flex gap-2 items-center shrink-0">
        <b className="text-warn font-heading text-[10.5px] tracking-widest shrink-0">AS REPORTED</b>
        <span className="truncate">Reports as published in news media · a marker is a named road, village or town, not a surveyed point · absence of data is absence of coverage</span>
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
              {unmappedLink}
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
            {filtered.length === 0 && noMatch}
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
          {filtered.length === 0 && noMatch}
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
                  {unmappedLink}
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

      {tab === 'unmapped' && <Unmapped meta={meta} initial={listPlace} onPlace={onListPlace} />}

      {tab === 'method' && <Method meta={meta} coverage={coverage} onUnmapped={() => setTab('unmapped')} />}
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
                  <span className="font-heading font-semibold text-[13px] block truncate">{nameOf(p)}</span>
                  <span className="text-[11.5px] text-muted-foreground block truncate">
                    {PRECISION_LABEL[p.precision] || PRECISION_LABEL.unchecked} · {[p.district, p.state].filter(Boolean).join(', ')} · {plural(p.incidents, 'report')}
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
        <Marker tier={p.tier} precision={p.precision} />
        <span className="font-heading font-semibold text-[13px] truncate">{nameOf(p)}</span>
        <span className="ml-auto text-[11px] text-muted-foreground shrink-0">{TIER_LABEL[p.tier]}</span>
      </div>
      <div className="text-[11.5px] text-muted-foreground mt-0.5 truncate">
        {PRECISION_LABEL[p.precision] || PRECISION_LABEL.unchecked} · {p.district} · {plural(p.incidents, 'report')}
        {p.fatalities > 0 ? ` · ${plural(p.fatalities, 'death')}` : ''}
        {p.escalation ? ` · ${EVIDENCE_LABEL.escalation.toLowerCase()}` : ''}
        {(p.defects || []).length ? ` · ${(meta.defect_labels[p.defects[0]] || p.defects[0])}` : ''}
      </div>
    </button>
  )
}

// The marker as the map draws it: a solid dot for a named road or an exact spot, a
// ring for an area (a village or a town) — the same rule as MapView's paint.
function Marker({ tier, precision, size = 10 }) {
  const c = TIER_COLOR[tier] || TIER_COLOR.watch
  const area = isArea(precision)
  return (
    <span className="rounded-full shrink-0 inline-block" aria-hidden="true"
      style={{ width: size, height: size, background: area ? `${c}29` : c, border: area ? `2px solid ${c}` : 'none' }} />
  )
}

function FilterPanel({ filters, sel, options, counts, valueLabels, previews, heat, setHeat, activeFilters, clearFilters, filtered, flagOn }) {
  const n = v => v.toLocaleString('en-IN')
  return (
    <div className="flex flex-col gap-2.5 text-[13px]">
      {FACETS.map(k => (
        <Select key={k} label={FILTER_LABEL[k]} value={filters[k]} onChange={v => sel(k, v)}
          options={options[k]} counts={counts[k]} labels={valueLabels[k]} />
      ))}
      <label className="flex items-center gap-2 mt-1 cursor-pointer text-muted-foreground">
        <input type="checkbox" checked={filters.repeatOnly} onChange={e => sel('repeatOnly', e.target.checked)} className="accent-danger h-4 w-4" />
        Repeated reports only (2+) <span className="ml-auto tabular-nums text-[11.5px]">{n(previews.repeat)}</span>
      </label>
      <label className="flex items-start gap-2 cursor-pointer text-muted-foreground">
        <input type="checkbox" checked={filters.hideWaterOnly} onChange={e => sel('hideWaterOnly', e.target.checked)} className="accent-danger h-4 w-4 mt-0.5" />
        <span className="flex-1">Hide waterlogging-only locations
          <span className="block text-[11.5px] leading-snug">Seasonal: drainage reports with no other defect named</span>
        </span>
        <span className="tabular-nums text-[11.5px] mt-0.5">{n(previews.noWater)}</span>
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
        <div className="pt-2 mt-1 border-t border-border">
          <div className="font-heading text-[10px] tracking-[0.2em] text-muted-foreground mb-1.5">WHAT A MARKER MEANS</div>
          <div className="flex items-start gap-2 text-[12px] text-muted-foreground py-0.5">
            <span className="mt-0.5"><Marker tier="high" precision="road" size={12} /></span>
            <span><b className="text-foreground font-medium">Solid dot</b> · the road the reports name, or an exact spot</span>
          </div>
          <div className="flex items-start gap-2 text-[12px] text-muted-foreground py-0.5">
            <span className="mt-0.5"><Marker tier="high" precision="town" size={12} /></span>
            <span><b className="text-foreground font-medium">Ring</b> · a village or a town the reports name. The report is somewhere in that area; the exact spot is not known</span>
          </div>
        </div>
        <div className="flex items-center gap-2 text-[12px] text-muted-foreground pt-2 mt-1 border-t border-border">
          <span className="w-5 h-5 rounded-full grid place-items-center text-[9px] font-bold text-white shrink-0 bg-brand">12</span>
          A blue bubble is a count of locations — tap to zoom in. Colour shows priority only on single locations.
        </div>
        <div className="text-[12px] text-muted-foreground pt-2 mt-1 border-t border-border">
          {Object.entries(EVIDENCE_LABEL).filter(([k]) => k !== 'escalation' || flagOn)
            .map(([k, v]) => <div key={k}><b className="text-foreground font-medium">{v}</b> · {EVIDENCE_HINT[k]}</div>)}
        </div>
      </div>
    </div>
  )
}

// Empty result. Two different truths: the place is covered and the OTHER filters emptied
// it (say how many it has, offer the way back), or there is simply nothing here (then,
// and only then, the coverage sentence).
function NoMatch({ place, placeCount, relax, describe, onShowPlace, onRelax, clearFilters }) {
  const byFilters = placeCount > 0 || relax.length > 0
  return (
    <div className="absolute inset-0 z-10 grid place-items-center pointer-events-none px-6">
      <div className="pointer-events-auto bg-white border border-border rounded-2xl px-6 py-5 max-w-sm text-center shadow-lg">
        <div className="font-heading font-bold text-[14px]">No locations match these filters</div>
        {byFilters ? (
          <>
            {place && placeCount > 0 && (
              <p className="text-[12.5px] text-muted-foreground mt-1.5">
                {place} has {plural(placeCount, 'location')} on record. None match the other filters.
              </p>
            )}
            {place && placeCount > 0 && (
              <button onClick={onShowPlace} className="mt-3 h-10 px-5 rounded-full bg-brand text-white text-[13px] cursor-pointer">
                Show all {placeCount.toLocaleString('en-IN')} in {place}
              </button>
            )}
            {relax.length > 0 && (
              <div className="mt-3 flex flex-wrap justify-center gap-1.5">
                {relax.slice(0, 3).map(r => (
                  <button key={r.key} onClick={() => onRelax(r.key)}
                    className="h-8 px-3 rounded-full border border-border text-[12px] text-brand hover:bg-brand-soft cursor-pointer">
                    Without {describe(r.key)} · {r.count.toLocaleString('en-IN')}
                  </button>
                ))}
              </div>
            )}
            <button onClick={clearFilters} className="mt-3 text-[12px] text-muted-foreground underline underline-offset-2 cursor-pointer">Clear all filters</button>
          </>
        ) : (
          <>
            <p className="text-[12.5px] text-muted-foreground mt-1.5">Absence here is absence of coverage — never a safety clearance.</p>
            <button onClick={clearFilters} className="mt-3 h-10 px-5 rounded-full bg-brand text-white text-[13px] cursor-pointer">Clear all filters</button>
          </>
        )}
      </div>
    </div>
  )
}

function Select({ label, value, onChange, options, counts, labels }) {
  const id = `f-${label.toLowerCase().replace(/\s+/g, '-')}`
  // Offer only options that leave at least one location under the other filters; the
  // chosen value stays listed (with its count, even 0) so a shared link stays legible.
  const shown = options.filter(o => (counts?.[o] || 0) > 0 || o === value)
  if (value && !options.includes(value)) shown.push(value)
  return (
    <label className="block" htmlFor={id}>
      <span className="font-heading text-[10px] tracking-[0.2em] text-muted-foreground">{label.toUpperCase()}</span>
      <select id={id} value={value} onChange={e => onChange(e.target.value)}
        className="mt-1 w-full h-10 border border-border rounded-lg px-2.5 bg-white text-[13px]">
        <option value="">All</option>
        {shown.map(o => (
          <option key={o} value={o}>{labels?.[o] || o}{counts ? ` (${(counts[o] || 0).toLocaleString('en-IN')})` : ''}</option>
        ))}
      </select>
    </label>
  )
}

function Method({ meta, coverage, onUnmapped }) {
  const outside = meta.outside_coverage?.reports || 0
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
            Only these states are shown and counted here.{outside > 0 ? ` ${outside.toLocaleString('en-IN')} reports that surfaced
            incidentally from other states are held back until those states are covered.` : ''} An empty state on
            this map is an uncovered state, not a safe one.
          </Item>
          <Item k="SOURCE" t="Everything traces to published articles">
            Every report links every article behind it — outlet, date, URL — and carries its evidence quotes
            verbatim, in the original language. No source, no record.
          </Item>
          <Item k="CONFIDENCE GATE" t="Only high-confidence entries publish">
            An entry appears only if extraction confidence is at least 0.7, the place it names is found on the
            map, the coverage itself implicates infrastructure, and a second automated review agrees — or a human
            reviewer approved it. Everything else waits in an internal review queue, and what no human will act
            on is archived rather than left to imply a backlog.
          </Item>
          <Item k="NO INFERRED BLAME" t="Defect tags only when coverage claims one">
            A crash report with no infrastructure claim is stored without a defect and never published. The
            repository never invents a pothole to explain a death, and it reports attribution, not accusation.
          </Item>
          <Item k="LOCATION" t="A named road, village or town, never a guessed point">
            A marker is placed only where the map knows a name that the report itself uses. A solid dot is the
            road the reports name; a ring is the village or town they name, and the report is somewhere inside
            it. Nearly all markers are one of these two. None is a surveyed point: all are indicative pending
            a physical audit.
          </Item>
          <Item k="NOT ON THE MAP" t="Published, but listed without a marker">
            When the place a report names cannot be found on the map, the report is not pinned somewhere
            nearby. It stays published and is listed by district.{' '}
            {(meta.unmapped || 0) > 0 && (
              <button onClick={onUnmapped} className="text-brand hover:underline cursor-pointer">
                See the {meta.unmapped.toLocaleString('en-IN')} reports listed this way
              </button>
            )}
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
          {' '}{plural(meta.hotspots, 'location')} · {plural(meta.incidents, 'public report')}
          {meta.unmapped != null ? ` (${meta.mapped.toLocaleString('en-IN')} on the map, ${meta.unmapped.toLocaleString('en-IN')} listed without a marker)` : ''} · Crashfree India (Vision Zero Trust).
          Cite as: Crashfree India, National Road-Infrastructure Defect Repository, as of {fmtDate(meta.generated_at)}.
        </p>
      </div>
    </div>
  )
}
