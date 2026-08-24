from django.urls import path

from .views import UserQuotaView

urlpatterns = [
    path('quotas', UserQuotaView.as_view(), name='user-quotas'),
]
