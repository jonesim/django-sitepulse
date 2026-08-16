"""The dashboard.

Server-rendered Django views behind a permission, with server-rendered SVG
charts. Deliberately not a SPA: this is an internal tool, and a page that needs
no build step is far easier for someone to install and trust.

Navigation is built with ``django-tab-menus``; there are no forms anywhere -- the
date range is a set of preset links plus ``?from=``/``?to=`` -- so the dashboard
needs no client-side JavaScript at all.
"""

from __future__ import annotations

from datetime import date, timedelta

from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.utils import timezone
from django.views.generic import TemplateView
from django_menus.menu import MenuItem, MenuMixin

from . import charts
from .conf import BUCKET_COUNT, sitepulse_settings
from .models import Device
from .query import Report, route_choices

RANGE_PRESETS = [
    ("Today", 1),
    ("7 days", 7),
    ("30 days", 30),
    ("90 days", 90),
    ("12 months", 365),
]


class DashboardView(MenuMixin, TemplateView):
    """Shared plumbing: permission, date range, tabs.

    ``MenuMixin`` rather than ``AjaxMenuTemplateView``: there is no AJAX on these
    pages, so there is no reason to pull in the request-routing machinery.
    """

    #: url name of this page, for the tab menu's active state
    page = ""
    title = ""

    def dispatch(self, request, *args, **kwargs):
        if not self.has_permission(request):
            raise PermissionDenied("You do not have permission to view the SitePulse dashboard.")
        return super().dispatch(request, *args, **kwargs)

    @staticmethod
    def has_permission(request) -> bool:
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return False
        if user.is_superuser:
            return True
        permission = sitepulse_settings.DASHBOARD_PERMISSION
        if not permission:
            return bool(user.is_staff)
        return user.has_perm(permission)

    # -- date range -------------------------------------------------------

    def setup(self, request, *args, **kwargs):
        super().setup(request, *args, **kwargs)
        self.start, self.end, self.range_label = self.get_range(request)
        self.include_bots = request.GET.get("bots") == "1"

    @staticmethod
    def get_range(request) -> tuple[date, date, str]:
        today = timezone.localdate()
        raw_from, raw_to = request.GET.get("from"), request.GET.get("to")
        if raw_from and raw_to:
            try:
                start, end = date.fromisoformat(raw_from), date.fromisoformat(raw_to)
            except ValueError as exc:
                raise Http404("Invalid date in the from/to parameters") from exc
            if end < start:
                start, end = end, start
            return start, min(end, today), f"{start} to {end}"
        try:
            days = int(request.GET.get("days", sitepulse_settings.DASHBOARD_DEFAULT_DAYS))
        except ValueError:
            days = sitepulse_settings.DASHBOARD_DEFAULT_DAYS
        days = max(1, min(days, 366 * 3))
        start = today - timedelta(days=days - 1)
        label = next((name for name, value in RANGE_PRESETS if value == days), f"{days} days")
        return start, today, label

    def query(self, **extra) -> dict:
        return {"start": self.start, "end": self.end, "include_bots": self.include_bots, **extra}

    def _preserved(self, **overrides) -> str:
        params = self.request.GET.copy()
        for key, value in overrides.items():
            if value is None:
                params.pop(key, None)
            else:
                params[key] = value
        query = params.urlencode()
        return f"{self.request.path}?{query}" if query else self.request.path

    # -- menus ------------------------------------------------------------

    def setup_menu(self):
        super().setup_menu()
        tabs = self.add_menu("tabs", "tabs")
        tabs.active = f"sitepulse:{self.page}"
        tabs.add_items(
            *[
                MenuItem(f"sitepulse:{name}", label)
                for name, label in (
                    ("overview", "Overview"),
                    ("pages", "Pages"),
                    ("sources", "Sources"),
                    ("performance", "Performance"),
                    ("errors", "Errors"),
                    ("health", "Health"),
                )
            ]
        )
        # The date range is a list of preset links, not a form -- there is no
        # state here that a link cannot carry.
        ranges = self.add_menu("ranges", "button_group", compare_full_path=True)
        ranges.add_items(
            *[
                MenuItem(
                    self._preserved(days=days, **{"from": None, "to": None}),
                    label,
                    link_type=MenuItem.HREF,
                    css_classes="sp-range",
                )
                for label, days in RANGE_PRESETS
            ]
        )
        bots = self.add_menu("bots", "button_group", compare_full_path=True)
        bots.add_items(
            MenuItem(self._preserved(bots=None), "Humans", link_type=MenuItem.HREF,
                     css_classes="sp-range"),
            MenuItem(self._preserved(bots="1"), "Include bots", link_type=MenuItem.HREF,
                     css_classes="sp-range"),
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "title": self.title,
                "start": self.start,
                "end": self.end,
                "range_label": self.range_label,
                "include_bots": self.include_bots,
                "health": Report.health(),
            }
        )
        return context


