"""Contract tests for the Google News resolver's decoder dependency.

Two unpinned upgrades silently killed CI's URL resolution (runs 94-122, 21 Sep-6 Oct
2026), while the pipeline kept reporting the loss as a Google throttle:

  * googlenewsdecoder 0.2.1 (PyPI 20 Sep 2026) renamed its result key
    `status` -> `success`. resolve_url() read only `status`, so every SUCCESSFUL
    decode was counted as a failure; 25 in a row tripped the circuit breaker and
    the rest of each run's ~5,000 feed items were dropped as `skipped_throttled`.
  * selectolax 1.0.0 (PyPI 3 Oct 2026) turned `selectolax.parser` into an
    ImportError. googlenewsdecoder imports exactly that, so the decoder could not
    even load.

These tests pin (a) both result shapes the resolver must accept, (b) that a dead
decoder is a counted failure rather than a crash, and (c) the requirements.txt
bounds that keep the two libraries off their breaking releases. No network: the
decoder is replaced with a fake module in sys.modules, exactly where resolve_url's
in-function `from googlenewsdecoder import gnewsdecoder` finds it.

Run: python3 -m pytest tests/test_resolver.py -q
"""
import sys
import types
from pathlib import Path

import pytest
from packaging.requirements import Requirement

from pipeline.collectors import rss

GURL = "https://news.google.com/rss/articles/CBMi_shape_test_{n}?oc=5"
PUB = "https://www.bhaskar.com/local/bihar/patna/news/some-crash-{n}.html"


@pytest.fixture
def clean_resolver(monkeypatch, tmp_path):
    """Fresh breaker + counters, no real disk cache, no base64 shortcut — so every
    call reaches the decoder branch under test."""
    monkeypatch.setattr(rss, "CACHE_PATH", tmp_path / "gnews_url_cache.json")
    monkeypatch.setattr(rss, "_disk", None)
    monkeypatch.setattr(rss, "_decode_gnews_id", lambda url: None)
    rss.reset_run_state()
    rss.STATS.pop("last_error", None)
    yield
    rss.reset_run_state()
    rss.STATS.pop("last_error", None)


def _install_decoder(monkeypatch, result: dict) -> list[str]:
    """Stand in for googlenewsdecoder; returns the list of URLs it was asked for."""
    calls: list[str] = []

    def fake_gnewsdecoder(url, interval=1):
        calls.append(url)
        return dict(result)

    mod = types.ModuleType("googlenewsdecoder")
    mod.gnewsdecoder = fake_gnewsdecoder
    monkeypatch.setitem(sys.modules, "googlenewsdecoder", mod)
    return calls


# ------------------------------------------------------------ result shapes
@pytest.mark.parametrize("shape", [
    pytest.param({"status": True, "decoded_url": PUB.format(n=1)}, id="0.1.x-status"),
    pytest.param({"success": True, "decoded_url": PUB.format(n=1)}, id="0.2.x-success"),
])
def test_resolve_url_accepts_both_decoder_result_shapes(clean_resolver, monkeypatch, shape):
    calls = _install_decoder(monkeypatch, shape)
    g = GURL.format(n=1)
    assert rss.resolve_url(g) == (shape["decoded_url"], True)
    assert calls == [g]
    assert rss.STATS["ok"] == 1 and rss.STATS["failed"] == 0
    assert not rss.throttled()


@pytest.mark.parametrize("shape", [
    pytest.param({"status": False, "message": "refused"}, id="0.1.x-failure"),
    pytest.param({"success": False, "message": "refused"}, id="0.2.x-failure"),
    pytest.param({"success": True}, id="success-without-url"),
])
def test_resolve_url_counts_a_refused_decode_as_a_failure(clean_resolver, monkeypatch, shape):
    _install_decoder(monkeypatch, shape)
    g = GURL.format(n=2)
    assert rss.resolve_url(g) == (g, False)
    assert rss.STATS["failed"] == 1 and rss.STATS["ok"] == 0


def test_new_shape_successes_keep_the_breaker_closed(clean_resolver, monkeypatch):
    """The production failure mode: 25 misread successes tripped the breaker and the
    remaining ~5,000 items of the run were skipped. A run of successes in the 0.2.x
    shape must never trip it."""
    _install_decoder(monkeypatch, {"success": True, "decoded_url": PUB.format(n=3)})
    n_calls = rss.FAILURE_STREAK_TRIP + 5
    for n in range(n_calls):
        rss.resolve_url(GURL.format(n=100 + n))
    assert not rss.throttled()
    assert rss.STATS["ok"] == n_calls
    assert rss.STATS["skipped_throttled"] == 0


