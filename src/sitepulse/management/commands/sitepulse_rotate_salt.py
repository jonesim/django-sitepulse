"""Force salt rotation and destroy every earlier salt."""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.utils import timezone

from sitepulse.conf import sitepulse_settings
from sitepulse.identity import current_salt, reset_salt_cache
from sitepulse.models import Salt


class Command(BaseCommand):
    help = (
        "Ensure today's visitor salt exists and delete all older ones. Rotation also "
        "happens by itself on the first hit of a new day; this command exists so the "
        "destruction is guaranteed even on a site with no overnight traffic."
    )

    def handle(self, *args, **options):
        using = sitepulse_settings.DATABASE_ALIAS
        today = timezone.localdate()
        reset_salt_cache()
        current_salt(today)
        deleted, _ = Salt.objects.using(using).filter(date__lt=today).delete()
        reset_salt_cache()
        self.stdout.write(
            self.style.SUCCESS(
                f"Salt for {today} is in place; destroyed {deleted} older salt(s). "
                f"Yesterday's visitor hashes can no longer be recomputed from an IP address."
            )
        )