def _labels(rows, key="date") -> list[str]:
    return [row[key].strftime("%d %b") for row in rows]


class OverviewView(DashboardView):
    template_name = "sitepulse/overview.html"
    page = "overview"
    title = "Overview"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        summary = Report.summary(**self.query())
        series = Report.timeseries(**self.query())

        tiles = [
            charts.stat_tile("Pageviews", charts.compact(summary["views"]),
                             values=[row["views"] for row in series]),
            charts.stat_tile("Visitor-days", charts.compact(summary["visitors"]),
                             sub="unique visitors per day, added up",
                             values=[row["visitors"] for row in series]),
            charts.stat_tile("Sessions", charts.compact(summary["sessions"]),
                             values=[row["sessions"] for row in series]),
            charts.stat_tile("Bounce rate", charts.percent(summary["bounce_rate"]),
                             sub="sessions with one pageview"),
            charts.stat_tile("Median response", charts.duration(summary["p50"]),
                             sub=f"p95 {charts.duration(summary['p95'])}"),
            charts.stat_tile("Queries per view",
                             charts.compact(summary["avg_query_count"]),
                             sub="SQL statements"),
        ]

        traffic = charts.line_chart(
            _labels(series),
            [
                {"name": "Pageviews", "values": [row["views"] for row in series]},
                {"name": "Sessions", "values": [row["sessions"] for row in series]},
                {"name": "Visitors", "values": [row["visitors"] for row in series]},
            ],
            "Traffic",
        )

        top_routes = Report.pageviews(**self.query(group_by="route", limit=10))
        top_sources = Report.sources(**self.query(limit=10))

        context.update(
            {
                "stats": charts.render(charts.stat_row(tiles)),
                "traffic": charts.render(traffic),
                "top_routes": charts.render(
                    charts.bar_list(
                        [{"label": row["route"] or "(unresolved)", "value": row["views"]}
                         for row in top_routes],
                        "Top routes by pageviews",
                        note="Routes, not URLs: /orders/<int:pk> is one row, not fifty thousand.",
                    )
                ),
                "top_sources": charts.render(
                    charts.bar_list(
                        [{"label": row["referrer_host"] or "direct / none",
                          "value": row["sessions"]} for row in top_sources],
                        "Top referrers by session",
                    )
                ),
            }
        )
        return context


class PagesView(DashboardView):
    template_name = "sitepulse/pages.html"
    page = "pages"
    title = "Pages"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        group_by = self.request.GET.get("by", "route")
        if group_by not in {"route", "path"}:
            group_by = "route"
        rows = Report.pageviews(**self.query(group_by=group_by, limit=100))

        context["group_by"] = group_by
        context["grouping_links"] = [
            (label, self._preserved(by=value), group_by == value)
            for label, value in (("By route", "route"), ("By URL", "path"))
        ]
        context["chart"] = charts.render(
            charts.bar_list(
                [{"label": row[group_by] or "(unresolved)", "value": row["views"]}
                 for row in rows[:15]],
                f"Top {'routes' if group_by == 'route' else 'URLs'} by pageviews",
            )
        )
        context["table"] = charts.render(
            charts.table(
                [
                    "Route" if group_by == "route" else "URL",
                    "Views", "Entries", "Exits", "Bounce rate", "Avg time", "Avg queries",
                ],
                [
                    [
                        row[group_by] or "(unresolved)",
                        charts.compact(row["views"]),
                        charts.compact(row["entries"]),
                        charts.compact(row["exits"]),
                        charts.percent(row["bounce_rate"]),
                        charts.duration(row["avg_duration_ms"]),
                        charts.compact(row["avg_query_count"]),
                    ]
                    for row in rows
                ],
            )
        )
        return context


