"""Data model.

Two tiers, as per the design:

* :class:`Hit` -- one row per request, partitioned by month on PostgreSQL, pruned
  to ``RAW_RETENTION_DAYS``. The only high-volume table.
* ``Daily*`` rollups -- written nightly, kept forever, small enough to query
  without thinking.

Nothing in here is written from the request path; see :mod:`sitepulse.buffer`.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models

from .conf import BUCKET_COUNT

# ---------------------------------------------------------------------------
# enums
# ---------------------------------------------------------------------------


class Method(models.IntegerChoices):
    OTHER = 0, "other"
    GET = 1, "GET"
    POST = 2, "POST"
    PUT = 3, "PUT"
    PATCH = 4, "PATCH"
    DELETE = 5, "DELETE"
    HEAD = 6, "HEAD"
    OPTIONS = 7, "OPTIONS"

    @classmethod
    def from_name(cls, name: str) -> int:
        return _METHOD_BY_NAME.get((name or "").upper(), cls.OTHER)


_METHOD_BY_NAME = {label: value for value, label in Method.choices}


class Device(models.IntegerChoices):
    OTHER = 0, "other"
    DESKTOP = 1, "desktop"
    MOBILE = 2, "mobile"
    TABLET = 3, "tablet"
    BOT = 4, "bot"


# ---------------------------------------------------------------------------
# lookups
# ---------------------------------------------------------------------------


class NameLookup(models.Model):
    """Tiny shared base for the browser/OS lookup tables.

    These stay in a lookup rather than an enum because new browsers appear and a
    package release should not be needed to record one.
    """

    #: Every lookup table has a row named this, so the dimension is never NULL and
    #: group-bys never need to special-case missing data.
    UNKNOWN = "unknown"

    id = models.SmallAutoField(primary_key=True)
    name = models.CharField(max_length=64, unique=True)

    class Meta:
        abstract = True
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Browser(NameLookup):
    class Meta(NameLookup.Meta):
        abstract = False
        verbose_name_plural = "browsers"


class OperatingSystem(NameLookup):
    class Meta(NameLookup.Meta):
        abstract = False
        verbose_name = "operating system"
        verbose_name_plural = "operating systems"


# ---------------------------------------------------------------------------
# raw table
# ---------------------------------------------------------------------------


class Hit(models.Model):
    """One request.

    On PostgreSQL the physical table is ``PARTITION BY RANGE (ts)`` with a
    composite ``(id, ts)`` primary key -- the ORM only ever sees ``id``, which is
    unique in practice because it comes from a single identity sequence shared by
    every partition. See :mod:`sitepulse.partitions`.
    """

    id = models.BigAutoField(primary_key=True)
    ts = models.DateTimeField()

    # identity (see identity.py) -- never a raw IP or user-agent
    visitor_hash = models.BinaryField(max_length=16)
    session_id = models.BinaryField(max_length=16)
    is_new_session = models.BooleanField(default=False)

    # request
    path = models.CharField(max_length=255)
    route = models.CharField(max_length=255, blank=True, default="")
    view_name = models.CharField(max_length=255, blank=True, default="")
    method = models.SmallIntegerField(choices=Method, default=Method.GET)
    status = models.SmallIntegerField()

    # performance
    duration_ms = models.IntegerField()
    query_count = models.SmallIntegerField(null=True, blank=True)
    query_ms = models.IntegerField(null=True, blank=True)

    # acquisition
    referrer_host = models.CharField(max_length=128, blank=True, default="")
    referrer_path = models.CharField(max_length=255, blank=True, default="")
    utm_source = models.CharField(max_length=64, blank=True, default="")
    utm_medium = models.CharField(max_length=64, blank=True, default="")
    utm_campaign = models.CharField(max_length=64, blank=True, default="")

    # audience
    country = models.CharField(max_length=2, blank=True, default="")
    region = models.CharField(max_length=64, blank=True, default="")
    device = models.SmallIntegerField(choices=Device, default=Device.OTHER)
    # Not nullable: enrichment always resolves a name, falling back to "unknown".
    # No FK constraint or index -- partitions must be droppable and the hot table
    # carries only the three indexes below.
    browser = models.ForeignKey(
        Browser, on_delete=models.DO_NOTHING,
        db_constraint=False, db_index=False, related_name="hits",
    )
    os = models.ForeignKey(
        OperatingSystem, on_delete=models.DO_NOTHING,
        db_constraint=False, db_index=False, related_name="hits",
    )
    is_bot = models.BooleanField(default=False)

    # opt-in, off by default
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        db_constraint=False, db_index=False, related_name="+",
    )

    screen_w = models.SmallIntegerField(null=True, blank=True)
    props = models.JSONField(null=True, blank=True)

    class Meta:
        db_table = "sitepulse_hit"
        indexes = [
            # Deliberately no index on `path`: the rollups answer path questions,
            # and a fourth index on the hot write table costs real insert time.
            models.Index(fields=["ts"], name="sitepulse_hit_ts"),
            models.Index(fields=["visitor_hash", "ts"], name="sitepulse_hit_visitor"),
            models.Index(fields=["route", "ts"], name="sitepulse_hit_route"),
        ]
        permissions = [("view_dashboard", "Can view the SitePulse dashboard")]
        verbose_name = "hit"
        verbose_name_plural = "hits"

    def __str__(self) -> str:
        return f"{self.get_method_display()} {self.path} {self.status} ({self.duration_ms}ms)"


# ---------------------------------------------------------------------------
# rollups
# ---------------------------------------------------------------------------


def _bucket_fields() -> dict[str, models.Field]:
    """The nine response-time histogram columns.

    Buckets are summable where percentiles are not, which is what lets the
    dashboard show p95 for any date range long after the raw rows are gone.
    """
    return {
        f"b{i}": models.PositiveIntegerField(default=0)
        for i in range(BUCKET_COUNT)
    }


class HistogramMixin(models.Model):
    """Adds ``b0``..``b8``. Declared dynamically so BUCKET_COUNT stays the one source."""

    class Meta:
        abstract = True

    @property
    def buckets(self) -> list[int]:
        return [getattr(self, f"b{i}") for i in range(BUCKET_COUNT)]


for _name, _field in _bucket_fields().items():
    HistogramMixin.add_to_class(_name, _field)


class DailyPageStat(HistogramMixin):
    """Grain: (date, route, path)."""

    date = models.DateField()
    route = models.CharField(max_length=255, blank=True, default="")
    path = models.CharField(max_length=255)
    #: Part of the grain, not a filter applied before rolling up. Bot traffic is
    #: flagged and kept at both tiers so the filter stays auditable after the raw
    #: rows are gone. Reports exclude it by default.
    is_bot = models.BooleanField(default=False)

    views = models.PositiveIntegerField(default=0)
    #: Correct for this row; NEVER sum across rows. Use DailyUniqueVisitor for ranges.
    visitors = models.PositiveIntegerField(default=0)
    sessions = models.PositiveIntegerField(default=0)
    entries = models.PositiveIntegerField(default=0)
    exits = models.PositiveIntegerField(default=0)
    bounces = models.PositiveIntegerField(default=0)
    total_duration_ms = models.BigIntegerField(default=0)
    #: Kept in the rollup so "which route runs 340 queries a request" survives
    #: retention -- it is the single most useful N+1 signal in the package.
    total_query_count = models.BigIntegerField(default=0)
    total_query_ms = models.BigIntegerField(default=0)
    #: Hits that actually carried query instrumentation, so the average has the
    #: right denominator when TRACK_QUERY_COUNTS was toggled mid-day.
    query_sampled_views = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "sitepulse_daily_page"
        constraints = [
            models.UniqueConstraint(
                fields=["date", "route", "path", "is_bot"], name="sitepulse_page_grain"
            )
        ]
        indexes = [models.Index(fields=["date"], name="sitepulse_page_date")]
        verbose_name = "daily page stat"

    def __str__(self) -> str:
        return f"{self.date} {self.path} ({self.views})"


class DailySourceStat(models.Model):
    """Grain: (date, referrer_host, utm_source, utm_medium, utm_campaign)."""

    date = models.DateField()
    referrer_host = models.CharField(max_length=128, blank=True, default="")
    utm_source = models.CharField(max_length=64, blank=True, default="")
    utm_medium = models.CharField(max_length=64, blank=True, default="")
    utm_campaign = models.CharField(max_length=64, blank=True, default="")
    is_bot = models.BooleanField(default=False)

    sessions = models.PositiveIntegerField(default=0)
    visitors = models.PositiveIntegerField(default=0)
    bounces = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "sitepulse_daily_source"
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "date", "referrer_host", "utm_source", "utm_medium", "utm_campaign", "is_bot",
                ],
                name="sitepulse_source_grain",
            )
        ]
        indexes = [models.Index(fields=["date"], name="sitepulse_source_date")]
        verbose_name = "daily source stat"

    def __str__(self) -> str:
        return f"{self.date} {self.referrer_host or self.utm_source or 'direct'}"


class DailyGeoDeviceStat(models.Model):
    """Grain: (date, country, device, browser, os)."""

    date = models.DateField()
    country = models.CharField(max_length=2, blank=True, default="")
    device = models.SmallIntegerField(choices=Device, default=Device.OTHER)
    browser = models.ForeignKey(Browser, on_delete=models.PROTECT, related_name="+")
    os = models.ForeignKey(OperatingSystem, on_delete=models.PROTECT, related_name="+")
    is_bot = models.BooleanField(default=False)

    views = models.PositiveIntegerField(default=0)
    visitors = models.PositiveIntegerField(default=0)
    sessions = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "sitepulse_daily_geo_device"
        constraints = [
            models.UniqueConstraint(
                fields=["date", "country", "device", "browser", "os", "is_bot"],
                name="sitepulse_geo_grain",
            )
        ]
        indexes = [models.Index(fields=["date"], name="sitepulse_geo_date")]
        verbose_name = "daily geo/device stat"

    def __str__(self) -> str:
        return f"{self.date} {self.country} {self.get_device_display()}"


class DailyStatusStat(HistogramMixin):
    """Grain: (date, route, status). The errors table."""

    date = models.DateField()
    route = models.CharField(max_length=255, blank=True, default="")
    status = models.SmallIntegerField()
    is_bot = models.BooleanField(default=False)
    count = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "sitepulse_daily_status"
        constraints = [
            models.UniqueConstraint(
                fields=["date", "route", "status", "is_bot"], name="sitepulse_status_grain"
            )
        ]
        indexes = [models.Index(fields=["date"], name="sitepulse_status_date")]
        verbose_name = "daily status stat"

    def __str__(self) -> str:
        return f"{self.date} {self.route} {self.status} ({self.count})"


class DailyUniqueVisitor(models.Model):
    """One row per (date, visitor) so range-wide distinct counts stay exact.

    This is the rollup that isn't negligible -- see ``UNIQUE_VISITOR_RETENTION_DAYS``
    and the note in the README about what pruning it costs you.
    """

    date = models.DateField()
    visitor_hash = models.BinaryField(max_length=16)
    #: An attribute, not part of the grain: the hash is derived from the
    #: user-agent, so a given visitor is consistently one or the other.
    is_bot = models.BooleanField(default=False)

    class Meta:
        db_table = "sitepulse_daily_visitor"
        constraints = [
            models.UniqueConstraint(fields=["date", "visitor_hash"], name="sitepulse_visitor_grain")
        ]
        verbose_name = "daily unique visitor"

    def __str__(self) -> str:
        return f"{self.date} {bytes(self.visitor_hash).hex()[:12]}"


# ---------------------------------------------------------------------------
# housekeeping
# ---------------------------------------------------------------------------


class Salt(models.Model):
    """The rotating daily salt (§3).

    Kept in the database rather than in memory because every worker process must
    agree on it, and in a cache it would vanish on a Redis restart mid-day and
    silently split one visitor into two. Rows for previous days are deleted as
    soon as a new day's salt is created, and by ``sitepulse_rotate_salt``.
    """

    date = models.DateField(primary_key=True)
    value = models.BinaryField(max_length=32)

    class Meta:
        db_table = "sitepulse_salt"

    def __str__(self) -> str:
        return f"salt for {self.date}"


class IngestHealth(models.Model):
    """Per-day record of hits the buffer had to drop, surfaced on the dashboard.

    A bounded buffer with visible loss beats an unbounded one with an OOM, but
    only if the loss is actually visible.
    """

    date = models.DateField(primary_key=True)
    dropped = models.BigIntegerField(default=0)
    write_errors = models.BigIntegerField(default=0)

    class Meta:
        db_table = "sitepulse_ingest_health"
        verbose_name = "ingest health"
        verbose_name_plural = "ingest health"

    def __str__(self) -> str:
        return f"{self.date}: {self.dropped} dropped, {self.write_errors} write errors"


class State(models.Model):
    """Small key/value store for things the package needs to remember.

    Currently: the histogram bucket boundaries in force, so that changing
    ``DURATION_BUCKETS_MS`` cannot silently reinterpret existing rollup rows.
    """

    key = models.CharField(max_length=64, primary_key=True)
    value = models.JSONField()
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "sitepulse_state"

    def __str__(self) -> str:
        return self.key
