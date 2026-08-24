"""Root URL configuration."""

from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path('admin/', admin.site.urls),
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
