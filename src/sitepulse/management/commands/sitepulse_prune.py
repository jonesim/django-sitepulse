"""Drop raw hits past the retention window, and old visitor rows."""

from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from sitepulse import partitions
from sitepulse.conf import sitepulse_settings
from sitepulse.models import DailyUniqueVisitor, Hit
from sitepulse.rollup import day_bounds, rolled_up_through

CHUNK = 10_000


class Command(BaseCommand):
    help = (
        "Delete raw hits older than RAW_RETENTION_DAYS. On PostgreSQL this drops whole "
        "monthly partitions, which is instant; elsewhere it deletes in chunks."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Say what would go, do nothing.")
        parser.add_argument(
            "--force", action="store_true",
            help="Prune even when the rollups have not caught up. This throws away data "
                 "that has not been aggregated yet -- it is gone for good.",
        )

    def handle(self, *args, **options):
        retention = sitepulse_settings.RAW_RETENTION_DAYS
        cutoff = timezone.localdate() - timedelta(days=retention)
        dry_run = options["dry_run"]
        using = sitepulse_settings.DATABASE_ALIAS
        start, _ = day_bounds(cutoff)

        # The guard below is about not discarding un-aggregated data -- so it has
        # to be conditional on there being data to discard. A fresh install has
        # nothing rolled up *and* nothing old enough to prune, and erroring there
        # would make the very first nightly run fail on every new project.
        prunable = Hit.objects.using(using).filter(ts__lt=start).exists()
        boundary = rolled_up_through()

        if prunable and not options["force"]:
            if boundary is None:
                raise CommandError(
                    f"There are raw hits older than {cutoff}, but nothing has been rolled up "
                    f"yet, so pruning would discard data that was never aggregated. Run "
                    f"sitepulse_rollup first, or pass --force."
                )
            if boundary < cutoff:
                raise CommandError(
                    f"Rollups only run to {boundary} but the retention cutoff is {cutoff}. "
                    f"Pruning now would lose {(cutoff - boundary).days} day(s) of "
                    f"un-aggregated data. Run sitepulse_rollup first, or pass --force."
                )

        self.stdout.write(f"Retention is {retention} days; pruning raw hits before {cutoff}.")
        if not prunable:
            self.stdout.write("No raw hits are outside the retention window.")

        if partitions.is_postgres():
            self._prune_partitions(cutoff, dry_run)
        else:
            self._prune_rows(cutoff, dry_run)

        self._prune_visitors(dry_run)

    def _prune_partitions(self, cutoff, dry_run):
        doomed = [
            name for name, month in partitions.existing_partitions()
            if month is not None and partitions.next_month(month) <= cutoff
        ]
        stray = partitions.default_partition_rows()
        if dry_run:
            self.stdout.write(
                "Would drop: " + (", ".join(doomed) if doomed else "nothing")
                + (f"; and delete up to {stray} row(s) from the catch-all partition."
                   if stray else "")
            )
            return
        if doomed:
            dropped = partitions.drop_partitions_before(cutoff)
            self.stdout.write(self.style.SUCCESS("Dropped: " + ", ".join(dropped)))
        else:
            self.stdout.write("No whole partitions are outside the window yet.")
        if stray:
            # Dropping partitions never touches the catch-all, so anything that
            # landed there while its month was missing needs deleting by hand.
            start, _ = day_bounds(cutoff)
            deleted = partitions.prune_default_partition(start)
            self.stdout.write(
                self.style.WARNING(
                    f"Deleted {deleted} row(s) from the catch-all partition. Rows should never "
                    f"land there -- run `sitepulse_partitions` on a schedule."
                )
            )

    def _prune_rows(self, cutoff, dry_run):
        start, _ = day_bounds(cutoff)
        using = sitepulse_settings.DATABASE_ALIAS
        queryset = Hit.objects.using(using).filter(ts__lt=start)
        total = queryset.count()
        if not total:
            return
        if dry_run:
            self.stdout.write(f"Would delete {total} raw hits.")
            return
        deleted = 0
        while True:
            ids = list(queryset.values_list("id", flat=True)[:CHUNK])
            if not ids:
                break
            deleted += Hit.objects.using(using).filter(id__in=ids).delete()[0]
            self.stdout.write(f"  deleted {deleted}/{total}", ending="\r")
        self.stdout.write(self.style.SUCCESS(f"Deleted {deleted} raw hits.        "))

    def _prune_visitors(self, dry_run):
        days = sitepulse_settings.UNIQUE_VISITOR_RETENTION_DAYS
        cutoff = timezone.localdate() - timedelta(days=days)
        using = sitepulse_settings.DATABASE_ALIAS
        queryset = DailyUniqueVisitor.objects.using(using).filter(date__lt=cutoff)
        count = queryset.count()
        if not count:
            return
        if dry_run:
            self.stdout.write(f"Would delete {count} visitor rows before {cutoff}.")
            return
        queryset.delete()
        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted {count} visitor rows before {cutoff}. Ranges older than that fall "
                f"back to per-row visitor counts, which are not summable."
            )
        )
