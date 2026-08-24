from django.urls import path

from .views import AlarmListView, EventListView

urlpatterns = [
    path('events', EventListView.as_view(), name='user-events'),
    path('alarms', AlarmListView.as_view(), name='user-alarms'),
]
