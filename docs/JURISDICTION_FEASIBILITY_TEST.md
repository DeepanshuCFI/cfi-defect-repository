# Jurisdiction Router — 50-Point Feasibility Test

**Status:** proposed, not run
**Purpose:** kill or confirm the "who owns this road?" router *before* any engineering.
**Effort:** ~2 person-days across 2 raters, one calendar week.
**Decision it produces:** build / don't build / build for a narrower stratum.

---

## 1. What is actually being tested

Not "can we geocode." Not "can we find the defect." Those are solved.

The claim under test is:

> Given a geocoded point and a defect type, a competent human can identify the
> authority legally responsible for that defect, from open sources, in reasonable
> time, and two competent humans will agree on the answer.

If a **human** can't do this reliably, no automated router can. That is the whole
logic of the test — it establishes the ceiling before anyone builds toward it.

### The unit is (point × defect_type), not point

An incident at an NH-through-town junction with `potholes_or_broken_surface` and
`missing_footpath_or_encroached_footpath` is **two rows**, and the correct answers
are probably two different authorities (NHAI/concessionaire vs ULB). A test that
resolves per-point rather than per-(point, defect) would report a false pass by
hiding the hardest part of the problem.

The `incident_defect.defect_type` vocabulary in `config/config_defect_taxonomy.json`
is already the asset-class axis this needs. Use it as-is.

---

## 2. Sample frame

**Do not sample randomly.** Open road-ownership data in India is best exactly where
crashes are rarest (rural/PMGSY, covered by GeoSadak open data under GODL) and worst
where they concentrate (urban arterials, NH ribbon development). A random sample
returns whatever is most common and teaches nothing about the failure mode.

### Stratify by `road_type` — 10 per stratum

| Stratum | `road_type` | Why it's in the sample |
|---|---|---|
| A | `NH` | Tests the NHAI / NHIDCL / state-PWD-agency split — the mapping that exists in no open layer |
| B | `SH` + `MDR` | The worst-supplied stratum; no national open layer |
| C | `district` + `rural` | Should be the easy one — GeoSadak DRRP covers it. If this fails, everything fails |
| D | `urban_arterial` + `urban_local` | The overlapping-jurisdiction stratum; where the model most likely breaks |
| E | `unknown` | Currently ~the extractor's failure bucket. Measures how much of the corpus is unroutable *in principle* |

Stratum E is the one people will want to drop. Don't — its size is itself a finding.
Run `select road_type, count(*) from incident group by 1` first; if `unknown` is a
large share of the corpus, the router's addressable coverage is capped there
regardless of how well A–D perform.

### Isolate the variable: sample only well-geocoded points

Restrict to `geocode_confidence >= 0.8` and exclude centroid methods. A bad pin
would fail the test for reasons that have nothing to do with jurisdiction, and the
`_unanchored` / `_wide_area` / `_stateless_hit` qualifiers from migration 012 exist
precisely to mark those.

This means **the test measures the best case**. That is deliberate: if resolution
fails at the ceiling, it certainly fails in production.

### Sampling query

```sql
-- Deterministic stratified sample: 10 incident×defect pairs per stratum.
-- Re-runnable, same 50 rows every time (md5 ordering, no random()).
with pool as (
  select
    i.id                as incident_id,
    d.defect_type,
    i.road_name,
    i.road_type,
    i.admin_state,
    i.admin_district,
    i.admin_city,
    st_y(i.geom::geometry) as lat,
    st_x(i.geom::geometry) as lon,
    i.crash_date,
    i.geocode_confidence,
    i.geocode_method,
    d.evidence_snippet,
    case i.road_type
      when 'NH' then 'A_NH'
      when 'SH' then 'B_SH_MDR'
      when 'MDR' then 'B_SH_MDR'
      when 'district' then 'C_district_rural'
      when 'rural' then 'C_district_rural'
      when 'urban_arterial' then 'D_urban'
      when 'urban_local' then 'D_urban'
      else 'E_unknown'
    end as stratum
  from incident i
  join incident_defect d on d.incident_id = i.id
  where i.geom is not null
    and i.geocode_confidence >= 0.8
    and i.geocode_method not in ('city_centroid','district_centroid')
    and coalesce(i.geocode_qualifier,'') = ''      -- no _unanchored/_wide_area/_stateless_hit
    and d.defect_type not in (
      'no_infrastructure_defect_identified',
      'other_infrastructure'                        -- reviewer-confirmed, not a clean asset class
    )
    and i.verification_status in ('reviewed','verified','auto_published','disputed')
),
ranked as (
  select *, row_number() over (partition by stratum order by md5(incident_id::text || defect_type)) rn
  from pool
)
select stratum, incident_id, defect_type, road_name, road_type,
       admin_state, admin_district, admin_city, lat, lon, crash_date,
       geocode_confidence, geocode_method, evidence_snippet
from ranked
where rn <= 10
order by stratum, rn;
```

