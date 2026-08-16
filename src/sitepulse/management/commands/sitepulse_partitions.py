"""Create upcoming monthly partitions for the raw hit table."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from sitepulse import partitions
from sitepulse.conf import sitepulse_settings


class Command(BaseCommand):
    help = (
        "Create this month's partition and the next PARTITION_MONTHS_AHEAD. Idempotent; "
        "run it nightly. No-op on non-PostgreSQL backends."
    )

    def add_arguments(self, parser):
        parser.add_argument("--ahead", type=int, default=None, help="Months to create ahead.")
        parser.add_argument(
            "--check", action="store_true",
            help="List partitions and report anything in the catch-all partition.",
        )

    def handle(self, *args, **options):
        if not partitions.is_postgres():
            self.stdout.write(
                "Not running on PostgreSQL -- the hit table is not partitioned and retention "
                "uses chunked deletes instead. Nothing to do."
            )
            return

        if options["check"]:
            self._check()
            return

        ahead = options["ahead"]
        if ahead is None:
            ahead = sitepulse_settings.PARTITION_MONTHS_AHEAD
        partitions.create_default_partition()
        created = partitions.ensure_partitions(months_ahead=ahead)
        self.stdout.write(self.style.SUCCESS("Ensured: " + ", ".join(created)))

    def _check(self):
        rows = partitions.existing_partitions()
        for name, month in rows:
            label = month.isoformat() if month else "catch-all"
            self.stdout.write(f"  {name:<40} {label}")
        stray = partitions.default_partition_rows()
        if stray:
            self.stdout.write(
                self.style.WARNING(
                    f"{stray} hit(s) landed in {partitions.DEFAULT_PARTITION}. That means a "
                    f"month partition was missing when they were written -- create the "
                    f"missing month, move them across, and check that this command is "
                    f"actually running on a schedule."
                )
            )
        else:
            self.stdout.write(self.style.SUCCESS("Catch-all partition is empty, as it should be."))
