"""The read API.

This is the only public read surface, which is what makes the upgrade paths in
the design possible: move raw hits somewhere else later and every caller here
keeps working.

The fiddly part, and the part the tests are densest around, is that a date range
can straddle the boundary between rolled-up history and today. The rule:

* days on or before ``rolled_up_through()`` are read from the rollup tables with
  ordinary SQL aggregation;
* later days are aggregated from raw hits **using the exact same aggregator the
  nightly rollup uses**, so today's numbers and tomorrow's rollup of today
  cannot disagree;
* the two are merged in Python by group key.

Live aggregation is cached for ``LIVE_CACHE_SECONDS`` so a dashboard refresh
doesn't re-scan the day.

Two honesty rules are enforced rather than documented and hoped for:

* distinct counts are never summed across rollup rows. Range-wide unique
  visitors come from ``DailyUniqueVisitor``; per-row ``visitors`` is only
  returned where it is exact.
* percentiles come from summable histogram buckets, so they are approximate to
  within a bucket width. Where a range is entirely live, exact values are
  available from the raw rows and the result says so.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable  # noqa: F401  (used in annotations)
from datetime import date as date_cls
from datetime import timedelta

from django.core.cache import caches
from django.db.models import Count, F, Sum

from .conf import BUCKET_COUNT, sitepulse_settings
from .models import (
    Browser,
    DailyGeoDeviceStat,
    DailyPageStat,
    DailySourceStat,
    DailyStatusStat,
    DailyUniqueVisitor,
    Device,
    Hit,
    OperatingSystem,
)
from .rollup import DayRollup, day_bounds, rolled_up_through

logger = logging.getLogger("sitepulse")

BUCKET_FIELDS = [f"b{i}" for i in range(BUCKET_COUNT)]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _days(start: date_cls, end: date_cls) -> list[date_cls]:
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def split_range(start: date_cls, end: date_cls) -> tuple[tuple[date_cls, date_cls] | None,
                                                         list[date_cls]]:
    """``((rollup_start, rollup_end) | None, [live_day, ...])``."""
    boundary = rolled_up_through()
    if boundary is None:
        return None, _days(start, end)
    if boundary >= end:
        return (start, end), []
    if boundary < start:
        return None, _days(start, end)
    return (start, boundary), _days(boundary + timedelta(days=1), end)


def _using():
    return sitepulse_settings.DATABASE_ALIAS


def _merge(rows: Iterable[dict], key_fields: tuple[str, ...], sum_fields: tuple[str, ...],
           extra: tuple[str, ...] = ()) -> list[dict]:
    """Group dicts by ``key_fields`` and add up ``sum_fields``."""
    merged: dict[tuple, dict] = {}
    for row in rows:
        key = tuple(row[name] for name in key_fields)
        target = merged.get(key)
        if target is None:
            target = {name: row[name] for name in key_fields}
            target.update({name: 0 for name in sum_fields})
            for name in extra:
                target[name] = row.get(name)
            merged[key] = target
        for name in sum_fields:
            target[name] += row.get(name) or 0
    return list(merged.values())


# ---------------------------------------------------------------------------
# live days
# ---------------------------------------------------------------------------


def live_day(day: date_cls) -> dict[str, list[dict]]:
    """Aggregate one not-yet-rolled-up day straight from the raw hits.

    Returns the same shapes the rollup tables hold, as plain dicts.
    """
    cache = caches[sitepulse_settings.CACHE_ALIAS]
    cache_key = f"sitepulse:live:{day.isoformat()}"
    try:
        cached = cache.get(cache_key)
    except Exception:  # pragma: no cover - cache outage
        cached = None
    if cached is not None:
        return cached

    boundaries = list(sitepulse_settings.DURATION_BUCKETS_MS)
    accumulator = DayRollup(day, boundaries)
    start, end = day_bounds(day)
    from .rollup import _ROW_FIELDS

    queryset = (
        Hit.objects.using(_using())
        .filter(ts__gte=start, ts__lt=end)
        .order_by("session_id", "ts", "id")
        .values(*_ROW_FIELDS)
    )
    for row in queryset.iterator(chunk_size=5000):
        row["session_id"] = bytes(row["session_id"])
        row["visitor_hash"] = bytes(row["visitor_hash"])
        accumulator.add(row)

    built = accumulator.build()
    result = {
        "pages": [_page_dict(row) for row in built["pages"]],
        "sources": [_source_dict(row) for row in built["sources"]],
        "geo": [_geo_dict(row) for row in built["geo"]],
        "statuses": [_status_dict(row) for row in built["statuses"]],
        "visitors": [
            {"visitor_hash": row.visitor_hash, "is_bot": row.is_bot} for row in built["visitors"]
        ],
    }
    try:
        cache.set(cache_key, result, timeout=sitepulse_settings.LIVE_CACHE_SECONDS)
    except Exception:  # pragma: no cover - value too large, cache down, ...
        logger.debug("sitepulse: could not cache live aggregation for %s", day, exc_info=True)
    return result


def _buckets_dict(row) -> dict[str, int]:
    return {name: getattr(row, name) for name in BUCKET_FIELDS}


def _page_dict(row) -> dict:
    return {
        "date": row.date, "route": row.route, "path": row.path, "is_bot": row.is_bot,
        "views": row.views, "visitors": row.visitors, "sessions": row.sessions,
        "entries": row.entries, "exits": row.exits, "bounces": row.bounces,
        "total_duration_ms": row.total_duration_ms,
        "total_query_count": row.total_query_count,
        "total_query_ms": row.total_query_ms,
        "query_sampled_views": row.query_sampled_views,
        **_buckets_dict(row),
    }


def _source_dict(row) -> dict:
    return {
        "date": row.date, "referrer_host": row.referrer_host, "utm_source": row.utm_source,
        "utm_medium": row.utm_medium, "utm_campaign": row.utm_campaign, "is_bot": row.is_bot,
        "sessions": row.sessions, "visitors": row.visitors, "bounces": row.bounces,
    }


def _geo_dict(row) -> dict:
    return {
        "date": row.date, "country": row.country, "device": row.device,
        "browser_id": row.browser_id, "os_id": row.os_id, "is_bot": row.is_bot,
        "views": row.views, "visitors": row.visitors, "sessions": row.sessions,
    }


def _status_dict(row) -> dict:
    return {
        "date": row.date, "route": row.route, "status": row.status, "is_bot": row.is_bot,
        "count": row.count, **_buckets_dict(row),
    }


# ---------------------------------------------------------------------------
# percentiles
# ---------------------------------------------------------------------------


def percentile_from_buckets(buckets: list[int], boundaries: list[int], p: float) -> float | None:
    """Approximate percentile from histogram buckets.

    Linear interpolation inside the containing bucket, so the answer is within a
    bucket width. Values in the unbounded top bucket report the last boundary --
    a lower bound, flagged by :func:`percentiles` as ``overflow``.
    """
    total = sum(buckets)
    if not total:
        return None
    target = total * p
    cumulative = 0.0
    for index, count in enumerate(buckets):
        if count and cumulative + count >= target:
            if index >= len(boundaries):
                return float(boundaries[-1])
            lower = 0.0 if index == 0 else float(boundaries[index - 1])
            upper = float(boundaries[index])
            within = (target - cumulative) / count
            return lower + (upper - lower) * within
        cumulative += count
    return None  # pragma: no cover - unreachable while total > 0


def percentiles(buckets: list[int], boundaries: list[int] | None = None) -> dict:
    boundaries = list(boundaries or sitepulse_settings.DURATION_BUCKETS_MS)
    total = sum(buckets)
    return {
        "count": total,
        "p50": percentile_from_buckets(buckets, boundaries, 0.50),
        "p75": percentile_from_buckets(buckets, boundaries, 0.75),
        "p95": percentile_from_buckets(buckets, boundaries, 0.95),
        "p99": percentile_from_buckets(buckets, boundaries, 0.99),
        "overflow": buckets[-1] if buckets else 0,
        "exact": False,
    }


def exact_percentiles(start: date_cls, end: date_cls, route: str | None = None,
                      include_bots: bool = False) -> dict | None:
    """Exact percentiles straight from the raw rows, PostgreSQL only.

    Only meaningful while the range is inside ``RAW_RETENTION_DAYS``; callers
    check that first.
    """
    from django.db import connections
    from django.db.models.aggregates import Aggregate

    if connections[_using()].vendor != "postgresql":
        return None

    class _Percentile(Aggregate):
        function = "PERCENTILE_CONT"
        template = "%(function)s(%(percentile)s) WITHIN GROUP (ORDER BY %(expressions)s)"

        def __init__(self, expression, percentile, **extra):
            super().__init__(expression, percentile=percentile, **extra)

    day_start, _ = day_bounds(start)
    _, day_end = day_bounds(end)
    queryset = Hit.objects.using(_using()).filter(ts__gte=day_start, ts__lt=day_end)
    if not include_bots:
        queryset = queryset.filter(is_bot=False)
    if route is not None:
        queryset = queryset.filter(route=route)
    result = queryset.aggregate(
        count=Count("id"),
        p50=_Percentile("duration_ms", 0.5),
        p75=_Percentile("duration_ms", 0.75),
        p95=_Percentile("duration_ms", 0.95),
        p99=_Percentile("duration_ms", 0.99),
    )
    result["overflow"] = 0
    result["exact"] = True
    return result


# ---------------------------------------------------------------------------
# the API
# ---------------------------------------------------------------------------


class Report:
    """Aggregate reads over the analytics data.

    Every method takes inclusive ``start``/``end`` dates and excludes bot traffic
    unless ``include_bots=True``. Nothing here needs a request.
    """

    # -- pages ------------------------------------------------------------

    @staticmethod
    def pageviews(start: date_cls, end: date_cls, group_by: str = "route",
                  limit: int | None = 20, include_bots: bool = False) -> list[dict]:
        """Traffic per page.

        ``group_by`` is ``"route"``, ``"path"`` or ``"date"``.

        ``sessions`` is exact when grouping by ``path`` or ``date`` and an upper
        bound when grouping by ``route`` (one session touching two paths of the
        same route is counted twice). ``visitors`` is only included for a
        single-day range, because per-row distinct counts do not sum -- use
        :meth:`visitors` for range-wide uniques.
        """
        if group_by not in {"route", "path", "date"}:
            raise ValueError("group_by must be 'route', 'path' or 'date'")

        rows = Report._page_rows(start, end, include_bots)
        sum_fields = (
            "views", "sessions", "entries", "exits", "bounces", "total_duration_ms",
            "total_query_count", "total_query_ms", "query_sampled_views", *BUCKET_FIELDS,
        )
        single_day = start == end
        if single_day:
            sum_fields = sum_fields + ("visitors",)
        merged = _merge(rows, (group_by,), sum_fields)

        for row in merged:
            row["avg_duration_ms"] = (
                row["total_duration_ms"] / row["views"] if row["views"] else 0
            )
            row["avg_query_count"] = (
                row["total_query_count"] / row["query_sampled_views"]
                if row["query_sampled_views"] else None
            )
            row["bounce_rate"] = row["bounces"] / row["sessions"] if row["sessions"] else None
            row["sessions_exact"] = group_by != "route"

        if group_by == "date":
            merged.sort(key=lambda row: row["date"])
        else:
            merged.sort(key=lambda row: row["views"], reverse=True)
        return merged[:limit] if limit else merged

    @staticmethod
    def _page_rows(start, end, include_bots) -> list[dict]:
        rollup_range, live_days = split_range(start, end)
        rows: list[dict] = []
        fields = (
            "date", "route", "path", "views", "visitors", "sessions", "entries", "exits",
            "bounces", "total_duration_ms", "total_query_count", "total_query_ms",
            "query_sampled_views", *BUCKET_FIELDS,
        )
        if rollup_range:
            queryset = DailyPageStat.objects.using(_using()).filter(
                date__gte=rollup_range[0], date__lte=rollup_range[1]
            )
            if not include_bots:
                queryset = queryset.filter(is_bot=False)
            rows.extend(queryset.values(*fields))
        for day in live_days:
            rows.extend(
                row for row in live_day(day)["pages"] if include_bots or not row["is_bot"]
            )
        return rows

    # -- visitors ---------------------------------------------------------

    @staticmethod
    def visitors(start: date_cls, end: date_cls, granularity: str = "day",
                 include_bots: bool = False) -> dict:
        """Unique visitors, counted exactly.

        ``total`` counts distinct ``(date, visitor)`` pairs, so with the default
        cookieless identity it necessarily equals the sum of the daily series:
        the identifier rotates at midnight, so one person visiting on three days
        is three uniques and there is no way to know otherwise. That is the
        stated cost of not setting a cookie, and the number is exact for what it
        measures -- "visitor-days" -- rather than being a wrong answer to
        "distinct people". Label it accordingly in any UI.

        With ``RETURNING_VISITORS`` on, the identifier is stable across days and
        ``total`` becomes a genuine range-wide distinct count, below the sum of
        the daily series.
        """
        if granularity not in {"day", "total"}:
            raise ValueError("granularity must be 'day' or 'total'")

        rollup_range, live_days = split_range(start, end)
        series: dict[date_cls, int] = {}
        # Cookieless identifiers rotate daily, so a row per (date, visitor) is
        # already distinct and COUNT(*) is both exact and cheap. Only the opt-in
        # cookie makes a hash recur across days, and only then is the more
        # expensive DISTINCT needed.
        distinct_across_days = sitepulse_settings.RETURNING_VISITORS
        seen: set[bytes] = set()
        total = 0

        if rollup_range:
            queryset = DailyUniqueVisitor.objects.using(_using()).filter(
                date__gte=rollup_range[0], date__lte=rollup_range[1]
            )
            if not include_bots:
                queryset = queryset.filter(is_bot=False)
            for row in queryset.values("date").annotate(n=Count("id")).order_by("date"):
                series[row["date"]] = row["n"]
                total += row["n"]
            if distinct_across_days:
                total = queryset.values("visitor_hash").distinct().count()
                seen.update(
                    bytes(value) for value in queryset.values_list("visitor_hash", flat=True)
                )

        for day in live_days:
            rows = [
                row for row in live_day(day)["visitors"] if include_bots or not row["is_bot"]
            ]
            series[day] = len(rows)
            if distinct_across_days:
                fresh = {bytes(row["visitor_hash"]) for row in rows} - seen
                seen.update(fresh)
                total += len(fresh)
            else:
                total += len(rows)

        if granularity == "total":
            return {"total": total}
        return {
            "total": total,
            "series": [{"date": day, "visitors": series.get(day, 0)} for day in _days(start, end)],
        }

    # -- summary ----------------------------------------------------------

    @staticmethod
    def summary(start: date_cls, end: date_cls, include_bots: bool = False) -> dict:
        """The headline numbers, all of them exact.

        Sessions and bounces come off the *source* rollup rather than the page
        rollup: a session has exactly one source, so summing across source rows
        counts each session once, while summing across page rows would count a
        session once per page it touched.
        """
        pages = Report._page_rows(start, end, include_bots)
        sources = Report._source_rows(start, end, include_bots)

        views = sum(row["views"] for row in pages)
        duration = sum(row["total_duration_ms"] for row in pages)
        query_count = sum(row["total_query_count"] for row in pages)
        query_sampled = sum(row["query_sampled_views"] for row in pages)
        sessions = sum(row["sessions"] for row in sources)
        bounces = sum(row["bounces"] for row in sources)
        buckets = [sum(row[name] for row in pages) for name in BUCKET_FIELDS]

        return {
            "views": views,
            "visitors": Report.visitors(start, end, "total", include_bots)["total"],
            "sessions": sessions,
            "bounces": bounces,
            "bounce_rate": bounces / sessions if sessions else None,
            "views_per_session": views / sessions if sessions else None,
            "avg_duration_ms": duration / views if views else None,
            "avg_query_count": query_count / query_sampled if query_sampled else None,
            **{k: v for k, v in percentiles(buckets).items() if k != "count"},
        }

    @staticmethod
    def timeseries(start: date_cls, end: date_cls, include_bots: bool = False) -> list[dict]:
        """Per-day views, sessions and unique visitors -- the overview chart."""
        pages = _merge(
            Report._page_rows(start, end, include_bots), ("date",), ("views",)
        )
        sources = _merge(
            Report._source_rows(start, end, include_bots), ("date",), ("sessions",)
        )
        views = {row["date"]: row["views"] for row in pages}
        sessions = {row["date"]: row["sessions"] for row in sources}
        visitors = {
            row["date"]: row["visitors"]
            for row in Report.visitors(start, end, "day", include_bots)["series"]
        }
        return [
            {
                "date": day,
                "views": views.get(day, 0),
                "sessions": sessions.get(day, 0),
                "visitors": visitors.get(day, 0),
            }
            for day in _days(start, end)
        ]

    # -- sources ----------------------------------------------------------

    @staticmethod
    def _source_rows(start, end, include_bots) -> list[dict]:
        rollup_range, live_days = split_range(start, end)
        rows: list[dict] = []
        fields = (
            "date", "referrer_host", "utm_source", "utm_medium", "utm_campaign",
            "sessions", "visitors", "bounces",
        )
        if rollup_range:
            queryset = DailySourceStat.objects.using(_using()).filter(
                date__gte=rollup_range[0], date__lte=rollup_range[1]
            )
            if not include_bots:
                queryset = queryset.filter(is_bot=False)
            rows.extend(queryset.values(*fields))
        for day in live_days:
            rows.extend(
                row for row in live_day(day)["sources"] if include_bots or not row["is_bot"]
            )
        return rows

    @staticmethod
    def sources(start: date_cls, end: date_cls, group_by: str = "referrer_host",
                limit: int | None = 20, include_bots: bool = False) -> list[dict]:
        """Where sessions came from.

        ``group_by`` is one of ``referrer_host``, ``utm_source``, ``utm_medium``,
        ``utm_campaign``. A blank key means direct/none.
        """
        valid = {"referrer_host", "utm_source", "utm_medium", "utm_campaign"}
        if group_by not in valid:
            raise ValueError(f"group_by must be one of {sorted(valid)}")
        merged = _merge(
            Report._source_rows(start, end, include_bots), (group_by,), ("sessions", "bounces")
        )
        for row in merged:
            row["bounce_rate"] = row["bounces"] / row["sessions"] if row["sessions"] else None
        merged.sort(key=lambda row: row["sessions"], reverse=True)
        return merged[:limit] if limit else merged

    # -- audience ---------------------------------------------------------

    @staticmethod
    def _geo_rows(start, end, include_bots) -> list[dict]:
        rollup_range, live_days = split_range(start, end)
        rows: list[dict] = []
        fields = ("date", "country", "device", "browser_id", "os_id", "views", "sessions")
        if rollup_range:
            queryset = DailyGeoDeviceStat.objects.using(_using()).filter(
                date__gte=rollup_range[0], date__lte=rollup_range[1]
            )
            if not include_bots:
                queryset = queryset.filter(is_bot=False)
            rows.extend(queryset.values(*fields))
        for day in live_days:
            rows.extend(
                row for row in live_day(day)["geo"] if include_bots or not row["is_bot"]
            )
        return rows

    @staticmethod
    def audience(start: date_cls, end: date_cls, dimension: str = "country",
                 limit: int | None = 20, include_bots: bool = False) -> list[dict]:
        """Views by ``country``, ``device``, ``browser`` or ``os``."""
        column = {
            "country": "country", "device": "device",
            "browser": "browser_id", "os": "os_id",
        }.get(dimension)
        if column is None:
            raise ValueError("dimension must be 'country', 'device', 'browser' or 'os'")

        merged = _merge(Report._geo_rows(start, end, include_bots), (column,), ("views",))
        for row in merged:
            row["key"] = row.pop(column)
        if dimension == "device":
            labels = dict(Device.choices)
            for row in merged:
                row["label"] = labels.get(row["key"], "other")
        elif dimension in {"browser", "os"}:
            model = Browser if dimension == "browser" else OperatingSystem
            names = dict(model.objects.using(_using()).values_list("id", "name"))
            for row in merged:
                row["label"] = names.get(row["key"], "unknown")
        else:
            for row in merged:
                row["label"] = row["key"] or "unknown"
        merged.sort(key=lambda row: row["views"], reverse=True)
        return merged[:limit] if limit else merged

    # -- performance ------------------------------------------------------

    @staticmethod
    def performance(start: date_cls, end: date_cls, route: str | None = None,
                    limit: int | None = 20, include_bots: bool = False) -> list[dict]:
        """Response time per route, as percentiles plus the raw histogram.

        Percentiles are approximate to within a bucket width, because that is
        what survives retention. Pass a single ``route`` to get one row.
        """
        rows = Report._page_rows(start, end, include_bots)
        if route is not None:
            rows = [row for row in rows if row["route"] == route]
        merged = _merge(
            rows,
            ("route",),
            ("views", "total_duration_ms", "total_query_count", "total_query_ms",
             "query_sampled_views", *BUCKET_FIELDS),
        )
        boundaries = list(sitepulse_settings.DURATION_BUCKETS_MS)
        for row in merged:
            buckets = [row[name] for name in BUCKET_FIELDS]
            row["buckets"] = buckets
            row.update(percentiles(buckets, boundaries))
            row["avg_duration_ms"] = (
                row["total_duration_ms"] / row["views"] if row["views"] else 0
            )
            row["avg_query_count"] = (
                row["total_query_count"] / row["query_sampled_views"]
                if row["query_sampled_views"] else None
            )
            row["avg_query_ms"] = (
                row["total_query_ms"] / row["query_sampled_views"]
                if row["query_sampled_views"] else None
            )
        merged.sort(key=lambda row: (row["p95"] or 0) * row["views"], reverse=True)
        return merged[:limit] if limit else merged

    @staticmethod
    def performance_trend(start: date_cls, end: date_cls, route: str | None = None,
                          include_bots: bool = False) -> list[dict]:
        """Per-day p50/p95 -- the "is this endpoint getting slower" chart.

        Works over any range, including one that reaches past raw retention,
        because it adds up histogram buckets rather than needing the rows.
        """
        rows = Report._page_rows(start, end, include_bots)
        if route is not None:
            rows = [row for row in rows if row["route"] == route]
        merged = {
            row["date"]: row
            for row in _merge(rows, ("date",), ("views", "total_duration_ms", *BUCKET_FIELDS))
        }
        boundaries = list(sitepulse_settings.DURATION_BUCKETS_MS)
        result = []
        for day in _days(start, end):
            row = merged.get(day)
            buckets = [row[name] for name in BUCKET_FIELDS] if row else [0] * BUCKET_COUNT
            stats = percentiles(buckets, boundaries)
            result.append(
                {
                    "date": day,
                    "views": row["views"] if row else 0,
                    "p50": stats["p50"] or 0,
                    "p95": stats["p95"] or 0,
                }
            )
        return result

    # -- errors -----------------------------------------------------------

    @staticmethod
    def _status_rows(start, end, include_bots) -> list[dict]:
        rollup_range, live_days = split_range(start, end)
        rows: list[dict] = []
        fields = ("date", "route", "status", "count", *BUCKET_FIELDS)
        if rollup_range:
            queryset = DailyStatusStat.objects.using(_using()).filter(
                date__gte=rollup_range[0], date__lte=rollup_range[1]
            )
            if not include_bots:
                queryset = queryset.filter(is_bot=False)
            rows.extend(queryset.values(*fields))
        for day in live_days:
            rows.extend(
                row for row in live_day(day)["statuses"] if include_bots or not row["is_bot"]
            )
        return rows

    @staticmethod
    def errors(start: date_cls, end: date_cls, min_status: int = 400,
               group_by: str = "route", limit: int | None = 20,
               include_bots: bool = False) -> list[dict]:
        """4xx/5xx by route, or by date, or by status code."""
        if group_by not in {"route", "date", "status"}:
            raise ValueError("group_by must be 'route', 'date' or 'status'")
        rows = [
            row for row in Report._status_rows(start, end, include_bots)
            if row["status"] >= min_status
        ]
        merged = _merge(rows, (group_by,), ("count",))
        if group_by == "date":
            merged.sort(key=lambda row: row["date"])
        else:
            merged.sort(key=lambda row: row["count"], reverse=True)
        return merged[:limit] if limit else merged

    @staticmethod
    def status_breakdown(start: date_cls, end: date_cls, include_bots: bool = False) -> list[dict]:
        """Every status code seen, with counts. Includes 2xx/3xx."""
        merged = _merge(Report._status_rows(start, end, include_bots), ("status",), ("count",))
        merged.sort(key=lambda row: row["status"])
        return merged

    @staticmethod
    def error_rate(start: date_cls, end: date_cls, include_bots: bool = False) -> list[dict]:
        """Per-day total, 4xx and 5xx counts."""
        rows = Report._status_rows(start, end, include_bots)
        totals: dict[date_cls, dict] = defaultdict(
            lambda: {"total": 0, "client_errors": 0, "server_errors": 0}
        )
        for row in rows:
            entry = totals[row["date"]]
            entry["total"] += row["count"]
            if 400 <= row["status"] < 500:
                entry["client_errors"] += row["count"]
            elif row["status"] >= 500:
                entry["server_errors"] += row["count"]
        result = []
        for day in _days(start, end):
            entry = totals.get(day, {"total": 0, "client_errors": 0, "server_errors": 0})
            errors = entry["client_errors"] + entry["server_errors"]
            result.append(
                {
                    "date": day,
                    **entry,
                    "error_rate": errors / entry["total"] if entry["total"] else 0,
                }
            )
        return result

    # -- diagnostics ------------------------------------------------------

    @staticmethod
    def health() -> dict:
        """Ingest health: buffer drops, write errors, and how current the rollups are."""
        from django.utils import timezone

        from .models import IngestHealth

        recent = timezone.localdate() - timedelta(days=7)
        totals = IngestHealth.objects.using(_using()).filter(date__gte=recent).aggregate(
            dropped=Sum("dropped"), write_errors=Sum("write_errors")
        )
        boundary = rolled_up_through()
        oldest = Hit.objects.using(_using()).order_by("ts").values_list("ts", flat=True).first()
        return {
            "dropped_7d": totals["dropped"] or 0,
            "write_errors_7d": totals["write_errors"] or 0,
            "rolled_up_through": boundary,
            "rollup_lag_days": (
                None if boundary is None else (timezone.localdate() - boundary).days - 1
            ),
            "oldest_raw_hit": oldest,
            "raw_hits_today": Hit.objects.using(_using())
            .filter(ts__gte=day_bounds(timezone.localdate())[0])
            .count(),
        }


def route_choices(start: date_cls, end: date_cls, limit: int = 200) -> list[str]:
    """Distinct routes seen in the range, busiest first -- for filter dropdowns."""
    rows = (
        DailyPageStat.objects.using(_using())
        .filter(date__gte=start, date__lte=end, is_bot=False)
        .values("route")
        .annotate(views=Sum(F("views")))
        .order_by("-views")[:limit]
    )
    return [row["route"] for row in rows]
