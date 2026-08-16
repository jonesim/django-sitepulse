from django.http import HttpResponse
from django.urls import include, path


def ok(request, **kwargs):
    return HttpResponse("ok")


def boom(request):
    return HttpResponse("no", status=500)


urlpatterns = [
    path("", ok, name="home"),
    path("orders/<int:pk>/", ok, name="order_detail"),
    path("orders/<int:pk>/lines/", ok, name="order_lines"),
    path("boom/", boom, name="boom"),
    path("admin-ish/", ok, name="adminish"),
    path("analytics/", include("sitepulse.urls", namespace="sitepulse")),
]
