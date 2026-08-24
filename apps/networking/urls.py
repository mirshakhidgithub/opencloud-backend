from django.urls import path

from .views import NetworkOverviewView, SecurityGroupRuleDeleteView, SecurityGroupRuleView

urlpatterns = [
    path('networks', NetworkOverviewView.as_view(), name='user-networks'),
    path('security-groups/<str:group_id>/rules', SecurityGroupRuleView.as_view(), name='user-sg-rule-create'),
    path('security-group-rules/<str:rule_id>', SecurityGroupRuleDeleteView.as_view(), name='user-sg-rule-delete'),
]