class SourcesView(DashboardView):
    template_name = "sitepulse/sources.html"
    page = "sources"
    title = "Sources"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        referrers = Report.sources(**self.query(group_by="referrer_host", limit=50))
        context["chart"] = charts.render(
            charts.bar_list(
                [{"label": row["referrer_host"] or "direct / none", "value": row["sessions"]}
                 for row in referrers[:15]],
                "Sessions by referrer",
                note="Self-referrals are dropped -- an internal link is navigation, not a source.",
            )
        )
        context["referrers"] = charts.render(
            charts.table(
                ["Referrer", "Sessions", "Bounce rate"],
                [
                    [row["referrer_host"] or "direct / none",
                     charts.compact(row["sessions"]),
                     charts.percent(row["bounce_rate"])]
                    for row in referrers
                ],
            )
        )
        campaigns = []
        for dimension, label in (
            ("utm_source", "Source"), ("utm_medium", "Medium"), ("utm_campaign", "Campaign")
        ):
            rows = [
                row for row in Report.sources(**self.query(group_by=dimension, limit=20))
                if row[dimension]
            ]
            if rows:
                campaigns.append(
                    (
                        label,
                        charts.render(
                            charts.table(
                                [label, "Sessions", "Bounce rate"],
                                [
                                    [row[dimension], charts.compact(row["sessions"]),
                                     charts.percent(row["bounce_rate"])]
                                    for row in rows
                                ],
                            )
                        ),
                    )
                )
        context["campaigns"] = campaigns

        context["audience"] = [
            (
                label,
                charts.render(
                    charts.bar_list(
                        [{"label": row["label"], "value": row["views"]}
                         for row in Report.audience(**self.query(dimension=dimension, limit=10))],
                        label,
                    )
                ),
            )
            for dimension, label in (
                ("country", "Country"), ("device", "Device"),
                ("browser", "Browser"), ("os", "Operating system"),
            )
        ]
        return context


class PerformanceView(DashboardView):
    template_name = "sitepulse/performance.html"
    page = "performance"
    title = "Performance"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        route = self.request.GET.get("route") or None
        rows = Report.performance(**self.query(route=route, limit=50))
        trend = Report.performance_trend(**self.query(route=route))
        buckets = [0] * BUCKET_COUNT
        for row in rows:
            buckets = [
                total + value
                for total, value in zip(buckets, row["buckets"], strict=True)
            ]

        context["route"] = route
        context["route_links"] = [
            (name or "(unresolved)", self._preserved(route=name or ""), name == route)
            for name in route_choices(self.start, self.end, limit=25)
        ]
        context["clear_route"] = self._preserved(route=None)
        context["trend"] = charts.render(
            charts.line_chart(
                _labels(trend),
                [
                    {"name": "p50", "values": [row["p50"] for row in trend]},
                    {"name": "p95", "values": [row["p95"] for row in trend]},
                ],
                f"Response time trend{f' -- {route}' if route else ''}",
                note="Percentiles come from stored histogram buckets, so they are accurate to "
                     "within a bucket width and stay available long after the raw rows are gone.",
            )
        )
        context["histogram"] = charts.render(
            charts.histogram(
                buckets,
                list(sitepulse_settings.DURATION_BUCKETS_MS),
                "Response time distribution",
            )
        )
        context["table"] = charts.render(
            charts.table(
                ["Route", "Views", "p50", "p95", "p99", "Avg queries", "Avg DB time"],
                [
                    [
                        row["route"] or "(unresolved)",
                        charts.compact(row["views"]),
                        charts.duration(row["p50"]),
                        charts.duration(row["p95"]),
                        charts.duration(row["p99"]),
                        charts.compact(row["avg_query_count"]),
                        charts.duration(row["avg_query_ms"]),
                    ]
                    for row in rows
                ],
            )
        )
        return context


