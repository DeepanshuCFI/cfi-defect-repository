import { Check, TriangleAlert } from 'lucide-react'
import { VERIFICATION_LABEL, fmtDate, locationWords, plural } from './vocab.js'

// One public report: date, casualties, summary, the reported defects with their
// evidence quotes, every source article, how it was checked and where it was pinned.
// Shared by the location dossier and the list of reports that have no pin.
export default function Report({ i, showPlace }) {
  const reportDate = i.date || i.sources?.[0]?.date
  const cas = (i.fatalities || 0) > 0 || (i.injuries || 0) > 0
  return (
    <article className="rounded-xl border border-border p-3.5 mb-2.5 bg-white">
      <div className="flex justify-between gap-2 text-[12px] text-muted-foreground">
        <span className="font-semibold text-foreground">{fmtDate(reportDate) || 'Date not stated'}</span>
        {cas && <span className="text-danger font-semibold">{[i.fatalities ? plural(i.fatalities, 'death') : null,
          i.injuries ? plural(i.injuries, 'injured', 'injured') : null].filter(Boolean).join(' · ')}</span>}
      </div>
      {showPlace && i.location && (
        <div className="text-[12px] font-medium mt-1.5">{i.location}</div>
      )}
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
        {' · '}Location: {locationWords(i)}
      </div>
    </article>
  )
}

function safeHost(url) {
  try { return new URL(url).hostname } catch { return url }
}
