"""Storage for source_article rows: Postgres when DATABASE_URL is live, JSONL otherwise.

Identical interface either way so collectors/CLI don't branch:
  store.seen_url(url) -> bool
  store.near_duplicate(dedup_hash) -> bool
  store.insert_article(dict) -> id|None
  store.counts() -> dict
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from pipeline.settings import DATABASE_URL, ROOT

ARTICLE_FIELDS = ["url", "outlet_name", "outlet_tier", "language", "state", "district",
                  "published_at", "raw_html", "clean_text", "dedup_hash",
                  "processing_status"]


INCIDENT_FIELDS = ["crash_date", "crash_time", "location_text_raw", "location_text_best",
                   "road_name", "road_type", "admin_state", "admin_district", "admin_city",
                   "admin_ward", "fatalities", "injuries", "vehicles_involved",
                   "victim_types", "narrative_summary", "infra_implicated",
                   "extraction_confidence", "primary_source_id"]


_NULLISH = {"null", "none", "nil", "na", "n/a", ""}


def _clean_nullish(v):
    """Model outputs sometimes carry the STRING 'null' instead of JSON null — a literal
    'null' in an integer column killed daily run #9 (2026-07-10). Normalise to None."""
    if isinstance(v, str) and v.strip().lower() in _NULLISH:
        return None
    return v


def _to_int(v, default=0):
    v = _clean_nullish(v)
    if v is None:
        return default
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _to_float(v, default=0.0):
    v = _clean_nullish(v)
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def coerce_incident(inc: dict) -> dict:
    """Fill NOT NULL defaults the model may omit and sanitise nullish/stringly-typed
    values (non-required schema fields)."""
    inc = {k: _clean_nullish(v) for k, v in inc.items()}
    inc["vehicles_involved"] = inc.get("vehicles_involved") or []
    inc["victim_types"] = inc.get("victim_types") or []
    inc["fatalities"] = _to_int(inc.get("fatalities"))
    inc["injuries"] = _to_int(inc.get("injuries"))
    # non-numeric confidence -> 0.0: fails the publish gate, lands in review. Honest.
    inc["extraction_confidence"] = _to_float(inc.get("extraction_confidence"))
    inc["road_type"] = inc.get("road_type") or "unknown"
    inc["infra_implicated"] = bool(inc.get("infra_implicated"))
    return inc


