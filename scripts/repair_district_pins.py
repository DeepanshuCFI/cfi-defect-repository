#!/usr/bin/env python3
"""One-off repair: re-geocode published incidents pinned in the WRONG DISTRICT.

Until 1 Aug 2026 the geocode ladder validated state and nothing else (_state_ok). A
Hathras crash could no longer land in Odisha, but a Gopalganj crash landing in Nawada
passed cleanly — same state, wrong district, 0.80 confidence, straight past the 0.6
publish gate. DBSCAN then correctly clustered such pins (genuinely ~100m apart) into
"hotspots" merging unrelated complaints from different districts. A blind audit of the
60 map locations claiming a repeat pattern found 1 sound; 34 of 35 escalation
candidates were built this way.

pipeline/processing/geocode.py now has _district_ok, but that only affects FUTURE
geocodes. Already-published pins sit at 0.80 — above the sub-0.6 rescue pass — so
nothing revisits them. This script does.

WHAT IT DOES per incident: re-geocode with the same inputs cmd_geocode uses
(coalescing incident.admin_* with source_article as the anchor), then
  - write the corrected geom/confidence/method/qualifier
  - if the corrected confidence falls below the 0.6 gate, demote to needs_human so it
    goes to review rather than silently vanishing
  - audit-log before AND after on every change, so each one is individually reversible

It does NOT recompute hotspots. Run `python -m pipeline.run recompute` afterwards, or
let the next daily run do it — the map only changes once clusters are rebuilt.

Measured on the audited set (57 incidents in the 34 unsupported escalation hotspots):
41 stay public, 16 demote, 8 pins move >25km (worst 341.9km). After recompute, 30 of
34 false escalation flags clear; 4 survive and were correct all along.

Publication status is the wrong axis for the escalation flag, though. cluster.py selects
on geocode confidence alone, so machine_ok records — behaviour-only crashes, never
published, deliberately kept so the >=3-in-6-months counter stays honest — sit in
hotspots and vote with pins nothing has district-validated. Measured 1 Aug 2026: every
one of the 15 escalation flags left on the public map needs its machine_ok members to
reach 3. --scope escalation and --scope voters reach them; for those records the fix is
the lower confidence itself (under 0.5 they stop clustering), not a status change.

Usage:
    python3 scripts/repair_district_pins.py                      # dry run, audited set
    python3 scripts/repair_district_pins.py --scope escalation   # the 15 live flags
    python3 scripts/repair_district_pins.py --scope voters       # everything that clusters
    python3 scripts/repair_district_pins.py --scope escalation --apply
Default is a dry run: it prints the before/after and changes nothing.
"""
import argparse
import json
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402

from pipeline.settings import DATABASE_URL  # noqa: E402

REVIEWER = "pipeline:repair_district_pins"
GATE = 0.6
# pipeline.processing.cluster.MIN_GEOCODE_CONF — below this an incident stops clustering,
# which is how a non-public record stops voting on the escalation rule.
CLUSTER_MIN_CONF = 0.5
PUBLISHED = "('auto_published','reviewed','verified')"
AUDIT = "docs/HOTSPOT_AUDIT.json"
SCOPES = ("audited", "published", "escalation", "voters")


def plan(published: bool, new_conf: float | None) -> str:
    """What a corrected confidence means for this record. Pure, so it is testable.

      'demote'      — published and under the publish gate: off the map, into review.
      'stop_voting' — not public and under the cluster floor: it keeps its row but stops
                      feeding DBSCAN, so it stops counting toward the escalation rule.
      'keep'        — the corrected pin stands.

    The two thresholds are deliberately different. A published record at 0.55 is below
    the 0.6 gate and must leave the public map; an identical non-public record at 0.55 is
    still a good enough pin to cluster on, and demoting it would throw away frequency
    evidence the >=3-in-6-months counter depends on.
    """
    c = new_conf or 0
    if published:
        return "demote" if c < GATE else "keep"
    return "stop_voting" if c < CLUSTER_MIN_CONF else "keep"


