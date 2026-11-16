// One vocabulary, used everywhere (launch review, 16 Oct 2026).
//
// What the data is: 87% of public entries are casualty-free defect reports and 85%
// carry no crash date. So a public entry is a REPORT; a report with casualties is a
// crash report; a hotspot is a LOCATION. "Crashes" and "crash timeline" overclaimed.

// Red intensity ramp — CFI rule: red = danger, and every dot here is a danger location.
// Darker = higher priority; tiers stay separable at small sizes. Tier meaning since
// 16 Oct: quantiles of the public score distribution (critical = top 5%).
export const TIER_COLOR = { critical: '#8F000B', high: '#F10015', medium: '#FF6159', watch: '#F3B0AA' }
export const TIER_TEXT = { critical: '#fff', high: '#fff', medium: '#fff', watch: '#8C1D16' }
export const TIER_LABEL = { critical: 'Critical', high: 'High', medium: 'Medium', watch: 'Watch' }
export const TIER_HINT = {
  critical: 'top 5% by public evidence', high: 'top 15%', medium: 'top 40%', watch: 'the rest',
}

// Evidence class per location: what stands behind the marker.
// 'escalation' (the red "Repeat crashes" flag) was withdrawn on 16 Nov 2026: of 671
// flags, none could be shown to be three crashes at one spot. It is only ever present
// in the data when the site setting repeat_crash_flag is switched back on.
export const EVIDENCE_LABEL = {
  escalation: 'Repeat crashes',
  repeat: 'Repeated reports',        // >= 2 public reports at this location
  single: 'Single report',
}
export const EVIDENCE_HINT = {
  escalation: '3 or more crashes with casualties here in the last 6 months',
  repeat: 'More than one independent report names this place',
  single: 'One report — real evidence, not yet a pattern',
}

// How precisely a location is known (16 Nov 2026). A marker is accepted only when the
// map provider's answer shares a name with the place in the article, and WHAT matched
// is the precision. Most reports name a village or a town, not a point — so most
// markers are areas, and the map draws them as rings, never as dots.
export const PRECISION_LABEL = {
  spot: 'Exact spot', road: 'Named road', locality: 'Village or locality', town: 'Town area',
  unchecked: 'Location being re-checked',
}
export const PRECISION_HINT = {
  spot: 'Coordinates in the report, or an address on the named road',
  road: 'The road the reports name. The marker sits on that road, not at a particular point on it',
  locality: 'The village or neighbourhood the reports name, a few kilometres across. The exact spot is not known',
  town: 'The town the reports name, with its surrounding area. The marker stands for the town, not a spot in it',
  unchecked: 'This marker predates the name check and has not been re-checked yet',
}
export const isArea = precision => precision === 'locality' || precision === 'town'
export const PRECISION_ORDER = ['spot', 'road', 'locality', 'town', 'unchecked']

export const ROAD_TYPE_LABEL = {
  NH: 'National highway', SH: 'State highway', MDR: 'Major district road', district: 'District road',
  rural: 'Rural road', urban_arterial: 'Urban arterial', urban_local: 'Urban local road', unknown: 'Unknown',
}

export const VERIFICATION_LABEL = {
  disputed: 'Disputed — correction under review',
  reviewed: 'Checked by a human reviewer',
  verified: 'Verified by a human reviewer',
  auto_published: 'Checked by a second automated review',
  auto: 'Passed the confidence gate',
}

/** One report's location in words: what its marker was pinned to, or why it has none. */
export function locationWords(i) {
  if (i.precision === 'highway') {
    return i.pinned_to ? `on ${i.pinned_to}; the point on the highway is not known` : 'on a numbered highway; the point is not known'
  }
  if (i.mapped === false) return 'the place named could not be found on the map'
  const what = { spot: 'exact spot', road: 'named road', locality: 'village or locality', town: 'town area' }[i.precision]
  if (!what) return 'marker not yet re-checked against the place named'
  return i.pinned_to ? `${i.pinned_to} (${what})` : what
}

export const plural = (n, one, many = one + 's') => `${n} ${n === 1 ? one : many}`

export const fmtDate = d => d ? new Date(d).toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric' }) : null

// Waterlogging is seasonal: 48% of public reports carried the tag on 17 Oct 2026, and
// 7% carried nothing else. A location is "waterlogging-only" when its reports name no
// defect besides drainage (the catch-all tag does not count as another defect). The
// map's "Hide waterlogging-only locations" toggle uses this; the data stays published.
export const WATERLOGGING = 'waterlogging_or_poor_drainage'
export const isWaterloggingOnly = defects =>
  (defects || []).includes(WATERLOGGING) &&
  (defects || []).every(d => d === WATERLOGGING || d === 'other_infrastructure')