If a stratum returns fewer than 10 rows, **record the shortfall rather than
back-filling from another stratum** — thin strata are a coverage finding.

---

## 3. Free ground truth you already hold

`watch_site` (migration 011) carries **`authority`** and **`authority_status`** for
32 junctions seeded from the Rakshak audit sheet — human-assigned, by CFI's own
auditors, before this question was ever framed. 12 have coordinates.

Use these as a **calibration set, run first**, before the 50.

Important caveat on what they mean: `watch_site.authority` is *"who the complaint
was filed with"*, which is revealed practice, not necessarily legal ownership. So
disagreement between a rater and the sheet is informative in both directions:

- Rater agrees with sheet → the resolution method reproduces expert judgement. Good.
- Rater disagrees, rater is right → **CFI has been filing complaints with the wrong
  authority**, which is a finding worth more than the router.
- Rater disagrees, sheet is right → the open-source method misses something auditors
  know from the ground. Bounds the router's achievable accuracy.

Run the 12 coordinate-bearing sites blind (authority column hidden) before touching
the 50. It costs half a day and calibrates the raters.

---

## 4. Protocol

**Raters:** two, working independently, no discussion until both are finished.
One should be Deep or Akhtar; the other should be someone with Rakshak audit
experience but no involvement in this proposal — a rater who knows the answer you
want will find it.

**Sources permitted** (this list defines what the router could later automate):

1. GeoSadak PMGSY OpenData — statewise shapefiles, GODL, includes the District
   Rural Road Plan network (`https://geosadak-pmgsy.nic.in/OpenData`)
2. OSM — route relations, `ref`, `operator`, boundary relations
3. MoRTH NH lists / gazette notifications
4. State PWD circle/division listings
5. ULB / municipal limit boundaries where they exist (DataMeet ~28 cities)
6. `data.gov.in`
7. The source article itself (`incident_defect.evidence_snippet`, `source_article`)

**Explicitly out of bounds:** phoning an official. The router will never have that,
so allowing it inflates the result.

**Time box:** 10 minutes per row, hard stop. Record whether the cap was hit. Without
a cap, "minutes per point" is unmeasurable and a determined rater will resolve
everything given unlimited time — which proves nothing about a product.

### Rater sheet — one row per (incident_id, defect_type)

| Column | Values |
|---|---|
| `incident_id` | from sample |
| `defect_type` | from sample |
| `stratum` | A–E |
| `authority_primary` | free text, e.g. "NHAI PIU Chhapra" — the entity you would name |
| `authority_class` | `NHAI` / `NHIDCL` / `state_PWD` / `ULB` / `panchayat` / `cantonment` / `rail` / `metro_or_parastatal` / `concessionaire` / `private_or_plot` / `unresolvable` |
| `concessionaire_suspected` | yes / no / unknown — is this a BOT/HAM/TOT stretch? |
| `secondary_authorities` | any others with a plausible claim on *this defect* |
| `resolution_path` | which of sources 1–7 actually produced the answer |
| `minutes` | integer; `10` if capped |
| `capped` | yes / no |
| `confidence` | high / medium / low |
| **`notice_ready`** | **yes / no — "would I name this authority as respondent in a legal notice?"** |
| `notice_evidence_exists` | yes / no — is there any public record this authority was already put on notice about this location? |

