"""
Health on its own, so a URLconf can include it without the schema and docs.

The admin process wants the probe (its compose healthcheck calls it) but not the
OpenAPI pages, which describe the cabinet API.
"""

from django.urls import path

from .views import HealthView

urlpatterns = [path('', HealthView.as_view(), name='health')]
