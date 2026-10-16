import { useEffect, useRef } from 'react'
import maplibregl from 'maplibre-gl'
import { TIER_COLOR } from './vocab.js'

const STYLE = 'https://basemaps.cartocdn.com/gl/positron-gl-style/style.json'
// Montserrat matches --font-heading; the trailing stacks are the basemap's CJK fallbacks.
const COUNT_FONT = ['Montserrat Medium', 'Open Sans Bold', 'Noto Sans Regular']
const BRAND = '#4736FE'

export default function MapView({ features, frame, heat, onSelect, selectedId, focus, fitKey, onViewChange, padding }) {
  const el = useRef(null)
  const map = useRef(null)
  const ready = useRef(false)
  const featRef = useRef(features)
  const focusRef = useRef(focus)
  const lastFitKey = useRef(fitKey)
  const fitKeyRef = useRef(fitKey)
  const mountFitKey = useRef(fitKey)
  const padRef = useRef(padding)
  const frameRef = useRef(frame)
  featRef.current = features
  focusRef.current = focus
  padRef.current = padding
  frameRef.current = frame
  fitKeyRef.current = fitKey

  const emitView = () => {
    if (!onViewChange || !map.current) return
    const b = map.current.getBounds()
    onViewChange({ w: b.getWest(), s: b.getSouth(), e: b.getEast(), n: b.getNorth(), zoom: map.current.getZoom() })
  }

  useEffect(() => {
    map.current = new maplibregl.Map({
      container: el.current, style: STYLE,
      center: [82.5, 23.2], zoom: 4.4, attributionControl: { compact: true },
    })
    map.current.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right')
    map.current.on('error', e => console.error('[maplibre]', e?.error?.message || e))
    if (import.meta.env.DEV) window.__map = map.current
    map.current.on('load', () => {
      const m = map.current
      const fc = { type: 'FeatureCollection', features: featRef.current }

      // Twin unclustered source: the heat layer weights by score, which cluster
      // features don't carry, so it can't share the clustered source.
      m.addSource('hs-all', { type: 'geojson', data: fc })
      m.addSource('hs', { type: 'geojson', data: fc, cluster: true, clusterRadius: 42, clusterMaxZoom: 11 })
      m.addLayer({
        id: 'hs-heat', type: 'heatmap', source: 'hs-all',
        layout: { visibility: 'none' },
        paint: {
          'heatmap-weight': ['interpolate', ['linear'], ['get', 'score'], 0, 0.1, 100, 1],
          'heatmap-radius': 40, 'heatmap-opacity': 0.55,
          'heatmap-color': ['interpolate', ['linear'], ['heatmap-density'],
            0, 'rgba(74,53,255,0)', 0.3, 'rgba(74,53,255,0.35)',
            0.6, 'rgba(245,124,0,0.6)', 1, 'rgba(241,0,21,0.8)'],
        },
      })
      // Clusters are a COUNT, drawn in the brand blue. They used to take the worst
      // tier inside them, and with hundreds of Critical locations every state-sized
      // bubble was dark red: no information at the zoom people land on. Tier colour
      // now belongs to individual locations only.
      m.addLayer({
        id: 'hs-clusters', type: 'circle', source: 'hs',
        filter: ['has', 'point_count'],
        paint: {
          'circle-color': BRAND,
          'circle-opacity': ['step', ['get', 'point_count'], 0.55, 10, 0.7, 50, 0.85],
          'circle-radius': ['step', ['get', 'point_count'], 14, 10, 18, 50, 23, 200, 28],
          'circle-stroke-width': 1.5,
          'circle-stroke-color': '#ffffff',
        },
      })
      m.addLayer({
        id: 'hs-cluster-count', type: 'symbol', source: 'hs',
        filter: ['has', 'point_count'],
        layout: {
          'text-field': ['get', 'point_count_abbreviated'],
          'text-font': COUNT_FONT, 'text-size': 12, 'text-allow-overlap': true,
        },
        paint: { 'text-color': '#ffffff' },
      })
      m.addLayer({
        id: 'hs-circles', type: 'circle', source: 'hs',
        filter: ['!', ['has', 'point_count']],
        paint: {
          'circle-radius': ['+', 6, ['*', 3, ['min', ['get', 'incidents'], 5]]],
          'circle-color': ['match', ['get', 'tier'],
            'critical', TIER_COLOR.critical, 'high', TIER_COLOR.high,
            'medium', TIER_COLOR.medium, TIER_COLOR.watch],
          'circle-opacity': 0.85,
          'circle-stroke-width': ['case', ['==', ['get', 'id'], selectedId ?? -1], 3, 1.5],
          'circle-stroke-color': '#ffffff',
        },
      })
      m.on('click', 'hs-clusters', async e => {
        const f = e.features?.[0]
        if (!f) return
        const zoom = await m.getSource('hs').getClusterExpansionZoom(f.properties.cluster_id)
        m.easeTo({ center: f.geometry.coordinates, zoom: Math.min(zoom + 0.5, 14), duration: 500 })
      })
      m.on('click', 'hs-circles', e => {
        const f = e.features?.[0]
        if (f) onSelect(hydrate(f))
      })
      for (const layer of ['hs-circles', 'hs-clusters']) {
        m.on('mouseenter', layer, () => { m.getCanvas().style.cursor = 'pointer' })
        m.on('mouseleave', layer, () => { m.getCanvas().style.cursor = '' })
      }
      m.on('moveend', emitView)
      // Arriving from Rankings (remount) or a ?h= link: land on the selected location,
      // zoomed past clustering so its dot is visible next to the open dossier.
      // Otherwise frame the covered states, not the whole country: the collector runs
      // in three states and an all-India view makes the other twenty-five look clear.
      if (focusRef.current) {
        m.jumpTo({ center: focusRef.current.geometry.coordinates, zoom: Math.max(m.getZoom(), 12) })
      } else if (fitKeyRef.current !== mountFitKey.current) {
        // the user filtered before the basemap finished loading (slow network, which
        // is the phone case): frame what they asked for, not the default view
        lastFitKey.current = fitKeyRef.current
        if (featRef.current.length) m.fitBounds(boundsOf(featRef.current), { padding: padRef.current || 80, maxZoom: 10, duration: 0 })
      } else {
        // frame = the locations in the covered states (stray pins elsewhere would
        // zoom the opening view out to all of India)
        const toFrame = (frameRef.current && frameRef.current.length) ? frameRef.current : featRef.current
        if (toFrame.length) m.fitBounds(boundsOf(toFrame), { padding: padRef.current || 40, maxZoom: 6.5, duration: 0 })
      }
      ready.current = true
      m.getSource('hs').setData({ type: 'FeatureCollection', features: featRef.current })
      m.getSource('hs-all').setData({ type: 'FeatureCollection', features: featRef.current })
      emitView()
    })
    return () => map.current?.remove()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    if (ready.current) {
      const fc = { type: 'FeatureCollection', features }
      map.current.getSource('hs').setData(fc)
      map.current.getSource('hs-all').setData(fc)
    }
  }, [features])

  // On a filter change (not mount, not tab return), frame the matching locations.
  useEffect(() => {
    if (lastFitKey.current === fitKey) return
    lastFitKey.current = fitKey
    if (!ready.current || !features.length) return
    map.current.fitBounds(boundsOf(features), { padding: padRef.current || 80, maxZoom: 10, duration: 600 })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fitKey])

  // Focus set while the map is already up (?h= deep link resolving after load, a
  // Rankings pick without remount, a search result). Map clicks never set focus.
  useEffect(() => {
    if (ready.current && focus) {
      map.current.easeTo({ center: focus.geometry.coordinates,
        zoom: Math.max(map.current.getZoom(), 12), duration: 700 })
    }
  }, [focus])

  useEffect(() => {
    if (ready.current) {
      map.current.setLayoutProperty('hs-heat', 'visibility', heat ? 'visible' : 'none')
    }
  }, [heat])

  useEffect(() => {
    if (ready.current) {
      map.current.setPaintProperty('hs-circles', 'circle-stroke-width',
        ['case', ['==', ['get', 'id'], selectedId ?? -1], 3, 1.5])
    }
  }, [selectedId])

  // inline style: maplibre-gl.css sets .maplibregl-map{position:relative}, which
  // overrides Tailwind's .absolute and collapses the container to 0 height.
  return <div ref={el} style={{ position: 'absolute', inset: 0 }} />
}

function boundsOf(features) {
  const b = new maplibregl.LngLatBounds()
  features.forEach(f => b.extend(f.geometry.coordinates))
  return b
}

// MapLibre flattens nested properties to JSON strings on the way through the GPU.
function hydrate(f) {
  const p = f.properties
  const parse = (v, d) => (typeof v === 'string' ? JSON.parse(v) : (v ?? d))
  return { type: 'Feature', geometry: f.geometry,
    properties: { ...p, defects: parse(p.defects, []), road_types: parse(p.road_types, []),
                  breakdown: parse(p.breakdown, null) } }
}