def _targets(cur, scope: str) -> list[tuple]:
    """Which incidents to re-geocode.

    'audited' (default) and 'published' cover the public map: the audited set is the
    incidents inside hotspots the blind audit judged to carry an unsupported escalation
    flag; 'published' widens to every published incident, since the guard gap reaches
    beyond the audited 60.

    But publication status is the WRONG axis for the escalation flag. cluster.py selects
    on `geocode_confidence >= 0.5` and nothing else — no verification_status filter — so
    a machine_ok record (a behaviour-only crash, deliberately kept for frequency stats
    and never published) still lands in a hotspot and still counts toward the
    >=3-in-6-months rule, carrying a pin nothing has ever district-validated. Measured
    1 Aug 2026: all 15 surviving escalation flags on the public map need their machine_ok
    members to reach 3; not one clears the bar on published incidents alone. 'escalation'
    and 'voters' reach those records.
    """
    params: list = []
    if scope in ("audited", "published"):
        where = f"i.verification_status in {PUBLISHED}"
        if scope == "audited":
            try:
                ids = [x["hotspot_id"] for x in json.load(open(AUDIT))["findings"]
                       if "ESCALATION FLAG UNSUPPORTED" in x["problems"]]
            except Exception as e:
                sys.exit(f"cannot read {AUDIT} ({e}). Run the hotspot audit first, or "
                         f"pass --scope published.")
            if not ids:
                sys.exit(f"{AUDIT} lists no unsupported escalation flags — nothing to "
                         f"repair.")
            where += " and i.cluster_id = any(%s)"
            params.append(ids)
    else:
        # Everything DBSCAN feeds on, whatever its status — these are the votes.
        where = "i.geom is not null and i.geocode_confidence >= %s"
        params.append(CLUSTER_MIN_CONF)
        if scope == "escalation":
            where += """ and i.cluster_id in (
                           select h.id from hotspot h
                           where h.escalation_candidate
                             and exists (select 1 from public_hotspot p where p.id = h.id))"""
    cur.execute(f"""
        select i.id, i.cluster_id, i.location_text_best, i.road_name, i.admin_city,
               coalesce(i.admin_district, a.district), coalesce(i.admin_state, a.state),
               ST_Y(i.geom::geometry), ST_X(i.geom::geometry),
               i.geocode_confidence, i.geocode_method, i.verification_status
        from incident i join source_article a on a.id = i.primary_source_id
        where {where}
        order by i.cluster_id, i.id""", params)
    return cur.fetchall()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    ap.add_argument("--scope", choices=SCOPES, default=None,
                    help="audited (default): incidents in the audited unsupported-flag "
                         "hotspots. published: every published incident. escalation: "
                         "every CLUSTERING incident inside a public hotspot that still "
                         "carries an escalation flag, whatever its status — this is the "
                         "set that decides the headline claim. voters: every clustering "
                         "incident in the DB.")
    ap.add_argument("--all-published", action="store_true",
                    help="deprecated alias for --scope published")
    ap.add_argument("--limit", type=int, default=0, help="0 = no limit")
    ap.add_argument("--min-move-km", type=float, default=0.0,
                    help="only act where the corrected pin moves at least this far. "
                         "Stages the repair: >25 is wrong by any standard, whereas a "
                         "0km/confidence-only correction is a judgement call.")
    args = ap.parse_args()

    from pipeline.processing import geocode as g
    # The live cache holds PRE-FIX results with no district field. Reusing it would make
    # every hit look 'districtless' and cap correct pins at 0.55 — repairing the map with
    # the very staleness we are repairing. Resolve fresh into a throwaway cache.
    g.CACHE_PATH = pathlib.Path(tempfile.mkdtemp()) / "repair_cache.json"
    g._cache = {}

    scope = args.scope or ("published" if args.all_published else "audited")
    conn = psycopg.connect(DATABASE_URL, connect_timeout=30)
    cur = conn.cursor()
    rows = _targets(cur, scope)
    if args.limit:
        rows = rows[:args.limit]
    print(f"re-geocoding {len(rows)} incident(s) — scope '{scope}'\n")

    stats = {"checked": 0, "corrected": 0, "unchanged": 0, "demoted": 0,
             "no_hit": 0, "moved_over_25km": 0, "skipped_below_min_move": 0,
             "stopped_voting": 0}
    for (iid, cid, loc, road, city, dist, state, olat, olon, oconf, omethod, ostatus) in rows:
        stats["checked"] += 1
        published = ostatus in ("auto_published", "reviewed", "verified")
        r = g.geocode(loc or "", road_name=road, admin_city=city,
                      admin_district=dist, admin_state=state)
        nlat, nlon, nconf = r["lat"], r["lon"], r["geocode_confidence"]
        if nlat is None:
            # Every ladder rung rejected — the old pin was wrong and we have no better
            # one. Never leave a known-bad pin standing, but the remedy differs: a
            # published record goes to a human, while a machine_ok record is already
            # invisible and the only harm it does is vote.
            stats["no_hit"] += 1
            if args.min_move_km > 0:
                # not a correction, a demotion — a different decision. Leave it for a
                # run without --min-move-km rather than fold it in silently.
                stats["skipped_below_min_move"] += 1
                continue
            action = plan(published, 0)
            if args.apply:
                if action == "demote":
                    _demote(cur, iid, "re-geocode found no district-valid location; "
                                      "prior pin failed the district guard")
                else:
                    _stop_voting(cur, iid, olat, olon, oconf, omethod, ostatus)
                conn.commit()
            if action == "stop_voting":
                stats["stopped_voting"] += 1
            print(f"  #{iid:<5} NO VALID PIN -> "
                  f"{'review' if action == 'demote' else 'stops clustering'}")
            continue
        km = (((nlat - olat) ** 2 + (nlon - olon) ** 2) ** 0.5) * 111.0 if olat else 0.0
        if km > 25:
            stats["moved_over_25km"] += 1
        same = km < 0.05 and abs((nconf or 0) - float(oconf or 0)) < 0.001
        if same:
            stats["unchanged"] += 1
            continue
        if km < args.min_move_km:
            # e.g. a 0km pin whose confidence merely fell 0.80 -> 0.55: the location was
            # right, only the certainty was unearned. Real, but a separate call.
            stats["skipped_below_min_move"] += 1
            continue
        stats["corrected"] += 1
        # A non-public record has no status to change — writing the lower confidence is
        # itself the fix, because anything under CLUSTER_MIN_CONF stops feeding DBSCAN.
        action = plan(published, nconf)
        demote = action == "demote"
        if demote:
            stats["demoted"] += 1
        if action == "stop_voting":
            stats["stopped_voting"] += 1
        flag = {"demote": "DEMOTES", "stop_voting": "stops voting",
                "keep": "stays"}[action]
        print(f"  #{iid:<5} hs{str(cid):<5} {str(dist)[:16]:<16} moved {km:>7.1f} km  "
              f"conf {float(oconf or 0):.2f}->{nconf}  {flag}")
        if not args.apply:
            continue
        before = {"lat": olat, "lon": olon, "geocode_confidence": float(oconf or 0),
                  "geocode_method": omethod, "verification_status": ostatus}
        after = {"lat": nlat, "lon": nlon, "geocode_confidence": nconf,
                 "geocode_method": r["geocode_method"],
                 "geocode_qualifier": r["geocode_qualifier"], "moved_km": round(km, 1)}
        cur.execute("""update incident
                       set geom = ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography,
                           geocode_confidence = %s, geocode_method = %s,
                           geocode_qualifier = %s, updated_at = now()
                       where id = %s""",
                    (nlon, nlat, nconf, r["geocode_method"], r["geocode_qualifier"], iid))
        if demote:
            _demote(cur, iid, f"district-corrected pin is {nconf} — below the {GATE} "
                              f"publish gate")
        cur.execute("""insert into review_action (entity_type, entity_id, reviewer,
                       action, before_json, after_json, note)
                       values ('incident',%s,%s,'edit',%s::jsonb,%s::jsonb,%s)""",
                    (iid, REVIEWER, json.dumps(before), json.dumps(after),
                     f"district-anchoring repair: pin moved {km:.1f} km into "
                     f"{dist or 'the expected district'}"))
        conn.commit()

    print(f"\nDONE {stats}")
    if stats["skipped_below_min_move"]:
        print(f"NOTE: {stats['skipped_below_min_move']} eligible change(s) skipped by "
              f"--min-move-km {args.min_move_km}. They are still wrong; rerun without "
              f"the flag (or with a lower value) to include them.")
    if args.apply:
        print("Hotspots are NOT recomputed. Run `python -m pipeline.run recompute` "
              "(or wait for the daily run) before the map reflects this.")
    else:
        print("DRY RUN — nothing written. Rerun with --apply.")
    conn.close()


