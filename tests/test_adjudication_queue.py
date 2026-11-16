"""The adjudication queue holds only records a verdict could publish (16 Nov 2026).

7-13 Nov: 676 of 1,026 adjudication calls went to records below the location bar.
decide() cannot publish those under any verdict, and 525 were archived by rule
straight afterwards. The call is now not made: pending_query() leaves them out and
park_unpublishable() archives them with an audit row. Three things have to agree on
the bar, so the tests hold them together.
"""
import pytest

from pipeline.processing import auto_review as ar

GATE = {"extraction_confidence_min": 0.7, "geocode_confidence_min": 0.6,
        "require_infra_implicated": True}


def _flat(sql: str) -> str:
    return " ".join(sql.lower().split())


def test_queue_excludes_records_below_the_location_bar():
    sql, params = ar.pending_query(GATE, limit=200)
    where = _flat(sql).split("where", 1)[1].split("order by", 1)[0]
    assert "r.verification_status = 'auto'" in where
    assert "r.geom is not null" in where
    assert "r.geocode_confidence >= %s" in where
    assert params[0] == ar.GEOCODE_FLOOR, "the WHERE must use decide()'s own bar"


@pytest.mark.parametrize("gc,publishes", [(None, False), (0.0, False), (0.55, False),
                                          (0.59, False), (0.6, True), (0.8, True)])
def test_decide_and_the_queue_agree_on_the_bar(gc, publishes):
    """If decide() could publish a record the queue excludes, real reports would be
    archived unjudged. If the queue admitted records decide() refuses, the call is
    wasted again. Both directions, at the boundary."""
    verdict = {"verdict": "confirm_publish", "confidence": 0.95}
    assert (ar.decide(verdict, gc) == "auto_published") is publishes
    in_queue = gc is not None and gc >= ar.GEOCODE_FLOOR
    assert in_queue is publishes
    # and what parking does with it matches where an unresolved verdict would go
    assert (ar.route_unresolved(gc) == "archived") is (not publishes)


def test_parking_never_touches_a_record_the_geocoder_has_not_tried():
    """The geocode stage works through 1,000 records a run. A fresh record with no
    location yet must stay 'auto' until it has had its attempt — otherwise a backlog
    day archives new reports before anyone tried to place them."""
    low = _flat(ar.PARK_UNPUBLISHABLE_SQL)
    assert "where verification_status = 'auto'" in low
    assert "geom is null and geocode_attempted_at is not null" in low
    assert "geom is not null and coalesce(geocode_confidence, 0) < %s" in low
    assert "set verification_status = 'archived'" in low, "terminal but rescuable"
    assert "returning id" in low


class _Cur:
    def __init__(self, ids):
        self.ids, self.calls = ids, []

    def execute(self, sql, params=None):
        self.calls.append((_flat(sql), params))

    def fetchall(self):
        return [(i,) for i in self.ids]


def test_parking_audit_logs_every_record_under_a_distinct_reviewer():
    cur = _Cur([11, 12, 13])
    assert ar.park_unpublishable(cur) == 3
    (update_sql, update_params), (insert_sql, insert_params) = cur.calls
    assert update_params == (ar.GEOCODE_FLOOR,)
    assert "insert into review_action" in insert_sql and "'pipeline:rule'" in insert_sql
    assert insert_params[0] == [11, 12, 13]
    assert "rescue pass" in insert_params[1], "the note must say how it comes back"


def test_parking_nothing_writes_no_audit_rows():
    cur = _Cur([])
    assert ar.park_unpublishable(cur) == 0
    assert len(cur.calls) == 1


def test_rescue_pass_still_reaches_parked_records():
    """Parking is only safe because the rescue pass re-geocodes archived records and
    returns them to 'auto' when they clear the bar. Hold the two together."""
    from pipeline.run import geocode_rescue_query
    low = _flat(geocode_rescue_query(10)[0])
    assert "'archived'" in low and "geocode_confidence < 0.6" in low
    assert ar.GEOCODE_FLOOR == 0.6, "rescue query hard-codes 0.6 — change both together"
