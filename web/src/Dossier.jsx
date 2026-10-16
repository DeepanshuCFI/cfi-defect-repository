import { useEffect, useState } from 'react'
import { Check, Copy, Flag, MapPin, TriangleAlert, X } from 'lucide-react'
import { EVIDENCE_HINT, EVIDENCE_LABEL, PRECISION_HINT, PRECISION_LABEL, TIER_COLOR, TIER_HINT, TIER_LABEL,
         TIER_TEXT, VERIFICATION_LABEL, fmtDate, geocodeWords, plural } from './vocab.js'

// Members are fetched per location on open (data/incidents/<id>.json), not parsed
// from a 10 MB file on page load. A small cache keeps a session's re-opens free.
const cache = new Map()
async function loadMembers(id) {
  if (!cache.has(id)) {
    cache.set(id, fetch(`data/incidents/${id}.json`).then(r => (r.ok ? r.json() : [])).catch(() => []))
  }
  return cache.get(id)
}

// Order (launch review, 16 Oct 2026): place and what stands behind it, the reported
// defects, then the evidence — quotes and sources, the strongest content — then the
// counts, with the score breakdown folded away. Vocabulary: reports, not crashes.
export default function Dossier({ feature, meta, onClose, mobile }) {
  const p = feature.properties
  const [members, setMembers] = useState(null)
  const [copied, setCopied] = useState(false)
  useEffect(() => {
    let live = true
    setMembers(null)
    loadMembers(p.id).then(m => { if (live) setMembers(sortByReportDate(m)) })
    return () => { live = false }
  }, [p.id])

  const withCas = (members || []).filter(i => (i.fatalities || 0) > 0 || (i.injuries || 0) > 0).length
  const bd = p.breakdown
  const comps = bd?.components || {}
  const compLabel = {
    casualties: 'Casualties (weighted)', frequency: 'Report frequency (6 mo)',
    recency: 'Recency', vulnerable: 'Vulnerable road users',
    defect_sev: 'Defect severity', evidence: 'Evidence strength',
  }
  const placeLine = [p.city, p.district, p.state].filter(Boolean).join(', ')
  const link = `${window.location.origin}${window.location.pathname}?h=${p.id}`
  const correctionMail = `mailto:contact@crashfreeindia.org?subject=${encodeURIComponent(
    `Correction: location #${p.id} (${p.road_name || p.district || ''})`)}&body=${encodeURIComponent(
    'Please describe the correction. The entry will be marked disputed while we check.\n\nLocation: ' + link)}`
  const copy = async () => {
    try { await navigator.clipboard.writeText(link); setCopied(true); setTimeout(() => setCopied(false), 1500) }
    catch { window.prompt('Copy this link', link) }
  }
  const hidden = Number(p.records_hidden || 0)
  const hiddenCrashes = Number(p.hidden_casualty_crashes_6mo || 0)

  return (
    <div className={mobile
      ? 'absolute inset-x-0 bottom-0 h-[92%] z-30 bg-white border-t border-border rounded-t-2xl shadow-2xl overflow-y-auto overscroll-contain'
      : 'absolute top-0 right-0 h-full w-[26rem] max-w-full bg-white border-l border-border shadow-2xl overflow-y-auto z-10'}
      style={mobile ? { paddingBottom: 'env(safe-area-inset-bottom, 0px)' } : undefined}
      role="dialog" aria-label={`Location dossier: ${p.road_name || placeLine}`}>
      <div className="sticky top-0 bg-white border-b border-border px-5 py-3.5 flex justify-between items-start gap-3 z-10">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-1.5">
            <Chip bg={TIER_COLOR[p.tier]} fg={TIER_TEXT[p.tier]} title={TIER_HINT[p.tier]}>
              {TIER_LABEL[p.tier].toUpperCase()} · {Number(p.score).toFixed(1)}
            </Chip>
            {p.escalation
              ? <Chip bg="var(--color-danger)" fg="#fff" title={EVIDENCE_HINT.escalation}><Flag className="h-3 w-3" /> REPEAT CRASHES</Chip>
              : <Chip outline title={EVIDENCE_HINT[p.evidence]}>{(EVIDENCE_LABEL[p.evidence] || 'Single report').toUpperCase()}</Chip>}
            <Chip outline title={PRECISION_HINT[p.location_precision || 'feature']}>
              <MapPin className="h-3 w-3" /> {(PRECISION_LABEL[p.location_precision || 'feature']).toUpperCase()}
            </Chip>
          </div>
          <h2 className="font-heading font-bold text-[16px] leading-snug mt-2">{p.road_name || 'Unnamed stretch'}</h2>
          <div className="text-[12.5px] text-muted-foreground mt-0.5">{placeLine}</div>
        </div>
        <button onClick={onClose} aria-label="Close"
          className="no-print text-muted-foreground hover:text-foreground cursor-pointer p-2 -m-2 shrink-0">
          <X className="h-5 w-5" />
        </button>
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

      <div className="px-5 py-3">
        <div className="font-heading text-[10px] tracking-[0.2em] text-muted-foreground mb-2">
          {members === null ? 'REPORTS' : `${plural(members.length, 'REPORT').toUpperCase()}${withCas ? ` · ${withCas} WITH CASUALTIES` : ''}`}
        </div>
        {members === null && <div className="text-[12.5px] text-muted-foreground py-6 text-center">Loading reports…</div>}
        {members && members.length === 0 && (
          <div className="text-[12.5px] text-muted-foreground py-6 text-center">No public reports at this location right now.</div>
        )}
        {(members || []).map(i => {
          const reportDate = i.date || i.sources?.[0]?.date
          const cas = (i.fatalities || 0) > 0 || (i.injuries || 0) > 0
          return (
            <article key={i.id} className="rounded-xl border border-border p-3.5 mb-2.5">
              <div className="flex justify-between gap-2 text-[12px] text-muted-foreground">
                <span className="font-semibold text-foreground">{fmtDate(reportDate) || 'Date not stated'}</span>
                {cas && <span className="text-danger font-semibold">{[i.fatalities ? plural(i.fatalities, 'death') : null,
                  i.injuries ? plural(i.injuries, 'injured', 'injured') : null].filter(Boolean).join(' · ')}</span>}
              </div>
              <p className="text-[12.5px] mt-1.5 leading-relaxed">{i.summary}</p>
              {(i.defects || []).map((d, ix) => (
                <div key={ix} className="mt-2 text-[11.5px] bg-brand-lite rounded-lg px-3 py-2">
                  <b className="text-brand">{d.label}</b>
                  {d.evidence && <div className="text-muted-foreground mt-0.5">“{d.evidence}”</div>}
                </div>
              ))}
              <div className="mt-2 text-[11.5px]">
                {(i.sources || []).map((s, ix) => (
                  <div key={ix}>
                    <a href={s.url} target="_blank" rel="noopener noreferrer" className="text-brand hover:underline break-words">
                      {s.outlet || safeHost(s.url)}
                    </a>
                    {s.date && <span className="text-muted-foreground"> · {fmtDate(s.date)}</span>}
                  </div>
                ))}
              </div>
              <div className="mt-1.5 text-[10.5px] text-muted-foreground">
                {i.verification === 'disputed'
                  ? <span className="inline-flex items-center gap-1 text-warn font-semibold"><TriangleAlert className="h-3 w-3" /> {VERIFICATION_LABEL.disputed}</span>
                  : <span className="inline-flex items-center gap-1"><Check className="h-3 w-3" /> {VERIFICATION_LABEL[i.verification] || VERIFICATION_LABEL.auto}</span>}
                {' · '}Location: {geocodeWords(i.geocode_method, i.geocode_conf)}
              </div>
            </article>
          )
        })}
      </div>

      {hidden > 0 && (
        <div className="mx-5 mb-3 rounded-xl bg-brand-soft border border-border px-4 py-3 text-[12px] text-muted-foreground">
          Also {plural(hidden, 'record')} at this location {hidden === 1 ? 'is' : 'are'} not shown publicly: crash reports that
          make no infrastructure claim{hiddenCrashes ? `, ${hiddenCrashes} of them with casualties in the last 6 months` : ''}.
          They count toward the repeat-crashes flag, never toward the score.
        </div>
      )}

      {bd && (
        <details className="px-5 py-3 border-t border-border group">
          <summary className="font-heading text-[10px] tracking-[0.2em] text-muted-foreground cursor-pointer list-none flex items-center justify-between">
            <span>WHY THIS SCORE <span className="normal-case tracking-normal">(public evidence only)</span></span>
            <span className="text-[11px] text-brand group-open:hidden">show</span>
            <span className="text-[11px] text-brand hidden group-open:inline">hide</span>
          </summary>
          <div className="mt-2">
            {Object.entries(comps).map(([k, v]) => (
              <div key={k} className="flex items-center gap-2 py-0.5">
                <span className="text-[11.5px] text-muted-foreground w-40 shrink-0">{compLabel[k] || k}</span>
                <div className="flex-1 h-1.5 rounded-full bg-brand-lite overflow-hidden">
                  <div className="h-full bg-brand rounded-full" style={{ width: `${Math.round(v * 100)}%` }} />
                </div>
                <span className="text-[11px] text-muted-foreground w-8 text-right tabular-nums">{(v * 100).toFixed(0)}</span>
              </div>
            ))}
            <div className="text-[10.5px] text-muted-foreground mt-1.5">
              Weights: casualties {bd.weights?.w1_fatalities_weighted} · frequency {bd.weights?.w2_crash_frequency} ·
              recency {bd.weights?.w3_recency} · vulnerable {bd.weights?.w4_vulnerable_user_share} ·
              severity {bd.weights?.w5_defect_severity} · evidence {bd.weights?.w6_evidence_strength} ·
              computed {bd.computed_for}. Tier: {TIER_HINT[p.tier]}.
            </div>
          </div>
        </details>
      )}

      <div className="px-5 pb-6 pt-3 border-t border-border flex flex-wrap gap-2 no-print">
        <button onClick={copy}
          className="inline-flex items-center gap-1.5 h-11 text-[13px] px-5 rounded-full border border-border text-muted-foreground hover:border-brand hover:text-brand cursor-pointer">
          {copied ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />} {copied ? 'Link copied' : 'Copy link'}
        </button>
        <a href={correctionMail}
          className="inline-flex items-center h-11 text-[13px] px-5 rounded-full border border-border text-muted-foreground hover:border-brand hover:text-brand">
          Report a correction
        </a>
        {!mobile && (
          <button onClick={() => window.print()}
            className="inline-flex items-center h-11 text-[13px] px-5 rounded-full border border-border text-muted-foreground hover:border-brand hover:text-brand cursor-pointer">
            Print / save PDF
          </button>
        )}
        <div className="w-full text-[10.5px] text-muted-foreground mt-1">
          Cite as: Crashfree India, National Road-Infrastructure Defect Repository, location #{p.id},
          as of {fmtDate(meta.generated_at)}.
        </div>
      </div>
    </div>
  )
}

function Chip({ children, bg, fg, outline, title }) {
  return (
    <span title={title}
      className={`inline-flex items-center gap-1 font-heading font-bold text-[10px] px-2 py-0.5 rounded-full ${outline ? 'border border-border text-muted-foreground' : ''}`}
      style={outline ? undefined : { background: bg, color: fg || '#fff' }}>
      {children}
    </span>
  )
}

function sortByReportDate(members) {
  const key = i => i.date || i.sources?.[0]?.date || ''
  return [...members].sort((a, b) => key(b).localeCompare(key(a)))
}

function safeHost(url) {
  try { return new URL(url).hostname } catch { return url }
}