`notice_ready` is the column that matters. Confidence is soft and raters inflate it;
a forced binary tied to a consequence is much harder to fudge.

---

## 5. Metrics and kill criteria

Computed over all 50 rows, and again per stratum.

| # | Metric | Definition | Kill threshold |
|---|---|---|---|
| 1 | **Resolution rate** | % of rows where both raters returned `notice_ready = yes` | **< 70% → dead** |
| 2 | **Inter-rater agreement** | raw agreement on `authority_class` | **< 0.80 → dead** |
| 3 | **Median effort** | median `minutes` over *resolved* rows | **> 10 min (i.e. mostly capped) → dead** |
| 4 | Concessionaire opacity | % of stratum A rows with `concessionaire_suspected = unknown` | not a kill; sizes the contract-layer gap |
| 5 | Notice-trail density | % with `notice_evidence_exists = yes` | not a kill; sizes the *actual* product |

**Metric 2 is the real gate.** Metric 1 can be gamed by a confident rater. If two
competent people independently disagree on who owns a road more than one time in
five, the ground truth does not exist in open sources, and there is nothing to
automate — no amount of engineering fixes an unresolvable target.

### Reading the stratum breakdown

Expected shape, if the pressure-test reasoning holds: **C > A > B ≈ D**, with E
largely unresolvable. If that is what comes out, the honest conclusion is *not*
"the router works" — it is "the router works where crashes aren't," which is an
argument against building it as a headline product.

A result where **D (urban) resolves well** would genuinely surprise me and would be
the strongest possible argument to build.

---

## 6. The second question this test answers

Metric 5 tests the more interesting claim: that the defensible product is the
**notice-and-liability trail**, not the router.

The Bombay High Court order of 13 October 2025 (suo motu, Mohite Dere & Patil JJ.)
makes the legally operative question *"which authority was put on notice, when, and
did it act within 48 hours"* — with ₹6 lakh for death, ₹50,000–₹2.5 lakh for injury,
District Compensation Committees, DLSA involvement, and personal liability of
officers for delayed disbursement.

The schema is already most of the way to serving that:

- `review_action` — attributed, timestamped, before/after JSON audit log
- `hotspot.status` — `new → monitoring → escalated_to_govt → acknowledged → fixed`
  is already a notice-and-response state machine
- `watch_site.authority` + `authority_status` — filed-with authority and its
  response state
- `watch_hit` — a fresh crash at a site marked "Fully Implemented" is exactly the
  reopener evidence the order's 48-hour rule turns into money

What's missing is a `notified_at` timestamp with served-proof, and a Maharashtra
corpus. Both are small next to a national router.

**So if metric 5 comes back high while metrics 1–2 come back marginal, that is not
a failed test — that is the test telling you to build the other thing.**

---

## 7. Deliverable

One page, at the end of the week:

- the five metrics, overall and per stratum
- the list of rows where raters disagreed, with both answers (this is the most
  informative artifact the exercise produces — read it before the numbers)
- calibration result against the 12 `watch_site` records, including any case where
  CFI appears to have filed with the wrong authority
- a build / don't build / narrow-scope recommendation

Store the completed rater sheets as `docs/jurisdiction_test_YYYYMMDD.csv`. Whatever
the verdict, the 50 adjudicated rows become the first ground-truth eval set for any
future automated attempt — so the week is not wasted even on a kill.

---

## 8. What this test deliberately does not do

- It does not test whether authorities *respond*. That needs a live complaint
  programme, not a dataset.
- It does not test demand. Even a clean pass leaves open whether anyone calls the
  API — the honest list of plausible consumers is short (DLSAs post-Bombay HC, MACT
  lawyers, insurers doing subrogation, and CFI's own pipeline).
- It does not model temporal validity. Authority changes by gazette — NH
  denotifications, NH↔state-PWD transfer disputes — with no spatial update anywhere.
  Every answer here is "as of today", and the maintenance cost of keeping it true is
  a separate question that the test does not touch.
