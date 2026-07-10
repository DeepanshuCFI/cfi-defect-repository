import { useEffect, useRef } from 'react'
import maplibregl from 'maplibre-gl'

const STYLE = 'https://basemaps.cartocdn.com/gl/positron-gl-style/style.json'
// Montserrat matches --font-heading; the trailing stacks are the basemap's CJK fallbacks.
const COUNT_FONT = ['Montserrat Medium', 'Open Sans Bold', 'Noto Sans Regular']

export default function MapView({ features, heat, tierColor, tierText, onSelect, selectedId, focus, fitKey }) {
  const el = useRef(null)
  const map = useRef(null)
  const ready = useRef(false)
  const featRef = useRef(features)
  const focusRef = useRef(focus)
  const lastFitKey = useRef(fitKey)
  featRef.current = features
  focusRef.current = focus

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
      m.addSource('hs', {
        type: 'geojson', data: fc,
        cluster: true, clusterRadius: 42, clusterMaxZoom: 11,
        clusterProperties: {
          // worst tier present in the cluster, so a bubble is never calmer than its contents
          worst: ['max', ['match', ['get', 'tier'], 'critical', 3, 'high', 2, 'medium', 1, 0]],
        },
      })
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
      m.addLayer({
        id: 'hs-clusters', type: 'circle', source: 'hs',
        filter: ['has', 'point_count'],
        paint: {
          'circle-color': ['match', ['get', 'worst'],
            3, tierColor.critical, 2, tierColor.high, 1, tierColor.medium, tierColor.watch],
          'circle-radius': ['step', ['get', 'point_count'], 14, 10, 18, 25, 23, 50, 28],
          'circle-opacity': 0.9,
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
        paint: {
          'text-color': ['match', ['get', 'worst'], 0, tierText.watch, '#ffffff'],
        },
      })
      m.addLayer({
        id: 'hs-circles', type: 'circle', source: 'hs',
        filter: ['!', ['has', 'point_count']],
        paint: {
          'circle-radius': ['+', 6, ['*', 3, ['min', ['get', 'incidents'], 5]]],
          'circle-color': ['match', ['get', 'tier'],
            'critical', tierColor.critical, 'high', tierColor.high,
            'medium', tierColor.medium, tierColor.watch],
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
        if (f) onSelect({ type: 'Feature', geometry: f.geometry,
          properties: { ...f.properties,
            defects: JSON.parse(f.properties.defects || '[]'),
            breakdown: JSON.parse(f.properties.breakdown || 'null') } })
      })
      for (const layer of ['hs-circles', 'hs-clusters']) {
        m.on('mouseenter', layer, () => { m.getCanvas().style.cursor = 'pointer' })
        m.on('mouseleave', layer, () => { m.getCanvas().style.cursor = '' })
      }
      // Arriving from Rankings (remount): land on the selected hotspot, zoomed
      // past clustering so its dot is visible next to the open drawer.
      if (focusRef.current) {
        m.jumpTo({ center: focusRef.current.geometry.coordinates, zoom: Math.max(m.getZoom(), 12) })
      }
      ready.current = true
      m.getSource('hs').setData({ type: 'FeatureCollection', features: featRef.current })
      m.getSource('hs-all').setData({ type: 'FeatureCollection', features: featRef.current })
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

  // On a filter change (not mount, not tab return), frame the matching hotspots.
  useEffect(() => {
    if (lastFitKey.current === fitKey) return
    lastFitKey.current = fitKey
    if (!ready.current || !features.length) return
    const b = new maplibregl.LngLatBounds()
    features.forEach(f => b.extend(f.geometry.coordinates))
    map.current.fitBounds(b, { padding: 80, maxZoom: 10, duration: 600 })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fitKey])

  // Focus set while the map is already up (?h= deep link resolving after load,
  // or a Rankings pick without remount). Map clicks never set focus.
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
