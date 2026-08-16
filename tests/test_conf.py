"""Settings validation and system checks -- the mistakes that produce
plausible-looking but wrong numbers."""

from __future__ import annotations

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from sitepulse.checks import check_sitepulse
from sitepulse.conf import DEFAULTS, sitepulse_settings

pytestmark = pytest.mark.django_db


def test_defaults_are_used_when_nothing_is_set():
    with override_settings(SITEPULSE={}):
        sitepulse_settings.reset()
        assert sitepulse_settings.RAW_RETENTION_DAYS == 90
        assert sitepulse_settings.RETURNING_VISITORS is False


def test_only_named_settings_are_overridden():
    with override_settings(SITEPULSE={"RAW_RETENTION_DAYS": 30}):
        sitepulse_settings.reset()
        assert sitepulse_settings.RAW_RETENTION_DAYS == 30
        assert sitepulse_settings.FLUSH_EVERY_ROWS == DEFAULTS["FLUSH_EVERY_ROWS"]


def test_a_typo_in_a_setting_name_is_an_error_not_a_shrug():
    with override_settings(SITEPULSE={"RAW_RETENTION_DAY": 30}):
        sitepulse_settings.reset()
        with pytest.raises(ImproperlyConfigured, match="RAW_RETENTION_DAY"):
            _ = sitepulse_settings.RAW_RETENTION_DAYS


@pytest.mark.parametrize(
    "value", [0, -1, "ninety", None, 1.5],
)
def test_retention_must_be_a_positive_integer(value):
    with override_settings(SITEPULSE={"RAW_RETENTION_DAYS": value}):
        sitepulse_settings.reset()
        with pytest.raises(ImproperlyConfigured):
            _ = sitepulse_settings.RAW_RETENTION_DAYS


def test_the_bucket_count_is_fixed_by_the_schema():
    with override_settings(SITEPULSE={"DURATION_BUCKETS_MS": [10, 20, 30]}):
        sitepulse_settings.reset()
        with pytest.raises(ImproperlyConfigured, match="exactly 8 boundaries"):
            _ = sitepulse_settings.DURATION_BUCKETS_MS


def test_buckets_must_ascend():
    with override_settings(SITEPULSE={"DURATION_BUCKETS_MS": [10, 5, 30, 40, 50, 60, 70, 80]}):
        sitepulse_settings.reset()
        with pytest.raises(ImproperlyConfigured, match="ascending"):
            _ = sitepulse_settings.DURATION_BUCKETS_MS


def test_a_broken_exclusion_regex_is_caught_at_startup():
    with override_settings(SITEPULSE={"EXCLUDE_PATHS": ["["]}):
        sitepulse_settings.reset()
        with pytest.raises(ImproperlyConfigured, match="invalid regex"):
            _ = sitepulse_settings.EXCLUDE_PATHS


def test_a_per_process_cache_is_warned_about():
    # The test settings deliberately use LocMemCache.
    ids = {issue.id for issue in check_sitepulse(None)}
    assert "sitepulse.W002" in ids


def test_missing_middleware_is_warned_about():
    with override_settings(MIDDLEWARE=[]):
        ids = {issue.id for issue in check_sitepulse(None)}
        assert "sitepulse.W001" in ids


def test_a_cookie_without_a_consent_check_is_warned_about():
    with override_settings(SITEPULSE={"RETURNING_VISITORS": True}):
        sitepulse_settings.reset()
        ids = {issue.id for issue in check_sitepulse(None)}
        assert "sitepulse.W003" in ids


def test_an_unimportable_consent_check_is_an_error():
    with override_settings(
        SITEPULSE={"RETURNING_VISITORS": True, "CONSENT_CHECK": "nope.nothing.here"}
    ):
        sitepulse_settings.reset()
        ids = {issue.id for issue in check_sitepulse(None)}
        assert "sitepulse.E003" in ids


def test_checks_are_quiet_when_the_app_is_switched_off():
    with override_settings(SITEPULSE={"ENABLED": False}):
        sitepulse_settings.reset()
        assert check_sitepulse(None) == []
