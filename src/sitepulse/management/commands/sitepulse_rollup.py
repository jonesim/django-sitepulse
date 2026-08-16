"""Aggregate raw hits into the permanent daily rollups."""

from __future__ import annotations

from datetime import date, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from sitepulse.rollup import BucketSchemeChanged, pending_days, rollup_day


class Command(BaseCommand):
    help = (
        "Roll raw hits up into the daily tables. With no arguments, rolls up every "
        "day that has hits but no rollup rows, up to and including yesterday."
    )

    def add_arguments(self, parser):
        parser.add_argument("--date", help="A single day to roll up (YYYY-MM-DD).")
        parser.add_argument("--from", dest="date_from", help="Start of a range (YYYY-MM-DD).")
        parser.add_argument("--to", dest="date_to", help="End of a range, inclusive.")
        parser.add_argument(
            "--days", type=int, default=7,
            help="How far back to look for missing days when no date is given (default 7).",
        )
        parser.add_argument(
            "--today", action="store_true",
            help="Also roll up today. Today is normally left live so it stays current; "
                 "use this if you would rather trade freshness for cheaper dashboards.",
        )
        parser.add_argument(
            "--allow-bucket-change", action="store_true",
            help="Accept a changed DURATION_BUCKETS_MS and rewrite the days being rolled up. "
                 "Days not re-rolled keep the old, now differently-meaning, buckets.",
        )

    def handle(self, *args, **options):
        days = self._days_to_do(options)
        if not days:
            self.stdout.write("Nothing to roll up.")
            return

        for day in days:
            try:
                counts = rollup_day(day, force_buckets=options["allow_bucket_change"])
            except BucketSchemeChanged as exc:
                raise CommandError(str(exc)) from exc
            self.stdout.write(
                self.style.SUCCESS(
                    f"{day}: {counts['hits']} hits -> {counts['pages']} page, "
                    f"{counts['sources']} source, {counts['geo']} geo/device, "
                    f"{counts['statuses']} status, {counts['visitors']} visitor rows"
                )
            )

    def _days_to_do(self, options) -> list[date]:
        if options["date"]:
            return [_parse(options["date"])]
        if options["date_from"] or options["date_to"]:
            if not (options["date_from"] and options["date_to"]):
                raise CommandError("--from and --to must be given together")
            start, end = _parse(options["date_from"]), _parse(options["date_to"])
            if end < start:
                raise CommandError("--to is before --from")
            return [start + timedelta(days=n) for n in range((end - start).days + 1)]
        days = pending_days(default_days=options["days"])
        if options["today"]:
            days.append(timezone.localdate())
        return days


def _parse(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise CommandError(f"{value!r} is not a date in YYYY-MM-DD form") from exc