class ErrorsView(DashboardView):
    template_name = "sitepulse/errors.html"
    page = "errors"
    title = "Errors"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        rate = Report.error_rate(**self.query())
        by_route = Report.errors(**self.query(group_by="route", limit=50))
        by_status = Report.status_breakdown(**self.query())

        context["chart"] = charts.render(
            charts.line_chart(
                _labels(rate),
                [
                    {"name": "4xx", "values": [row["client_errors"] for row in rate]},
                    {"name": "5xx", "values": [row["server_errors"] for row in rate]},
                ],
                "Errors per day",
            )
        )
        context["by_route"] = charts.render(
            charts.table(
                ["Route", "Errors"],
                [[row["route"] or "(unresolved)", charts.compact(row["count"])]
                 for row in by_route],
            )
        )
        context["by_status"] = charts.render(
            charts.table(
                ["Status", "Requests"],
                [[str(row["status"]), charts.compact(row["count"])] for row in by_status],
            )
        )
        total = sum(row["total"] for row in rate)
        errors = sum(row["client_errors"] + row["server_errors"] for row in rate)
        context["stats"] = charts.render(
            charts.stat_row(
                [
                    charts.stat_tile("Requests", charts.compact(total)),
                    charts.stat_tile("Errors", charts.compact(errors),
                                     tone="critical" if errors else ""),
                    charts.stat_tile(
                        "Error rate", charts.percent(errors / total if total else 0)
                    ),
                ]
            )
        )
        return context


class HealthView(DashboardView):
    template_name = "sitepulse/health.html"
    page = "health"
    title = "Health"

    def get_context_data(self, **kwargs):
        from . import partitions
        from .scheduling import lock_holder

        context = super().get_context_data(**kwargs)
        health = context["health"]
        context["settings_rows"] = sorted(sitepulse_settings.as_dict().items())
        context["partitions"] = partitions.existing_partitions()
        context["is_postgres"] = partitions.is_postgres()
        context["default_partition_rows"] = (
            partitions.default_partition_rows() if partitions.is_postgres() else 0
        )
        context["lock"] = lock_holder()
        context["warnings"] = self.warnings(
            health, context["default_partition_rows"], context["lock"]
        )
        context["devices"] = dict(Device.choices)
        return context

    @staticmethod
    def warnings(health, stray_rows, lock=None) -> list[str]:
        messages = []
        if lock is not None:
            holder, since = lock
            age = timezone.now() - since
            if age > timedelta(hours=1):
                # A run that is still going after an hour is either a large
                # backfill or a process that died holding the lock. Either way it
                # is the first thing to check when the rollups stop moving.
                messages.append(
                    f"The nightly lock has been held since {since:%Y-%m-%d %H:%M} by "
                    f"{holder.get('host', '?')} (pid {holder.get('pid', '?')}). If that "
                    f"process is gone, the lock is taken over automatically after 6 hours."
                )
        if health["dropped_7d"]:
            messages.append(
                f"{health['dropped_7d']} hits were dropped in the last 7 days because the write "
                f"buffer was full. Raise BUFFER_MAX, lower FLUSH_EVERY_SECONDS, or find out what "
                f"is making writes slow."
            )
        if health["write_errors_7d"]:
            messages.append(
                f"{health['write_errors_7d']} batches failed to write in the last 7 days. "
                f"Check the 'sitepulse' logger for the exceptions."
            )
        lag = health["rollup_lag_days"]
        if lag is None:
            messages.append(
                "Nothing has been rolled up yet. Run `manage.py sitepulse_rollup` -- until you "
                "do, every report is computed from raw hits on every page load."
            )
        elif lag > 0:
            messages.append(
                f"The rollups are {lag} day(s) behind. Reports still show the right numbers, but "
                f"they are aggregating raw rows to do it. Check that `sitepulse_nightly` is "
                f"actually running."
            )
        if stray_rows:
            messages.append(
                f"{stray_rows} hits landed in the catch-all partition, which means a monthly "
                f"partition was missing when they arrived. Run `manage.py sitepulse_partitions`."
            )
        return messages
