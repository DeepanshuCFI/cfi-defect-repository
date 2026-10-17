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

// Evidence class per location: what stands behind the dot.
export const EVIDENCE_LABEL = {
  escalation: 'Repeat crashes',      // >= 3 casualty crashes on tight pins in 6 months
  repeat: 'Repeated reports',        // >= 2 public reports at this location
  single: 'Single report',
}
export const EVIDENCE_HINT = {
  escalation: '3 or more crashes with casualties here in the last 6 months',
  repeat: 'More than one independent report at this location',
  single: 'One report — real evidence, not yet a pattern',
}

// Location precision (from the pipeline's breakdown): a named road or landmark
// with repeat reports, or >= 3 distinct coordinates behind it.
export const PRECISION_LABEL = { feature: 'Road-level', point: 'Point location' }
export const PRECISION_HINT = {
  feature: 'Reports name this road or landmark; the pin marks the feature, not a spot on it',
  point: 'Three or more distinct coordinates stand behind this location',
}

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

export function geocodeWords(method, conf) {
  const m = (method || '').replace(/_/g, ' ')
  const level = { 'landmark district': 'landmark level, district verified', 'road city': 'road level, city verified',
                  'district centroid': 'district level', 'city centroid': 'city level', 'coords in text': 'exact coordinates',
                  'manual': 'placed by a reviewer' }[m] || m || 'unknown'
  return conf ? `${level} (${Math.round(conf * 100)}% confidence)` : level
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
