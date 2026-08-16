"""Admin registration.

Provided for poking at individual rows, not as the reporting interface -- admin
list views are the wrong tool for aggregates. The dashboard is at
``sitepulse:overview``.
"""

from __future__ import annotations

from django.contrib import admin

from .models import (
    Browser,
    DailyGeoDeviceStat,
    DailyPageStat,
    DailySourceStat,
    DailyStatusStat,
    Hit,
    IngestHealth,
    OperatingSystem,
)


class ReadOnlyAdmin(admin.ModelAdmin):
    """Collected data is a record of what happened; editing it is never right."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Hit)
class HitAdmin(ReadOnlyAdmin):
    list_display = ("ts", "path", "route", "status", "duration_ms", "query_count", "is_bot")
    list_filter = ("is_bot", "device", "status")
    search_fields = ("path", "route", "view_name")
    date_hierarchy = "ts"
    list_select_related = False
    show_full_result_count = False  # counting a partitioned 20M-row table is not free


@admin.register(DailyPageStat)
class DailyPageStatAdmin(ReadOnlyAdmin):
    list_display = ("date", "route", "path", "views", "visitors", "sessions", "bounces", "is_bot")
    list_filter = ("is_bot", "date")
    search_fields = ("path", "route")


@admin.register(DailySourceStat)
class DailySourceStatAdmin(ReadOnlyAdmin):
    list_display = ("date", "referrer_host", "utm_source", "utm_medium", "sessions", "bounces")
    list_filter = ("is_bot", "date")
    search_fields = ("referrer_host", "utm_source", "utm_campaign")


@admin.register(DailyStatusStat)
class DailyStatusStatAdmin(ReadOnlyAdmin):
    list_display = ("date", "route", "status", "count", "is_bot")
    list_filter = ("is_bot", "status", "date")


@admin.register(DailyGeoDeviceStat)
class DailyGeoDeviceStatAdmin(ReadOnlyAdmin):
    list_display = ("date", "country", "device", "browser", "os", "views", "sessions")
    list_filter = ("is_bot", "device", "country")


@admin.register(IngestHealth)
class IngestHealthAdmin(ReadOnlyAdmin):
    list_display = ("date", "dropped", "write_errors")


admin.site.register([Browser, OperatingSystem], ReadOnlyAdmin)
