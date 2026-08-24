from django.urls import path

from .views import (
    AdminAuditView,
    AdminQuotaView,
    AdminResourcesView,
    AdminTenantsView,
    AdminUserCreateView,
    AdminUserDetailView,
    AdminUsersView,
)

urlpatterns = [
    path('resources', AdminResourcesView.as_view(), name='admin-resources'),
    path('audit', AdminAuditView.as_view(), name='admin-audit'),
    path('users', AdminUsersView.as_view(), name='admin-users'),
    path('users/create', AdminUserCreateView.as_view(), name='admin-user-create'),
    path('users/<str:user_id>', AdminUserDetailView.as_view(), name='admin-user-detail'),
    path('tenants', AdminTenantsView.as_view(), name='admin-tenants'),
    path('quotas', AdminQuotaView.as_view(), name='admin-quotas'),
]
