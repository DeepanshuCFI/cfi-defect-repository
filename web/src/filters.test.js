import { test } from 'node:test'
import assert from 'node:assert/strict'
import { EMPTY, applyFilters, countWith, facetCounts, matches, placeOnly, relaxations } from './filters.js'

const F = (id, p) => ({ type: 'Feature', properties: { id, incidents: 1, defects: [], road_types: [], ...p } })
const WL = 'waterlogging_or_poor_drainage'
const features = [
  F(1, { state: 'Bihar', district: 'Gaya', tier: 'high', road_types: ['NH'], defects: ['potholes_or_broken_surface'], incidents: 1 }),
  F(2, { state: 'Bihar', district: 'Gaya', tier: 'watch', road_types: ['rural'], defects: [WL], incidents: 1 }),
  F(3, { state: 'Bihar', district: 'Gaya', tier: 'watch', road_types: ['NH', 'SH'], defects: ['known_blackspot_untreated', WL], incidents: 1 }),
  F(4, { state: 'Bihar', district: 'Patna', tier: 'critical', road_types: ['NH'], defects: ['potholes_or_broken_surface'], incidents: 5 }),
  F(5, { state: 'Uttar Pradesh', district: 'Jhansi', tier: 'critical', road_types: ['NH'], defects: [WL, 'other_infrastructure'], incidents: 3 }),
]

test('matches applies every active filter and can skip some', () => {
  const f = { ...EMPTY, state: 'Bihar', district: 'Gaya', tier: 'critical' }
  assert.equal(matches(features[0].properties, f), false)
  assert.equal(matches(features[0].properties, f, ['tier']), true)
  assert.equal(applyFilters(features, { ...EMPTY, road: 'NH' }).length, 4)
  assert.equal(applyFilters(features, { ...EMPTY, repeatOnly: true }).length, 2)
  assert.equal(applyFilters(features, { ...EMPTY, hideWaterOnly: true }).length, 3)   // 2 and 5 are waterlogging-only
})

test("the owner's Gaya stack is empty, and the facets say so before it is picked", () => {
  const gaya = { ...EMPTY, state: 'Bihar', district: 'Gaya' }
  assert.deepEqual(facetCounts(features, gaya, 'tier'), { high: 1, watch: 2 })          // no "critical" offered
  assert.deepEqual(facetCounts(features, gaya, 'road'), { NH: 2, rural: 1, SH: 1 })
  assert.deepEqual(facetCounts(features, { ...gaya, tier: 'watch' }, 'road'), { rural: 1, NH: 1, SH: 1 })
  assert.deepEqual(facetCounts(features, { ...EMPTY, tier: 'critical' }, 'district'), { Patna: 1, Jhansi: 1 })   // Gaya drops out
  assert.deepEqual(facetCounts(features, { ...EMPTY, state: 'Bihar', tier: 'critical' }, 'district'), { Patna: 1 })
  const stack = { ...gaya, road: 'NH', defect: 'known_blackspot_untreated', tier: 'critical', repeatOnly: true }
  assert.equal(applyFilters(features, stack).length, 0)
})

test('the state facet ignores the district (changing state resets it)', () => {
  const f = { ...EMPTY, state: 'Bihar', district: 'Gaya', road: 'NH' }
  assert.deepEqual(facetCounts(features, f, 'state'), { Bihar: 3, 'Uttar Pradesh': 1 })
  assert.deepEqual(facetCounts(features, f, 'district'), { Gaya: 2, Patna: 1 })
})

test('a road or defect listed twice on one location counts once', () => {
  const dup = [F(9, { state: 'X', district: 'Y', tier: 'watch', road_types: ['NH', 'NH'], defects: [WL, WL] })]
  assert.deepEqual(facetCounts(dup, EMPTY, 'road'), { NH: 1 })
  assert.deepEqual(facetCounts(dup, EMPTY, 'defect'), { [WL]: 1 })
})

test('checkbox previews and relaxations', () => {
  const gaya = { ...EMPTY, state: 'Bihar', district: 'Gaya' }
  assert.equal(countWith(features, gaya, { repeatOnly: true }), 0)
  assert.equal(countWith(features, gaya, { hideWaterOnly: true }), 2)
  const stack = { ...gaya, road: 'NH', tier: 'watch', repeatOnly: true }
  // removing repeatOnly alone brings back 1 (feature 3); removing road or tier alone still leaves 0
  assert.deepEqual(relaxations(features, stack), [{ key: 'repeatOnly', count: 1 }])
  assert.deepEqual(relaxations(features, { ...gaya, tier: 'critical', road: 'rural' }), [{ key: 'tier', count: 1 }])
  assert.deepEqual(placeOnly(stack), { ...EMPTY, state: 'Bihar', district: 'Gaya' })
  assert.equal(applyFilters(features, placeOnly(stack)).length, 3)
})
