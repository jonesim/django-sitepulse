"""One cron entry: rotate, partition, roll up, prune."""

from __future__ import annotations

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from sitepulse.scheduling import AlreadyRunning, nightly_lock


class Command(BaseCommand):
    help = (
        "Run the whole nightly maintenance sequence: rotate the salt, create upcoming "
        "partitions, roll up outstanding days, then prune. Ordering matters -- pruning "
        "runs last and refuses to drop anything the rollup step did not cover.\n\n"
        "Safe to schedule from cron, Celery Beat, systemd timers or anything else: it "
        "takes a database lock, so a second copy started while one is running exits "
        "quietly instead of colliding with it."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--skip-prune", action="store_true", help="Roll up but keep all raw hits."
        )
        parser.add_argument(
            "--no-lock", action="store_true",
            help="Skip the concurrency lock. Only for debugging -- two concurrent rollups "
                 "of the same day can deadlock or collide on a unique constraint.",
        )

    def handle(self, *args, **options):
        if options["no_lock"]:
            self.run_steps(options)
            return
        try:
            with nightly_lock():
                self.run_steps(options)
        except AlreadyRunning as exc:
            # Not an error: a double-fired scheduler should not page anyone. The
            # work is not skipped, only deferred to the run that holds the lock.
            self.stdout.write(self.style.WARNING(f"Nothing to do -- {exc}."))

    def run_steps(self, options) -> None:
        steps = [
            ("sitepulse_rotate_salt", {}),
            ("sitepulse_partitions", {}),
            ("sitepulse_rollup", {}),
        ]
        if not options["skip_prune"]:
            steps.append(("sitepulse_prune", {}))

        for name, kwargs in steps:
            self.stdout.write(self.style.MIGRATE_HEADING(f"-- {name}"))
            try:
                call_command(name, **kwargs, stdout=self.stdout, stderr=self.stderr)
            except CommandError as exc:
                # A failure here should stop the sequence: the later steps assume
                # the earlier ones worked, and prune in particular must never run
                # after a failed rollup.
                raise CommandError(f"{name} failed: {exc}") from exc
