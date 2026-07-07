import { Check, Flag, TriangleAlert, X } from 'lucide-react'

export default function Drawer({ feature, incidents, meta, tierColor, tierText, tierLabel, onClose }) {
  const p = feature.properties
  const members = incidents
    .filter(i => i.hotspot_id === p.id)
    .sort((a, b) => (b.date || '').localeCompare(a.date || ''))
  const bd = p.breakdown
  const comps = bd?.components || {}
  const compLabel = {
    casualties: 'Casualties (weighted)', frequency: 'Crash frequency (6 mo)',
    recency: 'Recency', vulnerable: 'Vulnerable road users',
    defect_sev: 'Defect severity', evidence: 'Evidence strength',
  }
  const correctionMail = `mailto:contact@crashfreeindia.org?subject=${encodeURIComponent(
    `Correction: hotspot #${p.id} (${p.road_name || p.district || ''})`)}&body=${encodeURIComponent(
    'Please describe the correction. The entry will be marked disputed while we check.\n\nHotspot: #' + p.id)}`

  return (
    <div className="absolute top-0 right-0 h-full w-[26rem] max-w-full bg-white border-l border-border
                    shadow-2xl overflow-y-auto z-10">
      <div className="sticky top-0 bg-white border-b border-border px-5 py-3.5 flex justify-between items-start gap-3">
        <div>
          <div className="flex items-center gap-2">
            <span className="font-heading font-bold text-[11px] px-2.5 py-0.5 rounded-full"
              style={{ background: tierColor[p.tier], color: tierText?.[p.tier] || '#fff' }}>
              {tierLabel[p.tier].toUpperCase()} · {Number(p.score).toFixed(1)}
            </span>
            {p.escalation && (
              <span className="inline-flex items-center gap-1 font-heading font-bold text-[10px] px-2 py-0.5 rounded-full bg-danger text-white">
                <Flag className="h-3 w-3" /> ESCALATION CANDIDATE
              </span>
            )}
          </div>
          <h2 className="font-heading font-bold text-[16px] leading-snug mt-2">
            {p.road_name || 'Unnamed stretch'}
          </h2>
          <div className="text-[12.5px] text-muted-foreground mt-0.5">
            {[p.city, p.district, p.state].filter(Boolean).join(', ')}
          </div>
        </div>
        <button onClick={onClose} aria-label="Close"
          className="no-print text-muted-foreground hover:text-foreground cursor-pointer p-1">
          <X className="h-5 w-5" />
        </button>
      </div>

      <div className="px-5 py-4 grid grid-cols-3 gap-2 border-b border-border">
        <Stat n={p.incidents} l="crashes" />
        <Stat n={p.fatalities} l="deaths" red />
        <Stat n={p.injuries} l="injuries" />
      </div>

      {(p.defects || []).length > 0 && (
        <div className="px-5 py-3 border-b border-border">
          <div className="font-heading text-[10px] tracking-[0.2em] text-muted-foreground mb-1.5">REPORTED DEFECTS</div>
          <div className="flex flex-wrap gap-1.5">
            {p.defects.map(d => (
              <span key={d} className="text-[11.5px] px-2.5 py-1 rounded-full border border-warn/40 bg-warn/5 text-warn font-medium">
                {meta.defect_labels[d] || d}
              </span>
            ))}
          </div>
        </div>
      )}

      {bd && (
        <div className="px-5 py-3 border-b border-border">
          <div className="font-heading text-[10px] tracking-[0.2em] text-muted-foreground mb-2">
            WHY THIS SCORE <span className="normal-case tracking-normal">(never a black box)</span>
          </div>
          {Object.entries(comps).map(([k, v]) => (
            <div key={k} className="flex items-center gap-2 py-0.5">
              <span className="text-[11.5px] text-muted-foreground w-40 shrink-0">{compLabel[k] || k}</span>
              <div className="flex-1 h-1.5 rounded-full bg-brand-lite overflow-hidden">
                <div className="h-full bg-brand rounded-full" style={{ width: `${v * 100}%` }} />
              </div>
              <span className="text-[11px] text-muted-foreground w-8 text-right">{(v * 100).toFixed(0)}</span>
            </div>
          ))}
          <div className="text-[10.5px] text-muted-foreground mt-1.5">
            Weights: casualties {bd.weights?.w1_fatalities_weighted} · frequency {bd.weights?.w2_crash_frequency} ·
            recency {bd.weights?.w3_recency} · vulnerable {bd.weights?.w4_vulnerable_user_share} ·
            severity {bd.weights?.w5_defect_severity} · evidence {bd.weights?.w6_evidence_strength} ·
            computed {bd.computed_for}
          </div>
        </div>
      )}

      <div className="px-5 py-3">
        <div className="font-heading text-[10px] tracking-[0.2em] text-muted-foreground mb-2">
          CRASH TIMELINE · {members.length} PUBLIC INCIDENT{members.length !== 1 ? 'S' : ''}
        </div>
        {members.map(i => (
          <div key={i.id} className="rounded-xl border border-border p-3.5 mb-2.5">
            <div className="flex justify-between text-[12px] text-muted-foreground">
              <span className="font-semibold text-foreground">{i.date || 'undated'}</span>
              <span>F{i.fatalities} / I{i.injuries}</span>
            </div>
            <p className="text-[12.5px] mt-1.5 leading-relaxed">{i.summary}</p>
            {i.defects.map((d, ix) => (
              <div key={ix} className="mt-2 text-[11.5px] bg-brand-lite rounded-lg px-3 py-2">
                <b className="text-brand">{d.label}</b>
                <div className="text-muted-foreground mt-0.5">“{d.evidence}”</div>
              </div>
            ))}
            <div className="mt-2 text-[11.5px]">
              {i.sources.map((s, ix) => (
                <div key={ix}>
                  <a href={s.url} target="_blank" rel="noopener noreferrer" className="text-brand hover:underline">
                    {s.outlet || new URL(s.url).hostname}
                  </a>
                  <span className="text-muted-foreground"> · {s.date || ''}</span>
                </div>
              ))}
            </div>
            <div className="mt-1.5 text-[10.5px] text-muted-foreground">
              {i.verification === 'disputed'
                ? <span className="inline-flex items-center gap-1 text-warn font-semibold"><TriangleAlert className="h-3 w-3" /> disputed — correction under review</span>
                : i.verification === 'reviewed' || i.verification === 'verified'
                  ? <span className="inline-flex items-center gap-1"><Check className="h-3 w-3" /> human-reviewed</span>
                  : i.verification === 'auto_published'
                    ? <span className="inline-flex items-center gap-1"><Check className="h-3 w-3" /> machine-reviewed (2nd-pass AI adjudication)</span>
                    : 'auto-published (passed confidence gate)'}
              {' · '}location: {i.geocode_method?.replaceAll('_', ' ')} ({Math.round((i.geocode_conf || 0) * 100)}%)
            </div>
          </div>
        ))}
      </div>

      <div className="px-5 pb-6 flex gap-2 no-print">
        <a href={correctionMail}
          className="inline-flex items-center h-11 text-[13px] px-5 rounded-full border border-border text-muted-foreground hover:border-brand hover:text-brand">
          Report a correction
        </a>
        <button onClick={() => window.print()}
          className="inline-flex items-center h-11 text-[13px] px-5 rounded-full border border-border text-muted-foreground hover:border-brand hover:text-brand cursor-pointer">
          Print / save PDF
        </button>
      </div>
    </div>
  )
}

function Stat({ n, l, red }) {
  return (
    <div className="rounded-xl border border-border p-2.5 text-center">
      <div className={`font-heading font-bold text-xl ${red ? 'text-danger' : 'text-brand'}`}>{n}</div>
      <div className="text-[11px] text-muted-foreground">{l}</div>
    </div>
  )
}
