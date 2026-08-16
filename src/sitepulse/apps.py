from django.apps import AppConfig


class SitePulseConfig(AppConfig):
    name = "sitepulse"
    label = "sitepulse"
    verbose_name = "SitePulse analytics"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        # Note: the flush thread is deliberately *not* started here. A thread
        # started in ready() does not survive Gunicorn's fork under --preload,
        # and management commands have no business running one. It starts on the
        # first buffered hit instead -- see sitepulse.buffer.HitBuffer.
        from . import checks  # noqa: F401  (registers system checks)
