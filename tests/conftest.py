import pytest
from django.core.cache import caches

from sitepulse import enrich, identity, middleware, normalise
from sitepulse.buffer import buffer


@pytest.fixture(autouse=True)
def _clean_state(db):
    """Every test starts with empty process-local caches.

    These caches exist to make the hot path cheap and are keyed by nothing that
    changes within a process, so a test that changes settings or truncates the
    database has to clear them explicitly.
    """
    identity.reset_salt_cache()
    enrich.reset_caches()
    normalise.reset_caches()
    middleware.reset_caches()
    caches["default"].clear()
    buffer.dropped = 0
    buffer.write_errors = 0
    buffer._queue.clear()
    yield
    buffer._queue.clear()