def _stop_voting(cur, iid: int, olat, olon, oconf, omethod, ostatus) -> None:
    """Neutralise a non-public pin that has no district-valid location.

    Zeroing the confidence is enough: cluster.py selects on `geocode_confidence >= 0.5`,
    so the record stops forming and joining hotspots — which is the whole harm it was
    doing. geom is deliberately KEPT, and the prior values go in the audit row, so the
    change is reversible one incident at a time. Status is untouched: machine_ok is a
    real judgement about the article (behaviour-only crash), not about the pin, and
    review_queue only shows status='auto' so this never lands on a reviewer's desk.
    """
    cur.execute("""update incident set geocode_confidence = 0,
                   geocode_qualifier = 'no_district_valid_location', updated_at = now()
                   where id = %s""", (iid,))
    cur.execute("""insert into review_action (entity_type, entity_id, reviewer, action,
                   before_json, after_json, note)
                   values ('incident',%s,%s,'edit',%s::jsonb,%s::jsonb,%s)""",
                (iid, REVIEWER,
                 json.dumps({"lat": olat, "lon": olon,
                             "geocode_confidence": float(oconf or 0),
                             "geocode_method": omethod,
                             "verification_status": ostatus}),
                 json.dumps({"geocode_confidence": 0,
                             "geocode_qualifier": "no_district_valid_location"}),
                 "district-anchoring repair: no district-valid location exists, so this "
                 "pin no longer clusters or counts toward the escalation rule"))


def _demote(cur, iid: int, why: str) -> None:
    cur.execute("update incident set verification_status='needs_human', updated_at=now() "
                "where id=%s", (iid,))
    cur.execute("""insert into review_action (entity_type, entity_id, reviewer, action,
                   after_json, note) values ('incident',%s,%s,'edit',%s::jsonb,%s)""",
                (iid, REVIEWER, json.dumps({"demoted_to": "needs_human"}), why))


if __name__ == "__main__":
    main()