class DBStore:
    def __init__(self):
        from pipeline.db import connect
        self.conn = connect()

    def articles_by_status(self, status: str, limit: int = 100) -> list[dict]:
        with self.conn.cursor() as cur:
            cur.execute(
                """select id, url, outlet_name, language, state, district, clean_text,
                          published_at
                   from source_article where processing_status = %s
                   order by id limit %s""", (status, limit))
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def set_article_status(self, article_id, status: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute("update source_article set processing_status=%s where id=%s",
                        (status, article_id))
        self.conn.commit()

    def insert_incident(self, inc: dict, defects: list[dict], article_id) -> int:
        inc = coerce_incident(inc)
        cols = ", ".join(INCIDENT_FIELDS)
        ph = ", ".join(["%s"] * len(INCIDENT_FIELDS))
        try:
            with self.conn.cursor() as cur:
                cur.execute(
                    f"insert into incident ({cols}) values ({ph}) returning id",
                    tuple(inc.get(f) for f in INCIDENT_FIELDS))
                iid = cur.fetchone()[0]
                for d in defects:
                    cur.execute(
                        """insert into incident_defect
                           (incident_id, defect_type, defect_confidence, evidence_snippet, evidence_source_id)
                           values (%s,%s,%s,%s,%s)""",
                        (iid, d["defect_type"], d["confidence"], d["evidence_snippet"], article_id))
                cur.execute(
                    """insert into incident_source (incident_id, source_article_id, match_confidence)
                       values (%s,%s,1.0) on conflict do nothing""", (iid, article_id))
        except Exception:
            # leave the shared connection usable for the caller's failure handling
            self.conn.rollback()
            raise
        self.conn.commit()
        return iid

    def set_incident_status(self, incident_id: int, status: str, note: str) -> None:
        """Rule-based status routing with an audit trail (reviewer 'pipeline:rule')."""
        with self.conn.cursor() as cur:
            cur.execute("update incident set verification_status=%s, updated_at=now() "
                        "where id=%s", (status, incident_id))
            cur.execute("""insert into review_action (entity_type, entity_id, reviewer,
                           action, note) values ('incident',%s,'pipeline:rule','edit',%s)""",
                        (incident_id, note))
        self.conn.commit()

    def seen_url(self, url: str) -> bool:
        with self.conn.cursor() as cur:
            cur.execute("select 1 from source_article where url=%s", (url,))
            return cur.fetchone() is not None

    def near_duplicate(self, dedup_hash: str, district=None, state=None,
                       hamming_max: int = 3, window_days: int = 7) -> bool:
        if not dedup_hash:
            return False
        from pipeline.fetch import is_content_duplicate
        # Scope candidates to the SAME district (fallback: same state). Without this,
        # short vernacular defect stories collapse across unrelated districts/states.
        where = ["dedup_hash is not null", "dedup_hash != ''",
                 "fetched_at > now() - make_interval(days => %s)"]
        params: list = [window_days]
        if district:
            where.append("district = %s"); params.append(district)
        elif state:
            where.append("state = %s"); params.append(state)
        with self.conn.cursor() as cur:
            cur.execute("select dedup_hash from source_article where " + " and ".join(where), params)
            return is_content_duplicate(dedup_hash, (h for (h,) in cur.fetchall()), hamming_max)

    def insert_article(self, a: dict):
        cols = ", ".join(ARTICLE_FIELDS)
        ph = ", ".join(["%s"] * len(ARTICLE_FIELDS))
        with self.conn.cursor() as cur:
            cur.execute(
                f"insert into source_article ({cols}) values ({ph}) "
                f"on conflict (url) do nothing returning id",
                tuple(a.get(f) for f in ARTICLE_FIELDS))
            row = cur.fetchone()
        self.conn.commit()
        return row[0] if row else None

    def counts(self) -> dict:
        with self.conn.cursor() as cur:
            cur.execute("""select processing_status, count(*) from source_article
                           group by 1 order by 2 desc""")
            return dict(cur.fetchall())

    def close(self):
        self.conn.close()


class JsonlStore:
    """No-DB mode: appends to data/source_article.jsonl (raw_html to data/raw_html/)."""

    def __init__(self):
        self.data_dir = ROOT / "data"
        self.raw_dir = self.data_dir / "raw_html"
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "source_article.jsonl"
        self._rows = []
        if self.path.exists():
            self._rows = [json.loads(l) for l in self.path.read_text().splitlines() if l]
        self._urls = {r["url"] for r in self._rows}

    def articles_by_status(self, status: str, limit: int = 100) -> list[dict]:
        out = []
        for r in self._rows:
            if r.get("processing_status") == status:
                rec = dict(r)
                raw = self.raw_dir / f"{r['id']}.html"
                rec.setdefault("clean_text", rec.get("clean_text"))
                out.append(rec)
            if len(out) >= limit:
                break
        return out

    def set_article_status(self, article_id, status: str) -> None:
        for r in self._rows:
            if r["id"] == article_id:
                r["processing_status"] = status
        self._flush()

    def set_incident_status(self, incident_id, status: str, note: str) -> None:
        pass  # no-DB mode keeps incidents append-only; routing is a DB concern

    def insert_incident(self, inc: dict, defects: list[dict], article_id) -> int:
        path = self.data_dir / "incident.jsonl"
        rows = [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []
        rec = {f: inc.get(f) for f in INCIDENT_FIELDS}
        rec["id"] = len(rows) + 1
        rec["defects"] = defects
        rec["source_article_ids"] = [article_id]
        with open(path, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec["id"]

    def _flush(self) -> None:
        with open(self.path, "w") as f:
            for r in self._rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def seen_url(self, url: str) -> bool:
        return url in self._urls

    def near_duplicate(self, dedup_hash: str, district=None, state=None,
                       hamming_max: int = 3, window_days: int = 7) -> bool:
        if not dedup_hash:
            return False
        from pipeline.fetch import is_content_duplicate
        scoped = self._rows
        if district:
            scoped = [r for r in scoped if r.get("district") == district]
        elif state:
            scoped = [r for r in scoped if r.get("state") == state]
        return is_content_duplicate(dedup_hash, (r.get("dedup_hash") for r in scoped), hamming_max)

    def insert_article(self, a: dict):
        if a["url"] in self._urls:
            return None
        rec = {f: a.get(f) for f in ARTICLE_FIELDS}
        rec["id"] = len(self._rows) + 1
        rec["fetched_at"] = datetime.now(timezone.utc).isoformat()
        raw = rec.pop("raw_html", None) or ""
        if raw:
            (self.raw_dir / f"{rec['id']}.html").write_text(raw)
        with open(self.path, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._rows.append(rec)
        self._urls.add(rec["url"])
        return rec["id"]

    def counts(self) -> dict:
        out: dict = {}
        for r in self._rows:
            out[r.get("processing_status", "?")] = out.get(r.get("processing_status", "?"), 0) + 1
        return out

    def close(self):
        pass


def get_store(force_jsonl: bool = False):
    if not force_jsonl and DATABASE_URL and "REPLACE_ME" not in DATABASE_URL:
        return DBStore()
    return JsonlStore()
