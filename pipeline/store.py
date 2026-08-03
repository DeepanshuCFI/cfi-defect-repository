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

    def articles_by_status(self, status: str, limit: int = 100,
                           priority_terms: list[str] | None = None) -> list[dict]:
        """priority_terms: articles containing any of these sort FIRST, across the WHOLE
        queue. Without it the caller gets the oldest `limit` rows by id and can only
        re-order within that window — which silently starves the queue once the backlog
        outgrows it (measured 31 Jul 2026: the oldest-1000 window held 21 of the 601
        defect-vocabulary articles; the other 580 were never loaded, so the budget was
        spent on the behaviour-crash tail while real defect reports aged out of reach)."""
        cols_sql = """select id, url, outlet_name, language, state, district, clean_text,
                             published_at
                      from source_article where processing_status = %s"""
        with self.conn.cursor() as cur:
            if priority_terms:
                # strpos, not LIKE: terms are 13-language and would need % / _ escaping.
                # Case-sensitive, matching the caller's own substring test.
                cur.execute(cols_sql + """
                       order by (exists (select 1 from unnest(%s::text[]) as t(term)
                                         where strpos(clean_text, t.term) > 0)) desc, id
                       limit %s""", (status, list(priority_terms), limit))
            else:
                cur.execute(cols_sql + " order by id limit %s", (status, limit))
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def set_article_status(self, article_id, status: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute("update source_article set processing_status=%s where id=%s",
                        (status, article_id))
        self.conn.commit()

    def get_article(self, article_id) -> dict | None:
        """One article with its CURRENT status. The batch backlog script re-checks
        status right before every write — the daily run may have processed the same
        article while a batch was pending, and last-write-wins would double-extract."""
        with self.conn.cursor() as cur:
            cur.execute("""select id, url, outlet_name, language, state, district,
                                  clean_text, published_at, processing_status
                           from source_article where id = %s""", (article_id,))
            row = cur.fetchone()
            if row is None:
                return None
            return dict(zip([d.name for d in cur.description], row))

    def article_statuses(self, ids: list[int]) -> dict[int, str]:
        """Bulk status lookup — one round trip. Per-row lookups cost 168ms each
        against the Tokyo pooler; over thousands of batch results that is half an
        hour of pure RTT and a wide window for a mid-stream network stall."""
        out: dict[int, str] = {}
        ids = list(ids)
        for i in range(0, len(ids), 2000):
            with self.conn.cursor() as cur:
                cur.execute("""select id, processing_status from source_article
                               where id = any(%s)""", (ids[i:i + 2000],))
                out.update(dict(cur.fetchall()))
        return out

    def set_articles_status(self, ids: list[int], status: str,
                            only_if: str | None = None, chunk: int = 500) -> int:
        """Bulk status write, optionally guarded on the current status so a row the
        daily run already processed is left alone (the guard IS the concurrency
        check — no separate read needed). Returns rows actually changed.

        Chunked: one UPDATE over ~4,000 rows tripped the pooler's statement
        timeout (QueryCanceled, 3 Aug) — per-chunk statements stay well under it
        and each chunk commits on its own, so a failure loses one chunk, not all."""
        if not ids:
            return 0
        sql = "update source_article set processing_status=%s where id = any(%s)"
        if only_if:
            sql += " and processing_status = %s"
        total = 0
        ids = list(ids)
        for i in range(0, len(ids), chunk):
            params: list = [status, ids[i:i + chunk]]
            if only_if:
                params.append(only_if)
            with self.conn.cursor() as cur:
                cur.execute(sql, params)
                total += cur.rowcount
            self.conn.commit()
        return total

    def get_articles(self, ids: list[int], chunk: int = 300) -> list[dict]:
        """Bulk fetch with clean_text — chunked for the same statement-timeout
        reason as set_articles_status (thousands of multi-KB rows per statement)."""
        out: list[dict] = []
        ids = list(ids)
        for i in range(0, len(ids), chunk):
            with self.conn.cursor() as cur:
                cur.execute("""select id, url, outlet_name, language, state, district,
                                      clean_text, published_at, processing_status
                               from source_article where id = any(%s)""",
                            (ids[i:i + chunk],))
                cols = [d.name for d in cur.description]
                out.extend(dict(zip(cols, r)) for r in cur.fetchall())
        return out

    def unresolved_articles(self, limit: int = 200) -> list[dict]:
        """'new' rows still holding a Google News redirector — resolution was throttled
        at collection time so they were never fetched, and nothing retried them (19,831
        had accumulated by 31 Jul 2026). Newest first: Google News ids age out of the
        feed window and publisher URLs rot, so a recent miss is likeliest to recover."""
        with self.conn.cursor() as cur:
            cur.execute(
                """select id, url, state, district, language
                   from source_article
                   where processing_status = 'new' and url like %s
                   order by id desc limit %s""", ("%news.google.com%", limit))
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def update_article_fetch(self, article_id, url: str, clean_text: str,
                             dedup_hash: str, published_at, status: str) -> None:
        """Attach a late fetch to an existing row. `url` is unique-constrained, and a
        redirect can land on a URL another article already occupies — keep the old URL
        rather than lose the fetch."""
        sql = """update source_article
                    set clean_text=%s, dedup_hash=%s,
                        published_at=coalesce(%s, published_at),
                        fetched_at=now(), processing_status=%s{url_set}
                  where id=%s"""
        common = (clean_text, dedup_hash, published_at, status)
        try:
            with self.conn.cursor() as cur:
                cur.execute(sql.format(url_set=", url=%s"), (*common, url, article_id))
            self.conn.commit()
        except Exception:
            self.conn.rollback()                      # url taken — keep the old one
            with self.conn.cursor() as cur:
                cur.execute(sql.format(url_set=""), (*common, article_id))
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
        if not district and not state:
            # guard-audit 2026-07-18: with no locality the compare would silently go
            # GLOBAL (the original dedup bug). Fail toward keeping the article.
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

    def articles_by_status(self, status: str, limit: int = 100,
                           priority_terms: list[str] | None = None) -> list[dict]:
        out = []
        for r in self._rows:
            if r.get("processing_status") == status:
                rec = dict(r)
                rec.setdefault("clean_text", rec.get("clean_text"))
                out.append(rec)
            if not priority_terms and len(out) >= limit:
                break                      # unprioritised: first `limit` matches is fine
        if priority_terms:
            # same rule as the SQL path: whole queue ranked before the window is cut
            out.sort(key=lambda a: 0 if any(
                t in (a.get("clean_text") or "") for t in priority_terms) else 1)
        return out[:limit]

    def set_article_status(self, article_id, status: str) -> None:
        for r in self._rows:
            if r["id"] == article_id:
                r["processing_status"] = status
        self._flush()

    def get_article(self, article_id) -> dict | None:
        return next((dict(r) for r in self._rows if r["id"] == article_id), None)

    def article_statuses(self, ids: list[int]) -> dict[int, str]:
        want = set(ids)
        return {r["id"]: r.get("processing_status")
                for r in self._rows if r["id"] in want}

    def set_articles_status(self, ids: list[int], status: str,
                            only_if: str | None = None) -> int:
        want, n = set(ids), 0
        for r in self._rows:
            if r["id"] in want and (only_if is None
                                    or r.get("processing_status") == only_if):
                r["processing_status"] = status
                n += 1
        self._flush()
        return n

    def get_articles(self, ids: list[int]) -> list[dict]:
        want = set(ids)
        return [dict(r) for r in self._rows if r["id"] in want]

    def unresolved_articles(self, limit: int = 200) -> list[dict]:
        out = [dict(r) for r in self._rows
               if r.get("processing_status") == "new"
               and "news.google.com" in (r.get("url") or "")]
        return out[::-1][:limit]                       # newest first, as in DBStore

    def update_article_fetch(self, article_id, url: str, clean_text: str,
                             dedup_hash: str, published_at, status: str) -> None:
        taken = {r["url"] for r in self._rows if r["id"] != article_id}
        for r in self._rows:
            if r["id"] == article_id:
                if url not in taken:
                    r["url"] = url
                r.update({"clean_text": clean_text, "dedup_hash": dedup_hash,
                          "processing_status": status,
                          "published_at": published_at or r.get("published_at"),
                          "fetched_at": datetime.now(timezone.utc).isoformat()})
        self._urls = {r["url"] for r in self._rows}
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
        if not district and not state:
            return False       # no locality -> never compare globally (see DBStore)
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
