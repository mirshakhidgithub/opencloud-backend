from django.urls import path

from .views import SnapshotListView, VolumeListView

urlpatterns = [
    path('volumes', VolumeListView.as_view(), name='user-volumes'),
    path('snapshots', SnapshotListView.as_view(), name='user-snapshots'),
]
