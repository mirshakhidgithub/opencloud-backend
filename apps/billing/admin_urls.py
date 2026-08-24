"""Admin-side billing routes, mounted under /api/v1/admin/ alongside apps.admin_api."""

from django.urls import path

from .views import (
    AdminBillingProfileView,
    AdminBillingView,
    AdminInvoiceListView,
    AdminInvoiceView,
    AdminTariffView,
    AdminUsageExportView,
    AdminUsageHistoryView,
)

urlpatterns = [
    path('billing', AdminBillingView.as_view(), name='admin-billing'),
    path('billing/history', AdminUsageHistoryView.as_view(), name='admin-billing-history'),
    path('billing/export', AdminUsageExportView.as_view(), name='admin-billing-export'),
    path('billing/invoice', AdminInvoiceView.as_view(), name='admin-billing-invoice'),
    path('billing/profile', AdminBillingProfileView.as_view(), name='admin-billing-profile'),
    path('invoices', AdminInvoiceListView.as_view(), name='admin-invoices'),
    path('tariffs', AdminTariffView.as_view(), name='admin-tariffs'),
]
