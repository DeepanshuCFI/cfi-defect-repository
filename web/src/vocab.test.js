import { test } from 'node:test'
import assert from 'node:assert/strict'
import { isArea, locationWords } from './vocab.js'

test('a report says what its marker is pinned to, or plainly why it has none', () => {
  assert.equal(locationWords({ mapped: true, precision: 'town', pinned_to: 'Bilgram' }), 'Bilgram (town area)')
  assert.equal(locationWords({ mapped: true, precision: 'road', pinned_to: 'Sahpur Road' }), 'Sahpur Road (named road)')
  assert.equal(locationWords({ mapped: false, precision: 'highway', pinned_to: 'NH 31' }),
    'on NH 31; the point on the highway is not known')
  assert.equal(locationWords({ mapped: false, precision: null }), 'the place named could not be found on the map')
})

test('a marker that has not been re-checked is not described as missing', () => {
  // seen in the first local preview: a mapped report with no precision yet read
  // "the place named could not be found on the map" — under a marker on the map
  assert.match(locationWords({ mapped: true, precision: null }), /not yet re-checked/)
  assert.match(locationWords({ precision: undefined }), /not yet re-checked/)
})

test('only villages and towns are areas', () => {
  assert.deepEqual(['spot', 'road', 'locality', 'town', 'unchecked', undefined].map(isArea),
    [false, false, true, true, false, false])
})
