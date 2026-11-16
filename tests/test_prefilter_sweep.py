"""The free prefilter reaches the whole queue, and knows the words we search for
(16 Nov 2026).

Two linked faults:

1. The prefilter ran only inside the process loop. The loop stops on its daily
   ceiling after about 80 articles, so it never reached the ones the filter rejects:
   `prefiltered: 0` on every run while 6,047 of 14,086 queued articles were free
   discards. prefilter_sweep() walks the queue before the loop loads it.

2. On 1 Nov the seasonal query switched to "स्ट्रीट लाइट", but the defect vocabulary
   only held "स्ट्रीट लाइट नहीं". The pipeline was collecting articles its own
   filter would throw away: 630 queued street-light stories failed it. Measured on
   past articles with those words: 71% became infrastructure records, 41% published.

Order matters, which is why both ship together: the sweep without the words would
have retired those 630 for good.
"""
import json

import pytest

from pipeline import configload
from pipeline.processing import prefilter
from pipeline.run import prefilter_sweep, process_canary


@pytest.fixture
def real_vocab(monkeypatch):
    """prefilter built from the repo's own config files, never a DB."""
    monkeypatch.setattr(configload, "_db_available", lambda: False)
    configload.keywords.cache_clear()
    configload.settings.cache_clear()
    prefilter._terms.cache_clear()
    yield
    configload.keywords.cache_clear()
    configload.settings.cache_clear()
    prefilter._terms.cache_clear()


class _Queue:
    """Minimal store: the two methods the sweep uses, with the 'fetched' guard."""
    def __init__(self, rows):
        self.rows = {i: {"id": i, "clean_text": t, "processing_status": "fetched"}
                     for i, t in rows}
        self.pages = 0

    def articles_page(self, status, after_id=0, limit=1000):
        self.pages += 1
        ids = sorted(i for i, r in self.rows.items()
                     if r["processing_status"] == status and i > after_id)[:limit]
        return [{"id": i, "clean_text": self.rows[i]["clean_text"]} for i in ids]

    def set_articles_status(self, ids, status, only_if=None):
        n = 0
        for i in ids:
            if only_if is None or self.rows[i]["processing_status"] == only_if:
                self.rows[i]["processing_status"] = status
                n += 1
        return n

    def status(self, i):
        return self.rows[i]["processing_status"]


def _has_road(text):
    return "road" in text


def test_sweep_retires_rejects_keeps_the_rest_and_leaves_blanks_for_the_loop():
    q = _Queue([(1, "pothole on the road"), (2, "mass wedding scheme"), (3, ""),
                (4, None), (5, "   "), (6, "cyber fraud arrests"), (7, "road caved in")])
    out = prefilter_sweep(q, _has_road)
    assert out == {"scanned": 7, "swept": 2}
    assert q.status(2) == q.status(6) == "irrelevant"
    assert q.status(1) == q.status(7) == "fetched"
    for blank in (3, 4, 5):
        assert q.status(blank) == "fetched", "blank text is the loop's to mark 'failed'"


def test_sweep_walks_past_the_first_page():
    """The bug being fixed was a window: only what the loop loaded was ever filtered."""
    q = _Queue([(i, "road" if i % 2 else "weddings") for i in range(1, 2501)])
    out = prefilter_sweep(q, _has_road, page=1000)
    assert out == {"scanned": 2500, "swept": 1250}
    assert q.pages == 4, "three pages of rows and one empty page to finish"


def test_sweep_is_bounded():
    q = _Queue([(i, "weddings") for i in range(1, 501)])
    out = prefilter_sweep(q, _has_road, page=100, max_rows=300)
    assert out["scanned"] == 300 and out["swept"] == 300


def test_sweep_does_not_overwrite_an_article_someone_else_took():
    q = _Queue([(1, "weddings"), (2, "weddings")])
    real_page = q.articles_page

    def page_then_race(status, after_id=0, limit=1000):
        rows = real_page(status, after_id, limit)
        if rows:
            q.rows[2]["processing_status"] = "extracted"   # another process got there
        return rows

    q.articles_page = page_then_race
    assert prefilter_sweep(q, _has_road)["swept"] == 1
    assert q.status(1) == "irrelevant" and q.status(2) == "extracted"


def test_the_sweep_applies_the_loops_own_filter(real_vocab):
    """Same function, so the sweep changes when an article is discarded, never
    whether. Real headlines from the 16 Nov queue."""
    street_light = ("मौजीपुर घाट की स्ट्रीट लाइट आधी जल रही: आंधी में टूटी लाइट को "
                    "जोड़ने के बाद भी मरम्मत नहीं हुई")
    breadcrumb = "- Hindi News - Local - Bihar - Bhojpur - Nagri Solar Street Light Complaint"
    wedding = "जालौन मुख्यमंत्री सामूहिक विवाह योजना में 76 जोड़ों ने थामा एक-दूजे का हाथ"
    kyc = "बैंक KYC के लिए शहर जाने का झंझट खत्म, मधुबनी की पंचायतों में विशेष कैंप"
    q = _Queue([(1, street_light), (2, breadcrumb), (3, wedding), (4, kyc)])
    assert prefilter_sweep(q, prefilter.passes) == {"scanned": 4, "swept": 2}
    assert q.status(1) == q.status(2) == "fetched"
    assert q.status(3) == q.status(4) == "irrelevant"


def test_street_light_words_are_defect_vocabulary_in_every_queried_language(real_vocab):
    pack = configload.keywords()
    assert "स्ट्रीट लाइट" in pack["hi"]["infra_defect"]
    assert "street light" in pack["en"]["infra_defect"]
    assert "पथदिवे" in pack["mr"]["infra_defect"]
    # appended, not inserted: build_queries() falls back to infra_defect[:2]
    assert pack["hi"]["infra_defect"][:2] == ["गड्ढा", "गड्ढे"]


def test_every_term_we_query_for_passes_our_own_prefilter(real_vocab):
    """The contract the 1 Nov switch broke: a word we search the news for must be a
    word our filter accepts. Covers the seasonal slot (both seasons) and the fixed
    defect query terms, in every language that has them."""
    settings = json.load(open(configload.CONFIG_DIR / "config_settings.json"))["settings"]
    seasonal = settings["ingestion"]["seasonal_query_term"]
    queried = [(lang, term) for season in ("monsoon", "dry")
               for lang, term in seasonal[season].items()]
    queried += [(lang, t) for lang, block in configload.keywords().items()
                for t in block.get("query_defect", [])]
    assert len(queried) >= 8
    rejected = [(lang, t) for lang, t in queried if not prefilter.passes(f"… {t} …")]
    assert not rejected, f"we query for {rejected} and then discard what comes back"


def test_a_sweep_does_not_make_an_idle_day_look_busy():
    """The did-work canary counts the in-loop `prefiltered` as work; on 25 and 30 Oct
    that is what kept it quiet while every model call was refused. The sweep's count
    lives under its own key so it cannot do the same."""
    idle = {"extracted": 0, "extracted_light": 0, "irrelevant": 0, "prefiltered": 0,
            "failed": 754, "loaded": 1000, "stopped_reason": None,
            "prefilter_swept": 400, "prefilter_scanned": 14000}
    assert process_canary(idle, budget_stopped=False), "400 swept must not count as work"


def test_cmd_process_records_the_sweep(monkeypatch, tmp_path):
    from tests.test_core_logic import _run_cmd_process_on_empty_queue
    out = _run_cmd_process_on_empty_queue(monkeypatch, tmp_path)
    assert out["prefilter_swept"] == 0 and out["prefilter_scanned"] == 0
