from django.urls import path

from .views import DashboardMetricsView, DashboardView

urlpatterns = [
    path('dashboard', DashboardView.as_view(), name='user-dashboard'),
    path('dashboard/metrics', DashboardMetricsView.as_view(), name='user-dashboard-metrics'),
]
