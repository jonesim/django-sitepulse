"""The dashboard views."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth.models import Permission, User
from django.urls import reverse
from django.utils import timezone

from sitepulse import charts

from .factories import make_hit, make_session

pytestmark = pytest.mark.django_db

PAGES = ["overview", "pages", "sources", "performance", "errors", "health"]


@pytest.fixture
def viewer():
    user = User.objects.create_user("viewer")
    user.user_permissions.add(Permission.objects.get(codename="view_dashboard"))
    return User.objects.get(pk=user.pk)  # refresh the permission cache


@pytest.fixture
def some_traffic():
    make_session(["/a", "/b"], visitor=b"1" * 16, route="/a")
    make_session(["/a"], visitor=b"2" * 16, route="/a")
    make_hit(path="/boom", route="/boom", status=500, duration_ms=3000)
    make_hit(path="/gone", route="/gone", status=404)
    make_hit(path="/bot", route="/bot", is_bot=True, visitor_hash=b"9" * 16)


@pytest.mark.parametrize("page", PAGES)
def test_every_page_renders(client, viewer, some_traffic, page):
    client.force_login(viewer)
    response = client.get(reverse(f"sitepulse:{page}"))
    assert response.status_code == 200
    assert b"SitePulse" in response.content


@pytest.mark.parametrize("page", PAGES)
def test_every_page_renders_with_no_data_at_all(client, viewer, page):
    client.force_login(viewer)
    assert client.get(reverse(f"sitepulse:{page}")).status_code == 200


def test_the_dashboard_needs_a_permission(client, some_traffic):
    assert client.get(reverse("sitepulse:overview")).status_code == 403
    user = User.objects.create_user("nobody")
    client.force_login(user)
    assert client.get(reverse("sitepulse:overview")).status_code == 403


def test_a_superuser_always_gets_in(client, admin_user):
    client.force_login(admin_user)
    assert client.get(reverse("sitepulse:overview")).status_code == 200


def test_the_date_range_comes_off_the_query_string(client, viewer):
    client.force_login(viewer)
    today = timezone.localdate()
    response = client.get(reverse("sitepulse:overview"), {"days": 7})
    assert response.context["start"] == today - timedelta(days=6)
    response = client.get(
        reverse("sitepulse:overview"),
        {"from": "2026-01-01", "to": "2026-01-31"},
    )
    assert str(response.context["start"]) == "2026-01-01"


def test_a_broken_date_is_a_404_not_a_500(client, viewer):
    client.force_login(viewer)
    assert client.get(
        reverse("sitepulse:overview"), {"from": "not-a-date", "to": "2026-01-31"}
    ).status_code == 404


def test_bots_are_hidden_until_asked_for(client, viewer, some_traffic):
    client.force_login(viewer)
    humans = client.get(reverse("sitepulse:pages")).content
    with_bots = client.get(reverse("sitepulse:pages"), {"bots": "1"}).content
    assert b"/bot" not in humans
    assert b"/bot" in with_bots


def test_health_shows_a_nightly_run_in_progress(client, viewer):
    from sitepulse.scheduling import nightly_lock

    client.force_login(viewer)
    with nightly_lock():
        content = client.get(reverse("sitepulse:health")).content
    assert b"running since" in content
    assert b"not running" in client.get(reverse("sitepulse:health")).content


def test_the_dashboard_does_not_record_its_own_traffic(client, viewer):
    from sitepulse.models import Hit

    client.force_login(viewer)
    client.get(reverse("sitepulse:overview"))
    assert not Hit.objects.filter(path__contains="analytics").exists()


# ---------------------------------------------------------------------------
# charts
# ---------------------------------------------------------------------------


def test_charts_render_without_a_request():
    figure = str(
        charts.line_chart(
            ["1 Jan", "2 Jan"],
            [{"name": "Views", "values": [1, 2]}, {"name": "Sessions", "values": [1, 1]}],
            "Traffic",
        )
    )
    assert "<svg" in figure
    assert "sp-legend" in figure       # two series always get a legend
    assert "<table" in figure          # ...and a table view


def test_a_single_series_chart_has_no_legend_box():
    figure = str(charts.line_chart(["1 Jan"], [{"name": "Views", "values": [3]}], "Traffic"))
    assert "sp-legend" not in figure


def test_bar_lists_survive_an_empty_dataset():
    assert "No data" in str(charts.bar_list([], "Top routes"))


def test_chart_labels_are_escaped():
    figure = str(
        charts.bar_list([{"label": "<script>x</script>", "value": 1}], "Top routes")
    )
    assert "<script>" not in figure
    assert "&lt;script&gt;" in figure


@pytest.mark.parametrize(
    "value,expected",
    [(0, "0"), (999, "999"), (1284, "1,284"), (12900, "12.9K"), (4_200_000, "4.2M"), (None, "--")],
)
def test_compact_numbers(value, expected):
    assert charts.compact(value) == expected


def test_durations_read_as_ms_then_seconds():
    assert charts.duration(30) == "30ms"
    assert charts.duration(3000) == "3.00s"
    assert charts.duration(None) == "--"
