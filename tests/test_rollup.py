"""Rollups, and the histogram trick that makes percentiles survive retention."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.test import override_settings
from django.utils import timezone

from sitepulse.models import (
    DailyPageStat,
    DailySourceStat,
    DailyStatusStat,
    DailyUniqueVisitor,
    Hit,
)
from sitepulse.query import percentile_from_buckets
from sitepulse.rollup import BucketSchemeChanged, rolled_up_through, rollup_day

from .factories import make_hit, make_session

pytestmark = pytest.mark.django_db


def yesterday():
    return timezone.localdate() - timedelta(days=1)


def at(day, hour=12, minute=0):
    return timezone.make_aware(
        timezone.datetime(day.year, day.month, day.day, hour, minute),
        timezone.get_current_timezone(),
    )


def test_pageviews_and_visitors_roll_up():
    day = yesterday()
    visitor = b"v" * 16
    make_session(["/a", "/b"], start=at(day), visitor=visitor)
    make_session(["/a"], start=at(day, 14), visitor=b"w" * 16)

    rollup_day(day)

    a = DailyPageStat.objects.get(date=day, path="/a", is_bot=False)
    assert a.views == 2
    assert a.visitors == 2
    assert a.sessions == 2
    assert DailyPageStat.objects.get(date=day, path="/b").views == 1


def test_entries_exits_and_bounces_are_per_session():
    day = yesterday()
    # One two-page session (/land -> /leave) and one single-page session (/land).
    make_session(["/land", "/leave"], start=at(day), visitor=b"1" * 16)
    make_session(["/land"], start=at(day, 15), visitor=b"2" * 16)

    rollup_day(day)

    land = DailyPageStat.objects.get(date=day, path="/land")
    leave = DailyPageStat.objects.get(date=day, path="/leave")
    assert land.entries == 2
    assert land.exits == 1          # only the bounced session ended here
    assert land.bounces == 1        # attributed to the entry page
    assert leave.entries == 0
    assert leave.exits == 1
    assert leave.bounces == 0


def test_the_source_is_taken_from_the_first_hit_of_a_session():
    day = yesterday()
    hits = make_session(["/a", "/b"], start=at(day), visitor=b"1" * 16)
    Hit.objects.filter(pk=hits[0].pk).update(referrer_host="news.example")
    Hit.objects.filter(pk=hits[1].pk).update(referrer_host="internal.example")

    rollup_day(day)

    assert DailySourceStat.objects.filter(date=day).count() == 1
    source = DailySourceStat.objects.get(date=day)
    assert source.referrer_host == "news.example"
    assert source.sessions == 1


def test_sessions_summed_across_source_rows_count_each_session_once():
    day = yesterday()
    for index, host in enumerate(["a.example", "b.example", ""]):
        hits = make_session(["/x", "/y"], start=at(day, 10 + index), visitor=bytes([index]) * 16)
        Hit.objects.filter(pk=hits[0].pk).update(referrer_host=host)

    rollup_day(day)

    assert sum(DailySourceStat.objects.values_list("sessions", flat=True)) == 3


def test_bot_traffic_survives_the_rollup_as_its_own_rows():
    day = yesterday()
    make_hit(ts=at(day), path="/a", route="/a", is_bot=True, visitor_hash=b"b" * 16)
    make_hit(ts=at(day), path="/a", route="/a", is_bot=False, visitor_hash=b"h" * 16)

    rollup_day(day)

    assert DailyPageStat.objects.get(date=day, path="/a", is_bot=True).views == 1
    assert DailyPageStat.objects.get(date=day, path="/a", is_bot=False).views == 1


def test_status_codes_roll_up_with_their_own_histogram():
    day = yesterday()
    make_hit(ts=at(day), route="/boom", status=500, duration_ms=1200)
    make_hit(ts=at(day), route="/boom", status=500, duration_ms=30)
    make_hit(ts=at(day), route="/boom", status=200, duration_ms=30)

    rollup_day(day)

    error = DailyStatusStat.objects.get(date=day, route="/boom", status=500)
    assert error.count == 2
    assert error.buckets[1] == 1     # 30ms -> the <=50ms bucket
    assert error.buckets[6] == 1     # 1200ms -> the <=2500ms bucket
    assert sum(error.buckets) == 2


def test_query_counts_survive_into_the_rollup():
    day = yesterday()
    make_hit(ts=at(day), path="/n", route="/n", query_count=340, query_ms=800)
    make_hit(ts=at(day), path="/n", route="/n", query_count=2, query_ms=4)

    rollup_day(day)

    page = DailyPageStat.objects.get(date=day, path="/n")
    assert page.total_query_count == 342
    assert page.query_sampled_views == 2


def test_unrolled_query_counts_do_not_skew_the_average():
    day = yesterday()
    make_hit(ts=at(day), path="/n", route="/n", query_count=10, query_ms=10)
    make_hit(ts=at(day), path="/n", route="/n", query_count=None, query_ms=None)

    rollup_day(day)

    page = DailyPageStat.objects.get(date=day, path="/n")
    assert page.views == 2
    assert page.query_sampled_views == 1
    assert page.total_query_count == 10


def test_rolling_up_twice_is_idempotent():
    day = yesterday()
    make_session(["/a", "/b"], start=at(day))
    rollup_day(day)
    def snapshot():
        return list(
            DailyPageStat.objects.filter(date=day)
            .order_by("path").values_list("path", "views")
        )

    first = snapshot()
    rollup_day(day)
    second = snapshot()
    assert first == second
    assert DailyPageStat.objects.filter(date=day).count() == 2


def test_one_visitor_row_per_visitor_per_day():
    day = yesterday()
    visitor = b"v" * 16
    make_session(["/a", "/b", "/c"], start=at(day), visitor=visitor)
    rollup_day(day)
    assert DailyUniqueVisitor.objects.filter(date=day).count() == 1


def test_rollup_records_how_far_it_has_got():
    day = yesterday()
    make_hit(ts=at(day))
    rollup_day(day)
    assert rolled_up_through() == day


@override_settings(SITEPULSE={"SYNCHRONOUS_WRITES": True,
                              "DURATION_BUCKETS_MS": [1, 2, 3, 4, 5, 6, 7, 8]})
def test_changing_the_bucket_boundaries_is_refused_not_silently_applied():
    from sitepulse.models import State
    from sitepulse.rollup import BUCKET_STATE_KEY

    State.objects.create(key=BUCKET_STATE_KEY, value=[25, 50, 100, 250, 500, 1000, 2500, 5000])
    with pytest.raises(BucketSchemeChanged):
        rollup_day(yesterday())
    # ...unless you say you meant it.
    rollup_day(yesterday(), force_buckets=True)


@pytest.mark.parametrize(
    "buckets,p,expected",
    [
        ([10, 0, 0, 0, 0, 0, 0, 0, 0], 0.5, 12.5),      # all inside the first bucket
        ([0, 0, 0, 0, 0, 0, 0, 0, 10], 0.95, 5000.0),   # overflow reports a lower bound
        ([5, 5, 0, 0, 0, 0, 0, 0, 0], 0.5, 25.0),       # boundary between two buckets
    ],
)
def test_percentiles_from_histogram_buckets(buckets, p, expected):
    boundaries = [25, 50, 100, 250, 500, 1000, 2500, 5000]
    assert percentile_from_buckets(buckets, boundaries, p) == pytest.approx(expected)


def test_percentile_of_nothing_is_none():
    assert percentile_from_buckets([0] * 9, [25, 50, 100, 250, 500, 1000, 2500, 5000], 0.5) is None