def test_a_dead_decoder_is_a_counted_failure_not_a_crash(clean_resolver, monkeypatch):
    """3 Oct 2026 shape: selectolax 1.0 made importing googlenewsdecoder raise. The
    run must keep collecting (unresolved rows are retried later), and the error text
    must surface in STATS so the log says WHY resolution died."""
    class Broken(types.ModuleType):
        def __getattr__(self, name):
            raise ImportError("Modest backend is deprecated since selectolax 1.0.")

    monkeypatch.setitem(sys.modules, "googlenewsdecoder", Broken("googlenewsdecoder"))
    g = GURL.format(n=4)
    assert rss.resolve_url(g) == (g, False)
    assert rss.STATS["failed"] == 1
    assert "selectolax 1.0" in rss.STATS["last_error"]


# ------------------------------------------------------------ dependency bounds
REQUIREMENTS = Path(__file__).resolve().parents[1] / "requirements.txt"


def _requirement(name: str) -> Requirement:
    for line in REQUIREMENTS.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line and Requirement(line).name.lower() == name:
            return Requirement(line)
    raise AssertionError(f"{name} is not listed in requirements.txt")


def test_selectolax_is_held_below_the_import_breaking_major():
    r = _requirement("selectolax")
    assert not r.specifier.contains("1.0.0"), \
        "selectolax 1.0 makes googlenewsdecoder unimportable (3 Oct 2026)"
    assert r.specifier.contains("0.4.13")      # newest version the resolver was proven on


def test_googlenewsdecoder_has_an_upper_bound():
    r = _requirement("googlenewsdecoder")
    assert r.specifier.contains("0.2.1")       # the shape this branch was verified against
    assert not r.specifier.contains("0.3.0"), \
        "a new minor already renamed the result key once (0.2.1, 20 Sep 2026)"


# ------------------------------------------------------------ decode wall-clock bound
def test_a_stalled_decoder_connection_cannot_hang_resolve_url(clean_resolver, monkeypatch):
    """7 Oct 2026: googlenewsdecoder 0.1.7 (the Mac's version) calls `requests` with no
    timeout, so one Google connection that stopped answering froze the nightly sweep's
    process in an SSL read for 3h09m — and almost certainly the 27–30 Aug sweep for three
    days. resolve_url must bound every decode by wall-clock no matter which decoder
    version is installed. The fake decoder below does what 0.1.7 does: a plain urllib GET
    with no timeout, against a local socket that accepts and then never answers."""
    import socket
    import threading
    import urllib.request

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    accepted: list = []

    def tarpit():                       # accept, then say nothing, forever
        conn, _ = srv.accept()
        accepted.append(conn)

    threading.Thread(target=tarpit, daemon=True).start()

    def fake_gnewsdecoder(url, interval=1):
        urllib.request.urlopen(f"http://127.0.0.1:{port}/batchexecute")   # hangs without a timeout
        return {"status": True, "decoded_url": "https://never.example/"}

    mod = types.ModuleType("googlenewsdecoder")
    mod.gnewsdecoder = fake_gnewsdecoder
    monkeypatch.setitem(sys.modules, "googlenewsdecoder", mod)
    monkeypatch.setattr(rss, "DECODE_TIMEOUT_S", 1.0, raising=False)

    result: list = []
    worker = threading.Thread(target=lambda: result.append(rss.resolve_url(GURL.format(n=9))),
                              daemon=True)
    worker.start()
    worker.join(timeout=8)
    try:
        assert not worker.is_alive(), "resolve_url is still blocked on a silent connection"
        assert result == [(GURL.format(n=9), False)]
        assert rss.STATS["failed"] == 1
        assert "timed out" in rss.STATS.get("last_error", "").lower()
    finally:
        for c in accepted:
            c.close()
        srv.close()


def test_decode_timeout_is_scoped_to_the_call(clean_resolver, monkeypatch):
    """The bound must not leak: the process-wide socket default is restored after each
    decode, so DB and fetch sockets created later keep their own settings."""
    import socket
    monkeypatch.setattr(rss, "DECODE_TIMEOUT_S", 7.0, raising=False)
    seen: list = []

    def fake_gnewsdecoder(url, interval=1):
        seen.append(socket.getdefaulttimeout())
        return {"success": True, "decoded_url": PUB.format(n=5)}

    mod = types.ModuleType("googlenewsdecoder")
    mod.gnewsdecoder = fake_gnewsdecoder
    monkeypatch.setitem(sys.modules, "googlenewsdecoder", mod)
    before = socket.getdefaulttimeout()
    assert rss.resolve_url(GURL.format(n=5)) == (PUB.format(n=5), True)
    assert seen == [7.0]                       # in force during the decode …
    assert socket.getdefaulttimeout() == before   # … and gone afterwards
