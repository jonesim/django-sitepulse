"""The Report API, and especially the rollup/live boundary."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from sitepulse.query import Report, split_range
from sitepulse.rollup import rollup_day

from .factories import make_hit, make_session

pytestmark = pytest.mark.django_db


def at(day, hour=12, minute=0):
    return timezone.make_aware(
        timezone.datetime(day.year, day.month, day.day, hour, minute),
        timezone.get_current_timezone(),
    )


@pytest.fixture
def three_days():
    """Two rolled-up days plus today, still live."""
    today = timezone.localdate()
    days = [today - timedelta(days=2), today - timedelta(days=1), today]
    for index, day in enumerate(days):
        make_session(["/a", "/b"], start=at(day, 9), visitor=bytes([index]) * 16)
        make_session(["/a"], start=at(day, 15), visitor=bytes([index + 100]) * 16)
    for day in days[:2]:
        rollup_day(day)
    return days


def test_the_range_splits_at_the_rollup_boundary(three_days):
    start, end = three_days[0], three_days[2]
    rollup_range, live_days = split_range(start, end)
    assert rollup_range == (start, three_days[1])
    assert live_days == [three_days[2]]


def test_a_range_that_straddles_the_boundary_counts_every_day_once(three_days):
    start, end = three_days[0], three_days[2]
    rows = Report.pageviews(start, end, group_by="path")
    by_path = {row["path"]: row["views"] for row in rows}
    assert by_path == {"/a": 6, "/b": 3}   # 2 + 1 views of /a per day, over three days


def test_live_and_rolled_up_numbers_for_the_same_day_agree(three_days):
    """The single most important property: rolling a day up must not move its numbers."""
    today = three_days[2]
    live = Report.summary(today, today)
    rollup_day(today)
    stored = Report.summary(today, today)
    assert live == stored


def test_summary_counts_each_session_once_not_once_per_page(three_days):
    day = three_days[0]
    summary = Report.summary(day, day)
    assert summary["views"] == 3
    assert summary["sessions"] == 2
    assert summary["visitors"] == 2


def test_bounce_rate_uses_sessions_not_pageviews(three_days):
    day = three_days[0]
    summary = Report.summary(day, day)
    assert summary["bounces"] == 1          # the single-page session
    assert summary["bounce_rate"] == pytest.approx(0.5)


def test_visitors_over_a_range_are_not_the_naive_row_sum(three_days):
    start, end = three_days[0], three_days[2]
    result = Report.visitors(start, end)
    # Six visitor-days: two distinct visitors on each of three days.
    assert result["total"] == 6
    assert [row["visitors"] for row in result["series"]] == [2, 2, 2]


def test_pageviews_omits_visitors_over_multi_day_ranges(three_days):
    start, end = three_days[0], three_days[2]
    multi = Report.pageviews(start, end, group_by="path")
    single = Report.pageviews(end, end, group_by="path")
    assert "visitors" not in multi[0]
    assert "visitors" in single[0]


def test_pageviews_flags_when_sessions_are_only_an_upper_bound(three_days):
    day = three_days[0]
    assert Report.pageviews(day, day, group_by="route")[0]["sessions_exact"] is False
    assert Report.pageviews(day, day, group_by="path")[0]["sessions_exact"] is True


def test_bots_are_excluded_by_default_and_available_on_request():
    day = timezone.localdate()
    make_hit(path="/a", route="/a", is_bot=True, visitor_hash=b"b" * 16)
    make_hit(path="/a", route="/a", is_bot=False, visitor_hash=b"h" * 16)
    assert Report.summary(day, day)["views"] == 1
    assert Report.summary(day, day, include_bots=True)["views"] == 2


def test_sources_attributes_sessions_to_where_they_arrived_from():
    day = timezone.localdate()
    hits = make_session(["/a", "/b"], visitor=b"1" * 16)
    type(hits[0]).objects.filter(pk=hits[0].pk).update(referrer_host="news.example")
    rows = Report.sources(day, day)
    assert rows[0]["referrer_host"] == "news.example"
    assert rows[0]["sessions"] == 1


def test_performance_reports_percentiles_and_query_counts():
    day = timezone.localdate()
    for duration in [10, 20, 30, 40, 3000]:
        make_hit(route="/slow", path="/slow", duration_ms=duration, query_count=100, query_ms=50)
    rows = Report.performance(day, day)
    slow = next(row for row in rows if row["route"] == "/slow")
    assert slow["views"] == 5
    assert slow["p50"] is not None
    assert slow["p95"] >= 2500          # the 3s outlier lands in the top buckets
    assert slow["avg_query_count"] == 100
    assert slow["exact"] is False       # histogram-derived, and says so


def test_errors_are_grouped_by_route():
    day = timezone.localdate()
    make_hit(route="/boom", path="/boom", status=500)
    make_hit(route="/boom", path="/boom", status=500)
    make_hit(route="/gone", path="/gone", status=404)
    make_hit(route="/fine", path="/fine", status=200)
    rows = Report.errors(day, day)
    assert [(row["route"], row["count"]) for row in rows] == [("/boom", 2), ("/gone", 1)]


def test_error_rate_series_covers_every_day_in_the_range():
    today = timezone.localdate()
    make_hit(status=500)
    rows = Report.error_rate(today - timedelta(days=2), today)
    assert len(rows) == 3
    assert rows[-1]["server_errors"] == 1
    assert rows[0]["error_rate"] == 0


def test_audience_resolves_lookup_names():
    day = timezone.localdate()
    make_hit()
    rows = Report.audience(day, day, dimension="browser")
    assert rows[0]["label"] == "Firefox"
    rows = Report.audience(day, day, dimension="device")
    assert rows[0]["label"] == "desktop"


def test_reports_are_empty_not_broken_when_there_is_no_data():
    day = timezone.localdate()
    assert Report.pageviews(day, day) == []
    assert Report.summary(day, day)["views"] == 0
    assert Report.summary(day, day)["bounce_rate"] is None
    assert Report.visitors(day, day)["total"] == 0


def test_health_reports_the_rollup_lag(three_days):
    health = Report.health()
    assert health["rolled_up_through"] == three_days[1]
    assert health["rollup_lag_days"] == 0
