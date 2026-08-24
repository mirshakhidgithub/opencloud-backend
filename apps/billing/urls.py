from django.urls import path

from .views import UserBillingView

urlpatterns = [
    path('billing', UserBillingView.as_view(), name='user-billing'),
]
