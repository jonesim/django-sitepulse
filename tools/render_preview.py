"""Render every dashboard page to HTML with demo data, for eyeballing.

    python tools/render_preview.py [output_dir]

Generates a few weeks of plausible traffic in an in-memory database, rolls most
of it up, and writes one HTML file per dashboard page. Nothing here ships in the
package -- it exists so the charts can be looked at.
"""

from __future__ import annotations

import os
import pathlib
import random
import sys
from datetime import timedelta

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.settings")

import django  # noqa: E402

django.setup()

from django.test.runner import DiscoverRunner  # noqa: E402
from django.test.utils import setup_test_environment, teardown_test_environment  # noqa: E402

setup_test_environment()
runner = DiscoverRunner(verbosity=0, interactive=False)
old_config = runner.setup_databases()

from django.contrib.auth.models import User  # noqa: E402
from django.test import Client  # noqa: E402
from django.utils import timezone  # noqa: E402

from sitepulse.enrich import lookup_id  # noqa: E402
from sitepulse.models import Browser, Device, Hit, Method, OperatingSystem  # noqa: E402
from sitepulse.rollup import rollup_day  # noqa: E402

random.seed(20260816)

ROUTES = [
    ("/", "/", 1.00, 40),
    ("/orders/<int:pk>", "/orders/{}", 0.55, 120),
    ("/orders", "/orders", 0.40, 90),
    ("/products/<slug:slug>", "/products/{}", 0.35, 70),
    ("/pricing", "/pricing", 0.25, 35),
    ("/api/v1/quote", "/api/v1/quote", 0.20, 900),
    ("/docs/<path:page>", "/docs/{}", 0.15, 60),
]
REFERRERS = ["", "", "", "news.ycombinator.com", "google.com", "duckduckgo.com", "reddit.com"]
BROWSERS = ["Chrome", "Firefox", "Safari", "Edge"]
SYSTEMS = ["Windows", "Mac OS X", "iOS", "Android", "Linux"]
COUNTRIES = ["GB", "US", "DE", "FR", "IE", "NL"]


def build(days: int = 30) -> None:
    today = timezone.localdate()
    rows = []
    for offset in range(days):
        day = today - timedelta(days=days - 1 - offset)
        weekday = day.weekday()
        volume = int(70 * (0.6 if weekday >= 5 else 1.0) * (1 + offset / days))
        for _ in range(volume):
            visitor = random.randbytes(16)
            session = random.randbytes(16)
            start = timezone.make_aware(
                timezone.datetime(day.year, day.month, day.day,
                                  random.randint(6, 22), random.randint(0, 59)),
                timezone.get_current_timezone(),
            )
            bot = random.random() < 0.25
            browser = lookup_id(Browser, random.choice(BROWSERS) if not bot else "unknown")
            system = lookup_id(OperatingSystem, random.choice(SYSTEMS) if not bot else "unknown")
            referrer = random.choice(REFERRERS)
            for index in range(1 if random.random() < 0.45 else random.randint(2, 5)):
                route, path_fmt, weight, base = random.choice(ROUTES)
                path = path_fmt.format(random.randint(1, 5000)) if "{}" in path_fmt else path_fmt
                slow = random.random() < 0.05
                duration = int(random.gauss(base, base / 3)) + (2500 if slow else 0)
                status = 200
                if random.random() < 0.03:
                    status = random.choice([404, 404, 500, 302])
                rows.append(
                    Hit(
                        ts=start + timedelta(minutes=index * random.randint(1, 6)),
                        visitor_hash=visitor,
                        session_id=session,
                        is_new_session=index == 0,
                        path=path[:255],
                        route=route,
                        view_name=route.strip("/").replace("/", "_") or "home",
                        method=Method.GET,
                        status=status,
                        duration_ms=max(3, duration),
                        query_count=random.randint(1, 12) if route != "/api/v1/quote"
                        else random.randint(120, 400),
                        query_ms=random.randint(1, 60),
                        referrer_host=referrer if index == 0 else "",
                        utm_source=("newsletter" if not referrer and random.random() < 0.08
                                    else ""),
                        utm_medium="email" if referrer == "" and random.random() < 0.08 else "",
                        country=random.choice(COUNTRIES),
                        device=random.choice([Device.DESKTOP, Device.MOBILE, Device.TABLET])
                        if not bot else Device.BOT,
                        browser_id=browser,
                        os_id=system,
                        is_bot=bot,
                    )
                )
    Hit.objects.bulk_create(rows, batch_size=2000)
    print(f"{len(rows)} demo hits")
    for offset in range(days):
        day = today - timedelta(days=days - 1 - offset)
        if day < today:
            rollup_day(day)


def render(out: pathlib.Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    user = User.objects.create_superuser("preview", "p@example.com", "x")
    client = Client()
    client.force_login(user)
    for name, url in (
        ("overview", "/analytics/"),
        ("pages", "/analytics/pages/"),
        ("sources", "/analytics/sources/"),
        ("performance", "/analytics/performance/"),
        ("errors", "/analytics/errors/"),
        ("health", "/analytics/health/"),
    ):
        response = client.get(url, {"days": 30})
        assert response.status_code == 200, (url, response.status_code)
        (out / f"{name}.html").write_bytes(response.content)
        print(f"wrote {out / f'{name}.html'}")


try:
    build()
    render(pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "preview"))
finally:
    runner.teardown_databases(old_config)
    teardown_test_environment()
