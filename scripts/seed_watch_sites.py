#!/usr/bin/env python3
"""Seed watch_site from the Rakshak authority-complaint sheet (2026-07-20 snapshot).
Idempotent: upserts by name. Sites without sheet coordinates are name-match only.
Usage: python3 scripts/seed_watch_sites.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline.db import connect  # noqa: E402

# (name, variants, city, district, state, lat, lon, authority, authority_status)
SITES = [
 ("Outer Ring Road IIT Delhi", ["Outer Ring Road, Hauz Khas", "IIT Delhi, Outer Ring Road"],
  "Delhi", "South Delhi", "Delhi", 28.5458, 77.1956, "PWD", "Fully Implemented"),
 ("Britannia Chowk", ["ब्रिटानिया चौक"], "Delhi", "North West Delhi", "Delhi",
  None, None, "PWD; DRSC", "Escalated / Awaiting Response"),
 ("Kunnamangalam Junction", ["Kunnamangalam Jn", "കുന്ദമംഗലം"], "Kozhikode", "Kozhikode",
  "Kerala", 11.3068, 75.8795, "PWD NH Division Kozhikode", "Partially Implemented"),
 ("Chevayur Junction", ["Chevayoor"], "Kozhikode", "Kozhikode", "Kerala",
  None, None, "PWD", "Partially Implemented"),
 ("Medical College Junction Kozhikode", ["Medical College Junction"], "Kozhikode",
  "Kozhikode", "Kerala", None, None, "PWD", "No Response Received"),
 ("Sardar Patel Road IIT Madras", ["Sardar Patel Road", "IIT Madras Main Gate"],
  "Chennai", "Chennai", "Tamil Nadu", 13.0067, 80.2429,
  "C&M City Roads Chennai", "On Hold (Pending Construction)"),
 ("IITM Taramani Gate Junction", ["Taramani Gate"], "Chennai", "Chennai", "Tamil Nadu",
  None, None, "PWD Road Safety Wing", "Fully Implemented"),
 ("TIDEL Park U-Bridge Junction", ["TIDEL Park", "U-Bridge OMR"], "Chennai", "Chennai",
  "Tamil Nadu", None, None, "Greater Chennai Corporation", "Partially Implemented"),
 ("Gilat Bazar Shivpur", ["Zilat Bazar", "Gilat Bazaar", "गिलट बाजार", "Tarna Shivpur"],
  "Varanasi", "Varanasi", "Uttar Pradesh", 25.3593, 82.9633,
  "PWD Varanasi", "Partially Implemented"),
 ("Seergovardhanpur Dafi Toll Plaza", ["Dafi Toll", "सीरगोवर्धनपुर", "डाफी टोल"],
  "Varanasi", "Varanasi", "Uttar Pradesh", 25.2789, 82.9945, "PWD", "Fully Implemented"),
 ("Gurubagh Bhelupur", ["गुरुबाग", "भेलूपुर"], "Varanasi", "Varanasi", "Uttar Pradesh",
  None, None, "Nagar Nigam Varanasi", "Referred to Other Department"),
 ("Bhagwanpur Mod Trauma Centre", ["भगवानपुर मोड़", "Bhagwanpur Trauma"], "Varanasi",
  "Varanasi", "Uttar Pradesh", None, None, "Nagar Nigam Varanasi", "Action Initiated"),
 ("Akurli Road Underpass", ["Akurli Underpass", "Akurli Road Kandivali"], "Mumbai",
  "Mumbai Suburban", "Maharashtra", 19.2013, 72.8611, "PWD", "Fully Implemented"),
 ("C-49 Commercial Area Durgapur", ["Healthworld Hospital Durgapur", "EDIC Office"],
  "Durgapur", "Paschim Bardhaman", "West Bengal", 23.5396, 87.2883,
  "PWD", "Fully Implemented"),
 ("Chuttugunta Circle", ["Chuttugunta Roundabout", "చుట్టుగుంట"], "Guntur", "Guntur",
  "Andhra Pradesh", 16.2911, 80.4256, "PWD; Guntur MC", "Fully Implemented"),
 ("Tejaji Nagar Junction", ["तेजाजी नगर", "Tejaji Nagar Square"], "Indore", "Indore",
  "Madhya Pradesh", 22.7086, 75.8635, "NHAI / PWD", "Partially Implemented"),
 ("Rau Gol Square", ["राऊ गोल", "Rau Circle"], "Indore", "Indore", "Madhya Pradesh",
  22.6347, 75.8114, "CM Helpline MP; Indore MC", "Fully Implemented"),
 ("Teen Imli Square", ["तीन इमली", "टीन इमली"], "Indore", "Indore", "Madhya Pradesh",
  22.6901, 75.8834, "CM Helpline MP; Indore MC", "Escalated / Awaiting Response"),
 ("IT Park Chowk Dehradun", ["IT Park Chowk"], "Dehradun", "Dehradun", "Uttarakhand",
  30.355771, 78.084448, "PWD", "Pending Confirmation"),
 ("Roorkee-Manglaur Bypass", ["Roorkee Manglaur", "NH-344 bypass"], "Roorkee",
  "Haridwar", "Uttarakhand", None, None, "NHAI", "Action Initiated"),
 ("Y-Bifurcation Milk Bar Roorkee", ["Milk Bar Civil Lines"], "Roorkee", "Haridwar",
  "Uttarakhand", None, None, "PWD Haridwar", "Escalated / Awaiting Response"),
 ("Bhatha Sahib Chowk", ["Gurdwara Bhatha Sahib"], "Rupnagar", "Rupnagar", "Punjab",
  None, None, "NHAI", "Closed (Jurisdiction Reassigned)"),
 ("Surjit Chowk Rupnagar", ["Surjit Chowk"], "Rupnagar", "Rupnagar", "Punjab",
  None, None, "PWD; Rupnagar MC", "Closed (Jurisdiction Reassigned)"),
 # status-tab extras (name-match only)
 ("Kamta Chauraha", ["कमता चौराहा"], "Lucknow", "Lucknow", "Uttar Pradesh",
  None, None, None, "Disposed (other authority)"),
 ("Chandigarh-Nangal Road Rupnagar", ["Chandigarh Nangal road"], "Rupnagar", "Rupnagar",
  "Punjab", None, None, "NHAI", "Referred to NHAI"),
 ("Majura Gate", ["मजूरा गेट"], "Surat", "Surat", "Gujarat", None, None, None, None),
 ("RK Mission Hospital Ganga Market", ["Ganga Market Itanagar"], "Itanagar",
  "Papum Pare", "Arunachal Pradesh", None, None, None, None),
 ("Krishnalanka Bandar Locks Junction", ["Krishna Lanka", "Bandar Locks"], "Vijayawada",
  "Krishna", "Andhra Pradesh", None, None, None, None),
 ("Eranhipalam Junction", ["Eranhipalam"], "Kozhikode", "Kozhikode", "Kerala",
  None, None, None, "Not filed"),
 ("Boragaon Flyover", ["Boragaon"], "Guwahati", "Kamrup Metropolitan", "Assam",
  None, None, None, None),
 ("AEC Road Turn Jalukbari", ["Jalukbari AEC"], "Guwahati", "Kamrup Metropolitan",
  "Assam", None, None, None, None),
 ("Bhagat Singh Marg Hatora", ["Hatora C-Zone"], "Durgapur", "Paschim Bardhaman",
  "West Bengal", None, None, None, None),
]


def main() -> None:
    with connect() as conn, conn.cursor() as cur:
        for (name, variants, city, district, state, lat, lon, auth, status) in SITES:
            geom = f"POINT({lon} {lat})" if lat is not None else None
            cur.execute("""
              insert into watch_site (name, name_variants, city, district, state, geom,
                                      authority, authority_status)
              values (%s, %s, %s, %s, %s,
                      case when %s::text is not null then ST_GeogFromText(%s) end,
                      %s, %s)
              on conflict do nothing""",
              (name, variants, city, district, state, geom, geom, auth, status))
        conn.commit()
        n, g = cur.execute("""select count(*), count(geom) from watch_site""").fetchone()
        print(f"watch_site: {n} sites ({g} with coordinates)")


if __name__ == "__main__":
    main()
