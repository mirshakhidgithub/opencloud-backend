from django.urls import path

from .auth_views import (
    CsrfView,
    LoginVerifyView,
    LoginView,
    LogoutView,
    MeView,
    PasswordChangeView,
)
from .views import (
    AccountDetailView,
    AccountsView,
    ActivityView,
    AuditView,
    HealthView,
    ResourcesView,
    UserDetailView,
    UsersView,
)

urlpatterns = [
    path('auth/csrf', CsrfView.as_view(), name='platform-csrf'),
    path('auth/login', LoginView.as_view(), name='platform-login'),
    path('auth/login/verify', LoginVerifyView.as_view(), name='platform-login-verify'),
    path('auth/logout', LogoutView.as_view(), name='platform-logout'),
    path('auth/me', MeView.as_view(), name='platform-me'),
    path('auth/password', PasswordChangeView.as_view(), name='platform-password'),
    path('accounts', AccountsView.as_view(), name='platform-accounts'),
    path('accounts/<str:account_id>', AccountDetailView.as_view(), name='platform-account-detail'),
    path('users', UsersView.as_view(), name='platform-users'),
    path('users/<str:user_id>', UserDetailView.as_view(), name='platform-user-detail'),
    path('resources', ResourcesView.as_view(), name='platform-resources'),
    path('health', HealthView.as_view(), name='platform-health'),
    path('audit', AuditView.as_view(), name='platform-audit'),
    path('activity', ActivityView.as_view(), name='platform-activity'),
]
