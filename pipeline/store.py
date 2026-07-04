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


class DBStore:
    def __init__(self):
        from pipeline.db import connect
        self.conn = connect()

    def seen_url(self, url: str) -> bool:
        with self.conn.cursor() as cur:
            cur.execute("select 1 from source_article where url=%s", (url,))
            return cur.fetchone() is not None

    def near_duplicate(self, dedup_hash: str, hamming_max: int = 6) -> bool:
        if not dedup_hash:
            return False
        from pipeline.fetch import hamming
        with self.conn.cursor() as cur:
            cur.execute("""select dedup_hash from source_article
                           where dedup_hash is not null and dedup_hash != ''
                             and fetched_at > now() - interval '14 days'""")
            return any(hamming(dedup_hash, h) <= hamming_max for (h,) in cur.fetchall())

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

    def seen_url(self, url: str) -> bool:
        return url in self._urls

    def near_duplicate(self, dedup_hash: str, hamming_max: int = 6) -> bool:
        if not dedup_hash:
            return False
        from pipeline.fetch import hamming
        return any(r.get("dedup_hash") and hamming(dedup_hash, r["dedup_hash"]) <= hamming_max
                   for r in self._rows)

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
