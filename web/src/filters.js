// Filter rules and facet counts for the map (17 Oct 2026).
//
// Why this exists: each dropdown used to know only the whole dataset, so it offered
// "Critical" for a district with no critical location and "National highway" for one
// with none. Stack three such choices and every district looks empty — the owner hit
// exactly that on Gaya (12 locations, none critical, none repeated). Now every option
// carries the number of locations it would leave given the OTHER active filters, and
// options that would leave none are not offered. Pure functions, unit-tested with
// node:test (npm test).
import { isWaterloggingOnly } from './vocab.js'

export const EMPTY = { state: '', district: '', road: '', defect: '', tier: '', repeatOnly: false, hideWaterOnly: false }

// Select facets, in panel order. Checkbox filters are repeatOnly and hideWaterOnly.
export const FACETS = ['state', 'district', 'road', 'defect', 'tier']

export const FILTER_LABEL = {
  state: 'State', district: 'District', road: 'Road type', defect: 'Defect', tier: 'Priority tier',
  repeatOnly: 'Repeated reports only', hideWaterOnly: 'Hide waterlogging-only',
}

// One rule per key: do properties p pass the filter value v (v is truthy)?
const RULES = {
  state: (p, v) => p.state === v,
  district: (p, v) => p.district === v,
  road: (p, v) => (p.road_types || []).includes(v),
  defect: (p, v) => (p.defects || []).includes(v),
  tier: (p, v) => p.tier === v,
  repeatOnly: p => (p.incidents || 0) >= 2,
  hideWaterOnly: p => !isWaterloggingOnly(p.defects),
}

// The values a location contributes to a facet (a road or defect list counts once per entry).
const VALUES = {
  state: p => [p.state], district: p => [p.district], tier: p => [p.tier],
  road: p => p.road_types || [], defect: p => p.defects || [],
}

export const activeKeys = filters => Object.keys(RULES).filter(k => filters[k])

// Changing the state resets the district, so the state facet ignores both.
const skipFor = key => (key === 'state' ? ['state', 'district'] : [key])

/** Does location p pass every active filter except those in `skip`? */
export function matches(p, filters, skip = []) {
  for (const k of Object.keys(RULES)) {
    if (!filters[k] || skip.includes(k)) continue
    if (!RULES[k](p, filters[k])) return false
  }
  return true
}

export const applyFilters = (features, filters) => features.filter(f => matches(f.properties, filters))

/** { optionValue: locations it would leave } for one select facet, other filters applied. */
export function facetCounts(features, filters, key) {
  const skip = skipFor(key)
  const counts = {}
  for (const f of features) {
    const p = f.properties
    if (!matches(p, filters, skip)) continue
    for (const v of new Set(VALUES[key](p))) if (v) counts[v] = (counts[v] || 0) + 1
  }
  return counts
}

/** Locations left if `patch` were applied on top of the current filters (checkbox previews). */
export const countWith = (features, filters, patch) => applyFilters(features, { ...filters, ...patch }).length

/**
 * For an empty result: what each active filter's removal alone would bring back,
 * closest match first (fewest locations, but more than none). The place keys stay
 * out of it — the empty-state card handles "show all N in <district>" itself.
 */
export function relaxations(features, filters) {
  return activeKeys(filters)
    .filter(k => k !== 'state' && k !== 'district')
    .map(k => ({ key: k, count: features.reduce((n, f) => n + (matches(f.properties, filters, [k]) ? 1 : 0), 0) }))
    .filter(r => r.count > 0)
    .sort((a, b) => a.count - b.count)
}

/** The filters with only the place kept (state and district). */
export const placeOnly = filters => ({ ...EMPTY, state: filters.state, district: filters.district })
