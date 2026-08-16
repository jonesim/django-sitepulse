"""Dashboard URLs.

Mount them wherever you like::

    path("analytics/", include("sitepulse.urls", namespace="sitepulse")),

The middleware skips anything resolving into this namespace, so the dashboard
never appears in its own numbers.
"""

from django.urls import path

from . import views

app_name = "sitepulse"

urlpatterns = [
    path("", views.OverviewView.as_view(), name="overview"),
    path("pages/", views.PagesView.as_view(), name="pages"),
    path("sources/", views.SourcesView.as_view(), name="sources"),
    path("performance/", views.PerformanceView.as_view(), name="performance"),
    path("errors/", views.ErrorsView.as_view(), name="errors"),
    path("health/", views.HealthView.as_view(), name="health"),
]
