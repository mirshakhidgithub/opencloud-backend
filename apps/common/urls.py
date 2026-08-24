"""Common API routes: health + OpenAPI schema/docs."""

from django.urls import path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from .views import HealthView

urlpatterns = [
    path('health', HealthView.as_view(), name='health'),
    path('schema', SpectacularAPIView.as_view(), name='schema'),
    path('docs', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
]
