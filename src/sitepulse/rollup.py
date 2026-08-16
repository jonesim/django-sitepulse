"""Raw hits -> permanent daily aggregates.

Run nightly by ``manage.py sitepulse_rollup``. Rolling up a day is idempotent:
the day's rollup rows are deleted and rewritten, so re-running after a fix, or
after backfilling, is always safe.

Why a single streaming pass in Python rather than a pile of GROUP BY queries:
entries, exits and bounces are all *per-session* facts, and expressing them in
SQL means either window functions with a self-join or several passes over the
same rows. Sorted by ``(session_id, ts)``, one pass computes every metric here
while holding only the current session in memory, and stays readable. At the
volumes this package targets -- ~170k rows for a busy day -- it takes seconds.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date as date_cls
from datetime import datetime, time, timedelta

from django.db import transaction
from django.utils import timezone

from .conf import BUCKET_COUNT, sitepulse_settings
from .models import (
    DailyGeoDeviceStat,
    DailyPageStat,
    DailySourceStat,
    DailyStatusStat,
    DailyUniqueVisitor,
    Hit,
    State,
)

logger = logging.getLogger("sitepulse")

BUCKET_STATE_KEY = "duration_buckets_ms"
ROLLED_UP_THROUGH_KEY = "rolled_up_through"


class BucketSchemeChanged(RuntimeError):
    """Raised when DURATION_BUCKETS_MS no longer matches the stored rollups."""


# ---------------------------------------------------------------------------
# accumulators
# ---------------------------------------------------------------------------


def _bucket_of(duration_ms: int, boundaries: list[int]) -> int:
    for index, boundary in enumerate(boundaries):
        if duration_ms <= boundary:
            return index
    return len(boundaries)


@dataclass
class _PageAcc:
    views: int = 0
    sessions: set = field(default_factory=set)
    visitors: set = field(default_factory=set)
    entries: int = 0
    exits: int = 0
    bounces: int = 0
    total_duration_ms: int = 0
    total_query_count: int = 0
    total_query_ms: int = 0
    query_sampled_views: int = 0
    buckets: list = field(default_factory=lambda: [0] * BUCKET_COUNT)


@dataclass
class _SourceAcc:
    sessions: set = field(default_factory=set)
    visitors: set = field(default_factory=set)
    bounces: int = 0


@dataclass
class _GeoAcc:
    views: int = 0
    sessions: set = field(default_factory=set)
    visitors: set = field(default_factory=set)


@dataclass
class _StatusAcc:
    count: int = 0
    buckets: list = field(default_factory=lambda: [0] * BUCKET_COUNT)


class DayRollup:
    """Accumulates one day of hits, grouped by every rollup grain at once."""

    def __init__(self, day: date_cls, boundaries: list[int]):
        self.day = day
        self.boundaries = boundaries
        self.pages: dict[tuple, _PageAcc] = defaultdict(_PageAcc)
        self.sources: dict[tuple, _SourceAcc] = defaultdict(_SourceAcc)
        self.geo: dict[tuple, _GeoAcc] = defaultdict(_GeoAcc)
        self.statuses: dict[tuple, _StatusAcc] = defaultdict(_StatusAcc)
        self.visitors: dict[bytes, bool] = {}
        self.rows = 0
        self._session: list = []
        self._session_id: bytes | None = None

    # -- ingest -----------------------------------------------------------

    def add(self, row: dict) -> None:
        """Rows must arrive ordered by ``(session_id, ts, id)``."""
        self.rows += 1
        session_id = row["session_id"]
        if session_id != self._session_id:
            self._close_session()
            self._session_id = session_id
            self._session = []
        self._session.append(row)

        is_bot = row["is_bot"]
        self.visitors[row["visitor_hash"]] = is_bot

        page = self.pages[(row["route"], row["path"], is_bot)]
        page.views += 1
        page.sessions.add(session_id)
        page.visitors.add(row["visitor_hash"])
        page.total_duration_ms += row["duration_ms"]
        page.buckets[_bucket_of(row["duration_ms"], self.boundaries)] += 1
        if row["query_count"] is not None:
            page.total_query_count += row["query_count"]
            page.total_query_ms += row["query_ms"] or 0
            page.query_sampled_views += 1

        geo = self.geo[(row["country"], row["device"], row["browser_id"], row["os_id"], is_bot)]
        geo.views += 1
        geo.sessions.add(session_id)
        geo.visitors.add(row["visitor_hash"])

        status = self.statuses[(row["route"], row["status"], is_bot)]
        status.count += 1
        status.buckets[_bucket_of(row["duration_ms"], self.boundaries)] += 1

    def _close_session(self) -> None:
        """Attribute the per-session facts: entry page, exit page, bounce, source."""
        if not self._session:
            return
        first, last = self._session[0], self._session[-1]
        is_bot = first["is_bot"]
        bounced = len(self._session) == 1

        entry = self.pages[(first["route"], first["path"], is_bot)]
        entry.entries += 1
        if bounced:
            entry.bounces += 1

        exit_page = self.pages[(last["route"], last["path"], is_bot)]
        exit_page.exits += 1

        # A session's source is where it *arrived* from, so it comes off the
        # first hit -- referrers on later hits are internal navigation.
        source = self.sources[
            (
                first["referrer_host"],
                first["utm_source"],
                first["utm_medium"],
                first["utm_campaign"],
                is_bot,
            )
        ]
        source.sessions.add(first["session_id"])
        source.visitors.add(first["visitor_hash"])
        if bounced:
            source.bounces += 1

        self._session = []

    # -- output -----------------------------------------------------------

    def build(self) -> dict[str, list]:
        self._close_session()
        day = self.day

        pages = [
            DailyPageStat(
                date=day, route=route, path=path, is_bot=is_bot,
                views=acc.views,
                visitors=len(acc.visitors),
                sessions=len(acc.sessions),
                entries=acc.entries,
                exits=acc.exits,
                bounces=acc.bounces,
                total_duration_ms=acc.total_duration_ms,
                total_query_count=acc.total_query_count,
                total_query_ms=acc.total_query_ms,
                query_sampled_views=acc.query_sampled_views,
                **{f"b{i}": acc.buckets[i] for i in range(BUCKET_COUNT)},
            )
            for (route, path, is_bot), acc in self.pages.items()
        ]
        sources = [
            DailySourceStat(
                date=day, referrer_host=host, utm_source=source, utm_medium=medium,
                utm_campaign=campaign, is_bot=is_bot,
                sessions=len(acc.sessions), visitors=len(acc.visitors), bounces=acc.bounces,
            )
            for (host, source, medium, campaign, is_bot), acc in self.sources.items()
        ]
        geo = [
            DailyGeoDeviceStat(
                date=day, country=country, device=device, browser_id=browser, os_id=os_id,
                is_bot=is_bot,
                views=acc.views, visitors=len(acc.visitors), sessions=len(acc.sessions),
            )
            for (country, device, browser, os_id, is_bot), acc in self.geo.items()
        ]
        statuses = [
            DailyStatusStat(
                date=day, route=route, status=status, is_bot=is_bot, count=acc.count,
                **{f"b{i}": acc.buckets[i] for i in range(BUCKET_COUNT)},
            )
            for (route, status, is_bot), acc in self.statuses.items()
        ]
        visitors = [
            DailyUniqueVisitor(date=day, visitor_hash=visitor_hash, is_bot=is_bot)
            for visitor_hash, is_bot in self.visitors.items()
        ]
        return {
            "pages": pages,
            "sources": sources,
            "geo": geo,
            "statuses": statuses,
            "visitors": visitors,
        }


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------

_ROW_FIELDS = (
    "session_id", "visitor_hash", "is_bot", "route", "path", "duration_ms", "status",
    "query_count", "query_ms", "country", "device", "browser_id", "os_id", "referrer_host",
    "utm_source", "utm_medium", "utm_campaign",
)


def day_bounds(day: date_cls) -> tuple[datetime, datetime]:
    """Local midnight to local midnight, as aware datetimes."""
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(day, time.min), tz)
    return start, start + timedelta(days=1)


def check_bucket_scheme(force: bool = False) -> list[int]:
    """Refuse to mix histogram schemes without being told to.

    The bucket columns are positional, so changing ``DURATION_BUCKETS_MS`` after
    rollups exist would silently reinterpret every historical row.
    """
    boundaries = list(sitepulse_settings.DURATION_BUCKETS_MS)
    using = sitepulse_settings.DATABASE_ALIAS
    row = State.objects.using(using).filter(key=BUCKET_STATE_KEY).first()
    if row is None:
        State.objects.using(using).update_or_create(
            key=BUCKET_STATE_KEY, defaults={"value": boundaries}
        )
        return boundaries
    if row.value != boundaries and not force:
        raise BucketSchemeChanged(
            f"SITEPULSE['DURATION_BUCKETS_MS'] is {boundaries}, but existing rollups were "
            f"built with {row.value}. The bucket columns are positional, so mixing the two "
            f"would silently corrupt every historical percentile. Re-run with "
            f"--allow-bucket-change to accept that and rebuild the affected days."
        )
    if row.value != boundaries:
        State.objects.using(using).update_or_create(
            key=BUCKET_STATE_KEY, defaults={"value": boundaries}
        )
    return boundaries


def rollup_day(day: date_cls, force_buckets: bool = False,
               chunk_size: int = 5000) -> dict[str, int]:
    """Aggregate one day. Returns a count of rows written per table."""
    boundaries = check_bucket_scheme(force=force_buckets)
    using = sitepulse_settings.DATABASE_ALIAS
    start, end = day_bounds(day)

    accumulator = DayRollup(day, boundaries)
    queryset = (
        Hit.objects.using(using)
        .filter(ts__gte=start, ts__lt=end)
        .order_by("session_id", "ts", "id")
        .values(*_ROW_FIELDS)
    )
    for row in queryset.iterator(chunk_size=chunk_size):
        # BinaryField comes back as memoryview on some backends; bytes hashes.
        row["session_id"] = bytes(row["session_id"])
        row["visitor_hash"] = bytes(row["visitor_hash"])
        accumulator.add(row)

    built = accumulator.build()

    with transaction.atomic(using=using):
        DailyPageStat.objects.using(using).filter(date=day).delete()
        DailySourceStat.objects.using(using).filter(date=day).delete()
        DailyGeoDeviceStat.objects.using(using).filter(date=day).delete()
        DailyStatusStat.objects.using(using).filter(date=day).delete()
        DailyUniqueVisitor.objects.using(using).filter(date=day).delete()

        DailyPageStat.objects.using(using).bulk_create(built["pages"], batch_size=500)
        DailySourceStat.objects.using(using).bulk_create(built["sources"], batch_size=500)
        DailyGeoDeviceStat.objects.using(using).bulk_create(built["geo"], batch_size=500)
        DailyStatusStat.objects.using(using).bulk_create(built["statuses"], batch_size=500)
        DailyUniqueVisitor.objects.using(using).bulk_create(built["visitors"], batch_size=1000)

    mark_rolled_up(day)
    counts = {name: len(rows) for name, rows in built.items()}
    counts["hits"] = accumulator.rows
    logger.info("sitepulse: rolled up %s -- %s", day, counts)
    return counts


def mark_rolled_up(day: date_cls) -> None:
    """Record how far the rollups run, so reports know where live data starts."""
    using = sitepulse_settings.DATABASE_ALIAS
    current = rolled_up_through()
    if current is None or day > current:
        State.objects.using(using).update_or_create(
            key=ROLLED_UP_THROUGH_KEY, defaults={"value": day.isoformat()}
        )


def rolled_up_through() -> date_cls | None:
    """The most recent day covered by the rollups, or ``None`` if none are."""
    using = sitepulse_settings.DATABASE_ALIAS
    row = State.objects.using(using).filter(key=ROLLED_UP_THROUGH_KEY).first()
    if row is None or not row.value:
        return None
    return date_cls.fromisoformat(row.value)


def rollup_range(start: date_cls, end: date_cls, **kwargs) -> dict[str, int]:
    """Roll up every day in ``[start, end]`` inclusive."""
    totals: dict[str, int] = {}
    day = start
    while day <= end:
        for key, value in rollup_day(day, **kwargs).items():
            totals[key] = totals.get(key, 0) + value
        day += timedelta(days=1)
    return totals


def pending_days(default_days: int = 7) -> list[date_cls]:
    """Days with raw hits but no rollup rows, oldest first.

    Lets the nightly command catch up by itself after the cron didn't run,
    rather than needing someone to notice and backfill by hand.
    """
    using = sitepulse_settings.DATABASE_ALIAS
    today = timezone.localdate()
    earliest = today - timedelta(days=default_days)
    oldest_hit = (
        Hit.objects.using(using).order_by("ts").values_list("ts", flat=True).first()
    )
    if oldest_hit is None:
        return []
    first_day = max(timezone.localtime(oldest_hit).date(), earliest)
    done = set(
        DailyPageStat.objects.using(using)
        .filter(date__gte=first_day)
        .values_list("date", flat=True)
        .distinct()
    )
    days = []
    day = first_day
    while day < today:
        if day not in done:
            days.append(day)
        day += timedelta(days=1)
    return days
