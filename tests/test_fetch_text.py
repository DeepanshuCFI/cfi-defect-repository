"""The text a fetch produces must be storable in a UTF-8 Postgres column.

8 Oct 2026: a page with a lone UTF-16 surrogate in its body came through trafilatura
unchanged, and the drain's store.update_article_fetch raised
`UnicodeEncodeError: 'utf-8' codec can't encode character '\\udea8' ... surrogates not
allowed`. The row stayed 'new', oldest-first retried it first every chunk, and the drain
could not move. Every text field a fetch returns is now passed through utf8_safe().

Run: python3 -m pytest tests/test_fetch_text.py -q
"""
import types

import pytest

from pipeline import fetch


def test_utf8_safe_drops_lone_surrogates_and_keeps_everything_else():
    dirty = "सड़क पर गड्ढा \udea8 and a trailing pair \ud83d\ude00"
    with pytest.raises(UnicodeEncodeError):
        dirty.encode("utf-8")
    clean = fetch.utf8_safe(dirty)
    clean.encode("utf-8")                                   # storable
    assert clean == "सड़क पर गड्ढा  and a trailing pair "   # only the surrogates went
    assert fetch.utf8_safe("") == "" and fetch.utf8_safe(None) is None
    assert fetch.utf8_safe("ok ✓ ठीक") == "ok ✓ ठीक"         # real BMP/astral text untouched


def test_fetch_article_never_returns_unencodable_text(monkeypatch):
    body = "<html><head><title>Hadsa \udea8</title></head><body><article><p>" \
           + ("सड़क हादसे में दो की मौत \udea8 " * 40) + "</p></article></body></html>"
    monkeypatch.setattr(fetch, "robots_allowed", lambda url: True)
    monkeypatch.setattr(fetch, "_rate_limit", lambda url, delay_s: None)
    monkeypatch.setattr(fetch.httpx, "get", lambda url, **kw: types.SimpleNamespace(
        text=body, url=url, status_code=200))
    f = fetch.fetch_article("https://example.in/hadsa.html", delay_s=0)
    for field in (f.clean_text, f.title or "", f.raw_html):
        field.encode("utf-8")                               # would raise before the fix
    assert "मौत" in f.clean_text
    assert f.dedup_hash and len(f.dedup_hash) == 16
