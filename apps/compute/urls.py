from django.urls import path

from .views import VmActionView, VmConsoleView, VmDetailView, VmListView, VmMetricsView

urlpatterns = [
    path('vms', VmListView.as_view(), name='user-vms'),
    path('vms/<str:vm_id>', VmDetailView.as_view(), name='user-vm-detail'),
    path('vms/<str:vm_id>/actions', VmActionView.as_view(), name='user-vm-actions'),
    path('vms/<str:vm_id>/console', VmConsoleView.as_view(), name='user-vm-console'),
    path('vms/<str:vm_id>/metrics', VmMetricsView.as_view(), name='user-vm-metrics'),
]
