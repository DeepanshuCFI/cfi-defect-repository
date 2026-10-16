import { useRef } from 'react'

// Mobile bottom sheet with three resting positions. The phone layout used to be the
// desktop two-pane layout squeezed: a fixed 288px filter panel left the map 87px wide
// on a 375px phone (launch review, 16 Oct 2026). The map is now full-bleed and the
// panel content lives here, peeking above the map and expanding on demand.
const HEIGHT = { peek: 'h-24', half: 'h-[52%]', full: 'h-[90%]' }
const ORDER = ['peek', 'half', 'full']

export default function Sheet({ state, onState, title, children }) {
  const drag = useRef(null)

  const next = dir => {
    const i = ORDER.indexOf(state)
    onState(ORDER[Math.max(0, Math.min(ORDER.length - 1, i + dir))])
  }
  const onPointerDown = e => { drag.current = { y: e.clientY, t: Date.now() } }
  const onPointerUp = e => {
    if (!drag.current) return
    const dy = e.clientY - drag.current.y
    drag.current = null
    if (dy < -40) next(1)          // dragged up
    else if (dy > 40) next(-1)     // dragged down
    else if (Math.abs(dy) < 8) next(state === 'full' ? -1 : 1)   // tap cycles
  }

  return (
    <div className={`absolute inset-x-0 bottom-0 z-20 bg-white border-t border-border rounded-t-2xl shadow-2xl
                     flex flex-col transition-[height] duration-200 ease-out ${HEIGHT[state]}`}
      style={{ paddingBottom: 'env(safe-area-inset-bottom, 0px)' }}
      role="region" aria-label="Locations and filters">
      <button type="button" aria-label={state === 'full' ? 'Collapse panel' : 'Expand panel'}
        onPointerDown={onPointerDown} onPointerUp={onPointerUp} onPointerCancel={() => { drag.current = null }}
        className="w-full pt-2 pb-1 cursor-grab touch-none select-none">
        <span className="block mx-auto h-1.5 w-12 rounded-full bg-border" />
        {title && <span className="block mt-1.5 font-heading font-bold text-[13px] text-foreground">{title}</span>}
      </button>
      <div className="flex-1 min-h-0 overflow-y-auto overscroll-contain">{children}</div>
    </div>
  )
}
