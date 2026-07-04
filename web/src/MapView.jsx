import { useEffect, useRef } from 'react'
import maplibregl from 'maplibre-gl'

const STYLE = 'https://basemaps.cartocdn.com/gl/positron-gl-style/style.json'

export default function MapView({ features, heat, tierColor, onSelect, selectedId }) {
  const el = useRef(null)
  const map = useRef(null)
  const ready = useRef(false)

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
      m.addSource('hs', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } })
      m.addLayer({
        id: 'hs-heat', type: 'heatmap', source: 'hs',
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
        id: 'hs-circles', type: 'circle', source: 'hs',
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
      m.on('click', 'hs-circles', e => {
        const f = e.features?.[0]
        if (f) onSelect({ type: 'Feature', geometry: f.geometry,
          properties: { ...f.properties,
            defects: JSON.parse(f.properties.defects || '[]'),
            breakdown: JSON.parse(f.properties.breakdown || 'null') } })
      })
      m.on('mouseenter', 'hs-circles', () => { m.getCanvas().style.cursor = 'pointer' })
      m.on('mouseleave', 'hs-circles', () => { m.getCanvas().style.cursor = '' })
      ready.current = true
      m.getSource('hs').setData({ type: 'FeatureCollection', features })
    })
    return () => map.current?.remove()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    if (ready.current) {
      map.current.getSource('hs').setData({ type: 'FeatureCollection', features })
    }
  }, [features])

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
