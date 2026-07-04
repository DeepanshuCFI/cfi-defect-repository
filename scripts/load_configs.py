#!/usr/bin/env python3
"""Upsert config/ files into the config tables. Idempotent — run after any config edit.

Usage:
  python -m scripts.load_configs            # load into DB
  python -m scripts.load_configs --check    # parse + validate files only (no DB needed)
"""
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.settings import CONFIG_DIR  # noqa: E402

VALID_LANGS = {"en", "hi", "mr", "ta", "te", "bn", "kn", "gu", "ml", "pa", "or", "as", "ur"}
VALID_TIERS = {"national", "regional", "district", "aggregator"}


def read_districts() -> list[dict]:
    rows = list(csv.DictReader(open(CONFIG_DIR / "config_districts_master.csv")))
    assert rows, "districts CSV empty"
    for r in rows:
        assert r["district"] and r["state"], f"blank district/state row: {r}"
        assert r["primary_language"] in VALID_LANGS, f"bad language: {r}"
        assert r["starter_query_en"], f"missing starter query: {r}"
    return rows


def read_keywords() -> list[tuple]:
    pack = json.load(open(CONFIG_DIR / "config_keyword_pack.json"))
    out = []
    for lang, obj in pack["languages"].items():
        assert lang in VALID_LANGS, f"unknown language in keyword pack: {lang}"
        for cat in ("crash", "fatality", "injury", "crash_type", "infra_defect"):
            for term in obj.get(cat, []):
                assert term.strip(), f"blank term in {lang}.{cat}"
                out.append((lang, cat, term.strip()))
    assert out, "keyword pack produced no terms"
    return out


def read_outlets() -> list[dict]:
    rows = list(csv.DictReader(open(CONFIG_DIR / "config_regional_outlets.csv")))
    for r in rows:
        assert r["name"], f"outlet missing name: {r}"
        assert r["tier"] in VALID_TIERS, f"bad tier: {r}"
    return rows


def read_taxonomy() -> list[dict]:
    tax = json.load(open(CONFIG_DIR / "config_defect_taxonomy.json"))["taxonomy"]
    codes = [t["code"] for t in tax]
    assert len(codes) == len(set(codes)), "duplicate taxonomy codes"
    assert "no_infrastructure_defect_identified" in codes
    assert "other_infrastructure" in codes
    for t in tax:
        assert 1 <= t["severity_weight"] <= 5, f"severity out of range: {t}"
    return tax


def read_settings() -> dict:
    st = json.load(open(CONFIG_DIR / "config_settings.json"))["settings"]
    w = st["priority_weights"]
    total = sum(w.values())
    assert abs(total - 1.0) < 1e-6, f"priority weights must sum to 1.0 (got {total})"
    return st


def main() -> None:
    districts, keywords = read_districts(), read_keywords()
    outlets, taxonomy, settings = read_outlets(), read_taxonomy(), read_settings()
    print(f"parsed: {len(districts)} districts · {len(keywords)} keyword terms · "
          f"{len(outlets)} outlets · {len(taxonomy)} taxonomy codes · {len(settings)} settings")

    if "--check" in sys.argv:
        print("--check: files valid; DB not touched.")
        return

    from pipeline.db import connect
    with connect() as conn, conn.cursor() as cur:
        for r in districts:
            cur.execute(
                """insert into config_district (district, state, primary_language, query_name, starter_query_en)
                   values (%s,%s,%s,%s,%s)
                   on conflict (district, state) do update set
                     primary_language=excluded.primary_language,
                     query_name=excluded.query_name,
                     starter_query_en=excluded.starter_query_en""",
                (r["district"], r["state"], r["primary_language"], r["query_name"], r["starter_query_en"]),
            )
        for lang, cat, term in keywords:
            cur.execute(
                """insert into config_keyword (language, category, term) values (%s,%s,%s)
                   on conflict (language, category, term) do nothing""",
                (lang, cat, term),
            )
        for r in outlets:
            cur.execute(
                """insert into config_outlet (name, language, region_state, tier, website, domain_verify, coverage_notes)
                   values (%s,%s,%s,%s,%s,%s,%s)
                   on conflict (name) do update set
                     language=excluded.language, region_state=excluded.region_state,
                     tier=excluded.tier, website=excluded.website,
                     coverage_notes=excluded.coverage_notes""",
                (r["name"], r["language"], r["region_state"], r["tier"],
                 r["website"], r.get("domain_verify", "pending"), r.get("coverage_notes")),
            )
        for t in taxonomy:
            cur.execute(
                """insert into config_defect_taxonomy (code, label, severity_weight, requires_review, maps_to_defects)
                   values (%s,%s,%s,%s,%s)
                   on conflict (code) do update set
                     label=excluded.label, severity_weight=excluded.severity_weight,
                     requires_review=excluded.requires_review, maps_to_defects=excluded.maps_to_defects""",
                (t["code"], t["label"], t["severity_weight"],
                 t.get("requires_review", False), t.get("maps_to_defects", True)),
            )
        for key, value in settings.items():
            cur.execute(
                """insert into config_setting (key, value) values (%s, %s::jsonb)
                   on conflict (key) do update set value=excluded.value, updated_at=now()""",
                (key, json.dumps(value)),
            )
        conn.commit()

        for table in ("config_district", "config_keyword", "config_outlet",
                      "config_defect_taxonomy", "config_setting"):
            cur.execute(f"select count(*) from {table}")  # noqa: S608 — fixed identifiers
            print(f"  {table}: {cur.fetchone()[0]} rows")
    print("loaded.")


if __name__ == "__main__":
    main()
