"""
Root URL configuration — the tenant cabinet's process.

The platform admin panel's routes are deliberately NOT here. They live in
`config/urls_admin.py`, which the second process loads instead (see
PLATFORM_ADMIN_PROCESS in settings). Keeping the two maps disjoint is the point
of running two processes at all: /api/v1/platform/* has to be missing here, not
merely refused, or cabinet.opencloud.uz would still route to it.
"""

from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    # Not 'admin/': the console owns /admin/* on the same origin (its own
    # administration screens), so Django's admin lives out of the way.
    path('django-admin/', admin.site.urls),
    path('api/v1/auth/', include('apps.authentication.urls')),
    path('api/v1/user/', include('apps.tenants.urls')),
    path('api/v1/user/', include('apps.compute.urls')),
    path('api/v1/user/', include('apps.dashboard.urls')),
    path('api/v1/user/', include('apps.storage.urls')),
    path('api/v1/user/', include('apps.networking.urls')),
    path('api/v1/user/', include('apps.quotas.urls')),
    path('api/v1/user/', include('apps.monitoring.urls')),
    path('api/v1/user/', include('apps.billing.urls')),
    path('api/v1/admin/', include('apps.admin_api.urls')),
    path('api/v1/admin/', include('apps.billing.admin_urls')),
    path('api/v1/', include('apps.common.urls')),
]

# Project-wide JSON error envelopes (active when DEBUG=False).
handler404 = 'apps.common.exceptions.handler404'
handler500 = 'apps.common.exceptions.handler500'
