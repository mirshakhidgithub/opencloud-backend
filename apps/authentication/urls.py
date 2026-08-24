from django.urls import path

from .views import (
    CsrfView,
    LoginView,
    LogoutView,
    MeView,
    PasswordChangeView,
    PasswordResetRequestView,
    PasswordResetVerifyView,
    RefreshView,
)

urlpatterns = [
    path('csrf', CsrfView.as_view(), name='auth-csrf'),
    path('login', LoginView.as_view(), name='auth-login'),
    path('logout', LogoutView.as_view(), name='auth-logout'),
    path('refresh', RefreshView.as_view(), name='auth-refresh'),
    path('me', MeView.as_view(), name='auth-me'),
    path('password', PasswordChangeView.as_view(), name='auth-password-change'),
    path('password-reset', PasswordResetRequestView.as_view(), name='auth-password-reset'),
    path('password-reset/verify', PasswordResetVerifyView.as_view(), name='auth-password-reset-verify'),
]
