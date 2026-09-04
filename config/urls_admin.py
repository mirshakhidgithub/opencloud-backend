"""
URL map for the platform-admin process (admin-cabinet.opencloud.uz).

Deliberately short. The cabinet's own namespaces — `/api/v1/user/*`,
`/api/v1/auth/*`, `/api/v1/admin/*` — are absent, so on this process they do not
merely 403, they do not exist. That is the point of running a second container
rather than relying on nginx to withhold them: a routing mistake at the edge can
only ever produce a 404 here.

Django's own admin is absent for the same reason.
"""

from django.urls import include, path

urlpatterns = [
    path('api/v1/platform/', include('apps.platform_admin.urls')),
    # Health only — the schema and Swagger UI describe the cabinet API and
    # belong on the process that serves it.
    path('api/v1/health', include('apps.common.health_urls')),
]

# Project-wide JSON error envelopes (active when DEBUG=False).
handler404 = 'apps.common.exceptions.handler404'
handler500 = 'apps.common.exceptions.handler500'
